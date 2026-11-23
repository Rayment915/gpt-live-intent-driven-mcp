import json

from fastapi.testclient import TestClient

from gpt_live_python.mcp_server import TimedApp, make_server
from gpt_live_python.store import SQLiteStore


def test_stateless_mcp_sessions_do_not_close_shared_storage_transport(tmp_path):
    class TrackingStore(SQLiteStore):
        closes = 0

        async def mutate(self, contact, change):
            if self.closes:
                raise RuntimeError("HTTP transport has already been closed")
            return await super().mutate(contact, change)

        def close(self):
            self.closes += 1

    store = TrackingStore(str(tmp_path / "demo.sqlite"))
    server = make_server("faq", store=store)
    app = TimedApp(server.streamable_http_app(), "faq", store)
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        initialize = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "lifetime-test", "version": "1"},
            },
        })
        assert initialize.status_code == 200
        headers["MCP-Protocol-Version"] = initialize.json()["result"]["protocolVersion"]
        for request_id in (1, 2):
            result = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
                    "name": "faqSearch", "arguments": {
                        "query": "保修期多久", "kb_key": "demo", "productCategory": [], "contactId": "lifetime",
                    },
                },
            })
            assert result.status_code == 200
            data = result.json()["result"]
            assert not data.get("isError")
            assert json.loads(data["content"][0]["text"])["results"]
        assert store.closes == 0
    assert store.closes == 1
