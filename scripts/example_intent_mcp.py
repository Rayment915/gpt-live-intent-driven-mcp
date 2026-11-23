"""Minimal Decision -> authorized FAQ MCP example, without a Live voice session."""

import argparse
import asyncio
import json
import uuid
from typing import Any

from jsonschema import validate

from gpt_live_python.azure import Azure
from gpt_live_python.bridge import ALLOWED
from gpt_live_python.config import settings
from gpt_live_python.remote import Remote
from gpt_live_python.trace import Trace


async def route_and_call(
    text: str, contact_id: str, azure: Azure, remote: Remote, trace: Trace,
) -> dict[str, Any]:
    with trace.span("Decision -> FAQ MCP", "workflow") as parent:
        with trace.span("ms-d-1 intent classification", "decision", parent):
            route = await azure.decision({
                "request": text, "recent_transcript": "", "active_workflow": None,
            })
        print(f"Intent={route['intent']}; confidence={route['confidence']:.3f}")

        if route["confidence"] < azure.config.intent_threshold:
            raise ValueError("Intent is uncertain; clarify with the user before calling MCP")
        if route["intent"] != "faq" or "faqSearch" not in ALLOWED[route["intent"]]:
            raise ValueError("This read-only sample supports FAQ only; other intents need their own workflow")

        definitions = await remote.tools_for(["faqSearch"], trace, parent)
        # For this narrow example the query is unchanged and the two supported categories are explicit.
        # In the full app, gpt-6-luna extracts these arguments after receiving the Decision result.
        category = "Washing machine" if "洗衣机" in text else "Air Conditioner" if "空调" in text else None
        if "冰箱" in text or ("洗衣机" in text and "空调" in text):
            raise ValueError("This example cannot resolve the product category; clarify before execution")
        arguments = {
            "query": text, "kb_key": "demo", "productCategory": [category] if category else [],
            "contactId": contact_id,
        }
        validate(arguments, definitions[0]["parameters"])
        return await remote.call("faqSearch", arguments, trace, parent)


async def main(text: str) -> None:
    config = settings()
    azure, remote = Azure(config), Remote(config)
    contact_id = "example-" + uuid.uuid4().hex
    trace = Trace(lambda _: None)
    result = None
    try:
        result = await route_and_call(text, contact_id, azure, remote, trace)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await azure.close()
        if result is not None:
            await remote.cleanup(contact_id)
        await remote.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="?", default="请查询洗衣机过滤器怎么清洗")
    args = parser.parse_args()
    asyncio.run(main(args.text))
