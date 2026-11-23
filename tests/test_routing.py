import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from jsonschema.exceptions import ValidationError
from mcp.server.fastmcp import FastMCP

from gpt_live_python.azure import INTENTS, Azure, parse_decision
from gpt_live_python.bridge import Bridge
from gpt_live_python.business import register
from gpt_live_python.config import Settings
from gpt_live_python.protocol import Call
from gpt_live_python.remote import Remote
from gpt_live_python.store import SQLiteStore
from gpt_live_python.trace import Trace
from scripts.example_intent_mcp import route_and_call


def route(confidence=0.9):
    return {"intent": "faq", "confidence": confidence, "probabilities": {"faq": 0.95}}


def test_actual_decision_contract():
    probabilities = dict.fromkeys(INTENTS, 0)
    probabilities["faq"] = 1
    assert parse_decision({"answers": {"intent": {
        "choice": "faq", "confidence": 0.91, "probabilities": probabilities, "type": "choice",
    }}})["confidence"] == 0.91
    with pytest.raises(ValueError):
        parse_decision({"answers": {"intent": {"type": "choice", "choice": "wrong"}}})


@pytest.mark.parametrize("confidence,allowed", [(0.699, False), (0.70, True)])
async def test_gate_threshold_and_no_business_before_classification(confidence, allowed):
    cfg = Settings(_env_file=None)
    azure = Azure(cfg)
    remote = Remote(cfg)
    remote.tools = {"faqSearch": {"parameters": {"type": "object"}}}
    remote.call = AsyncMock(return_value={"mock": True, "results": []})
    azure.decision = AsyncMock(return_value=route(confidence))
    emit = []
    bridge = Bridge(cfg, azure, remote, Trace(emit.append), "trusted", emit.append)
    business = Call("b", "d", "batch", "faqSearch", '{"contactId":"spoofed"}')
    try:
        with pytest.raises(ValueError, match="Classify"):
            await bridge.execute(business, "parent")
        await bridge.execute(Call("c", "d", "first", "classify_intent", '{"request":"保修"}'), "parent")
        if allowed:
            await bridge.execute(business, "parent")
            assert remote.call.await_args.args[1] == {"contactId": "trusted", "kb_key": "demo"}
        else:
            with pytest.raises(ValueError):
                await bridge.execute(business, "parent")
            remote.call.assert_not_awaited()
    finally:
        await azure.close()


@pytest.fixture
async def business_remote(tmp_path):
    cfg = Settings(_env_file=None)
    remote = Remote(cfg)
    server = FastMCP("routing schemas")
    store = SQLiteStore(str(tmp_path / "routing.sqlite"))
    for service in ("faq", "verification", "repair"):
        register(server, store, service)
    remote.tools = {t.name: {"parameters": t.inputSchema} for t in await server.list_tools()}
    return remote


@pytest.mark.parametrize("name,args", [
    ("setCustomerByPhoneAndName", {
        "contactId": "spoof", "bu": "evil", "countryCode": "86",
        "phoneNumber": "0000000000", "customerName": "测试客户",
    }),
    ("createRepair", {
        "contactId": "spoof", "addressRemark": "", "description": "模拟不能启动",
        "zipCode": "000000", "email": "test@example.test", "srType": "Inquiry", "srSubType": "Product",
    }),
    ("cancelRepair", {"contactId": "spoof", "srNumber": "DEMO-TEST", "cancelReason": "测试结束"}),
])
async def test_mock_writes_auto_execute_after_validation(business_remote, name, args):
    cfg = Settings(_env_file=None)
    azure = Azure(cfg)
    remote = business_remote
    remote.call = AsyncMock(return_value={"mock": True, "status": "success"})
    if name != "setCustomerByPhoneAndName":
        remote.call.side_effect = [{"status": "customer_found"}, {"mock": True, "status": "success"}]
    events = []
    bridge = Bridge(cfg, azure, remote, Trace(events.append), "trusted", events.append)
    bridge.routes["d"] = {"allowed_tools": [name]}
    try:
        result = await asyncio.wait_for(
            bridge.execute(Call("write", "d", "b", name, json.dumps(args)), "parent"), 1,
        )
        assert result["status"] == "success"
        assert remote.call.await_args.args[0] == name
        assert remote.call.await_args.args[1]["contactId"] == "trusted"
        if name == "setCustomerByPhoneAndName":
            assert remote.call.await_args.args[1]["bu"] == "DEMO"
        assert [e for e in events if e["type"] == "studio.tool.auto_approved"] == [{
            "type": "studio.tool.auto_approved", "call_id": "write",
            "delegation_id": "d", "name": name, "mode": "auto_mock",
        }]
        assert not any(e["type"] == "studio.approval" for e in events)
    finally:
        await azure.close()


async def test_automatic_execution_preserves_schema_and_customer_gates(business_remote):
    cfg = Settings(_env_file=None)
    azure = Azure(cfg)
    remote = business_remote
    remote.call = AsyncMock(return_value={"status": "customer_not_found"})
    events = []
    bridge = Bridge(cfg, azure, remote, Trace(events.append), "trusted", events.append)
    bridge.routes["d"] = {"allowed_tools": ["createRepair", "cancelRepair"]}
    fields = {
        "addressRemark": "", "description": "模拟不能启动", "zipCode": "000000",
        "email": "test@example.test", "srType": "Inquiry", "srSubType": "Product",
    }
    try:
        with pytest.raises(ValidationError):
            await bridge.execute(Call("invalid", "d", "b", "createRepair", "{}"), "parent")
        with pytest.raises(ValidationError):
            await bridge.execute(Call("invalid", "d", "b", "cancelRepair", '{"srNumber":"DEMO-TEST"}'), "parent")
        remote.call.assert_not_awaited()
        with pytest.raises(ValueError, match="customer_not_verified"):
            await bridge.execute(Call("unverified", "d", "b", "createRepair", json.dumps(fields)), "parent")
        assert [c.args[0] for c in remote.call.await_args_list] == ["checkCustomer"]
        assert not any(e["type"] == "studio.tool.auto_approved" for e in events)
    finally:
        await azure.close()


async def test_auto_allowed_mcp_failure_is_not_reported_as_success(business_remote):
    cfg = Settings(_env_file=None)
    azure = Azure(cfg)
    remote = business_remote
    remote.call = AsyncMock(side_effect=[{"status": "customer_found"}, ValueError("MCP failed")])
    events = []
    bridge = Bridge(cfg, azure, remote, Trace(events.append), "trusted", events.append)
    bridge.routes["d"] = {"allowed_tools": ["createRepair"]}
    fields = {
        "addressRemark": "", "description": "模拟不能启动", "zipCode": "000000",
        "email": "test@example.test", "srType": "Inquiry", "srSubType": "Product",
    }
    try:
        with pytest.raises(ValueError, match="MCP failed"):
            await bridge.execute(Call("write", "d", "b", "createRepair", json.dumps(fields)), "parent")
        assert [c.args[0] for c in remote.call.await_args_list] == ["checkCustomer", "createRepair"]
        assert any(e["type"] == "studio.tool.auto_approved" for e in events)
    finally:
        await azure.close()


@pytest.mark.parametrize("confidence", [0.69, 0.9])
async def test_decision_mcp_example_classifies_before_any_mcp_operation(confidence):
    sequence = []
    cfg = Settings(_env_file=None)
    azure = Azure(cfg)
    remote = Remote(cfg)

    async def decision(_):
        sequence.append("decision")
        return route(confidence)

    async def discover(names, trace, parent):
        sequence.append("discover")
        assert names == ["faqSearch"]
        remote.tools = {"faqSearch": {"parameters": {"type": "object"}}}
        return list(remote.tools.values())

    async def call(name, args, trace, parent):
        sequence.append("call")
        assert name == "faqSearch"
        assert args["contactId"] == "trusted"
        assert args["productCategory"] == ["Washing machine"]
        return {"mock": True, "results": []}

    azure.decision = decision
    remote.tools_for = discover
    remote.call = call
    try:
        if confidence < cfg.intent_threshold:
            with pytest.raises(ValueError, match="uncertain"):
                await route_and_call("洗衣机过滤器如何清洗", "trusted", azure, remote, Trace(lambda _: None))
            assert sequence == ["decision"]
        else:
            result = await route_and_call("洗衣机过滤器如何清洗", "trusted", azure, remote, Trace(lambda _: None))
            assert result["mock"] is True
            assert sequence == ["decision", "discover", "call"]
    finally:
        await azure.close()
