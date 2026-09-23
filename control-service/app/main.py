from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import settings
from .mqtt_bridge import MqttBridge, valid_device_id
from .registry import DeviceRegistry

registry = DeviceRegistry()
bridge: MqttBridge | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bridge
    bridge = MqttBridge(settings, registry, asyncio.get_running_loop())
    bridge.start()
    yield
    bridge.stop()


app = FastAPI(title="Air Control Service", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.api_origin] if settings.api_origin != "*" else ["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class CommandRequest(BaseModel):
    type: str = Field(pattern=r"^[a-z]+(?:\.[a-z]+)*$")
    payload: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/devices")
async def list_devices() -> list[dict[str, Any]]:
    return await registry.list()


@app.get("/api/v1/devices/{device_id}")
async def get_device(device_id: str) -> dict[str, Any]:
    device = await registry.get(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="device not seen")
    return device


@app.post("/api/v1/devices/{device_id}/commands")
async def send_command(device_id: str, request: CommandRequest) -> dict[str, str]:
    if not valid_device_id(device_id):
        raise HTTPException(status_code=400, detail="invalid device id")
    if bridge is None:
        raise HTTPException(status_code=503, detail="MQTT bridge unavailable")
    request_id = request.request_id or uuid4().hex
    try:
        bridge.publish_command(device_id, {"request_id": request_id, "type": request.type, **request.payload})
    except (RuntimeError, ValueError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"request_id": request_id, "status": "queued"}


@app.websocket("/api/v1/events")
async def events(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = registry.subscribe()
    try:
        await websocket.send_json({"type": "snapshot", "devices": await registry.list()})
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        registry.unsubscribe(queue)
