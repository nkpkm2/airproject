/* Shared second-phase calibration workflow for USB and Wi-Fi pages. */
(function attachCalibrationWizard(global) {
  'use strict';

  const $ = id => document.getElementById(id);
  const keys = ['low', 'medium', 'high', 'auto'];
  const guidedSequence = ['low', 'medium', 'high', 'auto', 'dark'];
  const guidedStepMs = 3000;
  const labels = { dark: '关机暗态', low: '低速', medium: '中速', high: '高速', auto: '自动', complete: '校准完成', review: '需要复核', idle: '未开始' };
  const prompts = {
    dark: '请按电源键关机，确认低、中、高、自动四盏灯全部熄灭。软件会自动采集暗态。',
    low: '请安装好 PCB，让设备开机并保持低速。软件会自动采样约 3 秒。',
    medium: '请按一次净化器实体模式键切换到中速；软件会自动识别稳定平台。',
    high: '请按一次净化器实体模式键切换到高速；软件会自动识别稳定平台。',
    auto: '请按一次净化器实体模式键切换到自动；启动阶段的自动灯闪烁会被自动屏蔽。',
    complete: '模式序列已识别，请关机并确认四盏灯熄灭，完成最终暗态确认。',
  };

  function voltage(mv) { return Number.isFinite(Number(mv)) ? `${(Number(mv) / 1000).toFixed(3)} V` : '—'; }
  function timestamp(ms) { return ms ? new Date(ms).toLocaleTimeString('zh-CN', { hour12: false, fractionalSecondDigits: 3 }) : '—'; }
  function drawAnalysis(report, samples) {
    const canvas = $('calibration-analysis-scope'); if (!canvas) return;
    const context = canvas.getContext('2d'), width = canvas.width, height = canvas.height;
    context.fillStyle = '#050b05'; context.fillRect(0, 0, width, height);
    context.strokeStyle = '#17451d'; context.lineWidth = 1;
    for (let x = 0; x < width; x += 40) { context.beginPath(); context.moveTo(x + .5, 0); context.lineTo(x + .5, height); context.stroke(); }
    for (let y = 0; y < height; y += 20) { context.beginPath(); context.moveTo(0, y + .5); context.lineTo(width, y + .5); context.stroke(); }
    if (!samples.length) return;
    const start = samples[0].received_at_ms, end = samples[samples.length - 1].received_at_ms || start + 1, span = Math.max(1, end - start);
    const colors = ['#39f06a', '#4db3ff', '#ffd34d', '#e17cff'];
    const segmentColors = { dark: 'rgba(110,110,110,.18)', low: 'rgba(57,240,106,.12)', medium: 'rgba(77,179,255,.12)', high: 'rgba(255,211,77,.12)', auto: 'rgba(225,124,255,.16)', transition: 'rgba(255,255,255,.12)' };
    (report?.segments || []).forEach(segment => { const x = (segment.start_at_ms - start) / span * width, segmentWidth = Math.max(1, (segment.end_at_ms - segment.start_at_ms) / span * width); context.fillStyle = segmentColors[segment.label] || 'rgba(255,255,255,.08)'; context.fillRect(x, 0, segmentWidth, height); });
    keys.forEach((key, channel) => { context.strokeStyle = colors[channel]; context.lineWidth = 1.2; context.beginPath(); samples.forEach((sample, index) => { const x = (sample.received_at_ms - start) / span * width, y = height - 3 - Math.min(height - 6, Math.max(0, sample.adc_mv[channel]) / 3300 * (height - 6)); index ? context.lineTo(x, y) : context.moveTo(x, y); }); context.stroke(); });
    context.font = '10px monospace'; keys.forEach((key, index) => { context.fillStyle = colors[index]; context.fillText(key, 6 + index * 44, 12); });
  }

  function create(options = {}) {
    const recorder = options.recorder;
    const analyzer = options.analyzer;
    const ids = { start: 'calibration-start', stop: 'calibration-stop', clear: 'calibration-clear', csv: 'calibration-csv', json: 'calibration-json', report: 'calibration-report', save: 'calibration-save' };
    let busy = false;
    const guided = { active: false, phase: 'idle', step: 0, deadline: 0, timer: null, finishing: false };

    function ensureSaveButton() {
      if ($(ids.save) || !$(ids.report)) return;
      const button = document.createElement('button');
      button.id = ids.save; button.type = 'button'; button.textContent = '保存本设备参数';
      $(ids.report).insertAdjacentElement('afterend', button);
    }

    function toast(message) { if (typeof options.toast === 'function') options.toast(message); }
    function ready() { return typeof options.isReady !== 'function' || options.isReady(); }
    function clearGuidedTimer() { if (guided.timer) { clearInterval(guided.timer); guided.timer = null; } }
    function resetGuided() { guided.active = false; guided.phase = 'idle'; guided.step = 0; guided.deadline = 0; guided.finishing = false; clearGuidedTimer(); }
    function progressOf(analysis) {
      const value = analysis?.report?.sequence?.progress;
      if (Number.isFinite(Number(value))) return Number(value);
      const match = String(analysis?.progress || '').match(/^(\d+)/);
      return match ? Number(match[1]) : 0;
    }
    function guidedMessage(analysis) {
      if (!guided.active) {
        if (analysis.report) return analysis.report.overall_status === 'pass' ? '采样已完成。请查看四路质量、阈值和警告，再导出分析报告。' : '采样已停止，请查看质量提示；必要时清空本轮数据后重新校准。';
        return '请先安装好 PCB，让设备开机并保持低速。点击“开始自动标定”后，按大字提示依次切换档位。';
      }
      const target = guidedSequence[guided.step] || 'dark';
      if (guided.phase === 'countdown') {
        const remaining = Math.max(0, Math.ceil((guided.deadline - Date.now()) / 1000));
        return target === 'dark'
          ? `正在采集关机暗态，请保持关机、四盏灯全部熄灭。还需约 ${remaining} 秒。`
          : `正在采集${labels[target]}稳定数据，请保持当前档位。还需约 ${remaining} 秒。`;
      }
      if (target === 'dark') return '请按电源键关机，确认四盏灯全部熄灭。检测到稳定暗态后会自动完成采样。';
      if (target === 'low') return '请确认设备已开机并处于低速。保持低速，软件正在等待稳定数据。';
      return `请按一下风速键，切换到${labels[target]}。检测到切换后会自动采样 3 秒。`;
    }
    function guidedTick() {
      if (!guided.active || guided.finishing) return;
      const analysis = analyzer?.snapshot?.() || {};
      const progress = progressOf(analysis);
      const reached = progress >= guided.step + 1;
      if (guided.phase === 'await_transition' && reached) {
        guided.phase = 'countdown'; guided.deadline = Date.now() + guidedStepMs; render(); return;
      }
      if (guided.phase !== 'countdown' || Date.now() < guided.deadline || !reached) { render(); return; }
      if (guided.step >= guidedSequence.length - 1) {
        guided.finishing = true; guided.active = false; clearGuidedTimer();
        stop('guided_complete').catch(error => toast(`自动完成标定失败：${error.message}`));
        return;
      }
      guided.step += 1; guided.phase = 'await_transition'; guided.deadline = 0; render();
    }
    function startGuided() { resetGuided(); guided.active = true; guided.phase = 'countdown'; guided.step = 0; guided.deadline = Date.now() + guidedStepMs; guided.timer = setInterval(guidedTick, 100); }
    function messageFor(state) {
      if (!state.active) return state.report ? (state.report.overall_status === 'pass' ? '采样已完成。请查看四路质量、阈值和警告，再导出分析报告。' : '采样已停止，请查看质量提示；必要时清空本轮数据后重新校准。') : '请先安装好 PCB，让设备开机并保持低速。点击“开始自动标定”后，按大字提示依次切换档位。';
      return prompts[state.stage] || prompts.dark;
    }
    function render() {
      const capture = recorder?.snapshot?.() || { active: false, sampleCount: 0, durationMs: 0, receivedHz: 0, latest: null, sequenceGaps: 0 };
      const analysis = analyzer?.snapshot?.() || { active: false, sampleCount: 0, stage: 'idle', stageLabel: '未开始', progress: '0/5', report: null, latest: null };
      const report = analysis.report;
      const active = Boolean(capture.active || analysis.active);
      const status = $('calibration-status');
      if (status) { status.textContent = active ? `校准中 · ${labels[analysis.stage] || analysis.stageLabel}` : report ? (report.overall_status === 'pass' ? '已完成 · 可审阅' : '已停止 · 需要复核') : '未开始'; status.className = `tag ${active ? 'good' : report?.overall_status === 'pass' ? 'good' : 'muted'}`; }
      if ($('calibration-stage')) $('calibration-stage').textContent = active ? (labels[analysis.stage] || analysis.stageLabel) : report ? (report.overall_label || '需要复核') : '未开始';
      if ($('calibration-progress')) $('calibration-progress').textContent = (!active && !report) ? `0/${guidedSequence.length}` : (analysis.progress || `0/${guidedSequence.length}`);
      if ($('calibration-guidance')) $('calibration-guidance').textContent = guided.active ? guidedMessage(analysis) : messageFor({ ...analysis, report });
      if ($('calibration-quality')) $('calibration-quality').textContent = report ? `${report.overall_status === 'pass' ? '通过' : '复核'} · ${report.channels.filter(channel => channel.quality === 'pass').length}/4 路` : active ? '分析中' : '—';
      if ($('calibration-warning')) { const warning = report?.quality?.warnings?.[0] || report?.sequence?.warnings?.[0] || ''; $('calibration-warning').textContent = warning || (active ? '原始数据完整保留；分析标签不会删除任何样本。' : ''); }
      if ($('calibration-duration')) $('calibration-duration').textContent = capture.sampleCount ? CalibrationSampling.durationText(capture.durationMs) : '—';
      if ($('calibration-count')) $('calibration-count').textContent = capture.sampleCount.toLocaleString();
      if ($('calibration-rate-live')) $('calibration-rate-live').textContent = `${Number(capture.receivedHz || 0).toFixed(1)} Hz`;
      if ($('calibration-gaps')) $('calibration-gaps').textContent = options.transport === 'usb_serial' ? 'USB 无固件序号' : `${capture.sequenceGaps || 0}${capture.sequenceResets ? ` · 重置 ${capture.sequenceResets}` : ''}`;
      const values = capture.latest?.adc_mv || analysis.latest?.adc_mv || [];
      keys.forEach((key, index) => { if ($(`calibration-now-${key}`)) $(`calibration-now-${key}`).textContent = voltage(values[index]); });
      const body = $('calibration-rows');
      if (body) {
        body.replaceChildren();
        const samples = recorder?.payload?.()?.samples || [];
        if (!samples.length) { const row = document.createElement('tr'), cell = document.createElement('td'); cell.colSpan = 6; cell.textContent = '尚未记录样本。'; row.append(cell); body.append(row); }
        else samples.slice(-12).reverse().forEach(sample => { const row = document.createElement('tr'); [sample.sample_index, timestamp(sample.received_at_ms), ...sample.adc_mv.map(voltage)].forEach(value => { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); }); body.append(row); });
        drawAnalysis(report, samples);
      }
      const canStart = ready() && !active;
      if ($(ids.start)) $(ids.start).disabled = !canStart || busy;
      if ($(ids.stop)) $(ids.stop).disabled = !active;
      if ($(ids.clear)) $(ids.clear).disabled = busy;
      if ($(ids.csv)) $(ids.csv).disabled = active || !capture.sampleCount;
      if ($(ids.json)) $(ids.json).disabled = active || !capture.sampleCount;
      if ($(ids.report)) $(ids.report).disabled = active || !report;
      if ($(ids.save)) $(ids.save).disabled = busy || active || !report || report.overall_status !== 'pass' || typeof options.saveCalibration !== 'function';
    }
    async function start() {
      if (busy || recorder.snapshot().active || analyzer.snapshot().active) return;
      if (!ready()) return toast(options.notReadyMessage || '请先连接设备。');
      busy = true; render();
      try {
        if (typeof options.beforeStart === 'function' && !(await options.beforeStart())) return;
        recorder.clear(); analyzer.clear();
        const extra = { deviceId: options.getDeviceId?.() || 'unknown', requestedRateHz: options.getRequestedRateHz?.() || null, expectedSequence: guidedSequence, transportMetadata: options.transportMetadata || {} };
        analyzer.start({ ...extra, transport: options.transport });
        recorder.start(extra);
        startGuided();
        if (typeof options.onStarted === 'function') options.onStarted();
        toast(options.startMessage || '自动标定已开始；请保持低速，首段正在采样约 3 秒。');
      } finally { busy = false; render(); }
    }
    async function stop(reason = 'manual_stop') {
      if (busy || (!recorder.snapshot().active && !analyzer.snapshot().active)) return;
      resetGuided();
      busy = true; render();
      try {
        if (recorder.snapshot().active) recorder.stop(reason);
        const report = analyzer.finish();
        if (typeof options.afterStop === 'function') await options.afterStop();
        toast(report?.overall_status === 'pass' ? '自动标定完成；请导出分析报告审阅。' : '自动标定已停止；报告需要复核或重新采样。');
      } finally { busy = false; render(); }
    }
    async function clear() {
      if (busy) return;
      resetGuided();
      if (recorder.snapshot().active || analyzer.snapshot().active) await stop('cleared_by_operator');
      recorder.clear(); analyzer.clear(); render(); toast('本轮原始数据和分析结果已清空。');
    }
    function exportRaw(kind) { const ok = kind === 'csv' ? recorder.exportCsv() : recorder.exportJson(); if (!ok) return toast('当前没有可导出的原始样本。'); toast(`已导出原始 ${kind.toUpperCase()} 文件。`); }
    function exportReport() { if (!analyzer.exportJson()) return toast('当前没有可导出的分析报告。'); toast('已导出自动标定分析报告。'); }
    async function saveCalibration() {
      const report = analyzer.getReport?.() || analyzer.report?.();
      const config = CalibrationAnalyzer.toDeviceConfig(report);
      if (!config) return toast('当前报告未通过四路质量检查，不能保存为设备参数。');
      if (typeof options.saveCalibration !== 'function') return toast('当前页面未配置设备参数保存服务。');
      busy = true; render();
      try { await options.saveCalibration(config, report); toast('已按不可变设备 ID 保存本设备标定参数。'); }
      catch (error) { toast(`保存本设备参数失败：${error.message}`); }
      finally { busy = false; render(); }
    }
    function ingest(sample) { return analyzer.ingest(sample); }
    ensureSaveButton();
    $(ids.start)?.addEventListener('click', () => start().catch(error => toast(`无法开始标定：${error.message}`)));
    $(ids.stop)?.addEventListener('click', () => stop().catch(error => toast(`停止标定失败：${error.message}`)));
    $(ids.clear)?.addEventListener('click', () => clear().catch(error => toast(`清空标定失败：${error.message}`)));
    $(ids.csv)?.addEventListener('click', () => exportRaw('csv'));
    $(ids.json)?.addEventListener('click', () => exportRaw('json'));
    $(ids.report)?.addEventListener('click', exportReport);
    $(ids.save)?.addEventListener('click', () => saveCalibration());
    recorder?.subscribe?.(render); analyzer?.subscribe?.(render); render();
    return { start, stop, clear, render, ingest, exportRaw, exportReport, saveCalibration };
  }
  global.CalibrationWizard = { create };
})(window);
