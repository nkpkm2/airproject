const api = 'http://127.0.0.1:8780/api/v1';
window.name = 'air-wifi-console';
if (new URLSearchParams(location.search).has('embedded')) document.body.classList.add('embedded');

const keys = ['low', 'medium', 'high', 'auto'];
const labels = {
  'led.blink': '闪烁蓝色 LED', 'debug.configure': '调整 ADC 调试流',
  'device.reboot': '远程重启板卡', 'opto.power.on': '电源光耦导通',
  'opto.power.off': '电源光耦断开', 'opto.mode.pulse': '模式光耦脉冲', unknown: '未知命令'
};
const $ = id => document.getElementById(id);
const state = {
  selected: '', histories: Object.fromEntries(keys.map(key => [key, Array(140).fill(0)])),
  streamHz: 0, telemetryAt: 0, commands: [], stressRunning: false
};

function toast(message) {
  const node = $('toast'); node.textContent = message; node.classList.add('show');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => node.classList.remove('show'), 3500);
}
function duration(ms) { const seconds = Math.max(0, Math.floor(ms / 1000)); return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`; }
function timeText(timestamp) { return timestamp ? new Date(timestamp).toLocaleTimeString('zh-CN', { hour12: false }) : '—'; }
function commandLabel(item) { return labels[item.type] || item.type || labels.unknown; }
function setSignal(rssi) {
  if (!Number.isFinite(rssi)) return;
  const pct = Math.max(4, Math.min(100, ((rssi + 95) / 60) * 100));
  $('wifi-fill').style.width = `${pct}%`; $('rssi-value').textContent = rssi; $('rssi').value = `${rssi} dBm`;
  $('signal-label').textContent = rssi >= -55 ? '强' : rssi >= -70 ? '良好' : rssi >= -82 ? '一般' : '弱';
}
function draw(key) {
  const card = document.querySelector(`[data-adc="${key}"]`), canvas = card?.querySelector('canvas'); if (!canvas) return;
  const c = canvas.getContext('2d'), { width, height } = canvas, samples = state.histories[key];
  c.fillStyle = '#050b05'; c.fillRect(0, 0, width, height); c.strokeStyle = '#17451d'; c.lineWidth = 1;
  for (let x = 0; x < width; x += 26) { c.beginPath(); c.moveTo(x + .5, 0); c.lineTo(x + .5, height); c.stroke(); }
  for (let y = 0; y < height; y += 17) { c.beginPath(); c.moveTo(0, y + .5); c.lineTo(width, y + .5); c.stroke(); }
  c.strokeStyle = '#22ef48'; c.lineWidth = 1.5; c.beginPath();
  samples.forEach((mv, i) => { const x = i * width / (samples.length - 1), y = height - 2 - Math.min(height - 4, mv / 3300 * (height - 4)); i ? c.lineTo(x, y) : c.moveTo(x, y); });
  c.stroke(); c.fillStyle = '#8cff9b'; c.font = '10px monospace'; c.fillText('0–3.3 V · newest →', 5, 12);
}
function updateAdc(values) {
  if (!Array.isArray(values) || values.length !== 4) return;
  values.forEach((mv, i) => { const key = keys[i], card = document.querySelector(`[data-adc="${key}"]`); state.histories[key].push(mv); state.histories[key].shift(); card.querySelector('.adc-value strong').textContent = (mv / 1000).toFixed(3); card.querySelector('.lamp').textContent = '远程样本'; draw(key); });
  $('sensors-tag').textContent = `${state.streamHz || '低'} Hz 远程采样`;
}
function renderOpto(opto = {}) {
  ['power', 'mode'].forEach(key => { const article = document.querySelector(`[data-opto="${key}"]`), on = !!opto[key]; if (!article) return; article.querySelector('.opto-status').textContent = on ? '已导通' : '已断开'; article.querySelector('.switch').className = `switch ${on ? 'on' : 'off'}`; });
  $('opto-tag').textContent = '状态已上报';
}
function renderCommands(commands = []) {
  state.commands = commands;
  const pending = commands.filter(item => ['sending', 'pending'].includes(item.status)).slice(-3);
  $('command-tray').replaceChildren(...pending.map(item => { const card = document.createElement('div'); card.className = 'pending-card'; card.innerHTML = `<span class="pending-spinner"></span><div><b>${commandLabel(item)}</b><small>已发送请求 · 等待设备回执 · ${duration(Date.now() - item.sent_at_ms)}</small></div>`; return card; }));
  const history = [...commands].reverse().slice(0, 20);
  if (!history.length) { $('command-history').innerHTML = '<p class="empty-row">尚未发送命令</p>'; return; }
  $('command-history').replaceChildren(...history.map(item => {
    const row = document.createElement('div'); row.className = 'command-row'; let result = '<b class="waiting">已发请求 · 等待回执</b>';
    if (item.status === 'acknowledged') result = '<b class="success">已收到回复 · 执行成功</b>';
    if (item.status === 'failed') result = `<b class="failure">执行失败 · ${item.result || item.error || '设备拒绝'}</b>`;
    if (item.status === 'timeout') result = '<b class="failure">回执超时 · 已记录失败</b>';
    row.innerHTML = `<time>${timeText(item.sent_at_ms)}</time><span><b>${commandLabel(item)}</b><small>${String(item.request_id || '').slice(0, 12)}</small></span>${result}`; return row;
  }));
}
function showDevice(d) {
  if (!d) return; state.selected = d.device_id;
  const connection = d.connection_state || (d.online ? 'online' : 'offline'), online = connection !== 'offline', degraded = connection === 'degraded';
  const wifi = d.state?.wifi || {}, heartbeat = d.heartbeat || {}, telemetry = d.telemetry;
  $('device-title').textContent = d.device_id; $('device-id').value = d.device_id;
  $('availability').textContent = degraded ? '通信抖动' : online ? '在线' : '离线'; $('availability').className = `tag ${online && !degraded ? 'good' : 'muted'}`;
  $('connection-dot').className = `dot ${connection}`; $('connection-text').textContent = degraded ? '心跳延迟，暂不判离线' : online ? '设备已通过 MQTT 在线' : '设备离线 / 等待心跳';
  $('ip').value = wifi.ip || heartbeat.ip || '未联网'; const ssid = wifi.ssid; $('ssid').value = ssid || (online ? '已连接（SSID 未上报）' : '未上报');
  $('access-point').value = wifi.bssid ? `${wifi.bssid} / CH ${wifi.channel ?? '—'}` : '—';
  $('wifi-counters').value = `${heartbeat.disconnects ?? wifi.disconnects ?? 0} / ${heartbeat.radio_recoveries ?? wifi.radio_recoveries ?? 0}`;
  const rssi = heartbeat.rssi_dbm ?? wifi.rssi_dbm; setSignal(rssi); $('wifi-name').textContent = ssid || (online ? '局域网已连接' : '等待 Wi-Fi 连接');
  $('wifi-status').textContent = degraded ? '心跳延迟 · 保持在线' : online ? '在线 · DHCP 地址有效' : '离线';
  $('wifi-detail').textContent = online ? `IP ${wifi.ip || heartbeat.ip || '—'} · 心跳 ${duration(d.heartbeat_age_ms || 0)}前` : '板卡上电后会自动尝试保存的 Wi-Fi';
  $('uptime').value = typeof heartbeat.uptime_ms === 'number' ? duration(heartbeat.uptime_ms) : typeof d.state?.uptime_ms === 'number' ? duration(d.state.uptime_ms) : '—';
  $('ack').textContent = d.ack ? `${d.ack.result || '—'} ${d.ack.request_id ? '· 已收到回复' : ''}` : '等待命令'; $('diagnostic').textContent = d.diagnostic?.event || '—';
  $('measured-rate').textContent = `${d.telemetry_stats?.received_hz ?? 0} Hz`; $('sequence-gaps').textContent = d.telemetry_stats?.sequence_gaps ?? 0;
  renderCommands(d.commands || []); renderOpto(d.state?.opto || telemetry?.opto || {}); if (d.state?.adc_mv) updateAdc(d.state.adc_mv);
  if (telemetry?.adc_mv) { state.telemetryAt = Date.now() - (d.last_seen_age_ms || 0); updateAdc(telemetry.adc_mv); }
}
function renderDevices(devices) {
  $('count').textContent = devices.filter(device => device.online).length;
  $('devices').replaceChildren(...devices.map(d => { const connection = d.connection_state || (d.online ? 'online' : 'offline'), b = document.createElement('button'); b.className = `device-card ${d.device_id === state.selected ? 'selected' : ''}`; b.innerHTML = `<span class="status-dot ${connection}"></span><span><b>${d.device_id}</b><small>${connection === 'degraded' ? '心跳延迟 · ' : ''}${d.state?.wifi?.ip || '未上报 IP'}</small></span><span class="chevron">›</span>`; b.onclick = () => { state.selected = d.device_id; renderDevices(devices); showDevice(d); }; return b; }));
}
async function load() {
  try {
    const [service, response] = await Promise.all([fetch('http://127.0.0.1:8780/health'), fetch(`${api}/devices`)]), devices = await response.json();
    $('service-dot').className = `dot ${service.ok ? 'online' : 'offline'}`; $('service-status').textContent = service.ok ? '本机 MQTT 服务运行中' : '控制服务不可用'; renderDevices(devices); showDevice(devices.find(d => d.device_id === state.selected) || devices[0]);
  } catch (_) { $('service-dot').className = 'dot offline'; $('service-status').textContent = '控制服务不可用'; }
}
async function command(type, extra = {}) {
  if (!state.selected) { toast('请先等待设备上线'); return null; }
  if (state.commands.filter(item => ['sending', 'pending'].includes(item.status)).length >= 3) { toast('已有 3 条命令等待回执，请稍候'); return null; }
  const temporary = { request_id: `local-${Date.now()}`, type, status: 'sending', sent_at_ms: Date.now() }; renderCommands([...state.commands, temporary]);
  try {
    const response = await fetch(`${api}/devices/${state.selected}/commands`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ type, ...extra }) }), result = await response.json();
    if (!response.ok) throw new Error(result.error || '请求失败'); toast('已发出请求，正在等待设备回执'); await new Promise(resolve => setTimeout(resolve, 300)); await load(); return result.request_id;
  } catch (error) { temporary.status = 'failed'; temporary.error = error.message; temporary.completed_at_ms = Date.now(); renderCommands([...state.commands.filter(item => item.request_id !== temporary.request_id), temporary]); toast(`命令发送失败：${error.message}`); return null; }
}
async function setStream(hz, durationSeconds = 60) {
  state.streamHz = hz; const request = await command('debug.configure', { adc_stream_hz: hz, expires_in_s: durationSeconds }); if (!request) return false;
  $('diagnostic-tag').textContent = hz ? `${hz} Hz · ${durationSeconds} 秒` : '已关闭'; $('stream-rate').textContent = `${hz} Hz`; return true;
}
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function stressTest() {
  if (state.stressRunning) return; state.stressRunning = true; $('stress').disabled = true;
  for (const hz of [10, 20, 30, 40, 50, 60, 120]) { $('diagnostic-tag').textContent = `压测 ${hz} Hz`; if (!await setStream(hz, 12)) break; await wait(7000); await load(); }
  await setStream(0, 1); $('diagnostic-tag').textContent = '阶梯压测完成'; $('stress').disabled = false; state.stressRunning = false;
}

$('refresh').onclick = load; $('blink').onclick = () => command('led.blink'); $('adc').onclick = () => setStream(20, 60); $('stop').onclick = () => setStream(0, 1); $('stress').onclick = stressTest; $('reboot').onclick = () => command('device.reboot');
document.querySelectorAll('[data-stream-hz]').forEach(button => button.onclick = () => setStream(Number(button.dataset.streamHz), Number(button.dataset.streamHz) === 120 ? 30 : 60));
document.querySelectorAll('[data-command]').forEach(button => button.onclick = () => command(button.dataset.command));
keys.forEach(draw); load(); setInterval(load, 1000); setInterval(() => { $('telemetry-age').textContent = state.telemetryAt ? `${duration(Date.now() - state.telemetryAt)}前` : '—'; renderCommands(state.commands); }, 1000);
