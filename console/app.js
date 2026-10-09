const $ = id => document.getElementById(id);
const adcKeys = ['low', 'medium', 'high', 'auto'];
const ui = { port: null, reader: null, writer: null, connected: false, manualDisconnect: false, partial: '', lines: [], wifiConnectedAt: null, adc: {}, deviceId: '', calibrationConfig: null, calibrationConfigDevice: '', lockId: crypto.randomUUID?.() || String(Date.now()) };
const lockKey = 'air-control-usb-session';
window.name = 'air-usb-console';
if (new URLSearchParams(location.search).has('embedded')) document.body.classList.add('embedded');
const saved = JSON.parse(localStorage.getItem('purifier-console-device') || '{}');
adcKeys.forEach(key => { ui.adc[key] = { history: Array(140).fill(0), hasSample: false }; });
const calibration = CalibrationSampling.create({
  transport: 'usb_serial',
  getDeviceId: () => ui.deviceId || $('mac-input').value || 'unknown',
  getRequestedRateHz: () => 50,
  transportMetadata: { adc_sample_interval_ms: 5, serial_adc_frame_interval_ms: 20, firmware_sequence_available: false },
});
const calibrationAnalyzer = CalibrationAnalyzer.create({ transport: 'usb_serial', getDeviceId: () => ui.deviceId || $('mac-input').value || 'unknown', getRequestedRateHz: () => 50 });

function toast(message) { const node = $('toast'); node.textContent = message; node.classList.add('show'); clearTimeout(toast.timer); toast.timer = setTimeout(() => node.classList.remove('show'), 3200); }
function lockRecord() { try { return JSON.parse(localStorage.getItem(lockKey) || 'null'); } catch (_) { return null; } }
function lockHeldElsewhere() { const lock = lockRecord(); return lock && lock.id !== ui.lockId && Date.now() - lock.at < 8000; }
function claimLock() { if (lockHeldElsewhere()) return false; localStorage.setItem(lockKey, JSON.stringify({ id: ui.lockId, at: Date.now() })); return true; }
function refreshLock() { if (ui.connected) localStorage.setItem(lockKey, JSON.stringify({ id: ui.lockId, at: Date.now() })); }
function releaseLock() { const lock = lockRecord(); if (lock?.id === ui.lockId) localStorage.removeItem(lockKey); }
setInterval(refreshLock, 2000);
window.addEventListener('beforeunload', releaseLock);
window.addEventListener('storage', event => { if (event.key === lockKey && !ui.connected && lockHeldElsewhere()) $('connect-button').textContent = '串口已被另一调试台占用'; });

function setSerialControls(enabled) { document.querySelectorAll('#info-button,#blink-button,#reboot-button,#adc-test-button,[data-opto-command],#save-button').forEach(button => { button.disabled = !enabled; }); if (typeof calibrationWizard !== 'undefined') calibrationWizard.render(); }
function formatDuration(seconds) { if (seconds < 60) return `${seconds} 秒`; return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`; }
function refreshIdentity() { const alias = $('alias-input').value || '未命名设备'; $('device-title').textContent = alias; $('side-name').textContent = alias; $('side-room').textContent = `${$('room-input').value || '未分配房间'} · ${ui.connected ? 'USB 已连接' : 'USB 未连接'}`; }
function setUsbDisconnected(detail = 'USB 串口已断开；页面数据不再刷新。') { ui.wifiConnectedAt = null; $('wifi-status').textContent = 'USB 已断开'; $('wifi-detail').textContent = detail; $('ip-input').value = '无实时 USB 数据'; $('rssi-value').textContent = '—'; }
function setConnection(connected) { ui.connected = connected; $('connection-dot').className = `dot ${connected ? 'neutral' : 'offline'}`; $('connection-text').textContent = connected ? 'USB 串口已连接' : '未连接 USB 串口'; $('connect-button').textContent = connected ? '断开串口' : lockHeldElsewhere() ? '串口已被另一调试台占用' : '连接 USB 串口'; setSerialControls(connected); if (!connected) { ui.calibrationConfig = null; ui.calibrationConfigDevice = ''; setUsbDisconnected(); } refreshIdentity(); }
function setWifiConnecting(ssid, phase = '正在自动连接') { ui.wifiConnectedAt = null; $('wifi-status').textContent = phase; $('wifi-name').textContent = ssid; $('wifi-detail').textContent = '固定网络由固件自动扫描、认证和重试；无需手动操作。'; }
function setSignal(rssi) { const pct = Math.max(4, Math.min(100, ((rssi + 95) / 60) * 100)); $('wifi-fill').style.width = `${pct}%`; $('signal-label').textContent = rssi >= -55 ? '强' : rssi >= -70 ? '良好' : rssi >= -82 ? '一般' : '弱'; }
function send(command) { if (!ui.writer) { toast('USB 串口未连接；操作已禁用'); return false; } ui.writer.write(new TextEncoder().encode(`${command}\n`)).catch(() => toast('串口写入失败')); return true; }
function updateLampIndicator(key, mv) { const card = document.querySelector(`[data-adc="${key}"]`); if (!card || !globalThis.LampIndicator) return; ui.adc[key].lampState = LampIndicator.update(card, key, mv, ui.calibrationConfig, ui.adc[key].lampState || 'unknown'); }
function renderCalibratedLamps() { adcKeys.forEach(key => { const latest = ui.adc[key].history[ui.adc[key].history.length - 1]; if (ui.adc[key].hasSample) updateLampIndicator(key, latest); else LampIndicator.update(document.querySelector(`[data-adc="${key}"]`), key, NaN, ui.calibrationConfig); }); }
async function loadDeviceCalibration(deviceId) { if (!deviceId || deviceId === 'unknown' || ui.calibrationConfigDevice === deviceId) return; ui.calibrationConfigDevice = deviceId; try { const response = await fetch(`http://127.0.0.1:8780/api/v1/devices/${encodeURIComponent(deviceId)}/calibration`); const result = await response.json(); ui.calibrationConfig = result.config || null; renderCalibratedLamps(); } catch (_) { ui.calibrationConfig = null; renderCalibratedLamps(); } }
async function saveDeviceCalibration(config) { const deviceId = ui.deviceId || $('mac-input').value.trim(); if (!deviceId || deviceId === 'unknown') throw new Error('尚未读取到不可变设备 ID'); const response = await fetch(`http://127.0.0.1:8780/api/v1/devices/${encodeURIComponent(deviceId)}/calibration`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ config }) }); const result = await response.json(); if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`); ui.calibrationConfig = result.config; ui.calibrationConfigDevice = deviceId; renderCalibratedLamps(); }

async function connectSerial() {
  if (ui.connected) return disconnectSerial();
  if (!('serial' in navigator)) return toast('此浏览器不支持 Web Serial；请使用 Chrome 或 Edge。');
  if (!claimLock()) return toast('COM6 已被另一个串口调试页占用；请先在那个页面断开。');
  try {
    ui.port = await navigator.serial.requestPort(); await ui.port.open({ baudRate: 115200 }); ui.writer = ui.port.writable.getWriter(); ui.manualDisconnect = false; ui.partial = ''; ui.lines = []; setConnection(true); toast('USB 已连接，正在读取板卡真实联网状态…'); readSerial(); send('info');
  } catch (error) { releaseLock(); ui.port = null; setConnection(false); toast(`无法连接串口：${error.message}`); }
}
async function disconnectSerial() {
  ui.manualDisconnect = true;
  const reader = ui.reader, writer = ui.writer, port = ui.port;
  ui.connected = false;
  if (reader) { try { await reader.cancel(); } catch (_) {} try { reader.releaseLock(); } catch (_) {} }
  if (writer) { try { writer.releaseLock(); } catch (_) {} }
  if (port) { try { await port.close(); } catch (_) {} }
  ui.reader = ui.writer = ui.port = null;
  releaseLock(); setConnection(false); toast('USB 串口已断开；板卡仍会独立保持 Wi‑Fi。');
}
async function readSerial() {
  if (!ui.port?.readable || ui.reader) return;
  const decoder = new TextDecoder();
  try {
    ui.reader = ui.port.readable.getReader();
    while (ui.connected && ui.port.readable) { const { value, done } = await ui.reader.read(); if (done) break; if (value) consume(decoder.decode(value, { stream: true })); }
  } catch (error) { if (ui.connected && !ui.manualDisconnect) toast(`串口读取暂时中断：${error.message}`); }
  finally {
    if (ui.reader) { try { ui.reader.releaseLock(); } catch (_) {} ui.reader = null; }
    if (ui.connected && !ui.manualDisconnect) setTimeout(readSerial, 700);
  }
}
function consume(chunk) { const pieces = (ui.partial + chunk).replace(/\r/g, '').split('\n'); ui.partial = pieces.pop(); pieces.filter(Boolean).forEach(parseLine); }
function parseLine(line) {
  ui.lines.push(line); if (ui.lines.length > 180) ui.lines.shift();
  parseInfo(ui.lines.join('\n')); parseWifi(line); parseMqtt(line); parseAdc(line); parseOptocoupler(line); parseAdcTest(line);
}
function parseInfo(text) {
  const chip = text.match(/Chip model\s*:\s*([^,\n]+)/), flash = text.match(/Flash\s*:\s*(?:read-error, )?(\d+) bytes/), heap = text.match(/Heap free\/min\s*:\s*(\d+)/), mac = text.match(/MAC address\s*:\s*([0-9A-F:]{17})/i);
  if (chip) $('chip-value').textContent = chip[1].trim(); if (flash) $('flash-value').textContent = `${(Number(flash[1]) / 1048576).toFixed(0)} MB`; if (heap) $('heap-value').textContent = `${Math.round(Number(heap[1]) / 1024)} KB`; if (mac) { ui.deviceId = mac[1].toUpperCase(); $('mac-input').value = ui.deviceId; loadDeviceCalibration(ui.deviceId); }
}
function parseWifi(line) {
  const status = line.match(/^WIFI_STATUS\s+([^|]+)\|([^|]+)\|(-?\d+)/);
  if (status) { const [, ssid, ip, raw] = status, rssi = Number(raw); if (ui.wifiConnectedAt === null) ui.wifiConnectedAt = Date.now(); $('wifi-status').textContent = `已连接 ${formatDuration(Math.floor((Date.now() - ui.wifiConnectedAt) / 1000))}`; $('wifi-name').textContent = ssid; $('wifi-detail').textContent = `IP ${ip} · 信号每 200 ms 更新`; $('ip-input').value = ip; $('rssi-value').textContent = rssi; setSignal(rssi); return; }
  const progress = line.match(/^WIFI_PROGRESS\s+([^|]+)\|(.+)$/);
  if (progress) { const [, phase, ssid] = progress; if (phase === 'online') return; setWifiConnecting(ssid, phase === 'retry-wait' ? '自动重试等待中' : phase === 'associating' ? '正在认证…' : '正在自动连接…'); return; }
  const association = line.match(/^\[TEST\] Starting fixed Wi-Fi association(?: #\d+)?:\s*(.+)$/);
  if (association) { setWifiConnecting(association[1], '正在认证…'); return; }
  if (/WIFI_DISCONNECT|association timed out|retry scheduled/.test(line)) { $('wifi-status').textContent = '自动重试等待中'; $('wifi-detail').textContent = '本轮关联未成功；固件将自动退避后重试。'; }
}
function parseMqtt(line) { const match = line.match(/^MQTT_STATE\s+(online|offline|error|unconfigured|connecting)$/); if (!match) return; const value = match[1]; $('mqtt-value').textContent = value === 'online' ? '在线 · 正在上报' : value === 'connecting' ? '正在连接…' : value === 'error' ? '连接错误 · 自动恢复中' : value === 'unconfigured' ? '尚未配置' : '离线 · 自动恢复中'; $('mqtt-value').className = value === 'online' ? 'state-good' : value === 'connecting' ? '' : 'state-bad'; }
function parseAdc(line) { const frame = line.match(/^ADC_FRAME\s+(\d+)\|(\d+)\|(\d+)\|(\d+)$/); if (!frame) return; const values = adcKeys.map((key, i) => Number(frame[i + 1])); adcKeys.forEach((key, i) => updateAdc(key, values[i])); const sample = { adcMv: values, receivedAtMs: Date.now(), requestedRateHz: 50, deviceId: ui.deviceId || $('mac-input').value || 'unknown' }; calibration.append(sample); calibrationAnalyzer.ingest(sample); $('sensors-tag').textContent = '5 ms 采样 · 20 ms 串口帧'; }
function updateAdc(key, mv) { const sample = ui.adc[key], card = document.querySelector(`[data-adc="${key}"]`); sample.history.push(mv); sample.history.shift(); sample.hasSample = true; card.querySelector('.adc-value strong').textContent = (mv / 1000).toFixed(3); card.querySelector('.lamp').textContent = ui.calibrationConfig ? '已标定判定' : '未标定'; updateLampIndicator(key, mv); drawScope(key); }
function drawScope(key) { const card = document.querySelector(`[data-adc="${key}"]`), canvas = card?.querySelector('.adc-scope'); if (!canvas) return; const c = canvas.getContext('2d'), { width, height } = canvas, samples = ui.adc[key].history; c.fillStyle = '#050b05'; c.fillRect(0, 0, width, height); c.strokeStyle = '#17451d'; for (let x = 0; x < width; x += 26) { c.beginPath(); c.moveTo(x + .5, 0); c.lineTo(x + .5, height); c.stroke(); } for (let y = 0; y < height; y += 17) { c.beginPath(); c.moveTo(0, y + .5); c.lineTo(width, y + .5); c.stroke(); } c.strokeStyle = '#22ef48'; c.lineWidth = 1.5; c.beginPath(); samples.forEach((mv, i) => { const x = i * width / (samples.length - 1), y = height - 2 - Math.min(height - 4, mv / 3300 * (height - 4)); i ? c.lineTo(x, y) : c.moveTo(x, y); }); c.stroke(); }
function parseOptocoupler(line) { const m = line.match(/^OPTO_STATUS\s+(power|mode)\|(\d+)\|(0|1)$/); if (!m) return; const [, key, gpio, raw] = m, on = raw === '1', article = document.querySelector(`[data-opto="${key}"]`); article.querySelector('.opto-status').textContent = `GPIO${gpio} · ${on ? '已导通' : '已断开'}`; article.querySelector('.switch').className = `switch ${on ? 'on' : 'off'}`; }
function parseAdcTest(line) { const m = line.match(/^ADC_TEST\s+samples=(\d+)\|rate_hz=([\d.]+)\|read_us=([\d.]+)\|overruns=(\d+)\|frame_bps=([\d.]+)$/); if (m) $('adc-test-result').textContent = `10 秒结果：${m[1]} 轮 · ${m[2]} Hz · 每轮 ${m[3]} µs · 超时 ${m[4]} 次 · 串口帧 ${m[5]} B/s`; }

const calibrationWizard = CalibrationWizard.create({
  transport: 'usb_serial',
  recorder: calibration,
  analyzer: calibrationAnalyzer,
  getDeviceId: () => ui.deviceId || $('mac-input').value || 'unknown',
  getRequestedRateHz: () => 50,
  isReady: () => ui.connected,
  notReadyMessage: '请先连接 USB 串口，再开始自动标定。',
  startMessage: 'USB 自动标定已开始；请保持低速，首段正在采样约 3 秒。',
  saveCalibration: saveDeviceCalibration,
  transportMetadata: { adc_sample_interval_ms: 5, serial_adc_frame_interval_ms: 20, firmware_sequence_available: false },
  toast,
});

['alias', 'purifier', 'room'].forEach(key => { if (saved[key]) $(`${key}-input`).value = saved[key]; });
$('save-button').onclick = () => { localStorage.setItem('purifier-console-device', JSON.stringify({ alias: $('alias-input').value.trim(), purifier: $('purifier-input').value.trim(), room: $('room-input').value.trim() })); refreshIdentity(); $('save-state').textContent = '资料已保存到本浏览器。'; };
['alias-input', 'room-input'].forEach(id => $(id).addEventListener('input', refreshIdentity));
$('connect-button').onclick = connectSerial; $('info-button').onclick = () => send('info'); $('blink-button').onclick = () => send('blink'); $('reboot-button').onclick = () => { if (confirm('确认重启当前 ESP32-C3？')) { $('wifi-status').textContent = '板卡正在重启…'; $('wifi-detail').textContent = 'USB CDC 会短暂重置；页面将自动继续读取。'; send('reboot'); } }; $('adc-test-button').onclick = () => send('adc test'); document.querySelectorAll('[data-opto-command]').forEach(button => button.onclick = () => send(button.dataset.optoCommand));
navigator.serial?.addEventListener('disconnect', () => { if (ui.connected) disconnectSerial(); });
refreshIdentity(); setConnection(false); adcKeys.forEach(drawScope); renderCalibratedLamps();
