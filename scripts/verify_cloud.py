"""Verify remote endpoint authentication and all seven tools, without printing keys."""

import asyncio

import httpx

from gpt_live_python.config import settings
from gpt_live_python.remote import Remote


async def main():
    config = settings()
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        for name, (url, key) in config.mcp_servers.items():
            if not key:
                raise ValueError(f"{name} has no Function key configured")
            for headers in ({}, {"x-functions-key": "deliberately-invalid-key"}):
                result = await client.post(url, headers=headers, json={})
                if result.status_code not in (401, 403):
                    raise RuntimeError(f"{name} failed anonymous/invalid-key rejection: {result.status_code}")
            health = await client.get(url.removesuffix("/mcp") + "/healthz",
                                      headers={"x-functions-key": key})
            health.raise_for_status()
            print(f"{name}: authenticated health OK, anonymous/invalid key rejected")
    tools = await Remote(config).discover()
    assert len(tools) == 7
    print("All seven remote tool schemas verified")


if __name__ == "__main__":
    asyncio.run(main())
