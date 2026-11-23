"""Paid HTTPS login -> public WSS -> native Decision/MCP/audio acceptance."""

import argparse
import asyncio
import base64
import getpass
import json
import sys
import wave
from pathlib import Path
from urllib.parse import urlparse

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus


async def main(origin: str, username: str, password: str, path: Path):
    parsed = urlparse(origin)
    if parsed.scheme != "https" or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Public acceptance requires an HTTPS origin")
    origin = origin.rstrip("/")
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 24000):
            raise ValueError("Input must be synthetic mono PCM16 24kHz WAV")
        pcm = audio.readframes(audio.getnframes()) + bytes(48000)
    report = {"origin": origin, "anonymous_api_rejected": False, "anonymous_ws_rejected": False,
              "mcp_completed": False, "spoken_answer": False, "audio_frames": 0,
              "final_usage_confirmed": False, "errors": [], "spans": []}
    session = None
    async with httpx.AsyncClient(base_url=origin, timeout=60, follow_redirects=False) as client:
        report["anonymous_api_rejected"] = (await client.get("/api/config")).status_code == 401
        boot = await client.get("/api/auth")
        boot.raise_for_status()
        logged = await client.post("/api/auth/login", json={"username": username, "password": password},
                                   headers={"Origin": origin, "X-Studio-Csrf": boot.json()["csrf"]})
        logged.raise_for_status()
        report["secure_cookie"] = "Secure" in logged.headers.get("set-cookie", "")
        cfg_response = await client.get("/api/config")
        cfg_response.raise_for_status()
        cfg = cfg_response.json()
        headers = {"Origin": origin, "X-Studio-Csrf": cfg["csrf"]}
        if [t["name"] for t in cfg["defaultConfig"]["delegation"]["responses"]["tools"]] != ["classify_intent"]:
            raise ValueError("Expected a single initial classification tool")
        created = await client.post("/api/sessions", json={
            "session": cfg["defaultConfig"], "transport": "websocket",
        }, headers=headers)
        created.raise_for_status()
        session = created.json()["id"]
        url = origin.replace("https://", "wss://") + f"/api/sessions/{session}/events"
        try:
            try:
                async with connect(url, origin=origin, open_timeout=20):
                    raise RuntimeError("Anonymous access to an owned WebSocket was accepted")
            except InvalidStatus as exc:
                if exc.response.status_code != 403:
                    raise
                report["anonymous_ws_rejected"] = True
            async with connect(url, origin=origin, open_timeout=20,
                               additional_headers={"Cookie": "live_owner=" + client.cookies.get("live_owner")},
                               max_size=2 * 1024 * 1024, max_queue=256) as socket:
                async def feed():
                    await asyncio.sleep(1)
                    for offset in range(0, len(pcm), 960):
                        await socket.send(json.dumps({"type": "session.input_audio.append",
                                                     "audio": base64.b64encode(pcm[offset:offset + 960]).decode()}))
                        await asyncio.sleep(.02)

                sender = asyncio.create_task(feed())
                spoken = ""
                closed = False
                try:
                    async with asyncio.timeout(150):
                        async for raw in socket:
                            event = json.loads(raw)
                            kind = event["type"]
                            if kind == "studio.route":
                                report["route"] = {k: event[k] for k in ("intent", "confidence")}
                            if kind == "studio.span" and event["status"] != "running":
                                report["spans"].append({k: event[k] for k in
                                                        ("name", "kind", "status", "duration_ms") if k in event})
                                if event["name"] == "faq/faqSearch" and event["status"] == "completed":
                                    report["mcp_completed"] = True
                            if kind == "session.output_audio.delta":
                                report["audio_frames"] += 1
                            if kind == "session.output_transcript.delta":
                                spoken += event.get("delta", "")
                                report["spoken_answer"] = "一年" in spoken
                            if kind in ("error", "studio.error"):
                                report["errors"].append({"type": kind, "code": event.get("error", {}).get("code")})
                                raise RuntimeError("Public Live flow returned a real service error")
                            if report["mcp_completed"] and report["spoken_answer"] and not closed:
                                response = await client.post(f"/api/sessions/{session}/commands",
                                                             json={"event": {"type": "session.close"}},
                                                             headers=headers)
                                response.raise_for_status()
                                closed = True
                            if kind == "session.closed":
                                report["final_usage_confirmed"] = True
                            if kind == "studio.disconnected":
                                break
                finally:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
        finally:
            if session and not report["final_usage_confirmed"]:
                response = await client.post(f"/api/sessions/{session}/commands",
                                             json={"event": {"type": "session.close"}}, headers=headers)
                if response.status_code != 200:
                    report["cleanup_error"] = response.status_code
            left = await client.post("/api/auth/logout", json={}, headers=headers)
            left.raise_for_status()
            report["logout_revoked"] = (await client.get("/api/config")).status_code == 401
            Path("output/studio-cloud-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps({k: v for k, v in report.items() if k != "spans"}, ensure_ascii=False), flush=True)
    if not all(report[k] for k in ("anonymous_api_rejected", "anonymous_ws_rejected", "mcp_completed",
                                   "spoken_answer", "audio_frames", "final_usage_confirmed", "logout_revoked")):
        raise RuntimeError("Public studio acceptance did not satisfy all required boundaries")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("origin")
    parser.add_argument("wav", type=Path)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args()
    password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("Studio password: ")
    asyncio.run(main(args.origin, args.username, password, args.wav))
