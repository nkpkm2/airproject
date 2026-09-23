from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import paho.mqtt.client as mqtt

from .config import Settings
from .registry import DeviceRegistry

LOGGER = logging.getLogger(__name__)
ROOT_TOPIC = "air/v1"
VALID_DEVICE_SEGMENT = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def valid_device_id(value: str) -> bool:
    return bool(value) and all(character in VALID_DEVICE_SEGMENT for character in value)


class MqttBridge:
    def __init__(self, settings: Settings, registry: DeviceRegistry, loop: asyncio.AbstractEventLoop) -> None:
        self._settings = settings
        self._registry = registry
        self._loop = loop
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="air-control-service")
        if settings.mqtt_username:
            self._client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def start(self) -> None:
        self._client.connect_async(self._settings.mqtt_host, self._settings.mqtt_port, keepalive=20)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    def publish_command(self, device_id: str, command: dict[str, Any]) -> None:
        if not valid_device_id(device_id):
            raise ValueError("invalid device id")
        topic = f"{ROOT_TOPIC}/{device_id}/command"
        result = self._client.publish(topic, json.dumps(command, separators=(",", ":")), qos=1, retain=False)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish failed: {result.rc}")

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        if reason_code.is_failure:
            LOGGER.error("MQTT connection refused: %s", reason_code)
            return
        client.subscribe(f"{ROOT_TOPIC}/+/availability", qos=1)
        client.subscribe(f"{ROOT_TOPIC}/+/state", qos=1)
        client.subscribe(f"{ROOT_TOPIC}/+/heartbeat", qos=0)
        client.subscribe(f"{ROOT_TOPIC}/+/telemetry", qos=0)
        client.subscribe(f"{ROOT_TOPIC}/+/ack", qos=1)
        LOGGER.info("connected to MQTT broker")

    def _on_message(self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
        parts = message.topic.split("/")
        if len(parts) != 4 or parts[:2] != ["air", "v1"] or not valid_device_id(parts[2]):
            return
        try:
            decoded: Any = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded = {"raw": message.payload.decode("utf-8", errors="replace")}
        future = asyncio.run_coroutine_threadsafe(self._registry.apply(parts[2], parts[3], decoded), self._loop)
        future.add_done_callback(self._log_apply_error)

    @staticmethod
    def _log_apply_error(future: asyncio.Future[Any]) -> None:
        try:
            future.result()
        except Exception:  # pragma: no cover - logging path
            LOGGER.exception("failed to apply MQTT message")
