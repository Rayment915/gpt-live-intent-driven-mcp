import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from .privacy import redact


class Trace:
    def __init__(self, emit: Callable[[dict[str, Any]], None]):
        self.emit = emit
        self.trace_id = uuid.uuid4().hex
        self.epoch = time.perf_counter()
        self.spans: dict[str, dict[str, Any]] = {}

    def start(self, name: str, kind: str, parent: str | None = None, **attributes: Any) -> str:
        key = uuid.uuid4().hex[:16]
        self.spans[key] = {
            "type": "studio.span", "id": key, "trace_id": self.trace_id, "parent_id": parent,
            "name": name, "kind": kind, "start_ms": (time.perf_counter() - self.epoch) * 1000,
            "status": "running", "attributes": redact(attributes),
        }
        self.emit(dict(self.spans[key]))
        return key

    def finish(self, key: str, status: str = "completed", **attributes: Any) -> None:
        span = self.spans[key]
        if span["status"] != "running":
            return
        end = (time.perf_counter() - self.epoch) * 1000
        span.update(status=status, end_ms=end, duration_ms=end - span["start_ms"])
        span["attributes"] = {**span["attributes"], **redact(attributes)}
        self.emit(dict(span))

    @contextmanager
    def span(self, name: str, kind: str, parent: str | None = None, **attributes: Any) -> Iterator[str]:
        key = self.start(name, kind, parent, **attributes)
        try:
            yield key
        except BaseException as exc:
            self.finish(key, "cancelled" if isinstance(exc, (KeyboardInterrupt, GeneratorExit))
                        or type(exc).__name__ == "CancelledError" else "failed",
                        error_type=type(exc).__name__)
            raise
        else:
            self.finish(key)

    def header(self, span_id: str) -> str:
        return f"00-{self.trace_id}-{span_id}-01"
