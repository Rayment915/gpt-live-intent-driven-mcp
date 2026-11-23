import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from gpt_live_python.azure import Azure
from gpt_live_python.bridge import CLASSIFY_TOOL
from gpt_live_python.config import Settings
from gpt_live_python.live import Live
from gpt_live_python.protocol import SessionConfig
from gpt_live_python.remote import Remote

FAQ_TOOL = {
    "type": "function", "name": "faqSearch", "description": "FAQ",
    "parameters": {"type": "object"},
}


class Socket:
    def __init__(self, live, echo=True):
        self.live, self.echo = live, echo
        self.sent = []
        self.updated = asyncio.Event()

    async def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        if event["type"] == "session.update":
            self.updated.set()
            if self.echo:
                self.live.observe_update({"type": "session.updated", "session": event["session"]})


@pytest.fixture
async def live():
    cfg = Settings(_env_file=None)
    azure = Azure(cfg)
    azure.decision = AsyncMock(return_value={"intent": "faq", "confidence": .95, "probabilities": {"faq": .95}})
    remote = Remote(cfg)
    remote.tools_for = AsyncMock(return_value=[copy.deepcopy(FAQ_TOOL)])
    remote.call = AsyncMock(return_value={"mock": True, "results": []})
    session = Live("owner", SessionConfig.model_validate({
        "model": cfg.live_deployment, "delegation": {"type": "responses", "responses": {
            "model": cfg.responses_deployment, "tools": [CLASSIFY_TOOL], "tool_choice": "required",
        }},
    }), "websocket", cfg, azure, remote)
    session.status = "ready"
    session.socket = Socket(session)
    try:
        yield session
    finally:
        for task in list(session.tasks):
            task.cancel()
        if session.tasks:
            await asyncio.gather(*session.tasks, return_exceptions=True)
        await remote.close()
        await azure.close()


def batch(live, delegation, response_id, name=None, args="{}"):
    live.ensure_turn(delegation)
    live.ledger.ingest({
        "type": "response.event", "delegation_id": delegation,
        "event": {"type": "response.created", "response": {"id": response_id}},
    })
    call = None
    if name:
        call = live.ledger.ingest({
            "type": "response.event", "delegation_id": delegation,
            "event": {"type": "response.output_item.done", "item": {
                "type": "function_call", "name": name, "call_id": response_id, "arguments": args,
            }},
        })[0]
        call.state = "running"
    live.ledger.ingest({
        "type": "response.event", "delegation_id": delegation, "event": {"type": "response.completed"},
    })
    key = live.ledger.active[delegation]
    live.batch_sealed.setdefault(key, asyncio.Event()).set()
    return call, key


async def test_tool_update_ack_precedes_classification_result_and_continuation(live):
    live.socket.echo = False
    call, _ = batch(live, "d", "classify", "classify_intent", '{"request":"保修期"}')
    task = asyncio.create_task(live.run_tool(call))
    try:
        await asyncio.wait_for(live.socket.updated.wait(), 1)
        assert [e["type"] for e in live.socket.sent] == ["session.update"]
        assert live.authorized_version is None
        live.remote.call.assert_not_awaited()
        update = live.socket.sent[-1]
        assert [t["name"] for t in update["session"]["delegation"]["responses"]["tools"]] == [
            "classify_intent", "faqSearch",
        ]
        live.observe_update({"type": "session.updated", "session": update["session"]})
        await asyncio.wait_for(task, 1)
        assert [e["type"] for e in live.socket.sent] == [
            "session.update", "response.item.create", "response.create",
        ]
        assert live.authorized_version == 1
        live.remote.tools_for.assert_awaited_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_business_before_classification_is_rejected_and_low_confidence_discovers_nothing(live):
    call, _ = batch(live, "d", "wrong", "faqSearch")
    await live.run_tool(call)
    live.remote.call.assert_not_awaited()
    assert json.loads(live.socket.sent[-2]["item"]["output"])["ok"] is False
    assert live.tool_choice == "required"
    live.azure.decision.return_value["confidence"] = .69
    call, _ = batch(live, "d", "classify", "classify_intent", '{"request":"不确定"}')
    await live.run_tool(call)
    assert live.workflow_phase == "clarifying"
    assert live.authorized_version is None
    # No empty-list discovery either: there is no business directory to fetch.
    live.remote.tools_for.assert_not_awaited()


async def test_same_session_queue_restores_classifier_before_next_request(live):
    first, _ = batch(live, "a", "a-classify", "classify_intent", '{"request":"保修期"}')
    await live.run_tool(first)
    second, _ = batch(live, "b", "b-classify", "classify_intent", '{"request":"洗衣机"}')
    task = asyncio.create_task(live.run_tool(second))
    try:
        await asyncio.sleep(.02)
        assert live.azure.decision.await_count == 1
        assert live.active_delegation == "a"
        _, terminal = batch(live, "a", "a-final")
        await live.finish_turn("a", terminal)
        await asyncio.wait_for(task, 1)
        assert live.active_delegation == "b"
        assert live.azure.decision.await_count == 2
        assert "a" not in live.bridge.routes
        updates = [e["session"]["delegation"]["responses"] for e in live.socket.sent if e["type"] == "session.update"]
        assert updates[-2]["tools"] == [CLASSIFY_TOOL]
        assert updates[-2]["tool_choice"] == "required"
        assert updates[-1]["tools"][-1]["name"] == "faqSearch"
        with pytest.raises(ValueError, match="already ended"):
            await live.wait_turn("a")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_unrelated_ack_and_async_config_failure_do_not_confirm_update(live):
    live.socket.echo = False
    task = asyncio.create_task(live.apply_tools([CLASSIFY_TOOL, FAQ_TOOL], "auto"))
    try:
        await live.socket.updated.wait()
        update = copy.deepcopy(live.socket.sent[-1])
        update["session"]["delegation"]["responses"]["model"] = "wrong"
        live.observe_update({"type": "session.updated", "session": update["session"]})
        assert not task.done()
        live.observe_update({"type": "error", "error": {
            "code": "responses_validation_failed", "message": "Tools rejected",
        }})
        with pytest.raises(ValueError, match="Tools rejected"):
            await task
        assert live.pending_update is None
        assert live.visible_tools == [CLASSIFY_TOOL]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_request_reclassification_failure_invalidates_old_authorization(live):
    first, _ = batch(live, "d", "first", "classify_intent", '{"request":"保修期"}')
    await live.run_tool(first)
    live.azure.decision.side_effect = ValueError("Decision unavailable")
    second, _ = batch(live, "d", "second", "classify_intent", '{"request":"新请求"}')
    await live.run_tool(second)
    assert "d" not in live.bridge.routes
    assert live.authorized_version is None
    assert live.workflow_phase == "clarifying"
    assert live.visible_tools == [CLASSIFY_TOOL]
    live.remote.call.assert_not_awaited()


async def test_closing_session_cancels_pending_update_and_wakes_queued_call(live):
    live.socket.echo = False
    update = asyncio.create_task(live.apply_tools([CLASSIFY_TOOL, FAQ_TOOL], "auto"))
    try:
        await live.socket.updated.wait()
        await live.close()
        with pytest.raises(asyncio.CancelledError):
            await update
        with pytest.raises(ValueError, match="ended"):
            await live.wait_turn("queued")
        assert live.pending_update is None
    finally:
        update.cancel()
        await asyncio.gather(update, return_exceptions=True)


async def test_failed_response_cannot_execute_an_already_received_function(live):
    call, key = batch(live, "d", "failed", "classify_intent", '{"request":"保修"}')
    live.ledger.ingest({"type": "response.event", "delegation_id": "d",
                       "event": {"type": "response.failed"}})
    await live.run_tool(call)
    assert live.ledger.batches[key].failed
    assert live.ledger.ready() == []
    live.azure.decision.assert_not_awaited()
    live.remote.call.assert_not_awaited()


async def test_queued_premature_business_call_requires_fresh_classification_not_replay(live):
    first, _ = batch(live, "a", "a-classify", "classify_intent", '{"request":"保修"}')
    await live.run_tool(first)
    premature, _ = batch(live, "b", "b-action", "faqSearch")
    task = asyncio.create_task(live.run_tool(premature))
    try:
        await asyncio.sleep(.01)
        live.remote.call.assert_not_awaited()
        _, final = batch(live, "a", "a-final")
        await live.finish_turn("a", final)
        await asyncio.wait_for(task, 1)
        assert live.active_delegation == "b"
        assert live.tool_choice == "required"
        assert live.visible_tools == [CLASSIFY_TOOL]
        live.remote.call.assert_not_awaited()
        classified, _ = batch(live, "b", "b-classify", "classify_intent", '{"request":"保修"}')
        await live.run_tool(classified)
        corrected, _ = batch(live, "b", "b-corrected", "faqSearch")
        await live.run_tool(corrected)
        assert live.remote.call.await_count == 1
        assert live.remote.call.await_args.args[1]["contactId"] == live.id
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_update_timeout_does_not_open_business_tools(live, monkeypatch):
    live.socket.echo = False
    wait_for = asyncio.wait_for
    monkeypatch.setattr(asyncio, "wait_for", lambda value, timeout: wait_for(value, .01))
    with pytest.raises(TimeoutError):
        await live.apply_tools([CLASSIFY_TOOL, FAQ_TOOL], "auto")
    assert live.visible_tools == [CLASSIFY_TOOL]
    assert live.pending_update is None
    live.remote.call.assert_not_awaited()


async def test_manual_config_update_cannot_bypass_server_owned_tool_choice(live):
    with pytest.raises(ValueError, match="managed by"):
        await live.command({"type": "session.update", "session": {"delegation": {
            "type": "responses", "responses": {
                "model": live.settings.responses_deployment, "tools": [CLASSIFY_TOOL], "tool_choice": "auto",
            },
        }}})
    assert live.socket.sent == []
