import argparse
import logging
import time
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .business import register
from .config import settings
from .store import LazyStore, Store

log = logging.getLogger("gpt_live.mcp")


def make_server(service: str, port: int = 8000, host: str = "127.0.0.1",
                store: Store | None = None) -> FastMCP:
    config = settings()
    store = store or LazyStore(config)

    server = FastMCP(
        f"GPT-Live DEMO {service}", host=host, port=port,
        stateless_http=True, json_response=True,
        transport_security=None,
    )
    register(server, store, service)

    @server.custom_route("/healthz", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": service, "mock": True})

    if service == "verification":
        @server.custom_route("/maintenance/session", methods=["POST"])
        async def delete_session(request: Request) -> JSONResponse:
            data = await request.json()
            contact = data.get("contactId")
            if not isinstance(contact, str) or not contact or len(contact) > 80:
                return JSONResponse({"error": "Valid application session ID required"}, status_code=400)
            await store.delete(contact)
            return JSONResponse({"deleted": True})

        @server.custom_route("/maintenance/expired", methods=["POST"])
        async def delete_expired(_: Request) -> JSONResponse:
            return JSONResponse({"deleted": await store.cleanup()})

    return server


class TimedApp:
    def __init__(self, app: Any, service: str, store: Store):
        self.app, self.service, self.store = app, service, store

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            # FastMCP's protocol lifespan runs per stateless session, not per worker.
            try:
                await self.app(scope, receive, send)
            finally:
                self.store.close()
            return
        started = time.perf_counter()
        status = 500
        trace = dict(scope.get("headers", [])).get(b"traceparent", b"").decode("ascii", errors="replace")

        async def wrapped(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"server-timing", f"handler;dur={(time.perf_counter()-started)*1000:.3f}".encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        finally:
            if scope["type"] == "http":
                log.info("service=%s traceparent=%s status=%s duration_ms=%.3f",
                         self.service, trace[:80], status, (time.perf_counter() - started) * 1000)


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=["faq", "repair", "verification"])
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    store = LazyStore(settings())
    server = make_server(args.service, args.port, args.host, store)
    uvicorn.run(TimedApp(server.streamable_http_app(), args.service, store), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
