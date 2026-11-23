import json
import re
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP

from .store import Store

IDENTITY = ("bu", "countryCode", "phoneNumber", "customerName")
FAQ: list[dict[str, Any]] = [
    {
        "question": "洗衣机如何清洁过滤器",
        "answer": "这是模拟售后资料：请先断电，按照产品说明书取出过滤器，用清水冲洗并晾干后装回。",
        "answer_id": "DEMO-FAQ-001", "category": "Washing machine",
        "terms": ["洗衣", "过滤", "filter", "washing"], "confidence": 0.96,
    },
    {
        "question": "保修期是多久",
        "answer": "这是模拟保修政策：演示产品保修期为购买之日起一年。真实政策请查询实际售后渠道。",
        "answer_id": "DEMO-FAQ-002", "category": None,
        "terms": ["保修", "warranty"], "confidence": 0.95,
    },
    {
        "question": "空调如何清洁",
        "answer": "这是模拟售后资料：清洁前请断开电源，用柔软干布擦拭外壳；内部保养请联系专业人员。",
        "answer_id": "DEMO-FAQ-003", "category": "Air Conditioner",
        "terms": ["空调", "conditioner"], "confidence": 0.94,
    },
]


def encoded(**data: Any) -> str:
    return json.dumps({"mock": True, **data}, ensure_ascii=False)


def require_identity(state: dict[str, Any]) -> None:
    if any(not state.get(key) for key in IDENTITY):
        raise ValueError("customer_not_verified: complete checkCustomer and save customer details first")


def register(mcp: FastMCP, store: Store, service: str) -> None:
    if service == "faq":
        @mcp.tool(structured_output=False)
        async def faqSearch(query: str, kb_key: str, productCategory: list[str], contactId: str) -> str:
            """Search DEMO FAQ. No identity needed. Select one highest-confidence answer, never invent.
            Product questions require one clear category; policy questions use [].
            kb_key and contactId are supplied by the trusted application."""
            if not query.strip() or kb_key != "demo":
                raise ValueError("A query and the configured demo KB are required")
            if len(productCategory) > 1:
                raise ValueError("Ask one clarification question to resolve the product category")
            results = [
                {k: item[k] for k in ("question", "answer", "answer_id", "confidence")}
                for item in FAQ if any(term in query.lower() for term in item["terms"])
                and (item["category"] is None or item["category"] in productCategory)
            ]
            await store.mutate(contactId, lambda state: {**state, "last_faq_query": query})
            return encoded(results=results)

    elif service == "verification":
        @mcp.tool(structured_output=False)
        async def checkCustomer(contactId: str) -> str:
            """Check if all required DEMO customer details exist. Required before repair operations."""
            state = await store.get(contactId)
            found = all(state.get(key) for key in IDENTITY)
            return encoded(status="customer_found" if found else "customer_not_found")

        @mcp.tool(structured_output=False)
        async def setCustomerByPhoneAndName(
            contactId: str, bu: str, countryCode: str, phoneNumber: str, customerName: str,
        ) -> str:
            """Save DEMO customer-provided details automatically once all required fields are collected.
            Ask country code, phone, then full name one field per turn. Not OTP or identity authentication."""
            if not re.fullmatch(r"\d{1,4}", countryCode) or not re.fullmatch(r"\d{4,16}", phoneNumber):
                raise ValueError("Country code and phone must be digits of valid length")
            if not customerName.strip() or bu != "DEMO":
                raise ValueError("Customer name and configured demo BU required")
            fields = dict(zip(IDENTITY, (bu, countryCode, phoneNumber, customerName.strip()), strict=True))
            await store.mutate(contactId, lambda state: {**state, **fields})
            return encoded(status="success", notice="Customer-provided demo details saved; not identity proof")

    elif service == "repair":
        @mcp.tool(structured_output=False)
        async def createRepair(
            contactId: str, addressRemark: str, description: str, zipCode: str, email: str,
            srType: str, srSubType: str, productModel: str = "", serialNumber: str = "",
            productCategory: str = "", brand: str = "",
        ) -> str:
            """Create a DEMO repair automatically ONLY when explicitly requested and customer fields saved.
            No extra verbal or page approval is required. Collect issue, zip and email; optional brand/category/
            model/serial/address can be skipped. srType=Inquiry, srSubType=Product. Never offer repair
            just because FAQ had no result."""
            if not description.strip() or not zipCode.strip() or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                raise ValueError("Issue, zip code and a valid email are required")
            fields = {
                "addressRemark": addressRemark, "description": description, "zipCode": zipCode, "email": email,
                "srType": srType or "Inquiry", "srSubType": srSubType or "Product",
                "productModel": productModel, "serialNumber": serialNumber,
                "productCategory": productCategory, "brand": brand,
            }
            # Identical requests are idempotent within this demo session, including after a lost response.
            fingerprint = json.dumps(fields, sort_keys=True)
            number = f"DEMO-{uuid.uuid5(uuid.NAMESPACE_URL, contactId + fingerprint).hex[:10].upper()}"

            def create(state: dict[str, Any]) -> dict[str, Any]:
                require_identity(state)
                orders = dict(state.get("orders", {}))
                orders.setdefault(number, {**fields, "srNumber": number, "status": "Draft"})
                return {**state, "orders": orders}

            await store.mutate(contactId, create)
            return encoded(status="success", srNumber=number)

        @mcp.tool(structured_output=False)
        async def listRepairs(contactId: str) -> str:
            """List this DEMO customer's simulated repair orders. Requires saved customer fields."""
            state = await store.get(contactId)
            require_identity(state)
            orders = list(state.get("orders", {}).values())
            return encoded(status="success", count=len(orders), repairs=orders)

        @mcp.tool(structured_output=False)
        async def getRepairDetail(contactId: str, srNumber: str) -> str:
            """Get one DEMO order's details. Requires saved customer fields."""
            state = await store.get(contactId)
            require_identity(state)
            detail = state.get("orders", {}).get(srNumber)
            if not detail:
                return encoded(status="failed", message="repair_not_found")
            return encoded(status="success", detail=detail)

        @mcp.tool(structured_output=False)
        async def cancelRepair(contactId: str, srNumber: str, cancelReason: str) -> str:
            """Cancel one DEMO order on explicit request with order number and reason.
            No extra verbal confirmation or page click is required."""
            if not cancelReason.strip():
                raise ValueError("Cancellation reason required")

            def cancel(state: dict[str, Any]) -> dict[str, Any]:
                require_identity(state)
                orders = dict(state.get("orders", {}))
                if srNumber not in orders:
                    raise ValueError("repair_not_found")
                orders[srNumber] = {**orders[srNumber], "status": "Cancelled", "cancelReason": cancelReason}
                return {**state, "orders": orders}

            await store.mutate(contactId, cancel)
            return encoded(status="success", srNumber=srNumber)
    else:
        raise ValueError(f"Unknown MCP service {service!r}")
