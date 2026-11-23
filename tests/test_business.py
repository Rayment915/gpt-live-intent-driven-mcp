import json

import pytest
from mcp.server.fastmcp import FastMCP

from gpt_live_python.business import register
from gpt_live_python.store import SQLiteStore


async def invoke(server, name, **args):
    result = await server.call_tool(name, args)
    # FastMCP returns (unstructured content, structured content) on current SDKs.
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


@pytest.fixture
def business(tmp_path):
    store = SQLiteStore(str(tmp_path / "demo.sqlite"))
    server = FastMCP("test")
    for service in ("faq", "repair", "verification"):
        register(server, store, service)
    return server, store


async def test_faq_before_identity_and_full_repair_lifecycle(business):
    server, store = business
    faq = await invoke(server, "faqSearch", contactId="a", query="保修期多久", kb_key="demo", productCategory=[])
    assert faq["mock"] and faq["results"][0]["answer_id"] == "DEMO-FAQ-002"
    assert (await invoke(server, "checkCustomer", contactId="a"))["status"] == "customer_not_found"
    fields = dict(contactId="a", addressRemark="", description="不能启动", zipCode="DEMO", email="test@example.test",
                  srType="Inquiry", srSubType="Product")
    with pytest.raises(Exception, match="customer_not_verified"):
        await invoke(server, "createRepair", **fields)
    await invoke(server, "setCustomerByPhoneAndName", contactId="a", bu="DEMO",
                 countryCode="86", phoneNumber="0000000000", customerName="测试客户")
    assert (await store.get("a"))["last_faq_query"] == "保修期多久"
    created = await invoke(server, "createRepair", **fields)
    again = await invoke(server, "createRepair", **fields)
    assert created["srNumber"] == again["srNumber"]
    assert (await invoke(server, "listRepairs", contactId="a"))["count"] == 1
    number = created["srNumber"]
    await invoke(server, "cancelRepair", contactId="a", srNumber=number, cancelReason="测试结束")
    detail = await invoke(server, "getRepairDetail", contactId="a", srNumber=number)
    assert detail["detail"]["status"] == "Cancelled"
    with pytest.raises(Exception, match="customer_not_verified"):
        await invoke(server, "getRepairDetail", contactId="b", srNumber=number)


async def test_store_concurrency_expiry_and_restart(tmp_path):
    path = str(tmp_path / "state.sqlite")
    first = SQLiteStore(path, ttl=1800)
    await first.mutate("a", lambda s: {**s, "identity": True})
    second = SQLiteStore(path, ttl=1800)
    await second.mutate("a", lambda s: {**s, "faq": "warranty"})
    assert await first.get("a") == {"identity": True, "faq": "warranty"}
    expired = SQLiteStore(path, ttl=-1)
    await expired.mutate("a", lambda s: s)
    assert await second.get("a") == {}
    assert await second.get("other") == {}
