const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(`${__dirname}/lamp-indicator.js`, 'utf8');
const context = { window: {} };
vm.runInNewContext(source, context, { filename: 'lamp-indicator.js' });
const { evaluate } = context.window.LampIndicator;

const legacyNegativeOffEdge = {
  quality: 'pass', direction: 'rising', threshold_mv: 52,
  on_threshold_mv: 214, off_threshold_mv: -110,
  clear_threshold_mv: -110, off_median_mv: null, on_median_mv: null,
};
assert.equal(evaluate({ channels: { medium: legacyNegativeOffEdge } }, 'medium', 10, 'on').state,
  'off', 'a 10 mV dark reading must clear a legacy latched-on lamp');
assert.equal(evaluate({ channels: { medium: legacyNegativeOffEdge } }, 'medium', 10, 'unknown').state,
  'off', 'a 10 mV dark reading must not be treated as a newly lit lamp');

const measuredDarkEdge = {
  quality: 'pass', direction: 'rising', threshold_mv: 211.924,
  on_threshold_mv: 332.291, off_threshold_mv: 91.557,
  clear_threshold_mv: 91.557, off_median_mv: 5, on_median_mv: 1271, off_sigma_mv: 1.4826,
};
assert.equal(evaluate({ channels: { medium: measuredDarkEdge } }, 'medium', 10, 'on').state,
  'off', 'the measured 5 mV dark baseline must classify 10 mV as off');
assert.equal(evaluate({ channels: { medium: measuredDarkEdge } }, 'medium', 500, 'off').state,
  'on', 'a clear rise above the calibrated set edge must still turn the lamp on');

process.stdout.write('lamp indicator hysteresis tests passed\n');
