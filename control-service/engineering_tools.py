"""Safety-bounded engineering operations for the local browser console.

The browser never supplies a command line. It can only request an allow-listed
operation. Firmware upload has server-side prerequisites so a stale browser
tab cannot bypass the safeguards.
"""
from __future__ import annotations

import hashlib
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from uuid import uuid4

import serial
from serial.tools import list_ports

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ENV_FILE = PROJECT_ROOT / "control-service" / ".env"
PLATFORMIO_INI = PROJECT_ROOT / "platformio.ini"
PIO_ENVIRONMENT = "esp32-c3-super-mini"
FIRMWARE = PROJECT_ROOT / ".pio" / "build" / PIO_ENVIRONMENT / "firmware.bin"
ESPTOOL = Path.home() / ".platformio" / "packages" / "tool-esptoolpy" / "esptool.py"
PORT_PATTERN = re.compile(r"^COM\d{1,3}$", re.IGNORECASE)
MAX_LOG_LINES = 400
BUILD_VALID_SECONDS = 30 * 60
BOARD_CHECK_VALID_SECONDS = 10 * 60

TOOL_POLICY = {
    "provision_board": {"risk": "destructive_board_write", "writes_board": True,
        "description": "自动执行芯片只读检查、编译、写入校验、固件启动和 Wi-Fi/MQTT 联网验收。", "timeout_seconds": 540},
    "board_check": {"risk": "read_only_board", "writes_board": False,
        "description": "读取 ESP32-C3 芯片身份；不会写入 Flash，但串口握手可能使板卡复位。", "timeout_seconds": 45},
    "build_firmware": {"risk": "local_build", "writes_board": False,
        "description": "只在本机生成固件，不访问板卡。", "timeout_seconds": 360},
    "flash_firmware": {"risk": "destructive_board_write", "writes_board": True,
        "description": "覆盖目标板卡程序；必须先完成同一串口检查和当前源码编译。", "timeout_seconds": 360},
    "mqtt_stress": {"risk": "isolated_load", "writes_board": False,
        "description": "仅向 air/loadtest/v1 隔离主题发送非保留消息，不向真实设备发命令。", "timeout_seconds": 330},
}


def _port_record(port: Any) -> dict[str, Any]:
    vid, pid = port.vid, port.pid
    likely_esp32 = (vid, pid) in {
        (0x303A, 0x1001), (0x1A86, 0x55D4), (0x1A86, 0x7523), (0x10C4, 0xEA60),
    }
    return {"device": port.device, "description": port.description or "串行设备",
        "hardware_id": port.hwid or "", "serial_number": port.serial_number or "",
        "vid": vid, "pid": pid, "likely_esp32": likely_esp32}


def serial_ports() -> list[dict[str, Any]]:
    return sorted((_port_record(port) for port in list_ports.comports()),
                  key=lambda item: (not item["likely_esp32"], item["device"]))


def _port_fingerprint(port: dict[str, Any]) -> str:
    return "|".join(str(port.get(key, "")) for key in ("device", "vid", "pid", "serial_number", "hardware_id"))


def _usb_identity(port: dict[str, Any]) -> str:
    """Stable physical identity that survives a Windows COM-number change."""
    return "|".join(str(port.get(key, "")) for key in ("vid", "pid", "serial_number", "hardware_id"))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(128 * 1024), b""): digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint() -> str:
    """Hash every project input that can affect the firmware image."""
    digest = hashlib.sha256()
    paths = [PLATFORMIO_INI]
    for folder_name in ("src", "include"):
        folder = PROJECT_ROOT / folder_name
        if folder.exists(): paths.extend(path for path in folder.rglob("*") if path.is_file())
    for path in sorted(paths, key=lambda item: item.as_posix().lower()):
        digest.update(path.relative_to(PROJECT_ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0"); digest.update(path.read_bytes()); digest.update(b"\0")
    return digest.hexdigest()


def _mqtt_provisioning_command() -> bytes:
    values: dict[str, str] = {}
    try:
        for raw_line in CONTROL_ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            if "=" in raw_line and not raw_line.lstrip().startswith("#"):
                key, value = raw_line.split("=", 1); values[key.strip()] = value.strip()
    except OSError as error:
        raise RuntimeError("缺少本机 MQTT 配置，不能完成新板联网验收") from error
    required = ("MQTT_HOST", "MQTT_PORT", "MQTT_USERNAME", "MQTT_PASSWORD")
    if any(not values.get(key) for key in required):
        raise RuntimeError("本机 MQTT 配置不完整，不能完成新板联网验收")
    if any(any(mark in values[key] for mark in ("|", "\r", "\n")) for key in required):
        raise RuntimeError("MQTT 配置包含串口协议不支持的字符")
    return (f"mqtt mqtt://{values['MQTT_HOST']}:{values['MQTT_PORT']}|"
            f"{values['MQTT_USERNAME']}|{values['MQTT_PASSWORD']}\n").encode("utf-8")


def _tcp_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25): return True
    except OSError: return False


def environment_report() -> dict[str, Any]:
    pio_version = esptool_version = "不可用"
    try:
        completed = subprocess.run([sys.executable, "-m", "platformio", "--version"], cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=10, check=False)
        if completed.returncode == 0: pio_version = (completed.stdout or completed.stderr).strip()
    except (OSError, subprocess.TimeoutExpired): pass
    if ESPTOOL.exists():
        try:
            completed = subprocess.run([sys.executable, str(ESPTOOL), "version"], cwd=PROJECT_ROOT,
                capture_output=True, text=True, timeout=10, check=False)
            if completed.returncode == 0: esptool_version = (completed.stdout or completed.stderr).strip()
        except (OSError, subprocess.TimeoutExpired): pass
    return {"python": sys.version.split()[0], "platformio": pio_version,
        "platformio_ini": PLATFORMIO_INI.exists(), "firmware_exists": FIRMWARE.exists(),
        "firmware_bytes": FIRMWARE.stat().st_size if FIRMWARE.exists() else 0,
        "firmware_sha256": _file_sha256(FIRMWARE) if FIRMWARE.exists() else None,
        "esptool_exists": ESPTOOL.exists(), "esptool_version": esptool_version,
        "mqtt_listening": _tcp_open(1883), "api_listening": _tcp_open(8780),
        "console_listening": _tcp_open(8765), "ports": serial_ports(), "tool_policy": TOOL_POLICY}


class EngineeringTools:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._active_job_id: str | None = None
        self._board_checks: dict[str, dict[str, Any]] = {}
        self._last_build: dict[str, Any] | None = None

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock: return [self._public(job) for job in reversed(list(self._jobs.values()))]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._public(job) if job else None

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        result = dict(job); result["log"] = list(job.get("log", ())); result["policy"] = TOOL_POLICY[job["kind"]]
        return result

    @staticmethod
    def _find_port(port_name: str) -> dict[str, Any] | None:
        upper = str(port_name).upper()
        return next((item for item in serial_ports() if item["device"].upper() == upper), None)

    def safety_status(self, port_name: str = "") -> dict[str, Any]:
        now = time.time(); port = self._find_port(port_name) if port_name else None
        fingerprint = _port_fingerprint(port) if port else ""
        board, build = self._board_checks.get(fingerprint), self._last_build
        current_source = source_fingerprint() if PLATFORMIO_INI.exists() else ""
        board_valid = bool(board and now - board["completed_at"] <= BOARD_CHECK_VALID_SECONDS)
        build_valid = bool(build and now - build["completed_at"] <= BUILD_VALID_SECONDS
            and build["source_sha256"] == current_source and FIRMWARE.exists()
            and build["firmware_sha256"] == _file_sha256(FIRMWARE))
        blockers = []
        if not port: blockers.append("请选择当前存在的串口")
        elif not port["likely_esp32"]: blockers.append("所选串口未识别为 ESP32 USB/串口设备")
        if not board_valid: blockers.append("请先对同一块板执行芯片通信检查")
        if not build_valid: blockers.append("请重新编译当前源码")
        with self._lock: active = self._active_job_id
        if active: blockers.append("已有工程任务正在运行")
        return {"port": port, "board_check_valid": board_valid, "build_valid": build_valid,
            "flash_ready": not blockers, "blockers": blockers, "build": dict(build) if build else None,
            "active_job_id": active, "confirmation_text": f"写入 {port_name.upper()}" if port_name else ""}

    def start(self, request: dict[str, Any]) -> dict[str, Any]:
        kind = str(request.get("kind", ""))
        if kind not in TOOL_POLICY: raise ValueError("unsupported engineering job")
        params = self._validate(kind, request)
        with self._lock:
            if self._active_job_id:
                active = self._jobs.get(self._active_job_id)
                if active and active["status"] in {"queued", "running"}: raise ValueError("已有工程任务正在执行，请等待完成")
                self._active_job_id = None
            job_id = uuid4().hex
            job = {"job_id": job_id, "kind": kind, "status": "queued", "created_at_ms": int(time.time()*1000),
                "started_at_ms": None, "completed_at_ms": None, "exit_code": None, "summary": "等待执行",
                "params": params, "result": {}, "log": deque(maxlen=MAX_LOG_LINES)}
            self._jobs[job_id] = job; self._active_job_id = job_id
            while len(self._jobs) > 30:
                oldest = next(iter(self._jobs))
                if oldest == self._active_job_id: break
                self._jobs.pop(oldest)
        threading.Thread(target=self._run, args=(job_id,), daemon=True).start()
        return self._public(job)

    def _validated_port(self, request: dict[str, Any]) -> dict[str, Any]:
        name = str(request.get("port", "")).upper()
        if not PORT_PATTERN.fullmatch(name): raise ValueError("请选择有效的 COM 串口")
        port = self._find_port(name)
        if not port: raise ValueError("所选串口当前不存在")
        if not port["likely_esp32"]: raise ValueError("所选串口未识别为 ESP32；为防止误操作已拒绝执行")
        return port

    def _validate(self, kind: str, request: dict[str, Any]) -> dict[str, Any]:
        if kind == "provision_board":
            port = self._validated_port(request)
            expected = f"自动验收并写入 {port['device'].upper()}"
            if request.get("confirmation") != expected:
                raise ValueError(f"必须准确输入“{expected}”才能开始")
            if not PLATFORMIO_INI.exists() or not ESPTOOL.exists():
                raise ValueError("工程环境不完整，不能开始自动烧录验收")
            return {"port": port["device"].upper(), "port_fingerprint": _port_fingerprint(port),
                "usb_identity": _usb_identity(port), "environment": PIO_ENVIRONMENT, "confirmation_text": expected}
        if kind == "board_check":
            port = self._validated_port(request)
            return {"port": port["device"].upper(), "port_fingerprint": _port_fingerprint(port), "environment": PIO_ENVIRONMENT}
        if kind == "build_firmware":
            if not PLATFORMIO_INI.exists(): raise ValueError("缺少 platformio.ini，不能编译")
            return {"environment": PIO_ENVIRONMENT, "source_sha256": source_fingerprint()}
        if kind == "flash_firmware":
            port = self._validated_port(request); status = self.safety_status(port["device"])
            if status["blockers"]: raise ValueError("；".join(status["blockers"]))
            expected = status["confirmation_text"]
            if request.get("confirmation") != expected: raise ValueError(f"必须准确输入“{expected}”才能写入")
            build = status["build"]
            return {"port": port["device"].upper(), "port_fingerprint": _port_fingerprint(port),
                "environment": PIO_ENVIRONMENT, "source_sha256": build["source_sha256"],
                "firmware_sha256": build["firmware_sha256"]}
        try:
            devices = int(request.get("devices", 30)); rate = float(request.get("rate", 20)); duration = float(request.get("duration", 30))
        except (TypeError, ValueError) as error: raise ValueError("压力测试参数必须是数字") from error
        if not 1 <= devices <= 60: raise ValueError("虚拟设备数必须在 1–60 之间")
        if not 1 <= rate <= 120: raise ValueError("每台频率必须在 1–120 Hz 之间")
        if not 5 <= duration <= 300: raise ValueError("持续时间必须在 5–300 秒之间")
        aggregate = devices * rate
        if aggregate > 7200: raise ValueError("总负载不得超过每秒 7200 条消息")
        heavy = aggregate > 1200 or duration > 60
        if heavy and request.get("confirmation") != "我了解这是高负载测试": raise ValueError("高负载测试需要明确确认")
        return {"devices": devices, "rate": rate, "duration": duration, "aggregate_rate": aggregate, "high_load": heavy}

    def _append(self, job_id: str, line: str) -> None:
        clean = line.rstrip()
        if clean:
            with self._lock: self._jobs[job_id]["log"].append(clean[-1200:])

    def _command(self, job: dict[str, Any]) -> list[str]:
        kind, params = job["kind"], job["params"]
        if kind == "board_check": return [sys.executable, str(ESPTOOL), "--chip", "esp32c3", "--port", params["port"], "chip_id"]
        if kind == "build_firmware": return [sys.executable, "-m", "platformio", "run", "-e", params["environment"]]
        if kind == "flash_firmware": return [sys.executable, "-m", "platformio", "run", "-e", params["environment"], "-t", "upload", "--upload-port", params["port"]]
        return [sys.executable, str(PROJECT_ROOT / "tools" / "mqtt_fleet_stress.py"), "--devices", str(params["devices"]),
            "--rate", str(params["rate"]), "--duration", str(params["duration"]), "--topic-root", "air/loadtest/v1"]

    def _recheck_before_run(self, job: dict[str, Any]) -> None:
        kind, params = job["kind"], job["params"]
        if kind in {"provision_board", "board_check", "flash_firmware"}:
            port = self._find_port(params["port"])
            if not port or _port_fingerprint(port) != params["port_fingerprint"]:
                raise RuntimeError("串口设备在执行前发生变化，已停止以避免操作错误目标")
        if kind == "flash_firmware":
            if source_fingerprint() != params["source_sha256"]: raise RuntimeError("源码在编译后发生变化，请重新编译")
            if not FIRMWARE.exists() or _file_sha256(FIRMWARE) != params["firmware_sha256"]:
                raise RuntimeError("待写入固件与已验证编译产物不一致，请重新编译")

    def _set_provision_step(self, job_id: str, key: str, status: str, detail: str = "") -> None:
        with self._lock:
            job = self._jobs[job_id]
            steps = job.setdefault("result", {}).setdefault("steps", [])
            item = next((value for value in steps if value["key"] == key), None)
            if item is None:
                item = {"key": key, "label": {
                    "identify": "识别并检查芯片", "build": "编译并锁定固件", "flash": "写入并校验 Flash",
                    "boot": "验证固件启动", "network": "验证 Wi-Fi 与 MQTT",
                }[key]}
                steps.append(item)
            item.update(status=status, detail=detail, updated_at_ms=int(time.time() * 1000))
            job["summary"] = detail or item["label"]

    def _execute_process(self, job_id: str, command: list[str], timeout_seconds: int) -> tuple[int, list[str]]:
        env = os.environ.copy(); env.setdefault("PLATFORMIO_CORE_DIR", str(Path.home() / ".platformio"))
        self._append(job_id, "> " + subprocess.list2cmdline(command))
        process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        assert process.stdout
        output: queue.Queue[str | None] = queue.Queue(); lines: list[str] = []
        def read_output() -> None:
            assert process.stdout
            for output_line in process.stdout: output.put(output_line)
            output.put(None)
        threading.Thread(target=read_output, daemon=True).start()
        deadline = time.monotonic() + timeout_seconds; output_finished = False
        while True:
            try:
                line = output.get(timeout=0.1)
                if line is None: output_finished = True
                else:
                    clean = line.rstrip(); lines.append(clean); self._append(job_id, clean)
            except queue.Empty: pass
            if process.poll() is not None and output_finished: return process.returncode, lines
            if time.monotonic() > deadline:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill()
                raise TimeoutError("超过安全执行时限，当前步骤已终止")

    def _wait_for_same_port(self, params: dict[str, Any], timeout_seconds: int = 20) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            ports = serial_ports()
            port = next((item for item in ports if _usb_identity(item) == params.get("usb_identity")), None)
            if port:
                params["port"] = port["device"].upper()
                params["port_fingerprint"] = _port_fingerprint(port)
                return port
            time.sleep(0.25)
        raise RuntimeError("板卡复位后没有以同一 USB 身份重新出现")

    def _verify_firmware_runtime(self, job_id: str, params: dict[str, Any], timeout_seconds: int = 120) -> dict[str, Any]:
        self._wait_for_same_port(params)
        mqtt_command = _mqtt_provisioning_command()
        deadline = time.monotonic() + timeout_seconds
        booted = wifi_online = mqtt_online = False
        device_id = ""; ip_address = ""; rssi = None; next_query = 0.0; mqtt_configuration_sent = False
        connection: serial.Serial | None = None
        try:
            while time.monotonic() < deadline:
                if connection is None:
                    try:
                        connection = serial.Serial(params["port"], 115200, timeout=0.25, write_timeout=1,
                            dsrdtr=False, rtscts=False)
                        connection.dtr = False; connection.rts = False
                        self._append(job_id, "已重新打开 USB 串口，开始主动验收固件运行状态。")
                    except (OSError, serial.SerialException):
                        time.sleep(0.5); continue
                now = time.monotonic()
                if now >= next_query:
                    try: connection.write(b"info\n"); connection.flush()
                    except (OSError, serial.SerialException):
                        connection.close(); connection = None; continue
                    next_query = now + 2.0
                try: raw = connection.readline()
                except (OSError, serial.SerialException):
                    connection.close(); connection = None; continue
                if not raw: continue
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or line.startswith("ADC_FRAME "): continue
                if (line.startswith("[PASS]") or line.startswith("[FAIL]") or line.startswith("[INFO]")
                        or line.startswith("MQTT_STATE") or line.startswith("WIFI_STATUS")
                        or "Device ID" in line or "Firmware" in line):
                    self._append(job_id, "[板卡] " + line)
                if "[PASS] Firmware booted and USB serial is responding." in line or "=== ESP32-C3 SUPER MINI BOARD CHECK ===" in line:
                    booted = True
                match = re.search(r"Device ID\s*:\s*(pur-[0-9a-f]+)", line, re.IGNORECASE)
                if match: device_id = match.group(1).lower(); booted = True
                if booted and not mqtt_configuration_sent:
                    try:
                        connection.write(mqtt_command); connection.flush(); mqtt_configuration_sent = True
                        self._append(job_id, "已向新板写入本机 MQTT 配置（凭据已隐藏）。")
                    except (OSError, serial.SerialException):
                        connection.close(); connection = None; continue
                if line.startswith("[PASS] Wi-Fi ready."): wifi_online = True
                if line.startswith("WIFI_STATUS "):
                    fields = line.removeprefix("WIFI_STATUS ").split("|")
                    if len(fields) >= 3 and fields[0] and fields[1] not in {"", "0.0.0.0"}:
                        wifi_online = True; ip_address = fields[1]
                        try: rssi = int(fields[2])
                        except ValueError: pass
                if line == "MQTT_STATE online": mqtt_online = True
                if booted and wifi_online and mqtt_online and device_id: break
        finally:
            if connection is not None: connection.close()
        if not booted: raise RuntimeError("烧录完成，但未检测到目标固件启动")
        if not device_id: raise RuntimeError("固件已启动，但未读取到不可变设备 ID")
        if not wifi_online: raise RuntimeError("固件已启动，但在 120 秒内未连接固定 Wi-Fi")
        if not mqtt_online: raise RuntimeError("Wi-Fi 已连接，但 MQTT 在 120 秒内未上线")
        return {"device_id": device_id, "ip": ip_address, "rssi_dbm": rssi,
            "boot_verified": True, "wifi_verified": True, "mqtt_verified": True,
            "mqtt_configured": mqtt_configuration_sent, "blue_led_expected": "fast_blink"}

    def _run_provision(self, job_id: str, job: dict[str, Any]) -> None:
        params = job["params"]
        try:
            for key in ("identify", "build", "flash", "boot", "network"):
                self._set_provision_step(job_id, key, "waiting")
            self._set_provision_step(job_id, "identify", "running", "正在只读识别 ESP32-C3")
            self._recheck_before_run(job)
            code, check_lines = 2, []
            for attempt in range(1, 4):
                self._wait_for_same_port(params, 15)
                check_command = [sys.executable, str(ESPTOOL), "--chip", "esp32c3", "--port", params["port"], "chip_id"]
                code, check_lines = self._execute_process(job_id, check_command, 45)
                if code == 0: break
                self._append(job_id, f"芯片检查第 {attempt} 次未成功；等待同一 USB 设备稳定后重试。")
                time.sleep(1)
            if code != 0: raise RuntimeError("芯片通信检查失败，未执行烧录")
            mac_match = re.search(r"MAC:\s*([0-9a-f:]{17})", "\n".join(check_lines), re.IGNORECASE)
            chip_mac = mac_match.group(1).lower() if mac_match else ""
            self._set_provision_step(job_id, "identify", "passed", f"ESP32-C3 通信正常{f'，MAC {chip_mac}' if chip_mac else ''}")

            self._set_provision_step(job_id, "build", "running", "正在编译当前工程")
            source_hash = source_fingerprint()
            build_command = [sys.executable, "-m", "platformio", "run", "-e", params["environment"]]
            code, _ = self._execute_process(job_id, build_command, 360)
            if code != 0 or not FIRMWARE.exists(): raise RuntimeError("固件编译失败，未执行烧录")
            if source_fingerprint() != source_hash: raise RuntimeError("编译期间源码发生变化，未执行烧录")
            firmware_hash = _file_sha256(FIRMWARE); firmware_bytes = FIRMWARE.stat().st_size
            self._set_provision_step(job_id, "build", "passed", f"固件 {firmware_bytes} 字节，SHA-256 {firmware_hash[:12]}…")

            self._wait_for_same_port(params)
            self._set_provision_step(job_id, "flash", "running", "正在写入并由 esptool 校验 Flash")
            upload_command = [sys.executable, "-m", "platformio", "run", "-e", params["environment"],
                "-t", "upload", "--upload-port", params["port"]]
            code, _ = self._execute_process(job_id, upload_command, 360)
            if code != 0: raise RuntimeError("Flash 写入或校验失败")
            self._set_provision_step(job_id, "flash", "passed", "写入完成，上传工具校验通过")

            self._set_provision_step(job_id, "boot", "running", "等待板卡复位并启动目标固件")
            runtime = self._verify_firmware_runtime(job_id, params)
            if chip_mac:
                expected_id = "pur-" + chip_mac.replace(":", "")
                if runtime["device_id"] != expected_id:
                    raise RuntimeError("烧录前后设备身份不一致，验收停止")
            self._set_provision_step(job_id, "boot", "passed", f"目标固件启动，设备 ID {runtime['device_id']}")
            self._set_provision_step(job_id, "network", "passed",
                f"Wi-Fi 与 MQTT 已上线，IP {runtime['ip'] or '已分配'}，蓝灯应快速闪烁")
            with self._lock:
                job["result"].update(runtime, firmware_sha256=firmware_hash, firmware_bytes=firmware_bytes,
                    chip_mac=chip_mac, write_performed=True, accepted=True)
            self._finish(job_id, 0, "新板自动检测、烧录和联网验收全部通过")
        except Exception as error:
            with self._lock:
                steps = job.get("result", {}).get("steps", [])
                running = next((item for item in steps if item.get("status") == "running"), None)
                if running: running.update(status="failed", detail=str(error), updated_at_ms=int(time.time()*1000))
                job.setdefault("result", {})["accepted"] = False
            self._append(job_id, f"验收失败：{error}")
            self._finish(job_id, 2, str(error))

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]; job.update(status="running", started_at_ms=int(time.time()*1000), summary="正在执行")
            command = None if job["kind"] == "provision_board" else self._command(job)
        if job["kind"] == "provision_board":
            self._run_provision(job_id, job); return
        if job["kind"] == "board_check" and not ESPTOOL.exists(): self._finish(job_id, 2, "缺少 ESP 芯片检查工具"); return
        env = os.environ.copy(); env.setdefault("PLATFORMIO_CORE_DIR", str(Path.home() / ".platformio"))
        try:
            self._recheck_before_run(job)
            self._append(job_id, f"安全策略：{TOOL_POLICY[job['kind']]['description']}")
            self._append(job_id, "> " + subprocess.list2cmdline(command))
            process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            assert process.stdout
            deadline = time.monotonic() + TOOL_POLICY[job["kind"]]["timeout_seconds"]
            output: queue.Queue[str | None] = queue.Queue()
            def read_output() -> None:
                assert process.stdout
                for output_line in process.stdout: output.put(output_line)
                output.put(None)
            threading.Thread(target=read_output, daemon=True).start()
            output_finished = False
            while True:
                try:
                    line = output.get(timeout=0.1)
                    if line is None: output_finished = True
                    else: self._append(job_id, line)
                except queue.Empty: pass
                if process.poll() is not None and output_finished: break
                if time.monotonic() > deadline:
                    process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired: process.kill()
                    raise TimeoutError("超过安全执行时限，任务已终止")
            code = process.returncode
            if code == 0: self._record_success(job_id, job)
            summaries = {"board_check": "板卡芯片通信检查通过（未写入 Flash）",
                "build_firmware": "固件编译完成（仅生成本机产物）",
                "flash_firmware": "固件写入完成，上传工具校验通过", "mqtt_stress": "隔离 MQTT 压力测试完成"}
            self._finish(job_id, code, summaries[job["kind"]] if code == 0 else "执行失败，未宣告通过，请查看日志")
        except Exception as error:
            self._append(job_id, f"{type(error).__name__}: {error}")
            self._finish(job_id, 2, str(error) if isinstance(error, (RuntimeError, TimeoutError)) else "无法启动工程工具")

    def _record_success(self, job_id: str, job: dict[str, Any]) -> None:
        now = time.time()
        if job["kind"] == "board_check":
            self._board_checks[job["params"]["port_fingerprint"]] = {"completed_at": now, "job_id": job_id}
            job["result"] = {"write_performed": False, "port_fingerprint": job["params"]["port_fingerprint"]}
        elif job["kind"] == "build_firmware":
            if not FIRMWARE.exists(): raise RuntimeError("编译命令成功，但未找到 firmware.bin")
            record = {"completed_at": now, "job_id": job_id, "source_sha256": source_fingerprint(),
                "firmware_sha256": _file_sha256(FIRMWARE), "firmware_bytes": FIRMWARE.stat().st_size}
            self._last_build = record; job["result"] = dict(record)
        elif job["kind"] == "flash_firmware":
            job["result"] = {"write_performed": True, "firmware_sha256": job["params"]["firmware_sha256"], "port": job["params"]["port"]}

    def _finish(self, job_id: str, code: int, summary: str) -> None:
        with self._lock:
            self._jobs[job_id].update(status="succeeded" if code == 0 else "failed", completed_at_ms=int(time.time()*1000), exit_code=code, summary=summary)
            if self._active_job_id == job_id: self._active_job_id = None


tools = EngineeringTools()
