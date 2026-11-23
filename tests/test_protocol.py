import asyncio

import pytest

from gpt_live_python.privacy import redact
from gpt_live_python.protocol import Ledger, SessionConfig, validate_command
from gpt_live_python.trace import Trace


def event(kind, **values):
    return {"type": "response.event", "delegation_id": "d", "event": {"type": kind, **values}}


def test_parallel_results_continue_once_and_new_round():
    ledger = Ledger()
    ledger.ingest(event("response.created", response={"id": "r"}))
    for cid in ("one", "two"):
        envelope = event("response.output_item.done", item={
            "type": "function_call", "name": "faqSearch", "call_id": cid, "arguments": "{}",
        })
        assert len(ledger.ingest(envelope)) == 1
        assert ledger.ingest(envelope) == []
    ledger.calls["one"].state = "submitted"
    ledger.ingest(event("response.completed"))
    assert ledger.ready() == []
    ledger.calls["two"].state = "submitted"
    assert ledger.ready() == ["d:r"]
    ledger.batches["d:r"].continued = True
    assert ledger.ready() == []
    ledger.ingest(event("response.in_progress"))
    assert ledger.active["d"] != "d:r"
    assert ledger.ready() == []


def test_protocol_preserves_responses_extensions_but_rejects_live_fields():
    config = SessionConfig.model_validate({
        "model": "gpt-live-1", "delegation": {"type": "responses", "responses": {
            "model": "gpt-6-luna", "future_extension": {"enabled": True},
        }},
    })
    assert config.wire()["delegation"]["responses"]["future_extension"] == {"enabled": True}
    for extra in ({"temperature": 0.2}, {"headers": {"authorization": "secret"}}):
        with pytest.raises(ValueError):
            SessionConfig.model_validate({"model": "live", **extra})
    for patch in ({"audio": {}}, {"delegation": {"type": "client"}}):
        with pytest.raises(ValueError):
            validate_command({"type": "session.update", "session": patch}, config, "websocket")
    for audio in ("A===", "YQ==", ""):
        with pytest.raises(ValueError):
            validate_command({"type": "session.input_audio.append", "audio": audio}, config, "websocket")
    with pytest.raises(ValueError):
        validate_command({"type": "session.input_audio.append", "audio": "AAA="}, config, "webrtc")


def test_nested_arguments_are_redacted_without_destroying_approval_state():
    value = redact({"state": "pending", "arguments": '{"phoneNumber":"123456","query":"warranty"}',
                    "item": {"output": '{"private":"details"}'}})
    assert "123456" not in str(value)
    assert value["state"] == "pending"
    assert "warranty" in value["arguments"]
    assert value["item"]["output"] == "[redacted]"


def test_experimental_events_still_enforce_credentials_startup_and_transport():
    config = SessionConfig(model="gpt-live-1")
    with pytest.raises(ValueError):
        validate_command({"type": "future.event"}, config, "websocket")
    validate_command({"type": "future.event"}, config, "websocket", experimental=True)
    for event in ({"type": "session.start"}, {"type": "future.event", "api_key": "secret"},
                  {"type": "session.input_audio.append", "audio": "AAA="}):
        with pytest.raises(ValueError):
            validate_command(event, config, "webrtc", experimental=True)


def test_service_normalized_audio_metadata_does_not_relax_startup_validation():
    resource = {"id": "remote", "model": "gpt-live-1", "audio": {
        "format": "pcm16", "output": {"voice": "marin"},
    }}
    assert SessionConfig.from_resource(resource).audio.output.voice == "marin"
    with pytest.raises(ValueError):
        SessionConfig.model_validate({key: value for key, value in resource.items() if key != "id"})


async def test_timing_measures_actual_parallel_interval():
    emitted = []
    trace = Trace(emitted.append)
    parent = trace.start("parent", "delegation")

    async def work():
        with trace.span("child", "mcp", parent):
            await asyncio.sleep(0.04)

    await asyncio.gather(work(), work())
    trace.finish(parent)
    total = trace.spans[parent]["duration_ms"]
    children = [span for span in trace.spans.values() if span["parent_id"] == parent]
    assert all(30 <= child["duration_ms"] < 300 for child in children)
    assert total < sum(c["duration_ms"] for c in children)
