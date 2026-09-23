# 本地控制端

本目录承载局域网 MQTT Broker、设备状态服务和浏览器 API。控制端可运行在长期在线的电脑或 NAS 上；设备不需要固定 IP。

初期使用 Docker 运行本地 Mosquitto Broker：

```powershell
docker compose up -d
```

Broker 仅暴露给家庭局域网：MQTT TCP `1883`，浏览器 WebSocket `9001`。投入长期使用前应在 `.env` 中配置访问凭据；示例配置不包含任何真实密码。

后续的状态服务订阅 `air/v1/+/state`、`heartbeat`、`availability` 和 `ack`，向 `command` 下发请求，并通过 HTTP/WebSocket 供 `console/` 使用。
