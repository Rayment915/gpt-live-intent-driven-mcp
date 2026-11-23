import asyncio
from typing import Any

import httpx

from .auth import credential
from .config import Settings

INTENTS = {
    "faq": "Product usage, troubleshooting, warranty or after-sales questions; not asking to create repair",
    "repair_create": "Explicit request to create a repair order, or continuing fields for an active creation",
    "repair_list": "List or check existing repair orders",
    "repair_detail": "Read details or progress of a particular repair order",
    "repair_cancel": "Explicitly cancel an existing repair order",
    "customer_verification": "Provide or confirm customer details for an active repair workflow",
    "clarify": "Insufficient information to determine the requested business action",
    "out_of_scope": "Unrelated or prohibited request",
}


class Azure:
    def __init__(self, config: Settings):
        self.config = config
        self.credential = credential(config)
        self.http = httpx.AsyncClient(timeout=30, follow_redirects=False)

    async def headers(self) -> dict[str, str]:
        key = self.config.azure_api_key.get_secret_value()
        if key:
            return {"api-key": key}
        token = await asyncio.to_thread(self.credential.get_token, "https://cognitiveservices.azure.com/.default")
        return {"Authorization": f"Bearer {token.token}"}

    async def decision(self, state: dict[str, Any]) -> dict[str, Any]:
        if not self.config.azure_endpoint:
            raise ValueError("AZURE_ENDPOINT has not been configured")
        response = await self.http.post(
            self.config.azure_endpoint + "/providers/microsoft/v1/systemone",
            headers=await self.headers(),
            json={
                "model": self.config.decision_deployment, "state": state,
                "questions": {"intent": {
                    "type": "choice",
                    "instructions": "Classify the current customer request, considering the active workflow.",
                    "criteria": INTENTS,
                }},
            },
        )
        response.raise_for_status()
        return parse_decision(response.json())

    async def close(self) -> None:
        await self.http.aclose()
        if hasattr(self.credential, "close"):
            self.credential.close()


def parse_decision(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Decision returned an invalid object")
    answer = payload.get("answers", {}).get("intent", {})
    choice = answer.get("choice")
    probabilities = answer.get("probabilities")
    if answer.get("type") != "choice" or choice not in INTENTS or not isinstance(probabilities, dict):
        raise ValueError("Decision returned an invalid intent or probability distribution")
    if set(probabilities) != set(INTENTS) or any(
        type(p) not in (int, float) or not 0 <= p <= 1 for p in probabilities.values()
    ):
        raise ValueError("Decision returned malformed intent probabilities")
    confidence = answer.get("confidence")
    if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
        raise ValueError("Decision returned invalid confidence")
    return {
        "intent": choice, "confidence": confidence,
        "top_probability": probabilities[choice], "probabilities": probabilities,
    }
