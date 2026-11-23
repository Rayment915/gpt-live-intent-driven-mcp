import asyncio
import json
from collections.abc import Callable
from typing import Any

from jsonschema import validate

from .azure import Azure
from .config import Settings
from .protocol import Call
from .remote import TOOL_SERVER, Remote
from .trace import Trace

REPAIR = {"checkCustomer", "setCustomerByPhoneAndName"}
ALLOWED = {
    "faq": {"faqSearch"},
    "repair_create": REPAIR | {"createRepair"},
    "repair_list": REPAIR | {"listRepairs"},
    "repair_detail": REPAIR | {"getRepairDetail"},
    "repair_cancel": REPAIR | {"cancelRepair"},
    "customer_verification": REPAIR,
    "clarify": set(),
    "out_of_scope": set(),
}
CLASSIFY_TOOL = {
    "type": "function",
    "name": "classify_intent",
    "description": "MANDATORY first tool for every new customer business request. Classify before any MCP call.",
    "parameters": {
        "type": "object",
        "properties": {"request": {"type": "string"}},
        "required": ["request"],
        "additionalProperties": False,
    },
}
RESPONSES_INSTRUCTIONS = """You are the backend of a Chinese real-time DEMO after-sales assistant.
All business results are simulated; Live, Decision and MCP are real.
For EACH new customer request call classify_intent FIRST, alone in a batch. Await its result before
calling any allowed business tool. Never classify and act in parallel. On clarification or error,
ask a short question or explain the failure; do not bypass the gate or make up business facts.
Use current conversation and active workflow to resolve short answers during field collection.
For FAQ use kb_key=demo and contactId=application; categories are Washing machine, Air Conditioner,
Refrigerator, or [] for policy. Ask ONE category clarification when ambiguous. Never request identity
for FAQ. Select ONLY the highest confidence relevant FAQ answer. If none, say no answer found;
never propose a repair yourself.
Repair ONLY on explicit customer request. First checkCustomer; if missing, collect country code,
phone, full name one field per turn, then call setCustomerByPhoneAndName (bu=DEMO).
The current Mock business automatically allows customer detail saves and requested repair writes
after Python validates the intent, arguments and required customer fields. Do NOT ask for extra
verbal confirmation or a page click, and do NOT wait for an approval card. Once the required fields
are collected, call the allowed tool immediately and wait for its actual result before claiming success.
createRepair requires issue, zipCode,email. Optional brand/category/model/serial/address can be
skipped. Ask missing fields ONE per turn; srType=Inquiry, srSubType=Product. Never invent them.
For list/detail/cancel do not invent order numbers; use existing results or ask.
contactId and trusted business context are injected by Python. Never reveal private customer fields
unnecessarily. Return a short Chinese result for Live to speak. Do not speak JSON or private keys."""
LIVE_INSTRUCTIONS = """你是实时语音演示客服，请使用简洁自然的中文，允许插话。
所有售后业务均为模拟，不连接真实售后。涉及产品知识、客户资料或报修时必须委派后台，
等待实际工具结果后回答，不要凭记忆编造，不要自己提出创建报修。
收齐客户资料和业务必需字段后，后台自动执行允许的模拟操作；不需要额外口头确认或页面点击。
等待后台真实结果再说明操作完成，不要把自动允许执行说成已经执行成功。
第一句说：你好，这里是模拟售后服务，有什么可以帮你？"""


class Bridge:
    def __init__(
        self,
        config: Settings,
        azure: Azure,
        remote: Remote,
        trace: Trace,
        contact: str,
        emit: Callable[[dict[str, Any]], None],
    ):
        self.config, self.azure, self.remote, self.trace = config, azure, remote, trace
        self.contact, self.emit = contact, emit
        self.routes: dict[str, dict[str, Any]] = {}
        self.tool_definitions: dict[str, dict[str, dict[str, Any]]] = {}
        self.transcripts: list[str] = []
        self.workflow: str | None = None
        self.checked: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    async def execute(self, call: Call, parent: str) -> dict[str, Any]:
        args = json.loads(call.arguments)
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be an object")
        # Serialize business steps per delegation; separate tasks can still progress independently.
        async with self._locks.setdefault(call.delegation_id, asyncio.Lock()):
            if call.name == "classify_intent":
                validate(args, CLASSIFY_TOOL["parameters"])
                with self.trace.span(self.config.decision_deployment, "decision", parent):
                    route = await self.azure.decision(
                        {
                            "request": args["request"],
                            "recent_transcript": "".join(self.transcripts)[-4000:],
                            "active_workflow": self.workflow,
                        }
                    )
                    allowed = (
                        sorted(ALLOWED[route["intent"]])
                        if (route["confidence"] >= self.config.intent_threshold)
                        else []
                    )
                    route["allowed_tools"] = allowed
                    self.routes[call.delegation_id] = route
                    self.emit({"type": "studio.route", "delegation_id": call.delegation_id, **route})
                    if route["intent"].startswith("repair_") and allowed:
                        self.workflow = route["intent"]
                    elif route["intent"] in ("faq", "out_of_scope"):
                        self.workflow = None
                    return {**route, "needs_clarification": not allowed}
            current_route = self.routes.get(call.delegation_id)
            if not current_route or call.name not in current_route["allowed_tools"]:
                raise ValueError("Classify this request first; the tool is not allowed by the Decision route")
            definitions = self.tool_definitions.get(call.delegation_id, self.remote.tools)
            if call.name not in TOOL_SERVER or call.name not in definitions:
                raise ValueError("Unknown or undiscovered MCP tool")
            args["contactId"] = self.contact
            if call.name == "faqSearch":
                args["kb_key"] = "demo"
            if call.name == "setCustomerByPhoneAndName":
                args["bu"] = "DEMO"
            validate(args, definitions[call.name]["parameters"])
            if call.name in {"createRepair", "cancelRepair", "listRepairs", "getRepairDetail"}:
                if call.delegation_id not in self.checked:
                    check = await self.remote.call(
                        "checkCustomer", {"contactId": self.contact}, self.trace, parent
                    )
                    if check["status"] != "customer_found":
                        raise ValueError("customer_not_verified: collect and save customer details first")
                    self.checked.add(call.delegation_id)
            if call.name in {"createRepair", "cancelRepair", "setCustomerByPhoneAndName"}:
                self.emit(
                    {
                        "type": "studio.tool.auto_approved",
                        "call_id": call.call_id,
                        "delegation_id": call.delegation_id,
                        "name": call.name,
                        "mode": "auto_mock",
                    }
                )
            result = await self.remote.call(call.name, args, self.trace, parent)
            if call.name == "setCustomerByPhoneAndName":
                self.checked.add(call.delegation_id)
            if call.name in {"createRepair", "cancelRepair", "listRepairs", "getRepairDetail"}:
                self.workflow = None
            return result
