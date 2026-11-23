import asyncio
import copy
import json
import logging
import uuid
from collections import deque
from typing import Any

import httpx
from jsonschema.exceptions import SchemaError, ValidationError
from mcp.shared.exceptions import McpError
from pydantic import ValidationError as ModelValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .azure import Azure
from .bridge import CLASSIFY_TOOL, Bridge
from .config import Settings
from .privacy import redact
from .protocol import Call, Ledger, SessionConfig, validate_command
from .remote import Remote
from .trace import Trace

log = logging.getLogger("gpt_live.session")


class Live:
    def __init__(
        self,
        owner: str,
        config: SessionConfig,
        transport: str,
        settings: Settings,
        azure: Azure,
        remote: Remote,
    ):
        self.id, self.owner = uuid.uuid4().hex, owner
        self.config, self.transport, self.settings = config, transport, settings
        self.azure, self.remote = azure, remote
        self.socket: ClientConnection | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=800)
        self.clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self.seq = 0
        self.ledger = Ledger()
        self.trace = Trace(self.publish)
        self.bridge = Bridge(settings, azure, remote, self.trace, self.id, self.publish)
        self.delegations: dict[str, str] = {}
        self.response_spans: dict[str, str] = {}
        self.tasks: set[asyncio.Task[Any]] = set()
        self.reader: asyncio.Task[None] | None = None
        self.deadline: asyncio.Task[None] | None = None
        self.attach_deadline: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()
        self.continue_lock = asyncio.Lock()
        self.status = "starting"
        self.final_usage = False
        self.managed = bool(config.delegation and config.delegation.type == "responses")
        self.base_responses = copy.deepcopy(config.delegation.responses) if config.delegation else {}
        self.visible_tools = [copy.deepcopy(CLASSIFY_TOOL)]
        self.tool_choice = "required"
        self.workflow_phase = "classification"
        self.workflow_lock = asyncio.Lock()
        self.turn_condition = asyncio.Condition()
        self.active_delegation: str | None = None
        self.waiting_delegations: deque[str] = deque()
        self.finished_delegations: set[str] = set()
        self.request_versions: dict[str, int] = {}
        self.authorized_version: int | None = None
        self.config_version = 0
        self.pending_update: tuple[dict[str, Any], asyncio.Future[None], str] | None = None
        self.batch_sealed: dict[str, asyncio.Event] = {}
        self.finishing: set[str] = set()
        if self.managed:
            self.config = SessionConfig.model_validate(
                {
                    **config.wire(),
                    "delegation": {
                        "type": "responses",
                        "responses": self.phase_responses(
                            self.visible_tools,
                            self.tool_choice,
                        ),
                    },
                }
            )

    def phase_responses(self, tools: list[dict[str, Any]], choice: str) -> dict[str, Any]:
        return {
            **(self.base_responses or {}),
            "tools": copy.deepcopy(tools),
            "tool_choice": choice,
            "parallel_tool_calls": False
            if choice == "required"
            else (self.base_responses or {}).get("parallel_tool_calls", True),
        }

    def workflow_event(self) -> None:
        if self.managed:
            self.publish(
                {
                    "type": "studio.workflow",
                    "phase": self.workflow_phase,
                    "active_delegation": self.active_delegation,
                    "queued": list(self.waiting_delegations),
                    "visible_tools": [t["name"] for t in self.visible_tools],
                    "tool_choice": self.tool_choice,
                    "config_version": self.config_version,
                    "catalogs": self.remote.catalog_status(),
                }
            )

    def set_phase(self, phase: str) -> None:
        self.workflow_phase = phase
        self.workflow_event()

    def ensure_turn(self, delegation: str) -> None:
        if delegation in self.finished_delegations:
            return
        if self.active_delegation == delegation or delegation in self.waiting_delegations:
            return
        if self.active_delegation is None:
            self.active_delegation = delegation
        else:
            if len(self.waiting_delegations) >= 16:
                raise ValueError("Too many queued delegations; end the session and retry")
            self.waiting_delegations.append(delegation)
        self.workflow_event()

    async def wait_turn(self, delegation: str) -> None:
        async with self.turn_condition:
            await self.turn_condition.wait_for(
                lambda: self.active_delegation == delegation or delegation in self.finished_delegations
                or self.status != "ready",
            )
        if self.status != "ready":
            raise ValueError("Session ended while delegation was queued")
        if delegation in self.finished_delegations:
            raise ValueError("Delegation has already ended; classification authorization expired")

    @staticmethod
    def tool_signature(tools: list[dict[str, Any]]) -> str:
        return json.dumps(
            [{key: tool.get(key) for key in ("type", "name", "description", "parameters")} for tool in tools],
            sort_keys=True,
        )

    def observe_update(self, event: dict[str, Any]) -> None:
        if not self.pending_update:
            return
        target, future, identifier = self.pending_update
        if future.done():
            return
        if event["type"] == "session.updated":
            actual = event.get("session", {}).get("delegation", {}).get("responses", {})
            if (
                actual.get("model") == target["model"]
                and actual.get("tool_choice") == target["tool_choice"]
                and self.tool_signature(actual.get("tools", [])) == self.tool_signature(target["tools"])
            ):
                future.set_result(None)
        elif event["type"] == "error":
            error = event.get("error", {})
            if (
                error.get("client_event_id") == identifier
                or error.get("code") == "responses_validation_failed"
            ):
                future.set_exception(
                    ValueError("Responses tool configuration rejected: " + error.get("message", ""))
                )

    async def apply_tools(self, tools: list[dict[str, Any]], choice: str) -> None:
        responses = self.phase_responses(tools, choice)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        identifier = uuid.uuid4().hex
        self.pending_update = (responses, future, identifier)
        try:
            with self.trace.span(
                "session.update tools",
                "configuration",
                self.delegations.get(self.active_delegation or ""),
                visible_tools=[t["name"] for t in tools],
                tool_choice=choice,
            ):
                await self.send(
                    {
                        "type": "session.update",
                        "event_id": identifier,
                        "session": {"delegation": {"type": "responses", "responses": responses}},
                    }
                )
                await asyncio.wait_for(future, timeout=15)
            self.visible_tools = copy.deepcopy(tools)
            self.tool_choice = choice
            self.config_version += 1
            self.workflow_event()
        finally:
            self.pending_update = None
            if not future.done():
                future.cancel()

    async def classify(self, call: Call, parent: str) -> dict[str, Any]:
        delegation = call.delegation_id
        self.authorized_version = None
        self.bridge.routes.pop(delegation, None)
        self.bridge.tool_definitions.pop(delegation, None)
        self.bridge.checked.discard(delegation)
        version = self.request_versions.get(delegation, 0) + 1
        self.request_versions[delegation] = version
        self.set_phase("classifying")
        result = await self.bridge.execute(call, parent)
        self.set_phase("discovering")
        definitions = (
            await self.remote.tools_for(result["allowed_tools"], self.trace, parent)
            if result["allowed_tools"] else []
        )
        self.bridge.tool_definitions[delegation] = {t["name"]: t for t in definitions}
        self.set_phase("updating")
        await self.apply_tools([CLASSIFY_TOOL, *definitions], "auto" if definitions else "none")
        self.authorized_version = version if definitions else None
        self.bridge.routes[delegation]["version"] = version
        self.set_phase("executing" if definitions else "clarifying")
        return result

    async def finish_turn(self, delegation: str, batch_id: str) -> None:
        async with self.workflow_lock:
            try:
                if self.ledger.active.get(delegation) != batch_id:
                    return
                if self.active_delegation != delegation:
                    if delegation in self.waiting_delegations:
                        self.waiting_delegations.remove(delegation)
                        self.finished_delegations.add(delegation)
                        self.publish(
                            {
                                "type": "studio.error",
                                "message": "Queued delegation ended before classification; no business tools executed",
                            }
                        )
                        self.workflow_event()
                        async with self.turn_condition:
                            self.turn_condition.notify_all()
                    return
                self.authorized_version = None
                self.bridge.routes.pop(delegation, None)
                self.bridge.tool_definitions.pop(delegation, None)
                if self.status == "ready":
                    self.set_phase("restoring")
                    await self.apply_tools([CLASSIFY_TOOL], "required")
                    self.finished_delegations.add(delegation)
                    self.active_delegation = (
                        self.waiting_delegations.popleft() if self.waiting_delegations else None
                    )
                    self.set_phase("classification")
                    async with self.turn_condition:
                        self.turn_condition.notify_all()
            except (ValueError, TimeoutError, ConnectionClosed) as exc:
                self.publish(
                    {"type": "studio.error", "message": f"Cannot safely restore classification: {exc}"}
                )
                self.spawn(self.close())
            finally:
                self.finishing.discard(batch_id)

    def spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        def finished(done: asyncio.Task[Any]) -> None:
            self.tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                exc = done.exception()
                log.error("Background workflow failed: %s", type(exc).__name__)
                self.publish({"type": "studio.error", "message": f"Background workflow failed: {type(exc).__name__}"})
                if self.status == "ready":
                    self.spawn(self.close())
        task.add_done_callback(finished)

    def publish(self, event: dict[str, Any]) -> None:
        self.seq += 1
        frame = {**event, "_seq": self.seq}
        if event["type"] != "session.output_audio.delta":
            # The live transcript is needed by the user; raw tool results and arguments are not.
            safe = redact(frame)
            self.events.append(safe)
            frame = safe
        for queue in tuple(self.clients):
            if queue.full():
                # Keep the error explicit and disconnect the slow consumer, not the audio silently.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "studio.error", "message": "Event consumer too slow; reconnect"})
                queue.put_nowait({"type": "studio.disconnected", "final_usage_confirmed": False})
                self.clients.remove(queue)
            else:
                queue.put_nowait(frame)

    async def send(self, event: dict[str, Any]) -> None:
        if self.socket is None:
            raise ValueError("Upstream is not connected")
        async with self.lock:
            async with asyncio.timeout(10):
                await self.socket.send(
                    json.dumps({**event, "event_id": event.get("event_id", uuid.uuid4().hex)})
                )

    async def start(self, sdp: str | None = None) -> dict[str, Any]:
        startup = self.trace.start("Live startup", "startup")
        base = self.settings.azure_endpoint
        url = base.replace("https://", "wss://") + "/openai/v1/live/sessions"
        created: dict[str, Any] = {}
        remote_id: str | None = None
        try:
            with self.trace.span("Azure credentials", "startup", startup):
                headers = await self.azure.headers()
            if self.transport == "webrtc":
                if not sdp or not sdp.startswith("v=0") or len(sdp) > 128_000:
                    raise ValueError("Valid SDP offer required for WebRTC")
                with self.trace.span("Azure Live create", "startup", startup):
                    response = await self.azure.http.post(
                        base + "/openai/v1/live/sessions",
                        headers=headers,
                        json={"session": self.config.wire(), "transport": {"type": "webrtc", "sdp": sdp}},
                    )
                    response.raise_for_status()
                created = response.json()
                remote_id = created["session"]["id"]
                url += f"/{remote_id}/attach"
            with self.trace.span(
                "Live WebSocket" if self.transport == "websocket" else "Live sideband", "startup", startup
            ):
                self.socket = await connect(
                    url,
                    additional_headers=headers,
                    open_timeout=20,
                    max_size=2 * 1024 * 1024,
                    max_queue=32,
                    ping_interval=20,
                    ping_timeout=20,
                )
            if self.transport == "websocket":
                await self.send({"type": "session.start", "session": self.config.wire()})
                async with asyncio.timeout(30):
                    while True:
                        event = json.loads(await self.socket.recv())
                        if event["type"] == "error":
                            raise ValueError(event["error"].get("message", "Live session rejected"))
                        self.publish(event)
                        if event["type"] == "session.started":
                            created = {"session": event["session"]}
                            break
            else:
                self.publish({"type": "session.started", "session": created["session"]})
            self.status = "ready"
            self.reader = asyncio.create_task(self.read())
            self.deadline = asyncio.create_task(self.expire())
            self.attach_deadline = asyncio.create_task(self.expire_unattached())
            self.trace.finish(startup)
            self.workflow_event()
            return {
                "id": self.id,
                "session": created["session"],
                "sdp": created.get("transport", {}).get("sdp"),
            }
        except BaseException:
            self.trace.finish(startup, "failed")
            if self.socket:
                try:
                    await self.send({"type": "session.close"})
                except (ConnectionClosed, TimeoutError):
                    log.error("Startup cleanup could not send close for session %s", self.id)
                await self.socket.close()
            elif remote_id:
                try:
                    async with connect(
                        url,
                        additional_headers=headers,
                        open_timeout=10,
                    ) as cleanup:
                        await cleanup.send(json.dumps({"type": "session.close"}))
                        async with asyncio.timeout(12):
                            async for message in cleanup:
                                if json.loads(message).get("type") == "session.closed":
                                    break
                except (WebSocketException, OSError, TimeoutError, ValueError):
                    log.error("WebRTC session %s sideband cleanup failed; closure unconfirmed", remote_id)
            self.status = "closed"
            raise

    async def expire(self) -> None:
        await asyncio.sleep(self.settings.session_ttl_seconds)
        await self.close()

    async def expire_unattached(self) -> None:
        await asyncio.sleep(20)
        if not self.clients:
            await self.close()

    async def read(self) -> None:
        assert self.socket is not None
        try:
            async for message in self.socket:
                event = json.loads(message)
                kind = event.get("type")
                self.observe_update(event)
                if kind in ("session.started", "session.updated") and isinstance(event.get("session"), dict):
                    public = event["session"]
                    self.config = SessionConfig.from_resource(public)
                calls = self.ledger.ingest(event)
                if kind == "session.input_transcript.delta":
                    self.bridge.transcripts.append(event.get("delta", ""))
                    self.bridge.transcripts = self.bridge.transcripts[-3000:]
                if kind == "session.delegation.created":
                    d = event["delegation"]["id"]
                    if d not in self.delegations:
                        self.delegations[d] = self.trace.start(
                            "Responses delegation",
                            "delegation",
                            delegation_id=d,
                            model=self.settings.responses_deployment,
                            response_id=event["delegation"].get("response_id"),
                        )
                    if self.managed:
                        self.ensure_turn(d)
                if kind == "response.event":
                    nested = event["event"]
                    d = event["delegation_id"]
                    bid = self.ledger.active.get(d, f"{d}:initial")
                    if self.managed:
                        self.ensure_turn(d)
                    if nested["type"] in ("response.completed", "response.failed", "response.incomplete"):
                        self.batch_sealed.setdefault(bid, asyncio.Event()).set()
                    if nested["type"] in ("response.created", "response.in_progress"):
                        if bid not in self.response_spans:
                            self.response_spans[bid] = self.trace.start(
                                "Responses generation",
                                "response",
                                self.delegations.get(d),
                                batch_id=bid,
                                response_id=nested.get("response", {}).get("id"),
                            )
                    elif nested["type"] in ("response.completed", "response.failed", "response.incomplete"):
                        if bid in self.response_spans:
                            self.trace.finish(
                                self.response_spans[bid],
                                "completed" if (nested["type"] == "response.completed") else "failed",
                            )
                self.publish(event)
                for call in calls:
                    call.state = "running"
                    self.spawn(self.run_tool(call))
                if kind == "error" and event.get("error", {}).get("code") == "responses_validation_failed":
                    self.authorized_version = None
                    self.set_phase("failed")
                    self.spawn(self.close())
                if kind == "response.event":
                    await self.continue_ready()
                    nested = event["event"]
                    if nested["type"] in ("response.failed", "response.incomplete") and self.managed:
                        d = event["delegation_id"]
                        failed_batch = self.ledger.active.get(d)
                        if failed_batch and failed_batch not in self.finishing:
                            self.finishing.add(failed_batch)
                            self.spawn(self.finish_turn(d, failed_batch))
                    if nested["type"] in ("response.completed", "response.failed", "response.incomplete"):
                        d = event["delegation_id"]
                        has_tools = any(
                            c.delegation_id == d and c.state != "submitted"
                            for c in self.ledger.calls.values()
                        )
                        output = nested.get("response", {}).get("output", [])
                        active = self.ledger.active.get(d)
                        had_calls = bool(active and self.ledger.batches[active].calls)
                        if (
                            not has_tools
                            and not had_calls
                            and not any(i.get("type") == "function_call" for i in output)
                        ):
                            sid = self.delegations.get(d)
                            if sid:
                                self.trace.finish(
                                    sid, "completed" if nested["type"] == "response.completed" else "failed"
                                )
                            if self.managed and active and active not in self.finishing:
                                self.finishing.add(active)
                                self.spawn(self.finish_turn(d, active))
                if kind == "session.closed":
                    self.final_usage = True
                    break
        except (ConnectionClosed, httpx.HTTPError, ValueError, KeyError) as exc:
            log.error("Live stream failed: %s", type(exc).__name__)
            if isinstance(exc, ModelValidationError):
                log.error("Session resource validation: %s", exc.errors(include_input=False))
            self.publish({"type": "studio.error", "message": f"Live stream failed: {type(exc).__name__}"})
        finally:
            self.status = "closed"
            if self.pending_update and not self.pending_update[1].done():
                self.pending_update[1].cancel()
            async with self.turn_condition:
                self.turn_condition.notify_all()
            if self.deadline:
                self.deadline.cancel()
            if self.attach_deadline:
                self.attach_deadline.cancel()
            for task in tuple(self.tasks):
                task.cancel()
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            for key, span in list(self.trace.spans.items()):
                if span["status"] == "running":
                    self.trace.finish(key, "cancelled")
            await self.socket.close()
            self.publish({"type": "studio.disconnected", "final_usage_confirmed": self.final_usage})

    async def run_tool(self, call: Call) -> None:
        parent = self.delegations.get(call.delegation_id)
        sid = self.trace.start(call.name, "tool", parent, call_id=call.call_id)
        try:
            if self.managed:
                await self.batch_sealed.setdefault(call.batch_id, asyncio.Event()).wait()
                if self.ledger.batches[call.batch_id].failed:
                    call.state = "failed"
                    raise ValueError("Backend response failed; no business tools executed")
                await self.wait_turn(call.delegation_id)
        except asyncio.CancelledError:
            self.trace.finish(sid, "cancelled")
            raise
        except ValueError as exc:
            self.trace.finish(sid, "failed")
            self.publish({"type": "studio.error", "message": str(exc)})
            return
        async with self.workflow_lock:
            await self.execute_tool(call, parent, sid)

    async def execute_tool(self, call: Call, parent: str | None, sid: str) -> None:
        try:
            batch = self.ledger.batches[call.batch_id]
            if batch.failed:
                raise ValueError("Backend response failed; no business tools executed")
            if call.name != "classify_intent" and any(
                self.ledger.calls[cid].name == "classify_intent" for cid in batch.calls
            ):
                raise ValueError("Classification and business tools must use separate Responses batches")
            if self.managed and call.name == "classify_intent":
                if sum(self.ledger.calls[cid].name == "classify_intent" for cid in batch.calls) != 1:
                    raise ValueError("Classify only one request per Responses batch")
                result = await self.classify(call, sid)
            else:
                if self.managed and (
                    self.workflow_phase != "executing"
                    or self.active_delegation != call.delegation_id
                    or self.authorized_version != self.request_versions.get(call.delegation_id)
                ):
                    raise ValueError("Business tools require a confirmed tool phase for this request")
                result = await self.bridge.execute(call, sid)
        except asyncio.CancelledError:
            self.trace.finish(sid, "cancelled")
            raise
        except (
            ValueError,
            TypeError,
            ValidationError,
            SchemaError,
            httpx.HTTPError,
            TimeoutError,
            McpError,
            ExceptionGroup,
        ) as exc:
            # Tool errors are explicit tool results so the managed Responses loop can react.
            log.warning("Tool %s failed: %s", call.name, type(exc).__name__)
            result = {"ok": False, "error": str(exc)[:600], "error_type": type(exc).__name__}
            self.trace.finish(sid, "failed", error_type=type(exc).__name__)
            self.publish(
                {"type": "studio.error", "message": f"{call.name}: {type(exc).__name__}: {str(exc)[:200]}"}
            )
            if self.managed and call.name != "classify_intent" and self.workflow_phase == "classification":
                # A queued response may have been generated before the preceding turn restored tools.
                # Reject its action, then require a fresh classifier on its continuation.
                self.authorized_version = None
                self.bridge.routes.pop(call.delegation_id, None)
            elif self.managed and (call.name == "classify_intent" or self.workflow_phase != "executing"):
                self.authorized_version = None
                self.bridge.routes.pop(call.delegation_id, None)
                self.bridge.tool_definitions.pop(call.delegation_id, None)
                try:
                    self.set_phase("updating")
                    await self.apply_tools([CLASSIFY_TOOL], "none")
                    self.set_phase("clarifying")
                except (ValueError, TimeoutError, ConnectionClosed) as update_error:
                    self.publish(
                        {"type": "studio.error", "message": f"Cannot disable business tools: {update_error}"}
                    )
                    self.spawn(self.close())
                    return
        else:
            self.trace.finish(sid)
        try:
            with self.trace.span("function_call_output sent", "response", parent, call_id=call.call_id):
                await self.send(
                    {
                        "type": "response.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": json.dumps(result, ensure_ascii=False),
                        },
                    }
                )
            call.state = "submitted"
            self.publish(
                {
                    "type": "studio.tools",
                    "calls": [
                        {
                            "callId": c.call_id,
                            "delegationId": c.delegation_id,
                            "name": c.name,
                            "arguments": "[redacted]",
                            "state": c.state,
                        }
                        for c in self.ledger.calls.values()
                    ],
                }
            )
            await self.continue_ready()
        except (ConnectionClosed, TimeoutError) as exc:
            self.publish(
                {"type": "studio.error", "message": f"Tool result submission failed: {type(exc).__name__}"}
            )
            await self.close()

    async def continue_ready(self) -> None:
        async with self.continue_lock:
            for batch_id in self.ledger.ready():
                d = self.ledger.batches[batch_id].delegation_id
                if self.managed and (
                    d != self.active_delegation
                    or self.pending_update
                    or self.workflow_phase not in ("executing", "clarifying", "classification")
                ):
                    continue
                rounds = sum(b.continued for b in self.ledger.batches.values() if b.delegation_id == d)
                if rounds >= 12:
                    self.publish(
                        {"type": "studio.error", "message": "Tool round limit reached; ending session"}
                    )
                    self.status = "closing"
                    await self.send({"type": "session.close"})
                    return
                with self.trace.span("response.create sent", "response", self.delegations.get(d)):
                    await self.send({"type": "response.create"})
                self.ledger.batches[batch_id].continued = True

    async def command(self, event: dict[str, Any], experimental: bool = False) -> None:
        if self.status != "ready":
            raise ValueError("Session is not active")
        validate_command(event, self.config, self.transport, experimental)
        if event.get("delegation_id") and event["delegation_id"] not in self.delegations:
            raise ValueError("Delegation does not belong to this session")
        if event["type"] == "session.update":
            candidate = SessionConfig.model_validate({**self.config.wire(), **event["session"]})
            validate_models(candidate, self.settings, [t for t in self.visible_tools if t["name"] != "classify_intent"])
            if self.managed:
                responses = candidate.delegation.responses if candidate.delegation else None
                assert responses is not None
                if (
                    self.tool_signature(responses.get("tools", [])) != self.tool_signature(self.visible_tools)
                    or responses.get("tool_choice") != self.tool_choice
                ):
                    raise ValueError("tools and tool_choice are managed by the classification workflow")
                async with self.workflow_lock:
                    self.base_responses = copy.deepcopy(responses)
                    await self.apply_tools(self.visible_tools, self.tool_choice)
                return
        if (
            event["type"] == "response.item.create"
            and event.get("item", {}).get("type") == "function_call_output"
        ):
            raise ValueError("Business tool results can only be submitted by the trusted backend")
        if event["type"] == "response.create" and any(
            c.state != "submitted" for c in self.ledger.calls.values()
        ):
            raise ValueError("Wait for all pending tool results")
        if event["type"] == "session.close":
            await self.close()
        else:
            await self.send(event)

    async def close(self) -> None:
        if self.status in ("closed", "closing"):
            return
        self.status = "closing"
        if self.pending_update and not self.pending_update[1].done():
            self.pending_update[1].cancel()
        async with self.turn_condition:
            self.turn_condition.notify_all()
        self.publish({"type": "studio.closing"})
        await self.send({"type": "session.close"})
        if self.reader and self.reader is not asyncio.current_task():
            try:
                async with asyncio.timeout(12):
                    await asyncio.shield(self.reader)
            except TimeoutError:
                assert self.socket is not None
                await self.socket.close()


def validate_models(config: SessionConfig, settings: Settings, tools: list[dict[str, Any]]) -> None:
    if config.model != settings.live_deployment:
        raise ValueError("Live deployment is not allowed")
    if config.delegation and config.delegation.type == "responses":
        r = config.delegation.responses
        assert r is not None
        if r["model"] != settings.responses_deployment:
            raise ValueError("Responses deployment is not allowed")
        expected = {t["name"]: t for t in [CLASSIFY_TOOL, *tools]}
        selected = r.get("tools", [])
        if any(t.get("type") != "function" or expected.get(t.get("name")) != t for t in selected):
            raise ValueError("Only discovered tool definitions are allowed; reload configuration")
        if not any(t.get("name") == "classify_intent" for t in selected):
            raise ValueError("Automated business delegation requires classify_intent")
