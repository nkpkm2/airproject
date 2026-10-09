const toolApi = 'http://127.0.0.1:8780/api/v1';
const popup = document.getElementById('menu-popup');
const statusCells = [...document.querySelectorAll('.control-status span')];
const frameByName = Object.fromEntries([...document.querySelectorAll('[data-panel]')].map(frame => [frame.dataset.panel, frame]));
let currentEngineeringJob = null;
let jobPollTimer = null;
let latestEngineeringJob = null;

const provisionStepLabels = ['识别并检查芯片', '编译并锁定固件', '写入并校验 Flash', '验证固件启动', '验证 Wi-Fi 与 MQTT'];

function setShellStatus(message) {
  statusCells[0].textContent = message;
  clearTimeout(setShellStatus.timer);
  setShellStatus.timer = setTimeout(() => { statusCells[0].textContent = '就绪'; }, 5000);
}

async function toolJson(path, options = {}) {
  const response = await fetch(`${toolApi}${path}`, { headers: { 'Content-Type': 'application/json' }, ...options });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

function switchWorkspace(name) {
  selectView(name);
  setShellStatus(`已切换到${{ fleet: '家庭总览', serial: '串口调试', wifi: 'Wi‑Fi 调试' }[name]}`);
}

function frameDocument(name) {
  try { return frameByName[name]?.contentDocument || null; } catch (_) { return null; }
}

function clickFrame(name, selector) {
  const element = frameDocument(name)?.querySelector(selector);
  if (!element) { setShellStatus('当前功能尚未准备好'); return false; }
  element.click();
  return true;
}

function download(name, data, type = 'application/json') {
  const blob = new Blob([typeof data === 'string' ? data : JSON.stringify(data, null, 2)], { type });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob); link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

async function exportSnapshot() {
  const [devices, catalog, environment, jobs] = await Promise.all([
    toolJson('/devices'), toolJson('/catalog'), toolJson('/tools/environment'), toolJson('/tools/jobs'),
  ]);
  download(`air-control-snapshot-${new Date().toISOString().replaceAll(':', '-')}.json`, {
    format: 'air-control-snapshot-v1', exported_at: new Date().toISOString(), devices, catalog, environment, engineering_jobs: jobs,
  });
  setShellStatus('完整状态快照已导出');
}

async function exportCommandLog() {
  const devices = await toolJson('/devices');
  const rows = [['device_id', 'sent_at', 'type', 'status', 'result', 'request_id']];
  devices.forEach(device => (device.commands || []).forEach(command => rows.push([
    device.device_id, new Date(command.sent_at_ms || 0).toISOString(), command.type || '', command.status || '', command.result || command.error || '', command.request_id || '',
  ])));
  const csv = rows.map(row => row.map(value => `"${String(value).replaceAll('"', '""')}"`).join(',')).join('\r\n');
  download(`air-control-commands-${Date.now()}.csv`, '\ufeff' + csv, 'text/csv;charset=utf-8');
  setShellStatus('命令与回执日志已导出');
}

async function backupConfiguration() {
  const catalog = await toolJson('/catalog');
  download(`air-control-backup-${Date.now()}.json`, {
    format: 'air-control-backup-v1', exported_at: new Date().toISOString(), catalog,
    browser: {
      default_view: localStorage.getItem('air-default-view') || 'fleet',
      purifier_plan_positions: JSON.parse(localStorage.getItem('purifier-plan-positions') || '{}'),
    },
  });
  setShellStatus('配置备份已导出');
}

const restoreInput = document.getElementById('restore-file');
restoreInput.addEventListener('change', async () => {
  const file = restoreInput.files?.[0]; restoreInput.value = '';
  if (!file) return;
  try {
    const data = JSON.parse(await file.text());
    if (data.format !== 'air-control-backup-v1' || !data.catalog) throw new Error('不是有效的空气管理配置备份');
    if (!confirm('恢复配置将替换房间、分组和设备资料。是否继续？')) return;
    await toolJson('/catalog/restore', { method: 'POST', body: JSON.stringify({ catalog: data.catalog }) });
    if (data.browser?.purifier_plan_positions) localStorage.setItem('purifier-plan-positions', JSON.stringify(data.browser.purifier_plan_positions));
    if (data.browser?.default_view) localStorage.setItem('air-default-view', data.browser.default_view);
    frameByName.fleet.src = frameByName.fleet.src;
    frameByName.wifi.src = frameByName.wifi.src;
    setShellStatus('配置已恢复');
  } catch (error) { alert(`无法恢复配置：${error.message}`); }
});

function currentWifiSelection() {
  const doc = frameDocument('wifi');
  return doc?.querySelector('#device-id')?.value || '';
}

function openDeviceProperties() {
  switchWorkspace('wifi');
  setTimeout(() => {
    if (!clickFrame('wifi', '.device-card.selected .device-tools')) setShellStatus('请先选择一台设备');
  }, 0);
}

function runFleetCommand(mode) {
  const doc = frameDocument('fleet');
  const targetCount = doc?.querySelector('#command-scope')?.textContent || '';
  if (targetCount.includes('0 台')) { switchWorkspace('fleet'); setShellStatus('请先勾选设备或房间'); return; }
  const button = doc.querySelector(`[data-command="${mode}"]`);
  if (!button) return;
  button.click(); switchWorkspace('fleet'); setShellStatus(`已向${targetCount.replace('已选 ', '')}发出模式命令`);
}

const menus = {
  system: [
    ['⇩', '导出完整状态快照', 'JSON', exportSnapshot], ['▤', '导出命令与回执日志', 'CSV', exportCommandLog],
    'separator', ['▣', '备份系统配置', '', backupConfiguration], ['↥', '恢复系统配置…', '', () => restoreInput.click()],
    'separator', ['⚙', '系统设置…', '', () => openDialog('settings-dialog')],
  ],
  device: [
    ['↻', '刷新设备状态', 'F5', () => { clickFrame('wifi', '#refresh'); setShellStatus('设备状态已刷新'); }],
    ['…', '当前设备属性…', '', openDeviceProperties],
    'separator', ['▣', '进入串口调试', '', () => switchWorkspace('serial')], ['⌁', '进入 Wi‑Fi 调试', '', () => switchWorkspace('wifi')],
    'separator', ['↯', '固件检测与升级…', '', () => openEngineering('board')],
    ['!', '远程重启当前设备…', '', () => { if (!currentWifiSelection()) return setShellStatus('请先选择一台在线设备'); if (confirm('确定要远程重启当前设备吗？')) clickFrame('wifi', '#reboot'); }],
  ],
  control: [
    ['■', '关机', '', () => runFleetCommand('off')], ['●', '低速', '', () => runFleetCommand('low')],
    ['●', '中速', '', () => runFleetCommand('medium')], ['●', '高速', '', () => runFleetCommand('high')], ['✦', '自动', '', () => runFleetCommand('auto')],
    'separator', ['⌂', '查看家庭控制区', '', () => switchWorkspace('fleet')], ['▤', '查看等待回执命令', '', () => switchWorkspace('wifi')],
  ],
  tools: [
    ['↯', '新板自动检测、烧录与验收…', '', () => openEngineering('board')],
    ['◇', '焊接后外设检查…', '', () => openEngineering('peripheral')], ['◌', '光传感器标定向导…', '', () => openEngineering('calibration')], ['▥', 'MQTT 并发压力测试…', '', () => openEngineering('stress')],
    'separator', ['⌁', 'Wi‑Fi 信号安装助手', '', () => switchWorkspace('wifi')], ['⌁', 'ADC 波形和阈值检查', '', () => switchWorkspace('serial')],
    ['⚙', '工程环境检查…', '', () => openEngineering('environment')],
  ],
  view: [
    ['⌂', '家庭总览', 'Alt+1', () => switchWorkspace('fleet')], ['▣', '板卡串口调试', 'Alt+2', () => switchWorkspace('serial')], ['⌁', '板卡 Wi‑Fi 调试', 'Alt+3', () => switchWorkspace('wifi')],
    'separator', ['↻', '刷新当前工作区', 'F5', () => document.querySelector('iframe.active')?.contentWindow.location.reload()],
  ],
  workspace: [
    ['←', '上一个工作区', 'Ctrl+PgUp', () => cycleWorkspace(-1)], ['→', '下一个工作区', 'Ctrl+PgDn', () => cycleWorkspace(1)],
    'separator', ['▣', '保存当前工作区', '', () => { localStorage.setItem('air-default-view', location.hash.slice(1) || 'fleet'); setShellStatus('当前工作区已设为默认'); }],
    ['⌂', '恢复默认工作区', '', () => switchWorkspace(localStorage.getItem('air-default-view') || 'fleet')],
  ],
  help: [
    ['?', '系统架构', '', () => openHelp('architecture')], ['▦', '引脚与接线定义', '', () => openHelp('pins')], ['●', '状态和指示灯含义', '', () => openHelp('status')],
    ['✓', '新板检测流程', '', () => openHelp('workflow')], 'separator', ['i', '关于本系统', '', () => openHelp('about')],
  ],
};

function renderMenu(name, anchor) {
  popup.replaceChildren();
  menus[name].forEach(item => {
    if (item === 'separator') { popup.append(document.createElement('hr')); return; }
    const button = document.createElement('button');
    button.innerHTML = `<span class="menu-icon"></span><span></span><span class="shortcut"></span>`;
    button.children[0].textContent = item[0]; button.children[1].textContent = item[1]; button.children[2].textContent = item[2];
    button.onclick = async () => { closeMenu(); try { await item[3](); } catch (error) { setShellStatus(error.message); alert(error.message); } };
    popup.append(button);
  });
  const rect = anchor.getBoundingClientRect();
  popup.style.left = `${rect.left}px`; popup.style.top = `${rect.bottom}px`; popup.hidden = false;
  anchor.classList.add('open');
}

function closeMenu() { popup.hidden = true; document.querySelectorAll('.menu-bar .open').forEach(node => node.classList.remove('open')); }
document.querySelectorAll('[data-menu]').forEach(button => button.onclick = event => { event.stopPropagation(); const reopening = button.classList.contains('open'); closeMenu(); if (!reopening) renderMenu(button.dataset.menu, button); });
document.addEventListener('click', event => { if (!event.target.closest('#menu-popup')) closeMenu(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape') closeMenu(); if (event.altKey && ['1','2','3'].includes(event.key)) switchWorkspace(['fleet','serial','wifi'][Number(event.key)-1]); });

function cycleWorkspace(direction) {
  const order = ['fleet', 'serial', 'wifi']; const current = order.indexOf(location.hash.slice(1));
  switchWorkspace(order[(current + direction + order.length) % order.length]);
}

function openDialog(id) { document.getElementById(id).showModal(); }
document.querySelectorAll('[data-dialog-close]').forEach(button => button.onclick = () => document.getElementById(button.dataset.dialogClose).close());

const helpPages = {
  architecture: `<h2>系统架构</h2><p>设备使用动态局域网地址，通过不可变设备 ID 在 MQTT 上自报身份；控制服务维护状态、命令、回执和设备目录，三个前端工作区共享同一个服务。</p><div class="architecture-diagram">ESP32-C3 设备\n  ├─ 4 路 ADC / 2 路光耦\n  ├─ Wi‑Fi 2.4 GHz\n  └─ MQTT 状态、心跳、遥测、回执\n              │\n              ▼\n本机 MQTT Broker ── 本机控制服务\n              │\n      ┌───────┼────────┐\n      ▼       ▼        ▼\n  家庭总览  串口调试  Wi‑Fi 调试</div><p>心跳超过 3 秒进入黄色“暂时无响应”，超过 5 秒进入灰色“离线”。红色只表示设备在线但净化器关机。</p>`,
  pins: `<h2>ESP32-C3 Super Mini 引脚定义</h2><table class="help-table"><tr><th>用途</th><th>GPIO / ADC</th><th>说明</th></tr><tr><td>低速灯</td><td>GPIO0 / ADC4</td><td>光敏电阻输入</td></tr><tr><td>中速灯</td><td>GPIO4 / ADC1</td><td>光敏电阻输入</td></tr><tr><td>高速灯</td><td>GPIO3 / ADC2</td><td>光敏电阻输入</td></tr><tr><td>自动灯</td><td>GPIO1 / ADC3</td><td>光敏电阻输入</td></tr><tr><td>电源光耦</td><td>GPIO5</td><td>模拟电源按键</td></tr><tr><td>模式光耦</td><td>GPIO7</td><td>循环切换模式</td></tr><tr><td>板载蓝灯</td><td>GPIO8</td><td>低电平有效</td></tr></table>`,
  status: `<h2>状态与指示灯</h2><table class="help-table"><tr><th>显示</th><th>含义</th></tr><tr><td>蓝灯慢闪</td><td>正在扫描、认证或重试 Wi‑Fi</td></tr><tr><td>蓝灯快闪</td><td>Wi‑Fi 已连接并维持通信</td></tr><tr><td>绿色方灯</td><td>设备在线，心跳不超过 3 秒</td></tr><tr><td>黄色方灯</td><td>暂时无响应，最后信号为 3–5 秒前</td></tr><tr><td>灰色方灯</td><td>设备离线，超过 5 秒无有效状态或心跳</td></tr><tr><td>红色模式灯</td><td>设备在线，但净化器处于关机状态</td></tr></table>`,
  workflow: `<h2>新板自动烧录验收流程</h2><ol><li>新板不要焊接外设，先用 USB 连接电脑。</li><li>打开“诊断工具 → 新板自动检测、烧录与验收”。</li><li>确认系统选中标记为 ESP32 的目标串口，点击一次“开始自动检测、烧录并验收”。</li><li>系统自动完成芯片识别、编译、写入校验、启动检查和 Wi-Fi/MQTT 联网检查。</li><li>只有五步全部通过才算验收合格；蓝灯快速闪烁表示 Wi-Fi 已连接。</li><li>合格后再焊接外设，随后执行外设检查。</li></ol>`,
  about: `<h2>关于家庭空气管理控制台</h2><p>用于 ESP32-C3 空气净化器控制器的部署、局域网管理与工程调试。</p><p>界面结构采用 Office 2003 经典菜单、工具栏、资源区和工作区布局；控件采用 Windows 9x/2000 工业软件质感。</p><p>软件版本：0.2 工程控制台<br>通信协议：MQTT air/v1<br>目标板卡：ESP32-C3 Super Mini</p>`,
};

function openHelp(page) { openDialog('help-dialog'); showHelp(page); }
function showHelp(page) {
  document.querySelectorAll('[data-help-page]').forEach(button => button.classList.toggle('active', button.dataset.helpPage === page));
  document.getElementById('help-content').innerHTML = helpPages[page];
}
document.querySelectorAll('[data-help-page]').forEach(button => button.onclick = () => showHelp(button.dataset.helpPage));
showHelp('architecture');

function openEngineering(page) {
  openDialog('engineering-dialog'); showToolPage(page);
  refreshEngineeringEnvironment().catch(error => setShellStatus(error.message));
  if (page === 'calibration') renderCalibrationHub();
  if (page === 'board') {
    refreshPorts().catch(error => setShellStatus(error.message));
    loadLatestEngineeringJob(false).catch(error => setShellStatus(error.message));
  }
}
function showToolPage(page) {
  document.querySelectorAll('[data-tool-page]').forEach(button => button.classList.toggle('active', button.dataset.toolPage === page));
  document.querySelectorAll('[data-tool-content]').forEach(section => section.classList.toggle('active', section.dataset.toolContent === page));
  if (page === 'calibration') renderCalibrationHub();
}
document.querySelectorAll('[data-tool-page]').forEach(button => button.onclick = () => { showToolPage(button.dataset.toolPage); if (button.dataset.toolPage === 'environment') refreshEngineeringEnvironment().catch(error => setShellStatus(error.message)); });
document.querySelectorAll('[data-command-action]').forEach(button => button.onclick = () => { document.getElementById('engineering-dialog').close(); switchWorkspace(button.dataset.commandAction); });
document.querySelectorAll('[data-calibration-action]').forEach(button => button.onclick = () => { document.getElementById('engineering-dialog').close(); switchWorkspace(button.dataset.calibrationAction); setShellStatus(`已打开${button.dataset.calibrationAction === 'serial' ? ' USB 串口' : ' Wi‑Fi'}标定向导`); });

function renderCalibrationHub() {
  const target = document.getElementById('calibration-hub-status');
  if (!target) return;
  try {
    const value = JSON.parse(localStorage.getItem('air-light-calibration-session-status') || 'null');
    if (!value || value.format !== 'air-light-calibration-status-v1') { target.textContent = '尚未开始本机原始采样。'; return; }
    const transport = value.transport === 'usb_serial' ? 'USB 串口' : 'Wi‑Fi / MQTT';
    const duration = Math.max(0, Math.floor(Number(value.duration_ms || 0) / 1000));
    const elapsed = duration >= 60 ? `${Math.floor(duration / 60)} 分 ${String(duration % 60).padStart(2, '0')} 秒` : `${duration} 秒`;
    target.textContent = `${transport}${value.active ? '正在记录' : '最近一次记录已停止'} · ${value.device_id || '设备 ID 待识别'} · ${elapsed} · ${Number(value.sample_count || 0).toLocaleString()} 样本 · ${Number(value.received_hz || 0).toFixed(1)} Hz · 序号缺口 ${Number(value.sequence_gaps || 0)}`;
  } catch (_) { target.textContent = '本机采样状态暂时不可读；可直接打开串口或 Wi‑Fi 采样入口。'; }
}
setInterval(() => {
  if (document.querySelector('[data-tool-content="calibration"]')?.classList.contains('active')) renderCalibrationHub();
}, 500);

async function refreshPorts() {
  const ports = await toolJson('/tools/ports'), select = document.getElementById('engineering-port');
  select.replaceChildren(...ports.map(port => new Option(`${port.device} — ${port.description}${port.likely_esp32 ? '（推荐）' : ''}`, port.device)));
  if (!ports.length) select.append(new Option('没有检测到串口', ''));
  updatePortDescription(ports);
  select.onchange = () => { updatePortDescription(ports); refreshEngineeringSafety().catch(error => setShellStatus(error.message)); };
  await refreshEngineeringSafety();
}
function updatePortDescription(ports) {
  const port = ports.find(item => item.device === document.getElementById('engineering-port').value);
  document.getElementById('port-description').textContent = port ? `${port.device} · ${port.description} · ${port.hardware_id}` : '请连接开发板并重新扫描。';
}
document.getElementById('refresh-ports').onclick = () => refreshPorts().catch(error => setShellStatus(error.message));

async function refreshEngineeringSafety() {
  const port = document.getElementById('engineering-port').value;
  const status = await toolJson(`/tools/safety?port=${encodeURIComponent(port)}`);
  const provision = document.getElementById('provision-board');
  const targetReady = Boolean(status.port?.likely_esp32);
  provision.disabled = !targetReady || Boolean(status.active_job_id);
  document.getElementById('flash-safety').textContent = !status.port ? '没有检测到目标串口。'
    : !targetReady ? '该串口不是已识别的 ESP32，自动流程已锁定。'
    : status.active_job_id ? '已有工程任务正在运行，请等待完成。'
    : `${status.port.device} 已识别为 ESP32 候选。自动流程将从只读芯片检查开始，检查通过后才会编译和写入。`;
  return status;
}

async function refreshEngineeringEnvironment() {
  const report = await toolJson('/tools/environment');
  const fields = [
    ['Python', report.python], ['PlatformIO', report.platformio], ['工程配置', report.platformio_ini ? '已找到' : '缺失'],
    ['已编译固件', report.firmware_exists ? `${report.firmware_bytes.toLocaleString()} 字节` : '尚未生成'], ['ESP 检查工具', report.esptool_version !== '不可用' ? report.esptool_version : report.esptool_exists ? '已安装但自检失败' : '缺失'],
    ['MQTT Broker', report.mqtt_listening ? '正常监听' : '未运行'], ['控制服务', report.api_listening ? '正常监听' : '未运行'], ['网页服务', report.console_listening ? '正常监听' : '未运行'],
    ['串口数量', `${report.ports.length} 个`],
  ];
  document.getElementById('environment-grid').replaceChildren(...fields.flatMap(([label, value]) => { const a = document.createElement('span'), b = document.createElement('b'); a.textContent = label; b.textContent = value; return [a,b]; }));
}
document.getElementById('refresh-environment').onclick = () => refreshEngineeringEnvironment().catch(error => setShellStatus(error.message));

const jobNames = { provision_board: '新板自动检测、烧录和联网验收', board_check: '板卡芯片通信检查', build_firmware: '固件编译', flash_firmware: '固件写入与校验', mqtt_stress: 'MQTT 并发压力测试' };
function renderProvisionPlaceholders(activeIndex = -1, failedIndex = -1) {
  const steps = document.getElementById('provision-steps');
  if (!steps) return;
  steps.replaceChildren(...provisionStepLabels.map((label, index) => {
    const row = document.createElement('span');
    row.className = index === failedIndex ? 'step-failed' : index === activeIndex ? 'step-running' : 'step-waiting';
    row.textContent = `${index === failedIndex ? '✕' : index === activeIndex ? '►' : '○'} ${label}`;
    return row;
  }));
}

function resetEngineeringDisplay(message = '请连接一块新板，然后开始新一轮自动验收。旧任务仍保存在“查看上次记录”中。') {
  clearTimeout(jobPollTimer);
  currentEngineeringJob = null;
  document.getElementById('job-title').textContent = '本轮尚未开始';
  document.getElementById('job-status').textContent = '待命';
  const log = document.getElementById('job-log');
  log.textContent = message; log.classList.remove('failed');
  document.getElementById('export-job').disabled = true;
  renderProvisionPlaceholders();
}

function showEngineeringPreparation(message, status = '准备中') {
  clearTimeout(jobPollTimer);
  currentEngineeringJob = null;
  document.getElementById('job-title').textContent = '新板自动检测、烧录和联网验收 · 新一轮';
  document.getElementById('job-status').textContent = status;
  const log = document.getElementById('job-log');
  log.textContent = message; log.classList.remove('failed');
  document.getElementById('export-job').disabled = true;
  renderProvisionPlaceholders(0);
}

function showEngineeringNotStarted(message, status = '未启动') {
  currentEngineeringJob = null;
  document.getElementById('job-title').textContent = '新板自动检测、烧录和联网验收 · 本轮';
  document.getElementById('job-status').textContent = status;
  const log = document.getElementById('job-log');
  log.textContent = `${message}\n\n没有创建后台任务，也没有读取或写入任何板卡。`;
  log.classList.toggle('failed', status === '无法开始');
  document.getElementById('export-job').disabled = true;
  renderProvisionPlaceholders(-1, status === '无法开始' ? 0 : -1);
}

function requestTypedConfirmation(message, expected) {
  const dialog = document.getElementById('engineering-confirm-dialog');
  const input = document.getElementById('engineering-confirm-input');
  const accept = document.getElementById('engineering-confirm-accept');
  document.getElementById('engineering-confirm-message').textContent = message;
  document.getElementById('engineering-confirm-expected').textContent = expected;
  input.value = ''; accept.disabled = true;
  return new Promise(resolve => {
    let settled = false;
    const finish = value => {
      if (settled) return;
      settled = true;
      dialog.close();
      input.oninput = null; input.onkeydown = null; accept.onclick = null; dialog.oncancel = null;
      document.getElementById('engineering-confirm-cancel').onclick = null;
      document.getElementById('engineering-confirm-close').onclick = null;
      resolve(value);
    };
    input.oninput = () => { accept.disabled = input.value !== expected; };
    input.onkeydown = event => { if (event.key === 'Enter' && input.value === expected) finish(input.value); };
    accept.onclick = () => finish(input.value);
    document.getElementById('engineering-confirm-cancel').onclick = () => finish(null);
    document.getElementById('engineering-confirm-close').onclick = () => finish(null);
    dialog.oncancel = event => { event.preventDefault(); finish(null); };
    dialog.showModal(); input.focus();
  });
}

async function loadLatestEngineeringJob(render = false) {
  const jobs = await toolJson('/tools/jobs');
  latestEngineeringJob = jobs[0] || null;
  const button = document.getElementById('show-latest-job');
  button.disabled = !latestEngineeringJob;
  button.title = latestEngineeringJob ? `上次任务 ${latestEngineeringJob.job_id.slice(0, 8)} · ${latestEngineeringJob.summary}` : '没有历史任务';
  if (render && latestEngineeringJob) renderEngineeringJob(latestEngineeringJob, true);
}
document.getElementById('show-latest-job').onclick = () => loadLatestEngineeringJob(true).catch(error => setShellStatus(error.message));
document.getElementById('clear-job-view').onclick = () => resetEngineeringDisplay();
document.querySelectorAll('[data-engineering-job]').forEach(button => button.onclick = () => startEngineeringJob(button.dataset.engineeringJob).catch(error => {
  document.querySelectorAll('[data-engineering-job]').forEach(item => { item.disabled = false; });
  refreshEngineeringSafety().catch(() => {});
  showEngineeringNotStarted(error.message, '无法开始');
  setShellStatus(error.message);
}));
async function startEngineeringJob(kind) {
  const body = { kind };
  if (kind === 'provision_board') {
    showEngineeringPreparation('正在重新扫描串口并核对目标板卡……');
    await refreshPorts();
  }
  if (['provision_board','board_check','flash_firmware'].includes(kind)) body.port = document.getElementById('engineering-port').value;
  if (kind === 'mqtt_stress') {
    body.devices = Number(document.getElementById('stress-devices').value); body.rate = Number(document.getElementById('stress-rate').value); body.duration = Number(document.getElementById('stress-duration').value);
    const aggregate = body.devices * body.rate;
    if (aggregate > 1200 || body.duration > 60) {
      if (!confirm(`这是一项高负载测试：约 ${aggregate} 条/秒，持续 ${body.duration} 秒。它不会控制真实设备，但会明显占用本机 MQTT 和网络资源。是否继续？`)) return;
      body.confirmation = '我了解这是高负载测试';
    }
  }
  if (kind === 'flash_firmware') {
    const safety = await refreshEngineeringSafety();
    if (!safety.flash_ready) throw new Error(safety.blockers.join('；'));
    const expected = safety.confirmation_text;
    const entered = await requestTypedConfirmation(`这一步会覆盖 ${body.port} 上的现有程序。拔线、断电或选错串口会造成写入失败。`, expected);
    if (entered === null) { showEngineeringNotStarted('操作者取消了确认。', '已取消'); return; }
    if (entered !== expected) throw new Error('确认文字不正确，未执行任何写入');
    body.confirmation = entered;
  }
  if (kind === 'provision_board') {
    const safety = await refreshEngineeringSafety();
    if (!safety.port?.likely_esp32) throw new Error('请选择已识别的 ESP32 串口');
    showEngineeringPreparation(`已锁定 ESP32 目标 ${body.port}，正在直接启动自动烧录验收……`);
    // The one-click wizard supplies the backend safety token itself. The
    // backend still rejects a mismatched port or a non-ESP32 candidate.
    body.confirmation = `自动验收并写入 ${body.port}`;
  }
  document.querySelectorAll('[data-engineering-job]').forEach(button => { button.disabled = true; });
  showEngineeringPreparation(`正在创建后台任务……\n目标串口：${body.port || '不适用'}`);
  currentEngineeringJob = await toolJson('/tools/jobs', { method: 'POST', body: JSON.stringify(body) });
  renderEngineeringJob(currentEngineeringJob); pollEngineeringJob();
}
function renderEngineeringJob(job, historical = false) {
  currentEngineeringJob = job;
  const shortId = job.job_id ? job.job_id.slice(0, 8) : '无编号';
  document.getElementById('job-title').textContent = `${historical ? '上次记录 · ' : ''}${jobNames[job.kind] || job.kind} · ${shortId}`;
  const statusText = ({ queued: '排队中', running: '正在执行', succeeded: '通过', failed: '失败' })[job.status] || job.status;
  document.getElementById('job-status').textContent = historical ? `历史 · ${statusText}` : statusText;
  const log = document.getElementById('job-log'); log.textContent = (job.log || []).join('\n') || job.summary; log.classList.toggle('failed', job.status === 'failed'); log.scrollTop = log.scrollHeight;
  const steps = document.getElementById('provision-steps');
  if (job.kind === 'provision_board' && steps) {
    const symbols = { waiting: '○', running: '►', passed: '✓', failed: '✕' };
    steps.replaceChildren(...(job.result?.steps || []).map(step => {
      const row = document.createElement('span'); row.className = `step-${step.status}`;
      row.textContent = `${symbols[step.status] || '○'} ${step.label}${step.detail ? ` — ${step.detail}` : ''}`; return row;
    }));
  }
  document.getElementById('export-job').disabled = !['succeeded','failed'].includes(job.status);
  const finished = ['succeeded','failed'].includes(job.status);
  if (!historical) document.querySelectorAll('[data-engineering-job]').forEach(button => { button.disabled = !finished; });
  if (finished && !historical) {
    latestEngineeringJob = job;
    document.getElementById('show-latest-job').disabled = false;
    refreshEngineeringSafety().catch(error => setShellStatus(error.message));
  }
  setShellStatus(job.summary);
}
function pollEngineeringJob() {
  clearTimeout(jobPollTimer);
  if (!currentEngineeringJob || !['queued','running'].includes(currentEngineeringJob.status)) return;
  jobPollTimer = setTimeout(async () => { try { renderEngineeringJob(await toolJson(`/tools/jobs/${currentEngineeringJob.job_id}`)); pollEngineeringJob(); } catch (error) { setShellStatus(error.message); } }, 750);
}
document.getElementById('export-job').onclick = () => currentEngineeringJob && download(`engineering-report-${currentEngineeringJob.job_id}.json`, { format: 'air-engineering-report-v1', exported_at: new Date().toISOString(), job: currentEngineeringJob });
resetEngineeringDisplay();

const settings = JSON.parse(localStorage.getItem('air-control-settings') || '{}');
document.getElementById('default-view').value = settings.defaultView || localStorage.getItem('air-default-view') || 'fleet';
document.getElementById('save-settings').onclick = () => {
  const savedSettings = { defaultView: document.getElementById('default-view').value };
  localStorage.setItem('air-control-settings', JSON.stringify(savedSettings)); localStorage.setItem('air-default-view', savedSettings.defaultView);
  document.getElementById('settings-dialog').close(); setShellStatus('系统设置已保存');
};
