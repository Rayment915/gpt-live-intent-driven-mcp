import asyncio
import json
from unittest.mock import AsyncMock, Mock

from gpt_live_python.azure import Azure
from gpt_live_python.config import Settings
from gpt_live_python.live import Live
from gpt_live_python.protocol import SessionConfig
from gpt_live_python.remote import Remote


async def test_delegation_stays_open_when_completed_tool_round_omits_output():
    cfg = Settings(_env_file=None)
    live = Live("owner", SessionConfig(model="gpt-live-1"), "websocket", cfg, Mock(spec=Azure), Remote(cfg))
    live.status = "ready"
    live.bridge.execute = AsyncMock(return_value={"intent": "faq"})

    def wrapped(kind, **values):
        return {"type": "response.event", "delegation_id": "d", "event": {"type": kind, **values}}

    class Socket:
        def __init__(self):
            self.sent = []
            self.between_rounds = None

        async def send(self, value):
            self.sent.append(json.loads(value))

        async def close(self):
            return None

        async def __aiter__(self):
            sequence = [
                {"type": "session.delegation.created", "delegation": {"id": "d"}},
                wrapped("response.created", response={"id": "r"}),
                wrapped("response.output_item.done", item={
                    "type": "function_call", "call_id": "c", "name": "classify_intent", "arguments": "{}",
                }),
                wrapped("response.completed"),
            ]
            for frame in sequence:
                yield json.dumps(frame)
                await asyncio.sleep(0)
            self.between_rounds = live.trace.spans[live.delegations["d"]]["status"]
            yield json.dumps(wrapped("response.in_progress"))
            yield json.dumps(wrapped("response.completed", response={"output": [
                {"type": "message", "content": [{"type": "output_text", "text": "done"}]},
            ]}))
            yield json.dumps({"type": "session.closed", "usage": {"seconds": 1}})

    socket = Socket()
    live.socket = socket
    await live.read()
    assert socket.between_rounds == "running"
    assert live.trace.spans[live.delegations["d"]]["status"] == "completed"
    assert sum(frame["type"] == "response.create" for frame in socket.sent) == 1
    assert live.final_usage
