"""Paid native Responses delegation smoke; uses a synthetic PCM file, never a microphone."""

import argparse
import asyncio
import json
import wave
from pathlib import Path

from gpt_live_python.azure import Azure
from gpt_live_python.bridge import CLASSIFY_TOOL, LIVE_INSTRUCTIONS, RESPONSES_INSTRUCTIONS
from gpt_live_python.config import settings
from gpt_live_python.live import Live
from gpt_live_python.protocol import SessionConfig
from gpt_live_python.remote import Remote


def synthetic_reply(identity_saved: bool) -> str:
    if not identity_saved:
        return (
            "我明确请求创建模拟报修。客户资料是：国家/地区电话代码 countryCode=86，"
            "电话号码 phoneNumber=0000000000，姓名 customerName=测试客户。"
            "请直接保存以上客户提供的模拟资料，不需要额外确认或页面点击。"
        )
    return (
        "客户资料已保存。我明确请求创建模拟报修，全部报修字段如下："
        "description=模拟不能启动，zipCode=000000，email=test@example.test，"
        "productCategory=Washing machine，srType=Inquiry，srSubType=Product。"
        "brand、productModel、serialNumber、addressRemark 全部跳过留空。"
        "以上必需字段已经齐全，请直接创建模拟报修，不需要额外确认或页面点击。"
    )


async def main(path: Path, scenario: str = "faq"):
    import base64

    cfg = settings()
    remote = Remote(cfg)
    azure = Azure(cfg)
    config = SessionConfig.model_validate({
        "model": cfg.live_deployment, "instructions": LIVE_INSTRUCTIONS,
        "audio": {"output": {"voice": "marin"}},
        "delegation": {"type": "responses", "responses": {
            "model": cfg.responses_deployment, "instructions": RESPONSES_INSTRUCTIONS,
            "max_output_tokens": 2048, "service_tier": "default",
            "reasoning": {"effort": "low"}, "tools": [CLASSIFY_TOOL], "tool_choice": "required",
            "parallel_tool_calls": True,
        }},
    })
    live = Live("synthetic-smoke", config, "websocket", cfg, azure, remote)
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    live.clients.add(queue)
    output_audio = 0
    route = None
    errors = []
    automatic_calls: set[str] = set()
    expected_tool = "faq/faqSearch" if scenario == "faq" else "repair/createRepair"
    audio_done = asyncio.Event()
    waiting_for_reply = False
    backend_text = ""
    followups = 0
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 24000):
            raise ValueError("Synthetic input must be mono PCM16 24 kHz WAV")
        pcm = audio.readframes(audio.getnframes())
    try:
        await live.start()

        async def send_audio():
            await asyncio.sleep(1)
            # A finite file needs trailing silence for turn detection, not an endless synthetic audio stream.
            samples = pcm + bytes(48000)
            for offset in range(0, len(samples), 960):
                await live.command({"type": "session.input_audio.append",
                                    "audio": base64.b64encode(samples[offset:offset+960]).decode()})
                await asyncio.sleep(0.02)
            audio_done.set()

        sender = asyncio.create_task(send_audio())
        try:
            async with asyncio.timeout(100 if scenario == "faq" else 240):
                while True:
                    try:
                        frame = await asyncio.wait_for(queue.get(), timeout=1)
                    except TimeoutError:
                        frame = {"type": "smoke.tick"}
                    kind = frame["type"]
                    if kind == "response.event":
                        nested = frame["event"]
                        if nested["type"] in ("response.created", "response.in_progress"):
                            backend_text = ""
                        if nested["type"] == "response.output_text.delta":
                            backend_text += nested.get("delta", "")
                        if nested["type"] == "response.output_text.done":
                            backend_text = nested.get("text", backend_text)
                    if kind == "response.event" and frame["event"]["type"] == "response.completed":
                        d = frame["delegation_id"]
                        active = live.ledger.active.get(d)
                        waiting_for_reply = bool(active and not live.ledger.batches[active].calls)
                    if kind == "session.output_audio.delta":
                        output_audio += 1
                    if kind == "studio.route":
                        route = {key: frame[key] for key in ("intent", "confidence")}
                    if kind == "studio.approval":
                        raise RuntimeError("Manual approval event is not supported by the automatic Mock workflow")
                    if kind == "studio.tool.auto_approved":
                        automatic_calls.add(frame["name"])
                    if kind in ("studio.error", "error"):
                        errors.append(frame)
                        print("Service error:", json.dumps(frame, ensure_ascii=False))
                        raise RuntimeError("Live smoke received a real service/tool failure; no fallback is permitted")
                    if kind == "studio.disconnected":
                        raise RuntimeError("Live session disconnected before the expected tool flow completed")
                    if kind == "studio.span" and frame["status"] in ("completed", "failed"):
                        print(f'{frame["kind"]}: {frame["name"]}: {frame["status"]} {frame["duration_ms"]:.1f}ms')
                    if (
                        scenario == "repair" and audio_done.is_set()
                        and waiting_for_reply and backend_text.strip()
                    ):
                        followups += 1
                        if followups > 12:
                            raise RuntimeError("Synthetic repair exceeded the bounded interactive fixture")
                        identity_saved = any(
                            c.name == "setCustomerByPhoneAndName" and c.state == "submitted"
                            for c in live.ledger.calls.values()
                        )
                        answer = synthetic_reply(identity_saved)
                        print(f"Synthetic question: {backend_text[-400:]}", flush=True)
                        # Exercise the documented user-message debugger, never manufacture a tool result.
                        await live.command({"type": "response.item.create", "item": {
                            "type": "message", "role": "user", "content": [{
                                "type": "input_text", "text": answer,
                            }],
                        }})
                        await live.command({"type": "response.create"})
                        waiting_for_reply = False
                    target_delegations = {
                        c.delegation_id for c in live.ledger.calls.values()
                        if c.name == ("faqSearch" if scenario == "faq" else "createRepair")
                        and c.state == "submitted"
                    }
                    if (
                        route and any(live.trace.spans[live.delegations[d]]["status"] == "completed"
                                      for d in target_delegations)
                        and any(s["name"] == expected_tool and s["status"] == "completed"
                                for s in live.trace.spans.values())
                    ):
                        await asyncio.sleep(2)
                        break
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            # Drain audio queued while awaiting final speech.
            while not queue.empty():
                if queue.get_nowait()["type"] == "session.output_audio.delta":
                    output_audio += 1
        await live.close()
        if errors or not route or not output_audio or not live.final_usage or (
            scenario == "repair" and not {"setCustomerByPhoneAndName", "createRepair"} <= automatic_calls
        ):
            raise RuntimeError("Live smoke lacked a clean route, spoken output or final usage confirmation")
        report = {"route": route, "output_audio_frames": output_audio, "final_usage_confirmed": live.final_usage,
                  "spans": list(live.trace.spans.values()), "mock_business": True,
                  "execution_policy": "auto_mock", "auto_allowed_tools": sorted(automatic_calls),
                  "scenario": scenario}
        Path("output").mkdir(exist_ok=True)
        Path(f"output/live-smoke-{scenario}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"PASS: native delegation -> Decision -> remote MCP -> spoken output ({output_audio} PCM frames)")
    finally:
        await live.close()
        await azure.close()
        await remote.cleanup(live.id)
        await remote.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", type=Path)
    parser.add_argument("--scenario", choices=["faq", "repair"], default="faq")
    args = parser.parse_args()
    asyncio.run(main(args.wav, args.scenario))
