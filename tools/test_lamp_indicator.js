/* Pure classification tests for the per-device lamp indicator. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
global.window = global;
eval(fs.readFileSync('console/lamp-indicator.js', 'utf8'));

const config = {
  channels: {
    low: { direction: 'rising', on_threshold_mv: 600, off_threshold_mv: 350, quality: 'pass' },
    medium: { direction: 'rising', on_threshold_mv: 1400, off_threshold_mv: 850, quality: 'pass' },
  },
};
assert.deepEqual(LampIndicator.evaluate(config, 'low', 1200), { state: 'on', text: '亮' });
assert.deepEqual(LampIndicator.evaluate(config, 'low', 200), { state: 'off', text: '灭' });
assert.deepEqual(LampIndicator.evaluate(config, 'low', 500, 'on'), { state: 'on', text: '亮' });
assert.deepEqual(LampIndicator.evaluate(config, 'low', 500, 'off'), { state: 'off', text: '灭' });
// A fitted negative off edge must not latch the medium lamp on at 10 mV.
assert.deepEqual(LampIndicator.evaluate({ channels: {
  medium: { direction: 'rising', threshold_mv: 51.8, on_threshold_mv: 214.1, off_threshold_mv: -110.5, quality: 'pass' },
} }, 'medium', 10, 'on'), { state: 'off', text: '灭' });
assert.deepEqual(LampIndicator.evaluate({ channels: {
  medium: { direction: 'rising', threshold_mv: 51.8, on_threshold_mv: 214.1, off_threshold_mv: -110.5, quality: 'pass' },
} }, 'medium', 207, 'on'), { state: 'off', text: '灭' });
// With distribution statistics, noise around the dark baseline remains off.
assert.deepEqual(LampIndicator.evaluate({ channels: {
  medium: { direction: 'rising', on_median_mv: 1271, off_median_mv: 5, off_sigma_mv: 1.48,
    on_threshold_mv: 332, off_threshold_mv: 91, quality: 'pass' },
} }, 'medium', 10, 'on'), { state: 'off', text: '灭' });
assert.deepEqual(LampIndicator.evaluate(null, 'low', 1200), { state: 'unknown', text: '未标定' });
console.log('lamp indicator tests passed');
