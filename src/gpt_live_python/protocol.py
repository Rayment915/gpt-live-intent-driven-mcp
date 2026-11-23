import base64
import binascii
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .privacy import reject_secrets


class Voice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice: str = Field(default="marin", min_length=1, max_length=80)


class Audio(BaseModel):
    model_config = ConfigDict(extra="forbid")
    output: Voice = Field(default_factory=Voice)


class Delegation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    responses: dict[str, Any] | None = None

    @model_validator(mode="after")
    def valid(self) -> "Delegation":
        if self.type not in ("client", "responses"):
            raise ValueError("delegation.type must be client or responses")
        if self.type == "client" and self.responses is not None:
            raise ValueError("Client delegation has no responses configuration")
        if self.type == "responses":
            r = self.responses
            if not r or not isinstance(r.get("model"), str) or not r["model"].strip():
                raise ValueError("Responses deployment is required")
            if "service_tier" in r and r["service_tier"] not in ("auto", "default", "flex", "priority"):
                raise ValueError("Invalid service_tier")
            if "parallel_tool_calls" in r and not isinstance(r["parallel_tool_calls"], bool):
                raise ValueError("parallel_tool_calls must be boolean")
            if "max_output_tokens" in r and (
                type(r["max_output_tokens"]) is not int or r["max_output_tokens"] < 1
            ):
                raise ValueError("max_output_tokens must be a positive integer")
            if "tools" in r and (
                not isinstance(r["tools"], list)
                or any(not isinstance(t, dict) or not isinstance(t.get("type"), str) for t in r["tools"])
            ):
                raise ValueError("tools must be an array of tool objects")
        return self


class SessionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1)
    instructions: str | None = None
    audio: Audio | None = None
    delegation: Delegation | None = None

    @model_validator(mode="before")
    @classmethod
    def no_credentials(cls, value: Any) -> Any:
        reject_secrets(value)
        return value

    def wire(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True, exclude_none=False)

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "SessionConfig":
        public = {key: resource[key] for key in ("model", "instructions", "audio", "delegation")
                  if key in resource}
        if isinstance(public.get("audio"), dict):
            # The service adds normalized WS format metadata; it is not an accepted Studio startup field.
            public["audio"] = {key: value for key, value in public["audio"].items() if key != "format"}
        return cls.model_validate(public)


COMMANDS = {
    "session.update", "session.input_audio.append", "session.input_audio.mute",
    "session.input_audio.unmute", "session.instructions.append", "session.thinking.append",
    "session.commentary.append", "response.item.create", "response.create", "session.close",
}


def validate_command(event: dict[str, Any], config: SessionConfig, transport: str,
                     experimental: bool = False) -> None:
    reject_secrets(event)
    kind = event.get("type")
    if not isinstance(kind, str) or not kind or kind == "session.start" or (
        kind not in COMMANDS and not experimental
    ):
        raise ValueError("Unknown or forbidden event type")
    if kind == "session.update":
        patch = event.get("session")
        if not isinstance(patch, dict) or set(patch) != {"delegation"}:
            raise ValueError("Only a complete Responses delegation may be updated")
        new = Delegation.model_validate(patch["delegation"])
        if not config.delegation or config.delegation.type != "responses" or new.type != "responses":
            raise ValueError("Changing delegation mode requires a new session")
    if kind == "session.input_audio.append":
        if transport != "websocket":
            raise ValueError("WebRTC uses the negotiated media track")
        try:
            audio = base64.b64decode(event.get("audio", ""), validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise ValueError("Invalid base64 audio") from exc
        if not audio or len(audio) % 2 or len(audio) > 96_000:
            raise ValueError("Expected nonempty, even-sized PCM16 audio (maximum 2 seconds)")
    if kind in {"session.instructions.append", "session.thinking.append", "session.commentary.append"}:
        if not isinstance(event.get("content"), str) or not event["content"].strip():
            raise ValueError("content is required")
        if "delegation_id" not in event or (
            event["delegation_id"] is not None and not isinstance(event["delegation_id"], str)
        ):
            raise ValueError("delegation_id must be an ID or null")
    if kind and kind.startswith("response."):
        if not config.delegation or config.delegation.type != "responses":
            raise ValueError("This event requires Responses delegation")


@dataclass
class Call:
    call_id: str
    delegation_id: str
    batch_id: str
    name: str
    arguments: str
    state: str = "pending"


@dataclass
class Batch:
    delegation_id: str
    sealed: bool = False
    continued: bool = False
    calls: list[str] = field(default_factory=list)
    failed: bool = False


class Ledger:
    def __init__(self) -> None:
        self.calls: dict[str, Call] = {}
        self.batches: dict[str, Batch] = {}
        self.active: dict[str, str] = {}

    def ingest(self, envelope: dict[str, Any]) -> list[Call]:
        if envelope.get("type") != "response.event":
            return []
        d = envelope.get("delegation_id")
        e = envelope.get("event")
        if not isinstance(d, str) or not isinstance(e, dict):
            raise ValueError("Malformed response.event envelope")
        kind = e.get("type")
        if kind == "response.created":
            batch_id = f"{d}:{e.get('response', {}).get('id', len(self.batches))}"
            if batch_id not in self.batches:
                self.batches[batch_id] = Batch(d)
                self.active[d] = batch_id
        previous = self.active.get(d)
        if kind == "response.in_progress" and previous and self.batches[previous].continued:
            self.active[d] = f"{d}:continuation-{len(self.batches)}"
        batch_id = self.active.setdefault(d, f"{d}:initial")
        batch = self.batches.setdefault(batch_id, Batch(d))
        created = []
        item = e.get("item", {})
        if kind == "response.output_item.done" and item.get("type") == "function_call":
            if not all(isinstance(item.get(k), str) for k in ("call_id", "name", "arguments")):
                raise ValueError("Malformed function_call")
            cid = item["call_id"]
            if cid not in self.calls:
                call = Call(cid, d, batch_id, item["name"], item["arguments"])
                self.calls[cid] = call
                batch.calls.append(cid)
                created.append(call)
        if kind == "response.completed":
            batch.sealed = True
        if kind in ("response.failed", "response.incomplete"):
            batch.failed = True
        return created

    def ready(self) -> list[str]:
        return [
            key for key, b in self.batches.items()
            if b.sealed and not b.failed and not b.continued and b.calls
            and all(self.calls[cid].state == "submitted" for cid in b.calls)
        ]
