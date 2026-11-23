from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from gpt_live_python.bridge import CLASSIFY_TOOL
from gpt_live_python.config import Settings
from gpt_live_python.web import create_app


def test_config_has_only_classifier_no_credentials_and_session_boundaries():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        config = client.get("/api/config")
        assert config.status_code == 200
        data = config.json()
        tools = data["defaultConfig"]["delegation"]["responses"]["tools"]
        assert tools == [CLASSIFY_TOOL]
        assert data["managed_tools"] is True
        assert all(c["state"] == "unloaded" for c in data["catalogs"])
        assert "azure_api_key" not in config.text
        assert client.post("/api/sessions", json={}).status_code == 403
        assert client.post("/api/sessions", json={}, headers={
            "origin": "http://evil.test", "x-studio-csrf": data["csrf"],
        }).status_code == 403
        assert client.post("/api/sessions/old-session/approvals/old-call", json={"approved": True}, headers={
            "origin": "http://localhost:8000", "x-studio-csrf": data["csrf"],
        }).status_code in (404, 405)


def test_creating_live_does_not_wait_for_any_mcp_directory():
    cfg = Settings(_env_file=None, azure_endpoint="https://example.services.ai.azure.com")
    with patch("gpt_live_python.web.Live.start", AsyncMock(return_value={
        "id": "test", "session": {"model": cfg.live_deployment},
    })), patch("gpt_live_python.web.Remote.discover", AsyncMock(side_effect=AssertionError("No startup discovery"))) as discover:
        with TestClient(create_app(cfg)) as client:
            config = client.get("/api/config").json()
            created = client.post("/api/sessions", json={
                "session": config["defaultConfig"], "transport": "websocket",
            }, headers={"origin": "http://localhost:8000", "x-studio-csrf": config["csrf"]})
            assert created.status_code == 200
            discover.assert_not_awaited()


def test_catalog_refresh_requires_valid_owner_and_configured_server_names():
    with patch("gpt_live_python.web.Remote.catalog", AsyncMock()) as catalog:
        with TestClient(create_app(Settings(_env_file=None))) as client:
            assert client.post("/api/mcp/catalog/refresh", json={}).status_code == 403
            config = client.get("/api/config").json()
            headers = {"origin": "http://localhost:8000", "x-studio-csrf": config["csrf"]}
            assert client.post("/api/mcp/catalog/refresh", json={"servers": ["untrusted"]}, headers=headers).status_code == 400
            catalog.assert_not_awaited()
            result = client.post("/api/mcp/catalog/refresh", json={"servers": ["faq"]}, headers=headers)
            assert result.status_code == 200
            catalog.assert_awaited_once_with("faq", force=True)


def test_network_start_failure_is_explicit_and_releases_startup_reservation():
    cfg = Settings(_env_file=None, azure_endpoint="https://example.services.ai.azure.com")
    startup = AsyncMock(side_effect=[
        ConnectionResetError(),
        {"id": "test", "session": {"model": cfg.live_deployment}},
    ])
    with patch("gpt_live_python.web.Live.start", startup):
        with TestClient(create_app(cfg)) as client:
            config = client.get("/api/config").json()
            headers = {"origin": "http://localhost:8000", "x-studio-csrf": config["csrf"]}
            data = {"session": config["defaultConfig"], "transport": "websocket"}
            failed = client.post("/api/sessions", json=data, headers=headers)
            assert failed.status_code == 400
            assert failed.json()["error"] == "ConnectionResetError"
            assert client.post("/api/sessions", json=data, headers=headers).status_code == 200
