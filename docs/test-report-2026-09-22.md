# ESP32-C3 Super Mini 裸板测试报告

测试时间：2026-09-22 19:05（Asia/Singapore）  
测试接口：板载 USB Type-C  
Windows 串口：COM6

## 结论

**基础验收通过。** USB 数据连接、驱动、下载模式、Flash 写入与校验、固件启动、串口交互、软件复位和 Wi-Fi 扫描均正常。未发现 brownout、panic 或 watchdog 异常。

板载 LED 的控制命令已正常执行，但 LED 是否肉眼可见闪烁需要现场确认；不同 Super Mini 批次的 LED 引脚可能不同，因此该项不影响其余测试结论。

## 实测数据

| 项目 | 实测结果 | 状态 |
| --- | --- | --- |
| USB 设备 | Espressif `VID_303A&PID_1001` | 通过 |
| Windows 驱动 | USB 串行设备 COM6，状态 OK | 通过 |
| 芯片 | ESP32-C3 QFN32，revision v0.4 | 通过 |
| CPU | 160 MHz，单核 | 通过 |
| Flash | 4,194,304 bytes，XMC embedded flash | 通过 |
| 晶振 | 40 MHz | 通过 |
| USB 模式 | USB-Serial/JTAG | 通过 |
| 编译 | RAM 11.5%，固件 Flash 区 54.1% | 通过 |
| 烧录 | 所有写入区段 SHA 校验通过 | 通过 |
| 启动 | 固件正常启动并输出完整日志 | 通过 |
| 堆内存 | 启动时约 234 KB 空闲 | 通过 |
| 软件重启 | `reboot` 后正常重新启动 | 通过 |
| Wi-Fi | 扫描成功，可见 29 个网络 | 通过 |
| 串口命令 | `info`、`blink` 均正常响应 | 通过 |
| LED 肉眼确认 | 待用户现场确认 | 待确认 |

## 本次固件

固件构建时间：2026-09-22 19:03:26  
框架：Arduino for ESP32  
PlatformIO 环境：`esp32-c3-super-mini`

项目本地工具可用以下命令调用：

```powershell
.\.venv\Scripts\python.exe -m platformio run
.\.venv\Scripts\python.exe -m platformio run -t upload --upload-port COM6
.\.venv\Scripts\python.exe -m platformio device monitor --port COM6 --baud 115200
```
