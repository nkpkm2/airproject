from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    mqtt_host: str = os.getenv("MQTT_HOST", "mqtt")
    mqtt_port: int = int(os.getenv("MQTT_PORT", "1883"))
    mqtt_username: str | None = os.getenv("MQTT_USERNAME") or None
    mqtt_password: str | None = os.getenv("MQTT_PASSWORD") or None
    api_origin: str = os.getenv("API_ORIGIN", "*")


settings = Settings()
