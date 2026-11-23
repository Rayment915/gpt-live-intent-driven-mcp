import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from gpt_live_python.config import Settings
from gpt_live_python.login import password_hash, verify_password
from gpt_live_python.web import create_app


@pytest.fixture
def cfg():
    return Settings(_env_file=None, auth_required=True, auth_password_hash=password_hash("only-a-test-password"),
                    app_origin="https://localhost:8000")


def signin(client):
    bootstrap = client.get("/api/auth").json()
    headers = {"origin": "https://localhost:8000", "x-studio-csrf": bootstrap["csrf"]}
    response = client.post("/api/auth/login", headers=headers,
                           json={"username": "admin", "password": "only-a-test-password"})
    assert response.status_code == 200
    return {"origin": "https://localhost:8000", "x-studio-csrf": response.json()["csrf"]}


def test_hash_is_salted_and_password_is_not_stored():
    first, second = password_hash("only-a-test-password"), password_hash("only-a-test-password")
    assert first != second and "only-a-test-password" not in first
    assert verify_password("only-a-test-password", first)
    assert not verify_password("incorrect", first)


def test_public_hosting_and_auth_fail_closed_when_not_configured():
    with pytest.raises(ValidationError, match="AUTH_REQUIRED"):
        Settings(_env_file=None, allow_private_container=True)
    with pytest.raises(ValidationError, match="Invalid configured password hash"):
        Settings(_env_file=None, auth_required=True)


def test_anonymous_requests_are_rejected_and_success_rotates_session(cfg):
    with TestClient(create_app(cfg), base_url="https://localhost:8000") as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/config").status_code == 401
        bootstrap = client.get("/api/auth")
        assert bootstrap.json()["authenticated"] is False
        before = client.cookies.get("live_owner")
        headers = signin(client)
        assert client.cookies.get("live_owner") != before
        assert client.get("/api/config").status_code == 200
        assert client.post("/api/mcp/catalog/refresh", json={"servers": ["faq"]}).status_code == 403
        assert client.post("/api/auth/logout", json={}, headers=headers).status_code == 200
        assert client.get("/api/config").status_code == 401
        assert "only-a-test-password" not in bootstrap.text


def test_wrong_password_csrf_and_login_rate_limit(cfg):
    with TestClient(create_app(cfg), base_url="https://localhost:8000") as client:
        bootstrap = client.get("/api/auth").json()
        body = {"username": "admin", "password": "incorrect"}
        assert client.post("/api/auth/login", json=body).status_code == 403
        headers = {"origin": "https://localhost:8000", "x-studio-csrf": bootstrap["csrf"]}
        for _ in range(10):
            assert client.post("/api/auth/login", json=body, headers=headers).status_code == 401
        assert client.post("/api/auth/login", json=body, headers=headers).status_code == 429
        assert client.get("/api/config").status_code == 401


def test_logout_revokes_established_websocket_and_closes_owner_live(cfg):
    live = AsyncMock()
    live.id, live.status, live.events, live.clients = "test-session", "ready", [], set()
    live.close = AsyncMock()
    app = create_app(cfg)
    with patch("gpt_live_python.web.Remote.cleanup", AsyncMock()), TestClient(app, base_url="https://localhost:8000") as client:
        headers = signin(client)
        live.owner = client.cookies.get("live_owner")
        app.state.sessions[live.id] = live
        with client.websocket_connect(f"wss://localhost:8000/api/sessions/{live.id}/events", headers={"origin": headers["origin"]}) as ws:
            assert client.post("/api/auth/logout", json={}, headers=headers).status_code == 200
            ws.send_json({"type": "session.input_audio.append", "audio": ""})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        live.command.assert_not_awaited()
        assert live.close.await_count >= 1


def test_anonymous_websocket_and_expired_http_session_are_rejected(cfg):
    app = create_app(cfg)
    with TestClient(app, base_url="https://localhost:8000") as client:
        client.get("/api/auth")
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/sessions/unknown/events",
                                          headers={"origin": "https://localhost:8000"}):
                pass
        signin(client)
        import time
        with patch("gpt_live_python.web.time.time", return_value=time.time() + cfg.auth_session_seconds + 1):
            assert client.get("/api/config").status_code == 401


def test_status_does_not_overwrite_rotated_login_cookie(cfg):
    with TestClient(create_app(cfg), base_url="https://localhost:8000") as client:
        signin(client)
        assert "set-cookie" not in client.get("/api/auth").headers
        assert "set-cookie" not in client.get("/api/config").headers


def test_proxy_http_uses_secure_hosted_cookie_and_oversized_login_is_rejected(cfg):
    cfg = cfg.model_copy(update={"allow_private_container": True})
    with TestClient(create_app(cfg), base_url="http://localhost:8000") as client:
        boot = client.get("/api/auth")
        assert "Secure" in boot.headers["set-cookie"]
        assert boot.headers["strict-transport-security"] == "max-age=31536000"
        cookie = boot.cookies.get("live_owner")
        headers = {"origin": cfg.app_origin, "x-studio-csrf": boot.json()["csrf"],
                   "cookie": f"live_owner={cookie}"}
        assert client.post("/api/auth/login", content=b"x" * 1025, headers=headers).status_code == 400


def test_logout_during_startup_closes_upstream_without_returning_a_session(cfg):
    cfg = cfg.model_copy(update={"azure_endpoint": "https://example.services.ai.azure.com"})
    entered, release = threading.Event(), asyncio.Event()

    async def start(live, sdp=None):
        entered.set()
        await release.wait()
        live.status = "ready"
        return {"id": live.id, "session": {"model": cfg.live_deployment}}

    app = create_app(cfg)
    with patch("gpt_live_python.web.Live.start", start), patch(
        "gpt_live_python.web.Live.close", AsyncMock()
    ) as close, TestClient(app, base_url="https://localhost:8000") as client:
        headers = signin(client)
        config = client.get("/api/config").json()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(client.post, "/api/sessions",
                                      json={"session": config["defaultConfig"], "transport": "websocket"},
                                      headers=headers)
            try:
                assert entered.wait(2)
                assert client.post("/api/auth/logout", json={}, headers=headers).status_code == 200
            finally:
                assert client.portal is not None
                client.portal.call(release.set)
            assert pending.result(timeout=3).status_code == 401
        close.assert_awaited_once()
        assert not app.state.sessions
