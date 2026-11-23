import asyncio
import json
import logging
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from azure.core.exceptions import AzureError
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from websockets.exceptions import WebSocketException

from .azure import Azure
from .bridge import CLASSIFY_TOOL, LIVE_INSTRUCTIONS, RESPONSES_INSTRUCTIONS
from .config import Settings, settings
from .live import Live, validate_models
from .login import verify_password
from .protocol import SessionConfig
from .remote import TOOL_SERVER, Remote
from .store import LazyStore

log = logging.getLogger("gpt_live.web")


def create_app(config: Settings | None = None) -> FastAPI:
    cfg = config or settings()
    azure = Azure(cfg)
    remote = Remote(cfg)
    store = LazyStore(cfg)
    owners: dict[str, tuple[str, float]] = {}
    authenticated: set[str] = set()
    login_attempts: deque[float] = deque()
    login_slots = asyncio.Semaphore(2)
    sessions: dict[str, Live] = {}
    startup_lock = asyncio.Lock()
    creating: set[str] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await asyncio.gather(*(live.close() for live in sessions.values() if live.status == "ready"))
            await azure.close()
            await remote.close()
            store.close()

    app = FastAPI(title="GPT-Live Python", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.sessions = sessions
    app.state.azure = azure

    def owner(request: Request) -> str:
        key = request.cookies.get("live_owner", "")
        found = owners.get(key)
        if not found or found[1] < time.time():
            raise HTTPException(401, "Refresh the page to establish a local application session")
        if cfg.auth_required and key not in authenticated:
            raise HTTPException(401, "Login required")
        return key

    def establish_owner(request: Request) -> str:
        now = time.time()
        for key in [key for key, (_, expires) in owners.items() if expires <= now]:
            owners.pop(key)
            authenticated.discard(key)
        key = request.cookies.get("live_owner", "")
        if key not in owners:
            if len(owners) >= 500:
                raise HTTPException(429, "Too many application sessions")
            key = secrets.token_hex(32)
            duration = 600 if cfg.auth_required else cfg.auth_session_seconds
            owners[key] = (secrets.token_hex(32), now + duration)
        return key

    def owner_cookie(response: JSONResponse, key: str, request: Request) -> JSONResponse:
        if request.cookies.get("live_owner") == key:
            return response
        response.set_cookie(
            "live_owner", key, httponly=True, samesite="strict",
            secure=cfg.allow_private_container or request.url.scheme == "https",
            max_age=max(1, int(owners[key][1] - time.time())),
        )
        return response

    def owned(request: Request, identifier: str) -> Live:
        live = sessions.get(identifier)
        if not live or live.owner != owner(request):
            raise HTTPException(404, "Session not found")
        return live

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        if (
            request.url.path != "/healthz"
            and not cfg.allow_private_container
            and request.client
            and request.client.host not in ("127.0.0.1", "::1", "localhost", "testclient")
        ):
            return JSONResponse(
                {"error": "Local-only application; public hosting requires authentication"}, status_code=403
            )
        origin = request.headers.get("origin")
        if request.url.path.startswith("/api"):
            if origin and origin not in cfg.origins:
                return JSONResponse({"error": "Origin not allowed"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"error": "Cross-site request rejected"}, status_code=403)
            if cfg.auth_required and request.url.path not in ("/api/auth", "/api/auth/login"):
                key = request.cookies.get("live_owner", "")
                found = owners.get(key)
                if not found or found[1] <= time.time() or key not in authenticated:
                    return JSONResponse({"error": "Login required"}, status_code=401)
            if request.method != "GET":
                key = request.cookies.get("live_owner", "")
                item = owners.get(key)
                csrf = request.headers.get("x-studio-csrf", "")
                if (
                    not origin
                    or not item
                    or item[1] < time.time()
                    or not secrets.compare_digest(csrf, item[0])
                ):
                    return JSONResponse({"error": "Invalid origin, session or CSRF token"}, status_code=403)
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Permissions-Policy": "microphone=(self), camera=()",
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                    "connect-src 'self' ws: wss:; media-src 'self' blob:; "
                    "img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
                ),
            }
        )
        if cfg.allow_private_container:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/api/auth")
    async def auth_status(request: Request):
        key = establish_owner(request)
        logged_in = not cfg.auth_required or key in authenticated
        return owner_cookie(JSONResponse({
            "required": cfg.auth_required, "authenticated": logged_in, "csrf": owners[key][0],
            "username": cfg.auth_username if logged_in and cfg.auth_required else None,
        }), key, request)

    @app.post("/api/auth/login")
    async def login(request: Request):
        if not cfg.auth_required:
            raise HTTPException(409, "Authentication is not enabled in loopback development")
        now = time.monotonic()
        while login_attempts and login_attempts[0] <= now - 60:
            login_attempts.popleft()
        if len(login_attempts) >= 10:
            log.warning("Login rate limit reached")
            raise HTTPException(429, "Too many login attempts; retry after one minute")
        login_attempts.append(now)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 1024:
                raise HTTPException(400, "Login request too large")
            raw.extend(chunk)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "Invalid login request") from exc
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("username"), str)
            or not isinstance(data.get("password"), str)
            or len(data["username"]) > 128
            or len(data["password"]) > 128
        ):
            raise HTTPException(400, "Invalid login request")
        async with login_slots:
            matches = await asyncio.to_thread(
                verify_password, data["password"], cfg.auth_password_hash.get_secret_value()
            )
        if not (matches and secrets.compare_digest(data["username"].encode(), cfg.auth_username.encode())):
            log.warning("Login rejected")
            raise HTTPException(401, "Incorrect username or password")
        previous = request.cookies.get("live_owner", "")
        owners.pop(previous, None)
        authenticated.discard(previous)
        key = secrets.token_hex(32)
        owners[key] = (secrets.token_hex(32), time.time() + cfg.auth_session_seconds)
        authenticated.add(key)
        return owner_cookie(JSONResponse({
            "required": True, "authenticated": True, "csrf": owners[key][0], "username": cfg.auth_username,
        }), key, request)

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        key = owner(request)
        authenticated.discard(key)
        owners.pop(key, None)
        await asyncio.gather(*(live.close() for live in sessions.values()
                               if live.owner == key and live.status == "ready"))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie("live_owner", httponly=True, samesite="strict",
                               secure=cfg.allow_private_container or request.url.scheme == "https")
        return response

    @app.get("/api/config")
    async def configuration(request: Request):
        key = establish_owner(request)
        response = JSONResponse(
            {
                "csrf": owners[key][0],
                "configured": bool(cfg.azure_endpoint),
                "endpoint": cfg.azure_endpoint,
                "deployments": {
                    "live": cfg.live_deployment,
                    "responses": cfg.responses_deployment,
                    "decision": cfg.decision_deployment,
                },
                "defaultConfig": {
                    "model": cfg.live_deployment,
                    "instructions": LIVE_INSTRUCTIONS,
                    "audio": {"output": {"voice": "marin"}},
                    "delegation": {
                        "type": "responses",
                        "responses": {
                            "model": cfg.responses_deployment,
                            "instructions": RESPONSES_INSTRUCTIONS,
                            "max_output_tokens": 2048,
                            "service_tier": "default",
                            "reasoning": {"effort": "low"},
                            "text": {"verbosity": "low"},
                            "tools": [CLASSIFY_TOOL],
                            "tool_choice": "required",
                            "parallel_tool_calls": True,
                        },
                    },
                },
                "mcp": [
                    {
                        "name": name,
                        "url": url,
                        "configured": bool(url),
                        "tools": [t for t, server in TOOL_SERVER.items() if server == name],
                    }
                    for name, (url, _) in cfg.mcp_servers.items()
                ],
                "threshold": cfg.intent_threshold,
                "maxSessionSeconds": cfg.session_ttl_seconds,
                "catalogs": remote.catalog_status(),
                "managed_tools": True,
            }
        )
        return owner_cookie(response, key, request)

    @app.post("/api/sessions")
    async def start(request: Request):
        identity = owner(request)
        async with startup_lock:
            for key in [key for key, s in sessions.items() if s.status == "closed"]:
                sessions.pop(key)
            if identity in creating or any(s.owner == identity for s in sessions.values()):
                raise HTTPException(409, "End the current session first")
            if len(sessions) + len(creating) >= cfg.max_sessions:
                raise HTTPException(429, "Session capacity reached")
            if not cfg.azure_endpoint:
                raise HTTPException(503, "Configure AZURE_ENDPOINT in the backend .env")
            creating.add(identity)
        try:
            data = await request.json()
            session = SessionConfig.model_validate(data["session"])
            transport = data["transport"]
            if transport not in ("webrtc", "websocket"):
                raise ValueError("Invalid transport")
            validate_models(session, cfg, [])
            if session.delegation and session.delegation.type == "responses":
                responses = session.delegation.responses
                assert responses is not None
                if responses.get("tools") != [CLASSIFY_TOOL] or responses.get("tool_choice") != "required":
                    raise ValueError(
                        "Managed sessions start with only classify_intent and required tool choice"
                    )
            live = Live(identity, session, transport, cfg, azure, remote)
            created = await live.start(data.get("sdp"))
            try:
                owner(request)
            except HTTPException:
                await live.close()
                raise
            sessions[live.id] = live
            return created
        except (
            AzureError,
            ValueError,
            KeyError,
            ValidationError,
            httpx.HTTPError,
            WebSocketException,
            OSError,
            TimeoutError,
            ExceptionGroup,
        ) as exc:
            log.error("Session startup failed: %s", type(exc).__name__)
            detail = (
                str(exc)[:700] or type(exc).__name__
                if not isinstance(exc, httpx.HTTPStatusError)
                else (f"Upstream HTTP {exc.response.status_code}: {exc.response.text[:600]}")
            )
            raise HTTPException(400, detail) from exc
        finally:
            creating.discard(identity)

    @app.post("/api/mcp/catalog/refresh")
    async def refresh_catalog(request: Request):
        owner(request)
        try:
            data = await request.json()
            servers = data.get("servers", list(cfg.mcp_servers))
            if (
                not isinstance(servers, list)
                or not servers
                or any(not isinstance(name, str) or name not in cfg.mcp_servers for name in servers)
            ):
                raise ValueError("Select configured MCP servers to refresh")
            await asyncio.gather(*(remote.catalog(name, force=True) for name in set(servers)))
            return {"catalogs": remote.catalog_status()}
        except (ValueError, KeyError, httpx.HTTPError, OSError, TimeoutError, ExceptionGroup) as exc:
            log.error("Catalog refresh failed: %s", type(exc).__name__)
            raise HTTPException(400, f"MCP directory refresh failed: {type(exc).__name__}") from exc

    @app.post("/api/sessions/{identifier}/commands")
    async def command(identifier: str, request: Request):
        live = owned(request, identifier)
        try:
            data = await request.json()
            await live.command(data["event"], data.get("experimental") is True)
            return {"sent": True}
        except (ValueError, KeyError, WebSocketException, TimeoutError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.websocket("/api/sessions/{identifier}/events")
    async def events(websocket: WebSocket, identifier: str):
        key = websocket.cookies.get("live_owner", "")
        live = sessions.get(identifier)
        if (
            websocket.headers.get("origin") not in cfg.origins
            or not live
            or live.owner != key
            or key not in owners
            or owners[key][1] <= time.time()
            or (cfg.auth_required and key not in authenticated)
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        # Snapshot is copied before subscribing, with no await between the two.
        snapshot = list(live.events)
        live.clients.add(queue)

        async def receive():
            while True:
                raw = await websocket.receive_text()
                if (key not in owners or owners[key][1] <= time.time()
                        or (cfg.auth_required and key not in authenticated)):
                    raise ValueError("Login expired; sign in again")
                if len(raw) > 160_000:
                    raise ValueError("WebSocket event too large")
                event = json.loads(raw)
                if event.get("type") != "session.input_audio.append":
                    raise ValueError(
                        "Only audio events are accepted on this stream; use CSRF-protected commands"
                    )
                await live.command(event)

        receiver = asyncio.create_task(receive())
        next_event: asyncio.Task[dict[str, Any]] | None = None
        try:
            for frame in snapshot:
                await websocket.send_json(frame)
            while True:
                if (key not in owners or owners[key][1] <= time.time()
                        or (cfg.auth_required and key not in authenticated)):
                    await websocket.send_json({"type": "studio.error", "message": "Login expired; sign in again"})
                    await websocket.close(code=1008)
                    break
                next_event = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {receiver, next_event}, timeout=min(30, owners[key][1] - time.time()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                    continue
                if receiver in done:
                    next_event.cancel()
                    await receiver
                    break
                frame = next_event.result()
                await websocket.send_json(frame)
                if frame["type"] == "studio.disconnected":
                    break
        except WebSocketDisconnect:
            log.info("Browser disconnected session=%s", live.id)
        except (ValueError, WebSocketException, TimeoutError) as exc:
            log.warning("Browser event stream failed: %s", type(exc).__name__)
            await websocket.close(code=1008, reason="Invalid event or failed upstream")
        finally:
            if next_event:
                next_event.cancel()
                await asyncio.gather(next_event, return_exceptions=True)
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            live.clients.discard(queue)
            if not live.clients:
                await live.close()
                try:
                    await remote.cleanup(live.id)
                except (ValueError, httpx.HTTPError) as exc:
                    log.error("Session data cleanup failed: %s", type(exc).__name__)

    static = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    # Installed containers set FRONTEND_DIST; editable development uses the repository build.
    import os

    static = Path(os.environ.get("FRONTEND_DIST", str(static)))
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="frontend")
    return app


app = create_app()
