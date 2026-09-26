# 本地控制端

本目录承载局域网 MQTT Broker、设备状态服务和浏览器 API。控制端可运行在长期在线的电脑或 NAS 上；设备不需要固定 IP。

初期使用 Docker 运行本地 Mosquitto Broker：

```powershell
docker compose up -d
```

Broker 仅暴露给家庭局域网：MQTT TCP `1883`，浏览器 WebSocket `9001`。投入长期使用前应在 `.env` 中配置访问凭据；示例配置不包含任何真实密码。

状态服务订阅 `air/v1/+/state`、`heartbeat`、`availability` 和 `ack`，向 `command` 下发请求，并通过 HTTP 供 `console/` 使用。工程工具接口还提供：

- `GET /api/v1/tools/environment`：PlatformIO、固件、串口和本机服务检查；
- `GET /api/v1/tools/ports`：串口枚举和 ESP32 候选识别；
- `GET /api/v1/tools/safety?port=COMx`：读取烧录前置条件和锁定原因；
- `POST /api/v1/tools/jobs`：启动白名单工程任务；
- `GET /api/v1/tools/jobs/<id>`：读取任务状态和有界日志；
- `POST /api/v1/catalog/restore`：恢复经过字段过滤的设备目录备份。

工程任务不接受任意命令字符串。服务端只允许板卡通信检查、固件编译、固件写入和隔离 MQTT 压力测试，并对串口、设备数、频率和持续时间做范围校验。

面向新板的默认入口是 `provision_board` 自动验收任务。它按固定顺序完成 ESP32-C3 身份读取、当前源码编译、固件指纹锁定、Flash 写入与上传校验、固件启动验证、不可变设备 ID 核对、本机 MQTT 参数首次配置，以及固定 Wi-Fi 和 MQTT 上线验证。MQTT 凭据不会写入任务日志。任一步失败都会停止，只有全部通过才返回 `accepted: true`。

安全约束不依赖前端：普通串口不能进入板卡检查或写入流程；固件写入必须有同一串口十分钟内的 ESP32-C3 检查结果、当前源码三十分钟内的成功编译产物、未变化的源码与固件 SHA-256，以及精确的目标串口确认文字。工程任务全局串行执行并有最长时限。压力测试使用 `air/loadtest/v1` 隔离主题、不保留消息、不发送设备命令；高负载需要额外确认。
