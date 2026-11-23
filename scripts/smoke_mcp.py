"""Exercise the real MCP protocol and shared Mock state. --local owns temporary child processes."""

import argparse
import asyncio
import os
import subprocess
import sys
import uuid

import httpx

from gpt_live_python.config import Settings, settings
from gpt_live_python.remote import Remote
from gpt_live_python.trace import Trace


async def exercise(config: Settings) -> None:
    remote = Remote(config)
    tools = await remote.discover()
    assert len(tools) == 7
    trace = Trace(lambda _: None)
    contact = "smoke-" + uuid.uuid4().hex

    async def call(name, **args):
        return await remote.call(name, {"contactId": contact, **args}, trace, "")

    assert (await call("checkCustomer"))["status"] == "customer_not_found"
    faq = await call("faqSearch", query="保修期多久", kb_key="demo", productCategory=[])
    assert faq["results"][0]["answer_id"] == "DEMO-FAQ-002"
    await call("setCustomerByPhoneAndName", bu="DEMO", countryCode="86",
               phoneNumber="0000000000", customerName="测试客户")
    assert (await call("checkCustomer"))["status"] == "customer_found"
    fields = dict(addressRemark="", description="模拟无法启动", zipCode="DEMO",
                  email="smoke@example.test", srType="Inquiry", srSubType="Product")
    created = await call("createRepair", **fields)
    assert (await call("createRepair", **fields))["srNumber"] == created["srNumber"]
    assert (await call("listRepairs"))["count"] == 1
    number = created["srNumber"]
    assert (await call("getRepairDetail", srNumber=number))["detail"]["status"] == "Draft"
    await call("cancelRepair", srNumber=number, cancelReason="合成测试结束")
    assert (await call("getRepairDetail", srNumber=number))["detail"]["status"] == "Cancelled"
    await remote.cleanup(contact)
    print("PASS: 3 real MCP endpoints, 7 tools, shared identity, idempotent create/read/cancel, cleanup")


async def main(local: bool) -> None:
    config = settings()
    processes = []
    if local:
        values = {f"mcp_{name}_url": f"http://127.0.0.1:{port}/mcp"
                  for name, port in (("faq", 8101), ("repair", 8102), ("verification", 8103))}
        values.update({f"mcp_{name}_key": "" for name in ("faq", "repair", "verification")})
        config = Settings(**{**config.model_dump(), **values, "state_backend": "sqlite"})
    try:
        if local:
            for name, port in (("faq", 8101), ("repair", 8102), ("verification", 8103)):
                processes.append(subprocess.Popen(
                    [sys.executable, "-m", "gpt_live_python.mcp_server", name, "--port", str(port)],
                    env={**os.environ, "STATE_BACKEND": "sqlite"},
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                ))
            async with httpx.AsyncClient() as client:
                for port in (8101, 8102, 8103):
                    for _ in range(100):
                        try:
                            r = await client.get(f"http://127.0.0.1:{port}/healthz")
                            r.raise_for_status()
                            break
                        except httpx.ConnectError:
                            await asyncio.sleep(0.1)
                    else:
                        raise RuntimeError(f"Local MCP failed readiness at {port}")
        await exercise(config)
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            process.wait(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.local))
