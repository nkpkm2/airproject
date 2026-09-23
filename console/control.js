const views = new Set(['fleet', 'serial', 'wifi']);
const buttons = [...document.querySelectorAll('[data-view]')];
const panels = [...document.querySelectorAll('[data-panel]')];

function selectView(requested) {
  const view = views.has(requested) ? requested : 'fleet';
  buttons.forEach(button => button.classList.toggle('active', button.dataset.view === view));
  panels.forEach(panel => panel.classList.toggle('active', panel.dataset.panel === view));
  history.replaceState(null, '', `#${view}`);
  localStorage.setItem('air-control-view', view);
}

buttons.forEach(button => button.addEventListener('click', () => selectView(button.dataset.view)));
window.addEventListener('hashchange', () => selectView(location.hash.slice(1)));
selectView(location.hash.slice(1) || localStorage.getItem('air-control-view') || 'fleet');

async function updateService() {
  const lamp = document.getElementById('service-lamp'), text = document.getElementById('service-text');
  try {
    const [health, devices] = await Promise.all([fetch('http://127.0.0.1:8780/health'), fetch('http://127.0.0.1:8780/api/v1/devices')]);
    if (!health.ok || !devices.ok) throw new Error();
    const list = await devices.json(), online = list.filter(device => device.online).length;
    lamp.className = 'online'; text.textContent = `本机服务正常 · ${online} 台在线`;
  } catch (_) {
    lamp.className = 'offline'; text.textContent = '本机控制服务未运行';
  }
}
updateService(); setInterval(updateService, 2000);
