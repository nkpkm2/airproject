# 空气净化器调试台

这是一个本机运行的图形化调试台。它使用浏览器的 Web Serial 功能直接连接 ESP32-C3 的 USB 串口，因此无需 MQTT 服务即可读取现有板子的 `info`、`scan` 日志，并执行 LED、ADC 和光耦测试。

## 启动

在项目根目录运行：

```powershell
& 'C:\Users\CHJ\AppData\Local\Programs\Python\Python312\python.exe' -m http.server 8765 --directory console
```

使用 Chrome 或 Edge 打开 [http://localhost:8765](http://localhost:8765)。点击“连接 USB 串口”，在系统选择器中选择 COM6 对应的 ESP32-C3。浏览器会请求串口授权；这是 Web Serial 的安全要求。

同一局域网内的其他设备可访问 `http://<本机 IPv4 地址>:8765/fleet.html`，打开“家庭空气管理”户型图控制台。当前页面使用演示设备数据；真实设备的 MAC 注册、本地服务与确认回执接入后，圆点动画会由真实回执驱动。

## 当前可用能力

- 管理设备不可变 ID（MAC）、可变别名、净化器编号和房间；数据保存在本浏览器的本地存储中；
- 从已连接的开发板读取芯片、Flash、内存和 Wi-Fi 扫描信息；
- 输入 SSID 与密码，通过 USB 配置开发板联网；密码只保存在当前浏览器和板内 NVS，不写入源码；
- 显示已连接网络的实际 RSSI、IP 地址和线性信号刻度，并保留扫描到的网络列表；
- 发送 `info`、`scan`、`blink` 和 `reboot` 自检命令；
- 显示四路 ADC 和两路光耦的正式调试面板。当前无外设时会显示为“未接线”，不将模拟值伪装成实测值。

## 后续接入

局域网 MQTT 通信接入后，`wifi.html` 将通过控制端读取同一套板卡诊断数据；USB 串口页面仍保留为烧录、救援和硬件底层排障工具。
