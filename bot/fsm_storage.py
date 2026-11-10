"""Persistent aiogram FSM state on the existing Upstash REST connection.

Each chat/user FSM key is stored independently with an expiration. The
existing Upstash credentials are reused; no new paid service is required.
"""
import asyncio
import json

from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder, StorageKey
from bot import config, content_store


class UpstashFSMStorage(BaseStorage):
    def __init__(self, ttl_seconds: int = 7 * 24 * 3600):
        self.ttl_seconds = ttl_seconds
        self.key_builder = DefaultKeyBuilder(prefix="design-bot-fsm", with_bot_id=True)

    def _key(self, key: StorageKey) -> str:
        return self.key_builder.build(key)

    async def _read(self, key: StorageKey) -> dict:
        raw = await asyncio.to_thread(content_store._upstash_command, "GET", self._key(key))
        if raw is None:
            return {}
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Invalid FSM storage payload")
        return value

    async def _write(self, key: StorageKey, *, state_marker=..., data_marker=...) -> None:
        # This operation is used by aiogram under SimpleEventIsolation:
        # sequential updates for the same user cannot race within a worker.
        current = await self._read(key)
        if state_marker is not ...:
            current["state"] = state_marker
        if data_marker is not ...:
            current["data"] = data_marker
        if not current.get("state") and not current.get("data"):
            await asyncio.to_thread(content_store._upstash_command, "DEL", self._key(key))
            return
        await asyncio.to_thread(
            content_store._upstash_command, "SET", self._key(key),
            json.dumps(current, ensure_ascii=False), "EX", self.ttl_seconds,
        )

    async def set_state(self, key: StorageKey, state=None) -> None:
        value = state.state if hasattr(state, "state") else state
        await self._write(key, state_marker=value)

    async def get_state(self, key: StorageKey):
        return (await self._read(key)).get("state")

    async def set_data(self, key: StorageKey, data: dict) -> None:
        await self._write(key, data_marker=dict(data))

    async def get_data(self, key: StorageKey) -> dict:
        return dict((await self._read(key)).get("data") or {})

    async def close(self) -> None:
        pass


def create_fsm_storage() -> BaseStorage:
    if config.UPSTASH_REDIS_REST_URL and config.UPSTASH_REDIS_REST_TOKEN:
        return UpstashFSMStorage()
    from aiogram.fsm.storage.memory import MemoryStorage
    return MemoryStorage()
