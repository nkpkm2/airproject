from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any


class DeviceRegistry:
    """In-memory source of truth for live MQTT state.

    Persistence is intentionally a later concern: a device's immutable ID and
    retained MQTT state are enough to rebuild this registry after a service
    restart.
    """

    def __init__(self) -> None:
        self._devices: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._listeners: set[asyncio.Queue[dict[str, Any]]] = set()

    async def apply(self, device_id: str, category: str, payload: Any) -> dict[str, Any]:
        async with self._lock:
            record = self._devices.setdefault(device_id, {"device_id": device_id})
            record[category] = payload
            record["last_seen"] = datetime.now(UTC).isoformat()
            event = {"type": "device.updated", "device": deepcopy(record)}
            for listener in tuple(self._listeners):
                if listener.full():
                    listener.get_nowait()
                listener.put_nowait(event)
            return deepcopy(record)

    async def list(self) -> list[dict[str, Any]]:
        async with self._lock:
            return sorted((deepcopy(item) for item in self._devices.values()), key=lambda item: item["device_id"])

    async def get(self, device_id: str) -> dict[str, Any] | None:
        async with self._lock:
            record = self._devices.get(device_id)
            return deepcopy(record) if record else None

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
        self._listeners.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._listeners.discard(queue)
