/* Lightweight replay tests for the browser calibration analyzer. */
const assert = require('node:assert/strict');
global.window = global;
require('../console/calibration-analyzer.js');

function makeSession({ startupPulses = false, complete = true, gap = false } = {}) {
  const samples = [];
  let t = 0;
  let sequence = 0;
  const dark = [110, 700, 80, 650];
  const modes = {
    low: [1500, 700, 80, 650],
    medium: [160, 2800, 80, 650],
    high: [160, 700, 1450, 650],
    auto: [160, 700, 80, 2700],
  };
  const push = values => {
    samples.push({ received_at_ms: t, sequence, adc_mv: values.slice() });
    sequence += 1;
    t += gap && samples.length === 180 ? 120 : 20;
  };
  for (let i = 0; i < 60; i += 1) push(dark);
  if (startupPulses) {
    for (let cycle = 0; cycle < 3; cycle += 1) {
      for (let i = 0; i < 40; i += 1) push(modes.auto);
      for (let i = 0; i < 40; i += 1) push(dark);
    }
  }
  for (const mode of ['low', 'medium', 'high', 'auto', 'low']) for (let i = 0; i < 100; i += 1) push(modes[mode]);
  if (complete) for (let i = 0; i < 100; i += 1) push(dark);
  return samples;
}

const clean = CalibrationAnalyzer.analyze(makeSession(), { transport: 'wifi_mqtt', requestedRateHz: 50 });
assert.equal(clean.overall_status, 'pass');
assert.equal(clean.sequence.complete, true);
assert.ok(clean.channels.every(channel => channel.quality === 'pass'));
assert.ok(clean.channels.find(channel => channel.channel === 'medium').delta_mv > 1800);

const pulsed = CalibrationAnalyzer.analyze(makeSession({ startupPulses: true }), { transport: 'wifi_mqtt', requestedRateHz: 50 });
assert.equal(pulsed.overall_status, 'pass');
assert.ok(pulsed.startup_transient.pulses.length >= 2);
assert.ok(pulsed.quality.warnings.some(message => message.includes('启动脉冲')));

const usbSamples = makeSession({ complete: false }).map(sample => ({ ...sample, sequence: null }));
const incomplete = CalibrationAnalyzer.analyze(usbSamples, { transport: 'usb_serial', requestedRateHz: 50 });
assert.equal(incomplete.overall_status, 'review');
assert.equal(incomplete.sequence.complete, false);
assert.ok(incomplete.quality.warnings.some(message => message.includes('USB')));

const gapped = CalibrationAnalyzer.analyze(makeSession({ gap: true }), { transport: 'wifi_mqtt', requestedRateHz: 50 });
assert.ok(gapped.quality.timestamp_gaps >= 1);

// Replay the supplied USB capture when it is available on the development
// machine. It intentionally remains a review result because it predates the
// new explicit dark-reference step.
const fs = require('node:fs');
const suppliedCsv = process.env.AIR_CALIBRATION_CSV || 'C:/Users/CHJ/Downloads/air-light-calibration-usb_serial-2026-10-09T09-06-57Z.csv';
if (fs.existsSync(suppliedCsv)) {
  const lines = fs.readFileSync(suppliedCsv, 'utf8').trim().split(/\r?\n/).slice(1);
  const replay = lines.map(line => {
    const fields = line.split(',').map(value => value.replace(/^"|"$/g, ''));
    return { received_at_ms: Number(fields[4]), sequence: null, adc_mv: fields.slice(11, 15).map(Number) };
  });
  const supplied = CalibrationAnalyzer.analyze(replay, { transport: 'usb_serial', requestedRateHz: 50 });
  assert.equal(supplied.sequence.complete, true);
  assert.equal(supplied.startup_transient.detected, true);
  assert.equal(supplied.overall_status, 'review');
}

console.log('calibration analyzer tests passed');
