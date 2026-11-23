import asyncio
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode

from .auth import credential
from .config import Settings

State = dict[str, Any]
Change = Callable[[State], State]


class Store(Protocol):
    async def get(self, contact: str) -> State: ...
    async def mutate(self, contact: str, change: Change) -> State: ...
    async def delete(self, contact: str) -> None: ...
    async def cleanup(self) -> int: ...
    def close(self) -> None: ...


class SQLiteStore:
    """Explicit local/test store; never selected as a cloud failure fallback."""

    def __init__(self, path: str, ttl: int = 1800):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path, self.ttl = path, ttl
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, expires REAL, data TEXT)")

    async def get(self, contact: str) -> State:
        def read() -> State:
            with sqlite3.connect(self.path, timeout=10) as db:
                row = db.execute("SELECT expires,data FROM sessions WHERE id=?", (contact,)).fetchone()
                if not row:
                    return {}
                if row[0] <= time.time():
                    db.execute("DELETE FROM sessions WHERE id=? AND expires<=?", (contact, time.time()))
                    return {}
                return json.loads(row[1])
        return await asyncio.to_thread(read)

    async def mutate(self, contact: str, change: Change) -> State:
        def write() -> State:
            with sqlite3.connect(self.path, timeout=10) as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT expires,data FROM sessions WHERE id=?", (contact,)).fetchone()
                state = json.loads(row[1]) if row and row[0] > time.time() else {}
                result = change(state)
                db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?)",
                           (contact, time.time() + self.ttl, json.dumps(result, ensure_ascii=False)))
                return result
        return await asyncio.to_thread(write)

    async def delete(self, contact: str) -> None:
        def remove() -> None:
            with sqlite3.connect(self.path) as db:
                db.execute("DELETE FROM sessions WHERE id=?", (contact,))
        await asyncio.to_thread(remove)

    async def cleanup(self) -> int:
        def remove() -> int:
            with sqlite3.connect(self.path) as db:
                return db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),)).rowcount
        return await asyncio.to_thread(remove)

    def close(self) -> None:
        pass


class AzureStore:
    def __init__(self, endpoint: str, table: str, ttl: int = 1800, config: Settings | None = None):
        self.credential = credential(config or Settings())
        self.client = TableClient(endpoint, table, credential=self.credential, logging_enable=False)
        self.ttl = ttl

    def _read(self, contact: str) -> Any:
        try:
            return self.client.get_entity(contact, "state")
        except ResourceNotFoundError:
            return None

    async def get(self, contact: str) -> State:
        entity = await asyncio.to_thread(self._read, contact)
        if not entity:
            return {}
        if entity["expiresAt"] <= time.time():
            try:
                await asyncio.to_thread(
                    self.client.delete_entity, contact, "state",
                    etag=entity.metadata["etag"], match_condition=MatchConditions.IfNotModified,
                )
            except (ResourceNotFoundError, ResourceModifiedError):
                # Another request refreshed or removed the expired row.
                return await self.get(contact)
            return {}
        return json.loads(entity["payload"])

    async def mutate(self, contact: str, change: Change) -> State:
        for _ in range(5):
            entity = await asyncio.to_thread(self._read, contact)
            state = json.loads(entity["payload"]) if entity and entity["expiresAt"] > time.time() else {}
            result = change(state)
            row: State = {
                "PartitionKey": contact, "RowKey": "state",
                "expiresAt": time.time() + self.ttl, "payload": json.dumps(result, ensure_ascii=False),
            }
            if len(row["payload"].encode("utf-16-le")) > 60_000:
                raise ValueError("Demo session storage limit reached; start a new session")
            try:
                if entity:
                    await asyncio.to_thread(
                        self.client.update_entity, row, mode=UpdateMode.REPLACE,
                        etag=entity.metadata["etag"], match_condition=MatchConditions.IfNotModified,
                    )
                else:
                    await asyncio.to_thread(self.client.create_entity, row)
                return result
            except (ResourceModifiedError, ResourceExistsError):
                continue
        raise RuntimeError("Shared state changed concurrently; retry the operation")

    async def delete(self, contact: str) -> None:
        try:
            await asyncio.to_thread(self.client.delete_entity, contact, "state")
        except ResourceNotFoundError:
            return

    async def cleanup(self) -> int:
        def remove() -> int:
            count = 0
            rows = self.client.query_entities(f"expiresAt le {time.time()}")
            for entity in rows:
                if count >= 200:
                    break
                try:
                    self.client.delete_entity(
                        entity, etag=entity.metadata["etag"], match_condition=MatchConditions.IfNotModified
                    )
                    count += 1
                except (ResourceNotFoundError, ResourceModifiedError):
                    continue
            return count
        return await asyncio.to_thread(remove)

    def close(self) -> None:
        self.client.close()
        if hasattr(self.credential, "close"):
            self.credential.close()


def create_store(config: Settings) -> Store:
    if config.state_backend == "sqlite":
        return SQLiteStore(config.sqlite_path, config.session_ttl_seconds)
    if config.state_backend != "table" or not config.table_endpoint:
        raise ValueError("TABLE_ENDPOINT is required; use STATE_BACKEND=sqlite only for explicit local testing")
    return AzureStore(config.table_endpoint, config.table_name, config.session_ttl_seconds, config)


class LazyStore:
    def __init__(self, config: Settings):
        self.config = config
        self.instance: Store | None = None

    def _store(self) -> Store:
        if self.instance is None:
            self.instance = create_store(self.config)
        return self.instance

    async def get(self, contact: str) -> State:
        return await self._store().get(contact)

    async def mutate(self, contact: str, change: Change) -> State:
        return await self._store().mutate(contact, change)

    async def delete(self, contact: str) -> None:
        await self._store().delete(contact)

    async def cleanup(self) -> int:
        return await self._store().cleanup()

    def close(self) -> None:
        if self.instance:
            self.instance.close()
