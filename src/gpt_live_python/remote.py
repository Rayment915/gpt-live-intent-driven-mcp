import asyncio
import copy
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from mcp.client.streamable_http import streamablehttp_client

from mcp import ClientSession

from .config import Settings
from .trace import Trace

log = logging.getLogger("gpt_live.catalog")


@dataclass(frozen=True)
class Catalog:
    tools: dict[str, dict[str, Any]]
    version: str
    expires: float


TOOL_SERVER = {
    "faqSearch": "faq",
    "checkCustomer": "verification",
    "setCustomerByPhoneAndName": "verification",
    "createRepair": "repair",
    "listRepairs": "repair",
    "getRepairDetail": "repair",
    "cancelRepair": "repair",
}
BUSINESS_DESCRIPTIONS = {
    "createRepair": (
        "Create a DEMO repair automatically after application validation. Call this function once "
        "the customer explicitly requests repair and issue, zipCode and email are collected. "
        "Optional brand/category/model/serial/address can be empty. srType=Inquiry, srSubType=Product. "
        "Customer details must already be saved. No extra verbal confirmation or page click is required. "
        "Call immediately when fields are complete; wait for the actual tool result before claiming success."
    ),
    "cancelRepair": (
        "Cancel one DEMO order automatically after application validation. "
        "Requires the customer's explicit cancellation request, srNumber and cancelReason. "
        "Do not ask for extra verbal confirmation or a page click. "
        "Wait for the actual tool result before claiming cancellation succeeded."
    ),
    "setCustomerByPhoneAndName": (
        "Save customer-provided DEMO details automatically after application validation. "
        "Collect countryCode digits, phoneNumber digits and customerName, one missing field per turn. "
        "bu and contactId are injected by Python. Call once all three customer fields are available, "
        "without an extra verbal confirmation or page click. "
        "This is not OTP or identity authentication; wait for the actual save result."
    ),
}


def response_definition(name: str, description: str | None, schema: dict[str, Any]) -> dict[str, Any]:
    # Keep the model-visible workflow consistent with the application's execution policy.
    return {
        "type": "function",
        "name": name,
        "description": BUSINESS_DESCRIPTIONS.get(name, description or name),
        "parameters": schema,
    }


class Remote:
    def __init__(self, config: Settings):
        self.config = config
        self.tools: dict[str, dict[str, Any]] = {}
        self._catalogs: dict[str, Catalog] = {}
        self._refreshing: dict[str, asyncio.Task[Catalog]] = {}
        self._catalog_configs: dict[str, str] = {}
        self._catalog_errors: dict[str, str] = {}
        self._clock = time.monotonic

    def configuration_key(self, server: str) -> str:
        return hashlib.sha256(json.dumps(self.config.mcp_servers[server]).encode()).hexdigest()

    def cache_valid(self, server: str) -> bool:
        catalog = self._catalogs.get(server)
        return bool(catalog and catalog.expires > self._clock()
                    and self._catalog_configs.get(server) == self.configuration_key(server))

    def headers(self, server: str, traceparent: str | None = None) -> dict[str, str]:
        key = self.config.mcp_servers[server][1]
        headers = {"x-functions-key": key} if key else {}
        if traceparent:
            headers["traceparent"] = traceparent
        return headers

    @staticmethod
    def client_factory(trace: Trace | None = None, span: str | None = None):
        async def timing(response: httpx.Response) -> None:
            match = re.search(r"handler;dur=([0-9.]+)", response.headers.get("server-timing", ""))
            if trace and span and match:
                trace.spans[span]["attributes"]["server_time_to_headers_ms"] = float(match[1])

        def create(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                headers=headers,
                timeout=timeout or httpx.Timeout(30),
                auth=auth,
                follow_redirects=False,
                event_hooks={"response": [timing]},
            )

        return create

    async def _fetch(self, server: str) -> Catalog:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError

        discovered: dict[str, dict[str, Any]] = {}
        url, _ = self.config.mcp_servers[server]
        async with asyncio.timeout(35):
            async with streamablehttp_client(
                url,
                headers=self.headers(server),
                timeout=timedelta(seconds=30),
                httpx_client_factory=self.client_factory(),
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    cursor = None
                    while True:
                        listed = await session.list_tools(cursor=cursor)
                        for tool in listed.tools:
                            if TOOL_SERVER.get(tool.name) != server or tool.name in discovered:
                                raise ValueError(f"Unexpected or duplicate tool: {server}/{tool.name}")
                            try:
                                Draft202012Validator.check_schema(tool.inputSchema)
                            except SchemaError as exc:
                                raise ValueError(f"Invalid MCP inputSchema: {server}/{tool.name}") from exc
                            discovered[tool.name] = response_definition(
                                tool.name,
                                tool.description,
                                tool.inputSchema,
                            )
                        if not listed.nextCursor:
                            break
                        if listed.nextCursor == cursor:
                            raise ValueError(f"MCP catalog pagination did not advance: {server}")
                        cursor = listed.nextCursor
        expected = {name for name, owner in TOOL_SERVER.items() if owner == server}
        if set(discovered) != expected:
            raise ValueError(f"MCP {server} did not expose its expected tools")
        version = hashlib.sha256(json.dumps(discovered, sort_keys=True).encode()).hexdigest()[:12]
        return Catalog(discovered, version, self._clock() + self.config.mcp_catalog_ttl_seconds)

    async def catalog(self, server: str, force: bool = False) -> Catalog:
        if server not in self.config.mcp_servers:
            raise ValueError("Unknown configured MCP server")
        cached = self._catalogs.get(server)
        if not force and cached and self.cache_valid(server):
            return Catalog(copy.deepcopy(cached.tools), cached.version, cached.expires)
        task = self._refreshing.get(server)
        if task is None:

            async def refresh() -> Catalog:
                configuration = self.configuration_key(server)
                result = await self._fetch(server)
                if configuration != self.configuration_key(server):
                    raise ValueError("MCP configuration changed during directory refresh")
                self._catalogs[server] = result
                self._catalog_configs[server] = configuration
                self._catalog_errors.pop(server, None)
                self.tools.update(copy.deepcopy(result.tools))
                return result

            task = asyncio.create_task(refresh())
            self._refreshing[server] = task

            def finished(done: asyncio.Task[Catalog]) -> None:
                if self._refreshing.get(server) is done:
                    self._refreshing.pop(server, None)
                if not done.cancelled() and done.exception() is not None:
                    self._catalog_errors[server] = type(done.exception()).__name__
                    log.error(
                        "MCP directory refresh failed: server=%s error=%s",
                        server,
                        type(done.exception()).__name__,
                    )

            task.add_done_callback(finished)
        result = await asyncio.shield(task)
        return Catalog(copy.deepcopy(result.tools), result.version, result.expires)

    async def tools_for(
        self, names: list[str], trace: Trace | None = None, parent: str | None = None
    ) -> list[dict[str, Any]]:
        if any(name not in TOOL_SERVER for name in names):
            raise ValueError("Unknown business tool")

        async def load(server: str) -> Catalog:
            if trace:
                with trace.span(
                    "MCP catalog: " + server,
                    "discovery",
                    parent,
                    server=server,
                    cache_hit=self.cache_valid(server),
                    shared_refresh=server in self._refreshing,
                ) as sid:
                    result = await self.catalog(server)
                    trace.spans[sid]["attributes"]["catalog_version"] = result.version
                    return result
            return await self.catalog(server)

        servers = sorted({TOOL_SERVER[name] for name in names})
        catalogs = await asyncio.gather(*(load(server) for server in servers))
        tools = {name: tool for catalog in catalogs for name, tool in catalog.tools.items()}
        return [copy.deepcopy(tools[name]) for name in sorted(set(names))]

    async def discover(self, force: bool = False) -> list[dict[str, Any]]:
        catalogs = await asyncio.gather(*(self.catalog(server, force) for server in self.config.mcp_servers))
        return [tool for catalog in catalogs for tool in catalog.tools.values()]

    def catalog_status(self) -> list[dict[str, Any]]:
        return [
            {
                "server": server,
                "state": "refreshing"
                if server in self._refreshing
                else (
                    "failed"
                    if server in self._catalog_errors
                    else
                    "valid"
                    if self.cache_valid(server)
                    else "expired"
                    if catalog
                    else "unloaded"
                ),
                "version": catalog.version if catalog else None,
                "expires_in_seconds": max(0, catalog.expires - self._clock()) if catalog else 0,
                "tools": sorted(catalog.tools) if catalog else [],
            }
            for server in self.config.mcp_servers
            for catalog in [self._catalogs.get(server)]
        ]

    async def close(self) -> None:
        tasks = list(self._refreshing.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def call(self, name: str, arguments: dict[str, Any], trace: Trace, parent: str) -> dict[str, Any]:
        server = TOOL_SERVER[name]
        url, _ = self.config.mcp_servers[server]
        with trace.span(f"{server}/{name}", "mcp", parent, server=server, tool=name) as sid:
            async with asyncio.timeout(35):
                async with streamablehttp_client(
                    url,
                    headers=self.headers(server, trace.header(sid)),
                    timeout=timedelta(seconds=30),
                    httpx_client_factory=self.client_factory(trace, sid),
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        with trace.span("MCP initialize", "mcp", sid, server=server):
                            await session.initialize()
                        with trace.span("tools/call", "mcp", sid, server=server, tool=name):
                            result = await session.call_tool(name, arguments)
                        text = "\n".join(part.text for part in result.content if part.type == "text")
                        if result.isError:
                            raise ValueError(f"MCP {server}/{name} rejected the operation: {text[:400]}")
                        value = json.loads(text)
                        if not isinstance(value, dict) or value.get("mock") is not True:
                            raise ValueError("Expected an explicitly marked Mock business result")
                        if value.get("status") == "failed":
                            raise ValueError(f"Business operation rejected: {value.get('message')}")
                        return value

    async def cleanup(self, contact: str | None = None) -> dict[str, Any]:
        url, _ = self.config.mcp_servers["verification"]
        endpoint = url.removesuffix("/mcp") + ("/maintenance/session" if contact else "/maintenance/expired")
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.post(
                endpoint, headers=self.headers("verification"), json={"contactId": contact} if contact else {}
            )
            response.raise_for_status()
            return response.json()
