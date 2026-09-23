# MQTT 通信契约（v1）

Broker 在本地控制端运行。所有主题以 `air/v1/` 开头；`<id>` 为 MAC 派生的不可变设备 ID。

| 主题 | 方向 | QoS | retained | 用途 |
| --- | --- | ---: | ---: | --- |
| `air/v1/<id>/availability` | 设备 → 服务 | 1 | 是 | `online` / `offline` |
| `air/v1/<id>/state` | 设备 → 服务 | 1 | 是 | 模式、开关、ADC 判定、光耦最终状态 |
| `air/v1/<id>/heartbeat` | 设备 → 服务 | 0 | 否 | RSSI、IP、运行时间、Wi-Fi 状态 |
| `air/v1/<id>/telemetry` | 设备 → 服务 | 0 | 否 | 可选 ADC 帧与诊断流 |
| `air/v1/<id>/command` | 服务 → 设备 | 1 | 否 | 设备命令与调试流配置 |
| `air/v1/<id>/ack` | 设备 → 服务 | 1 | 否 | 命令执行确认或失败 |

## 状态载荷

```json
{
  "schema": 1,
  "device_id": "pur-e83dc1951534",
  "sequence": 42,
  "mode": "low",
  "powered": true,
  "adc_mv": [121, 35, 42, 39],
  "opto": { "power": false, "mode": false },
  "wifi": { "rssi_dbm": -58, "ip": "192.168.1.181" }
}
```

## 命令载荷

每条命令必须有 `request_id`，设备在 `ack` 回传同一 ID，保证控制端可去重和等待确认。

```json
{
  "request_id": "a9e2f8d1",
  "type": "debug.configure",
  "adc_stream_hz": 20,
  "opto_trace": true,
  "expires_in_s": 60
}
```

`adc_stream_hz: 0` 关闭 ADC 调试流。设备即使未启用调试流，仍会在模式、开关、光耦最终状态变化时立即发布 `state`。
