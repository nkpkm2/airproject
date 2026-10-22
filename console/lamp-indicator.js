(function attachLampIndicator(global) {
  'use strict';

  const labels = { low: '低速灯', medium: '中速灯', high: '高速灯', auto: '自动灯' };
  function ensure(card) {
    let indicator = card?.querySelector('.lamp-indicator');
    if (indicator) return indicator;
    if (!card) return null;
    indicator = document.createElement('div');
    indicator.className = 'lamp-indicator unknown';
    indicator.setAttribute('role', 'status');
    indicator.innerHTML = '<span class="lamp-block"></span><b>未标定</b>';
    const meter = card.querySelector('.vertical-meter');
    if (meter) meter.insertAdjacentElement('beforebegin', indicator); else card.append(indicator);
    return indicator;
  }
  function channelConfig(config, key) {
    return config?.channels?.[key] || null;
  }
  function finiteOrNull(value) {
    if (value == null || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }
  function evaluate(config, key, mv, previous = 'unknown') {
    const channel = channelConfig(config, key);
    if (!channel || channel.quality && channel.quality !== 'pass') return { state: 'unknown', text: '未标定' };
    const value = Number(mv);
    if (!Number.isFinite(value)) return { state: 'unknown', text: '无数据' };
    const rising = channel.direction !== 'falling';
    const on = Number(channel.on_threshold_mv ?? channel.threshold_mv);
    const configuredOff = Number(channel.off_threshold_mv ?? channel.threshold_mv);
    if (!Number.isFinite(on) || !Number.isFinite(configuredOff)) return { state: 'unknown', text: '未标定' };

    // Use each channel's own measured dark distribution when available.  The
    // clear edge is deliberately anchored to the dark side, so a negative
    // fitted edge can never latch a lamp on at 0–10 mV.  Older configs without
    // distribution fields fall back to the calibrated threshold and still get
    // the same non-negative safety floor.
    const offMedian = finiteOrNull(channel.off_median_mv);
    const offSigma = Math.max(0, Number(channel.off_sigma_mv) || 0);
    const onMedian = finiteOrNull(channel.on_median_mv);
    const span = offMedian !== null && onMedian !== null ? Math.abs(onMedian - offMedian) : 0;
    const darkMargin = offMedian !== null ? Math.max(3 * offSigma, Math.min(50, span * 0.03), 2) : 0;
    let off = Number(channel.clear_threshold_mv);
    if (!Number.isFinite(off)) off = offMedian !== null ? offMedian + (rising ? darkMargin : -darkMargin) : configuredOff;
    if (rising) off = Math.max(configuredOff, offMedian !== null ? offMedian + darkMargin : Number(channel.threshold_mv ?? configuredOff), 0);
    else off = Math.min(configuredOff, offMedian !== null ? offMedian - darkMargin : 3_300);
    // Legacy records with a negative off edge have no distribution data from
    // which to infer a safe hysteresis band. Treat the calibrated turn-on
    // edge as the conservative clear edge until the device is recalibrated;
    // this prevents a 0.01–0.20 V dark reading from staying latched on.
    if (offMedian === null || onMedian === null) {
      if (rising && configuredOff < 0) off = Math.max(off, on);
      if (!rising && configuredOff > 3_300) off = Math.min(off, on);
    }
    let set = on;
    if (onMedian !== null && offMedian !== null) {
      const minimumGap = Math.max(5, span * 0.05);
      if (rising) set = Math.max(set, off + minimumGap);
      else set = Math.min(set, off - minimumGap);
    }
    if (rising && set <= off) set = off + Math.max(1, Math.abs(on - configuredOff) || 1);
    if (!rising && set >= off) set = off - Math.max(1, Math.abs(on - configuredOff) || 1);
    let isOn;
    if (previous === 'on') isOn = rising ? value > off : value < off;
    else if (previous === 'off') isOn = rising ? value >= set : value <= set;
    else isOn = rising ? value >= set : value <= set;
    return { state: isOn ? 'on' : 'off', text: isOn ? '亮' : '灭' };
  }
  function update(card, key, mv, config, previous = 'unknown') {
    const indicator = ensure(card);
    if (!indicator) return 'unknown';
    const result = evaluate(config, key, mv, previous);
    indicator.className = `lamp-indicator ${result.state}`;
    const label = indicator.querySelector('b');
    if (label) label.textContent = result.text;
    indicator.title = `${labels[key] || key}：${result.text}`;
    return result.state;
  }
  function initialize(root = document, config = null) {
    root.querySelectorAll('.adc-card').forEach(card => update(card, card.dataset.adc, NaN, config));
  }
  global.LampIndicator = { ensure, evaluate, update, initialize };
})(window);
