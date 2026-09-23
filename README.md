# 家庭空气管理系统

面向约 15 台 ESP32-C3 净化器控制板的局域网系统。设备通过 Wi-Fi 独立联网，以 MAC 派生的不可变 ID 识别；电脑、手机和板卡不依赖固定 IP。

## 目录

| 目录 | 职责 |
| --- | --- |
| `src/`、`include/` | ESP32-C3 固件与硬件映射 |
| `console/` | 家庭总览、USB 串口调试和后续 Wi-Fi 调试页面 |
| `control-service/` | 局域网 MQTT Broker 与状态 / 命令服务 |
| `docs/` | 架构、MQTT 契约、硬件测试记录 |
| `variants/` | 不同开发板的隔离实验副本；不与主板固件混用 |

## 架构原则

- 设备身份由 MAC 派生，不使用 DHCP IP 作为身份。
- 设备只连接 2.4 GHz；控制端可以连接 5 GHz。两者只需处于同一 LAN/VLAN，且未启用访客隔离。
- MQTT 是设备与控制端的唯一局域网消息总线。
- 模式、开关、在线状态、命令回执立即上报；ADC 波形和详细诊断默认关闭、按需开启并自动过期。
- Wi-Fi 只能由一个非阻塞状态机控制；禁止 SDK 自动重连、后台重连和手动连接同时争用射频。
- Wi-Fi 密码不进入源码。首次配置经串口或配网页写入板内 NVS，网页只可在本浏览器本地保存。

详见 [系统架构](docs/architecture.md) 与 [MQTT 契约](docs/mqtt-contract.md)。

## 当前开发阶段

主板固件已经具备 USB 串口、四路 ADC、两路光耦、手动 Wi-Fi 配置和基础诊断能力。控制端已经具备 MQTT Broker 编排、设备注册表、HTTP API 和 WebSocket 推送骨架。

下一阶段是把主板的连接流程收敛为单一 Wi-Fi 状态机，并接入 MQTT 状态、命令和可过期的调试数据流。

## 本地启动

### USB 串口调试页

```powershell
python -m http.server 8765 --directory console
```

在 Chrome 或 Edge 打开 `http://localhost:8765/index.html`，选择对应的 ESP32-C3 串口。

### 局域网控制端

需要 Docker。控制端尚未配置真实设备前，可以只启动 Broker 与 API 骨架：

```powershell
cd control-service
docker compose up -d
```

服务 API 地址为 `http://localhost:8780`，Broker 为 `mqtt://<控制端局域网IP>:1883`。

> `control-service/mosquitto/mosquitto.conf` 目前是仅限家庭内网的开发配置。正式部署前必须启用账户、访问控制和备份。
