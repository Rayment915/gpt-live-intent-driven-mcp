import asyncio
import time
from collections import Counter
from contextlib import asynccontextmanager

import pytest
from mcp.types import ListToolsResult, Tool

import gpt_live_python.remote as remote_module
from gpt_live_python.config import Settings
from gpt_live_python.remote import TOOL_SERVER, Catalog, Remote


def definitions(server):
    return {
        name: {"type": "function", "name": name, "parameters": {"type": "object"}}
        for name, owner in TOOL_SERVER.items() if owner == server
    }


async def test_on_demand_catalog_single_refresh_ttl_and_copy_isolation():
    remote = Remote(Settings(_env_file=None))
    now = [0.0]
    remote._clock = lambda: now[0]
    calls = Counter()

    async def fetch(server):
        calls[server] += 1
        await asyncio.sleep(.02)
        return Catalog(definitions(server), f"v{calls[server]}", now[0] + 300)

    remote._fetch = fetch
    try:
        responses = await asyncio.gather(*(remote.tools_for(["faqSearch"]) for _ in range(8)))
        assert calls == {"faq": 1}
        responses[0][0]["parameters"]["malicious"] = True
        assert "malicious" not in (await remote.tools_for(["faqSearch"]))[0]["parameters"]
        now[0] = 299.9
        await remote.tools_for(["faqSearch"])
        assert calls == {"faq": 1}
        now[0] = 300
        await remote.tools_for(["faqSearch"])
        assert calls == {"faq": 2}
        await remote.catalog("faq", force=True)
        assert calls == {"faq": 3}
        assert await remote.tools_for([]) == []
        assert calls == {"faq": 3}
    finally:
        await remote.close()


async def test_required_servers_discover_in_parallel_not_sequentially():
    remote = Remote(Settings(_env_file=None))
    started = set()
    both = asyncio.Event()
    durations = []

    async def fetch(server):
        start = time.perf_counter()
        started.add(server)
        if len(started) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 1)
        await asyncio.sleep(.04)
        durations.append(time.perf_counter() - start)
        return Catalog(definitions(server), "v1", time.monotonic() + 300)

    remote._fetch = fetch
    try:
        start = time.perf_counter()
        result = await remote.tools_for(["checkCustomer", "setCustomerByPhoneAndName", "createRepair"])
        elapsed = time.perf_counter() - start
        assert started == {"verification", "repair"}
        assert {t["name"] for t in result} == {
            "checkCustomer", "setCustomerByPhoneAndName", "createRepair",
        }
        assert elapsed < sum(durations)
    finally:
        await remote.close()


async def test_expired_refresh_failure_never_falls_back_to_stale_tools():
    remote = Remote(Settings(_env_file=None))
    now = [0.0]
    remote._clock = lambda: now[0]

    async def success(server):
        return Catalog(definitions(server), "v1", 300)

    async def failure(server):
        raise ValueError("Directory unavailable")

    remote._fetch = success
    await remote.tools_for(["faqSearch"])
    now[0] = 300
    remote._fetch = failure
    try:
        with pytest.raises(ValueError, match="Directory unavailable"):
            await remote.tools_for(["faqSearch"])
        assert remote._catalogs["faq"].version == "v1"
        assert not remote.cache_valid("faq")
        assert next(s for s in remote.catalog_status() if s["server"] == "faq")["state"] == "failed"
    finally:
        await remote.close()


async def test_cancelling_one_waiter_preserves_shared_refresh_and_config_changes_invalidate():
    remote = Remote(Settings(_env_file=None))
    release = asyncio.Event()
    entered = asyncio.Event()
    calls = 0

    async def fetch(server):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return Catalog(definitions(server), "v1", time.monotonic() + 300)

    remote._fetch = fetch
    first = asyncio.create_task(remote.tools_for(["faqSearch"]))
    second = asyncio.create_task(remote.tools_for(["faqSearch"]))
    try:
        await entered.wait()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        release.set()
        assert (await second)[0]["name"] == "faqSearch"
        assert calls == 1
        remote.config.mcp_faq_url = "http://127.0.0.1:9101/mcp"
        await remote.tools_for(["faqSearch"])
        assert calls == 2
    finally:
        await remote.close()


async def test_invalid_remote_schema_is_reported_without_caching(monkeypatch):
    class Session:
        async def initialize(self):
            pass

        async def list_tools(self, cursor=None):
            return ListToolsResult(tools=[
                Tool(name="faqSearch", inputSchema={"type": "invalid-json-schema-type"}),
            ])

    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield None, None, None

    @asynccontextmanager
    async def client(*args, **kwargs):
        yield Session()

    monkeypatch.setattr(remote_module, "streamablehttp_client", transport)
    monkeypatch.setattr(remote_module, "ClientSession", client)
    remote = Remote(Settings(_env_file=None))
    try:
        with pytest.raises(ValueError, match="Invalid MCP inputSchema: faq/faqSearch"):
            await remote.catalog("faq")
        assert not remote._catalogs
        assert not remote.cache_valid("faq")
    finally:
        await remote.close()
