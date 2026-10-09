/*
 * Second-phase light-sensor calibration analyzer.
 *
 * The recorder keeps every sample exactly as received.  This module only
 * creates an analysis view over those samples: startup/transient labels,
 * ordered stable platforms, per-channel baselines and hysteresis thresholds.
 * It never sends a device command and never changes the raw capture.
 */
(function attachCalibrationAnalyzer(global) {
  'use strict';

  const CHANNELS = ['low', 'medium', 'high', 'auto'];
  const EXPECTED_SEQUENCE = ['dark', 'low', 'medium', 'high', 'auto', 'low', 'dark'];
  const GUIDED_SEQUENCE = ['low', 'medium', 'high', 'auto', 'dark'];
  const MODE_LABELS = {
    dark: '关机暗态', low: '低速', medium: '中速', high: '高速', auto: '自动', transition: '切换中', startup: '启动抑制', unknown: '未识别',
  };
  const FORMAT = 'air-light-calibration-analysis-v2';

  const finite = value => Number.isFinite(Number(value));
  const num = value => finite(value) ? Number(value) : null;
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

  function sorted(values) { return values.filter(finite).map(Number).sort((a, b) => a - b); }
  function median(values) {
    const x = sorted(values); if (!x.length) return null;
    const mid = Math.floor(x.length / 2); return x.length % 2 ? x[mid] : (x[mid - 1] + x[mid]) / 2;
  }
  function quantile(values, q) {
    const x = sorted(values); if (!x.length) return null;
    const index = (x.length - 1) * clamp(q, 0, 1); const lo = Math.floor(index); const hi = Math.ceil(index);
    return x[lo] + (x[hi] - x[lo]) * (index - lo);
  }
  function mad(values, center = median(values)) {
    if (!finite(center)) return null;
    return median(values.map(value => Math.abs(Number(value) - center)));
  }
  function robustSigma(values) { const value = mad(values); return finite(value) ? 1.4826 * value : 0; }
  function mean(values) { const x = values.filter(finite).map(Number); return x.length ? x.reduce((a, b) => a + b, 0) / x.length : null; }
  function slope(values, times) {
    const pairs = values.map((value, index) => [Number(times[index]), Number(value)]).filter(pair => finite(pair[0]) && finite(pair[1]));
    if (pairs.length < 3) return 0;
    const tx = mean(pairs.map(pair => pair[0])), vx = pairs.reduce((sum, pair) => sum + (pair[0] - tx) ** 2, 0);
    if (!vx) return 0;
    const ty = mean(pairs.map(pair => pair[1]));
    return pairs.reduce((sum, pair) => sum + (pair[0] - tx) * (pair[1] - ty), 0) / vx;
  }
  function correlation(a, b) {
    const pairs = a.map((value, index) => [Number(value), Number(b[index])]).filter(pair => finite(pair[0]) && finite(pair[1]));
    if (pairs.length < 4) return 0;
    const ax = mean(pairs.map(pair => pair[0])), bx = mean(pairs.map(pair => pair[1]));
    const aa = pairs.reduce((sum, pair) => sum + (pair[0] - ax) ** 2, 0);
    const bb = pairs.reduce((sum, pair) => sum + (pair[1] - bx) ** 2, 0);
    if (!aa || !bb) return 0;
    return pairs.reduce((sum, pair) => sum + (pair[0] - ax) * (pair[1] - bx), 0) / Math.sqrt(aa * bb);
  }
  function rollingMedian(values, radius) {
    const result = new Array(values.length);
    for (let index = 0; index < values.length; index += 1) {
      result[index] = median(values.slice(Math.max(0, index - radius), Math.min(values.length, index + radius + 1)));
    }
    return result;
  }
  function range(values) {
    const low = quantile(values, 0.05), high = quantile(values, 0.95);
    return Math.max(50, (high ?? 0) - (low ?? 0));
  }

  function normalizeSamples(samples) {
    return (samples || []).map((sample, index) => {
      const time = num(sample.received_at_ms) ?? (index ? index * 20 : 0);
      return {
        index,
        sample_index: sample.sample_index ?? index + 1,
        received_at_ms: time,
        firmware_uptime_ms: num(sample.firmware_uptime_ms),
        sequence: Number.isInteger(sample.sequence) ? sample.sequence : null,
        adc_mv: Array.isArray(sample.adc_mv) ? sample.adc_mv.slice(0, 4).map(value => num(value) ?? 0) : [0, 0, 0, 0],
        source: sample,
      };
    }).filter(sample => sample.adc_mv.length === 4);
  }

  function estimateRate(samples, requestedRate) {
    const deltas = [];
    for (let index = 1; index < samples.length; index += 1) {
      const delta = samples[index].received_at_ms - samples[index - 1].received_at_ms;
      if (delta > 0 && delta < 2000) deltas.push(delta);
    }
    const interval = median(deltas);
    return interval ? 1000 / interval : (finite(requestedRate) ? Number(requestedRate) : 50);
  }

  function detectStartup(samples, rate) {
    if (samples.length < Math.max(30, Math.round(rate * 0.8))) return { detected: false, startIndex: 0, endIndex: 0, correlations: [] };
    const probeCount = Math.min(samples.length, Math.max(40, Math.round(rate * 4)));
    const values = CHANNELS.map((_, channel) => samples.slice(0, probeCount).map(sample => sample.adc_mv[channel]));
    const times = samples.slice(0, probeCount).map(sample => (sample.received_at_ms - samples[0].received_at_ms) / 1000);
    const correlations = [];
    for (let channel = 1; channel < 4; channel += 1) correlations.push(correlation(values[0], values[channel]));
    const slopes = values.map(channel => slope(channel, times));
    const commonSign = slopes.filter(value => Math.abs(value) > 5).map(value => Math.sign(value));
    const sameSign = commonSign.length >= 3 && Math.abs(commonSign.reduce((a, b) => a + b, 0)) >= 3;
    const correlated = correlations.filter(value => value >= 0.7).length >= 2;
    const totalChange = values.map(channel => Math.abs(median(channel.slice(-Math.min(10, channel.length))) - median(channel.slice(0, Math.min(10, channel.length)))));
    const moving = totalChange.filter((value, channel) => value >= Math.max(12, range(samples.map(sample => sample.adc_mv[channel])) * 0.025)).length >= 3;
    if (!sameSign || !correlated || !moving) return { detected: false, startIndex: 0, endIndex: 0, correlations, slopes };

    const window = Math.max(10, Math.round(rate * 0.45));
    const maxEnd = Math.min(samples.length - 1, Math.round(rate * 7));
    let endIndex = Math.min(maxEnd, Math.round(rate * 4));
    for (let index = Math.max(window, Math.round(rate * 0.8)); index <= maxEnd; index += 1) {
      const slice = samples.slice(index - window, index + 1);
      const sliceTimes = slice.map(sample => (sample.received_at_ms - slice[0].received_at_ms) / 1000);
      const stable = values.every((_, channel) => {
        const x = slice.map(sample => sample.adc_mv[channel]);
        return Math.abs(slope(x, sliceTimes)) <= Math.max(18, range(values[channel]) * 0.04) && robustSigma(x) <= Math.max(35, range(values[channel]) * 0.16);
      });
      if (stable) { endIndex = index; break; }
    }
    return {
      detected: true,
      startIndex: 0,
      endIndex,
      correlations,
      slopes,
      startAtMs: samples[0].received_at_ms,
      endAtMs: samples[endIndex]?.received_at_ms || samples[Math.min(samples.length - 1, endIndex)].received_at_ms,
    };
  }

  function classify(samples, rate) {
    if (!samples.length) return { labels: [], scores: [], scales: [] };
    const scales = CHANNELS.map((_, channel) => {
      const values = samples.map(sample => sample.adc_mv[channel]);
      return { q15: quantile(values, 0.15), q85: quantile(values, 0.85), scale: range(values) };
    });
    const smoothed = CHANNELS.map((_, channel) => rollingMedian(samples.map(sample => sample.adc_mv[channel]), Math.max(2, Math.min(12, Math.round(rate * 0.08)))));
    const scores = samples.map((_, index) => smoothed.map((channel, channelIndex) => clamp((channel[index] - scales[channelIndex].q15) / Math.max(40, scales[channelIndex].q85 - scales[channelIndex].q15), 0, 1.5)));
    const labels = scores.map(row => {
      const sortedScores = row.map((value, index) => [value, index]).sort((a, b) => b[0] - a[0]);
      const [top, second] = sortedScores;
      if (top[0] < 0.42) return 'dark';
      if (top[0] < 0.55 || top[0] - second[0] < 0.12) return 'transition';
      return CHANNELS[top[1]];
    });
    return { labels, scores, scales };
  }

  function buildRuns(samples, labels, scores, rate) {
    if (!labels.length) return [];
    const runs = [];
    let start = 0;
    const push = end => {
      const label = labels[start];
      const values = samples.slice(start, end + 1);
      const times = values.map(sample => (sample.received_at_ms - values[0].received_at_ms) / 1000);
      const durationMs = values.length > 1 ? values[values.length - 1].received_at_ms - values[0].received_at_ms : 0;
      const scoreRows = scores.slice(start, end + 1);
      const strength = Math.max(...scoreRows.map(row => Math.max(...row)));
      const channel = label === 'dark' || label === 'transition' ? null : CHANNELS.indexOf(label);
      const medians = CHANNELS.map((_, index) => median(values.map(sample => sample.adc_mv[index])));
      const noises = CHANNELS.map((_, index) => robustSigma(values.map(sample => sample.adc_mv[index])));
      const slopes = CHANNELS.map((_, index) => slope(values.map(sample => sample.adc_mv[index]), times));
      const scale = channel === null ? 100 : Math.max(50, Math.abs(medians[channel] - quantile(samples.map(sample => sample.adc_mv[channel]), 0.2)));
      const stable = label !== 'transition' && values.length >= Math.max(5, Math.round(rate * 0.4)) && noises.every((noise, index) => noise <= Math.max(35, range(samples.map(sample => sample.adc_mv[index])) * 0.18)) && slopes.every((value, index) => Math.abs(value) <= Math.max(20, scale / 2));
      runs.push({ label, label_text: MODE_LABELS[label] || MODE_LABELS.unknown, start_index: start, end_index: end, start_at_ms: values[0].received_at_ms, end_at_ms: values[values.length - 1].received_at_ms, duration_ms: durationMs, sample_count: values.length, strength, medians, noises, slopes, stable, startup_transient: false });
    };
    for (let index = 1; index < labels.length; index += 1) {
      if (labels[index] !== labels[start]) { push(index - 1); start = index; }
    }
    push(labels.length - 1);
    // Merge short transition fragments only for presentation. Raw samples stay untouched.
    const merged = [];
    for (const run of runs) {
      const previous = merged[merged.length - 1];
      if (previous && run.label === previous.label && run.start_index - previous.end_index <= Math.max(3, Math.round(rate * 0.12))) {
        previous.end_index = run.end_index; previous.end_at_ms = run.end_at_ms; previous.sample_count += run.sample_count; previous.duration_ms = previous.end_at_ms - previous.start_at_ms;
      } else merged.push(run);
    }
    return merged;
  }

  function markStartupPulses(runs, startup) {
    const earlyEnd = startup.detected ? startup.endAtMs : (runs[0]?.start_at_ms || 0) + 7000;
    const candidates = runs.filter(run => run.start_at_ms <= earlyEnd && run.label === 'auto' && run.duration_ms >= 450 && run.duration_ms <= 1300);
    const pulseTrain = candidates.length >= 2 && candidates.every((run, index) => index === 0 || (run.start_at_ms - candidates[index - 1].end_at_ms) >= 350 && (run.start_at_ms - candidates[index - 1].end_at_ms) <= 1500);
    if (!pulseTrain) return [];
    candidates.forEach(run => { run.startup_transient = true; });
    return candidates.map(run => ({ start_at_ms: run.start_at_ms, end_at_ms: run.end_at_ms, reason: 'periodic_auto_startup_pulse' }));
  }

  function selectSequence(runs, startup, expectedSequence = EXPECTED_SEQUENCE) {
    const warnings = [];
    const usable = runs.filter(run => !run.startup_transient && run.label !== 'transition' && run.stable);
    const accepted = [];
    let cursor = 0;
    for (const run of usable) {
      const expected = expectedSequence[cursor];
      if (run.label === expected) { accepted.push({ ...run, sequence_index: cursor, expected: expected }); cursor += 1; continue; }
      if (expectedSequence[0] === 'dark' && cursor === 0 && run.label !== 'dark') {
        // A legacy raw capture may have started after the initial dark
        // reference. Keep it useful for review, but make the missing reference
        // explicit instead of silently treating the first mode as dark.
        if (run.label === 'low') {
          warnings.push('本次回放未包含稳定的初始暗态，已从低速平台开始识别；正式标定仍会要求暗态参考。');
          accepted.push({ ...run, sequence_index: 1, expected: 'low' }); cursor = 2;
        }
        continue;
      }
      if (run.label === 'dark' && cursor > 0 && cursor < expectedSequence.length - 1) continue;
      if (cursor < expectedSequence.length && expectedSequence.slice(cursor + 1).includes(run.label)) warnings.push(`检测到${MODE_LABELS[run.label] || run.label}，但当前期待${MODE_LABELS[expected] || expected}，可能存在跳档或未记录切换。`);
    }
    if (cursor < expectedSequence.length) warnings.push(`校准序列未完成：当前完成 ${cursor}/${expectedSequence.length} 个稳定阶段。`);
    if (startup.detected) warnings.push('已屏蔽启动阶段四路共同漂移；启动闪烁不会直接作为自动档。');
    return { accepted, cursor, complete: cursor === expectedSequence.length, warnings };
  }

  function baselineFunction(samples, evidence, channel, binMs = 500) {
    const points = [];
    const bins = new Map();
    evidence.forEach(index => {
      const sample = samples[index]; if (!sample) return;
      const key = Math.floor((sample.received_at_ms - samples[0].received_at_ms) / binMs);
      if (!bins.has(key)) bins.set(key, []); bins.get(key).push(sample.adc_mv[channel]);
    });
    [...bins.entries()].sort((a, b) => a[0] - b[0]).forEach(([key, values]) => { if (values.length >= 2) points.push({ t_ms: key * binMs, value_mv: median(values), samples: values.length }); });
    if (!points.length) {
      const values = samples.map(sample => sample.adc_mv[channel]);
      const value = median(values) || 0;
      return { points: [{ t_ms: 0, value_mv: value, samples: values.length }], at: () => value };
    }
    const at = relativeMs => {
      if (relativeMs <= points[0].t_ms) return points[0].value_mv;
      if (relativeMs >= points[points.length - 1].t_ms) return points[points.length - 1].value_mv;
      for (let index = 1; index < points.length; index += 1) {
        if (relativeMs <= points[index].t_ms) {
          const left = points[index - 1], right = points[index], ratio = (relativeMs - left.t_ms) / Math.max(1, right.t_ms - left.t_ms);
          return left.value_mv + (right.value_mv - left.value_mv) * ratio;
        }
      }
      return points[points.length - 1].value_mv;
    };
    return { points, at };
  }

  function thresholdFor(offResidual, onResidual, direction) {
    const off = offResidual.map(value => value * direction), on = onResidual.map(value => value * direction);
    const candidates = sorted(off.concat(on));
    if (!candidates.length) return { offset: 0, hysteresis: 0, falsePositive: 1, falseNegative: 1 };
    let best = { error: Infinity, offset: median(candidates) };
    const step = Math.max(1, Math.floor(candidates.length / 80));
    for (let index = step; index < candidates.length - step; index += step) {
      const offset = candidates[index];
      const falsePositive = off.filter(value => value > offset).length / Math.max(1, off.length);
      const falseNegative = on.filter(value => value <= offset).length / Math.max(1, on.length);
      if (falsePositive + falseNegative < best.error) best = { error: falsePositive + falseNegative, offset, falsePositive, falseNegative };
    }
    const sigma = Math.max(1, robustSigma(off), robustSigma(on));
    const delta = Math.abs((median(on) || 0) - (median(off) || 0));
    return { offset: best.offset, hysteresis: Math.max(3 * sigma, delta * 0.1), falsePositive: best.falsePositive ?? 1, falseNegative: best.falseNegative ?? 1 };
  }

  function analyze(rawSamples, metadata = {}) {
    const samples = normalizeSamples(rawSamples);
    const expectedSequence = Array.isArray(metadata.expected_sequence) && metadata.expected_sequence.length ? metadata.expected_sequence : EXPECTED_SEQUENCE;
    const rate = estimateRate(samples, metadata.requestedRateHz);
    const startup = detectStartup(samples, rate);
    const classified = classify(samples, rate);
    const runs = buildRuns(samples, classified.labels, classified.scores, rate);
    const startupPulses = markStartupPulses(runs, startup);
    const sequence = selectSequence(runs, startup, expectedSequence);
    const baselineByChannel = [];
    const channels = [];
    for (let channel = 0; channel < CHANNELS.length; channel += 1) {
      const offEvidence = [];
      const onEvidence = [];
      sequence.accepted.forEach(run => {
        const indexes = Array.from({ length: run.end_index - run.start_index + 1 }, (_, offset) => run.start_index + offset);
        if (run.label === 'dark' || run.label !== CHANNELS[channel]) offEvidence.push(...indexes);
        if (run.label === CHANNELS[channel]) onEvidence.push(...indexes);
      });
      const baseline = baselineFunction(samples, offEvidence, channel);
      baselineByChannel.push(baseline);
      const residual = index => samples[index].adc_mv[channel] - baseline.at(samples[index].received_at_ms - samples[0].received_at_ms);
      const offResidual = offEvidence.map(residual);
      const onResidual = onEvidence.map(residual);
      const offRaw = offEvidence.map(index => samples[index].adc_mv[channel]);
      const onRaw = onEvidence.map(index => samples[index].adc_mv[channel]);
      const offMedian = median(offRaw), onMedian = median(onRaw), delta = (onMedian ?? 0) - (offMedian ?? 0);
      const direction = delta >= 0 ? 1 : -1;
      const threshold = thresholdFor(offResidual, onResidual, direction);
      const effectSize = Math.abs(delta) / Math.max(1, Math.sqrt(robustSigma(offResidual) ** 2 + robustSigma(onResidual) ** 2));
      const overlap = threshold.falsePositive + threshold.falseNegative;
      // Do not make the quality gate depend on one arbitrary effect-size
      // number.  A channel with a larger bright-state noise can still be very
      // trustworthy when its two distributions barely overlap.  The relaxed
      // branch requires both a strong physical span and <=1% overlap.
      const separationPass = (effectSize >= 8 && overlap <= 0.06)
        || (effectSize >= 6 && overlap <= 0.01 && Math.abs(delta) >= 100);
      const pass = onEvidence.length >= Math.max(15, Math.round(rate * 0.4)) && Math.abs(delta) >= 40 && separationPass;
      // Keep the two hysteresis edges inside the measured dark/bright
      // distributions. A line-fit edge may otherwise land below zero (or
      // above the dark baseline), which is harmless in a plot but unsafe in a
      // state machine because a noisy sample could latch the previous state.
      const darkMargin = Math.max(3 * robustSigma(offResidual), Math.min(50, Math.abs(delta) * 0.03), 2);
      const darkGuard = (offMedian ?? 0) + darkMargin * direction;
      let onEdge = (median(offRaw) || 0) + (threshold.offset + threshold.hysteresis / 2) * direction;
      let offEdge = (median(offRaw) || 0) + (threshold.offset - threshold.hysteresis / 2) * direction;
      const minimumGap = Math.max(5, Math.abs(delta) * 0.05);
      if (direction > 0) {
        offEdge = Math.max(offEdge, darkGuard);
        onEdge = Math.max(onEdge, offEdge + minimumGap);
      } else {
        offEdge = Math.min(offEdge, darkGuard);
        onEdge = Math.min(onEdge, offEdge - minimumGap);
      }
      const midpointEdge = (onEdge + offEdge) / 2;
      channels.push({
        channel: CHANNELS[channel],
        logical_mapping: { low: 'GPIO0 / ADC4', medium: 'GPIO4 / ADC1', high: 'GPIO3 / ADC2', auto: 'GPIO1 / ADC3' }[CHANNELS[channel]],
        off_samples: offEvidence.length,
        on_samples: onEvidence.length,
        baseline_start_mv: baseline.at(0),
        baseline_end_mv: baseline.at((samples[samples.length - 1]?.received_at_ms || 0) - (samples[0]?.received_at_ms || 0)),
        baseline_points: baseline.points,
        off_median_mv: offMedian,
        on_median_mv: onMedian,
        delta_mv: delta,
        direction: direction > 0 ? 'rising' : 'falling',
        off_sigma_mv: robustSigma(offResidual),
        on_sigma_mv: robustSigma(onResidual),
        effect_size: Number(effectSize.toFixed(2)),
        distribution_overlap: Number(overlap.toFixed(4)),
        threshold_offset_mv: threshold.offset * direction,
        hysteresis_mv: Math.abs(onEdge - offEdge),
        threshold_mv_at_baseline_median: midpointEdge,
        on_threshold_mv_at_baseline_median: onEdge,
        off_threshold_mv_at_baseline_median: offEdge,
        quality: pass ? 'pass' : 'review',
        quality_reason: pass ? '亮灭分布分离充分。' : '样本不足、幅度不足或亮灭分布重叠，需要重试或复核。',
      });
    }
    const timestampDeltas = [];
    let duplicateTimestamps = 0, timestampGaps = 0;
    for (let index = 1; index < samples.length; index += 1) {
      const delta = samples[index].received_at_ms - samples[index - 1].received_at_ms;
      if (delta === 0) duplicateTimestamps += 1;
      if (delta > Math.max(45, 2.5 * 1000 / Math.max(1, rate))) timestampGaps += 1;
      if (delta > 0) timestampDeltas.push(delta);
    }
    const warnings = [...sequence.warnings];
    if (metadata.transport === 'usb_serial' && samples.every(sample => sample.sequence === null)) warnings.push('USB 原始帧没有固件序号和运行时间，丢帧只能通过本机时间间隔估计。');
    if (duplicateTimestamps || timestampGaps) warnings.push(`时间轴发现 ${duplicateTimestamps} 个重复时间戳、${timestampGaps} 个较大采样间隔；原始样本仍完整保留。`);
    if (startupPulses.length) warnings.push(`已识别并屏蔽 ${startupPulses.length} 个周期性自动灯启动脉冲。`);
    const complete = sequence.complete && channels.every(channel => channel.quality === 'pass');
    const report = {
      format: FORMAT,
      generated_at_ms: Date.now(),
      metadata: { ...metadata, sample_count: samples.length, estimated_received_hz: Number(rate.toFixed(2)), duration_ms: samples.length ? samples[samples.length - 1].received_at_ms - samples[0].received_at_ms : 0 },
      overall_status: complete ? 'pass' : 'review',
      overall_label: complete ? '可审阅的标定结果' : '需要复核或重新采样',
      expected_sequence: expectedSequence,
      sequence: { complete: sequence.complete, progress: sequence.cursor, total: expectedSequence.length, accepted: sequence.accepted.map(run => ({ sequence_index: run.sequence_index, label: run.label, label_text: run.label_text, start_at_ms: run.start_at_ms, end_at_ms: run.end_at_ms, duration_ms: run.duration_ms, sample_count: run.sample_count, stable: run.stable })), warnings: sequence.warnings },
      startup_transient: { detected: startup.detected, start_at_ms: startup.startAtMs || null, end_at_ms: startup.endAtMs || null, correlations: startup.correlations, slopes_mv_per_s: startup.slopes || [], pulses: startupPulses },
      segments: runs.map(run => ({ label: run.label, label_text: run.label_text, start_at_ms: run.start_at_ms, end_at_ms: run.end_at_ms, duration_ms: run.duration_ms, sample_count: run.sample_count, stable: run.stable, startup_transient: run.startup_transient, strength: Number(run.strength.toFixed(3)) })),
      channels,
      quality: { duplicate_timestamps: duplicateTimestamps, timestamp_gaps: timestampGaps, median_interval_ms: median(timestampDeltas), warnings },
    };
    return report;
  }

  function toDeviceConfig(report) {
    if (!report || report.overall_status !== 'pass' || !Array.isArray(report.channels) || report.channels.length !== CHANNELS.length) return null;
    const channels = {};
    report.channels.forEach(channel => {
      channels[channel.channel] = {
        logical_mapping: channel.logical_mapping,
        direction: channel.direction,
        off_median_mv: channel.off_median_mv,
        on_median_mv: channel.on_median_mv,
        off_sigma_mv: channel.off_sigma_mv,
        on_sigma_mv: channel.on_sigma_mv,
        delta_mv: channel.delta_mv,
        effect_size: channel.effect_size,
        distribution_overlap: channel.distribution_overlap,
        threshold_mv: channel.threshold_mv_at_baseline_median,
        on_threshold_mv: channel.on_threshold_mv_at_baseline_median,
        off_threshold_mv: channel.off_threshold_mv_at_baseline_median,
        hysteresis_mv: channel.hysteresis_mv,
        quality: channel.quality,
      };
    });
    if (CHANNELS.some(key => !channels[key] || channels[key].quality !== 'pass')) return null;
    return {
      schema: 'air-light-device-calibration-v1',
      source_format: report.format,
      device_id: report.metadata?.device_id || report.metadata?.deviceId || 'unknown',
      overall_status: report.overall_status,
      expected_sequence: report.expected_sequence || [],
      quality: report.quality || {},
      channels,
    };
  }

  function previewSamples(samples, limit = 8000) {
    if (samples.length <= limit) return samples;
    const edge = Math.min(500, Math.floor(limit / 8));
    const middle = samples.length - edge * 2;
    const step = Math.max(1, Math.ceil(middle / Math.max(1, limit - edge * 2)));
    const result = samples.slice(0, edge);
    for (let index = edge; index < samples.length - edge; index += step) result.push(samples[index]);
    result.push(...samples.slice(-edge));
    return result;
  }

  function download(name, content, mimeType) {
    const blob = new Blob([content], { type: mimeType });
    const anchor = document.createElement('a'); anchor.href = URL.createObjectURL(blob); anchor.download = name; anchor.click();
    setTimeout(() => URL.revokeObjectURL(anchor.href), 1000);
  }

  function create(options = {}) {
    let active = false;
    let samples = [];
    let metadata = {};
    let report = null;
    let dirty = true;
    let lastAnalysisAt = 0;
    const listeners = new Set();
    function snapshot() {
      if (active && dirty && (Date.now() - lastAnalysisAt > 180 || samples.length < 10)) {
        // Keep the live page responsive during a long 120 Hz capture. The
        // final report always analyzes every raw sample after finish().
        report = analyze(previewSamples(samples), { ...metadata, preview: true }); dirty = false; lastAnalysisAt = Date.now();
      }
      const sequence = report?.sequence;
      const stageIndex = sequence?.progress ?? 0;
      const expectedSequence = report?.expected_sequence || metadata.expected_sequence || EXPECTED_SEQUENCE;
      const expected = expectedSequence[Math.min(stageIndex, expectedSequence.length - 1)] || 'dark';
      return { active, sampleCount: samples.length, stage: active ? expected : report ? (report.overall_status === 'pass' ? 'complete' : 'review') : 'idle', stageLabel: active ? MODE_LABELS[expected] : report?.overall_label || '未开始', progress: sequence ? `${sequence.progress}/${sequence.total}` : `0/${expectedSequence.length}`, latest: samples[samples.length - 1] || null, report };
    }
    function notify() { const state = snapshot(); listeners.forEach(listener => listener(state)); }
    return {
      start(extra = {}) { active = true; samples = []; report = null; dirty = true; metadata = { transport: options.transport || extra.transport || 'unknown', device_id: extra.deviceId || options.getDeviceId?.() || 'unknown', requested_rate_hz: num(extra.requestedRateHz ?? options.getRequestedRateHz?.()), expected_sequence: extra.expectedSequence || options.expectedSequence || EXPECTED_SEQUENCE, ...extra.transportMetadata, ...extra }; delete metadata.transportMetadata; notify(); return snapshot(); },
      ingest(sample) { if (!active || !sample || !Array.isArray(sample.adcMv || sample.adc_mv)) return false; const normalized = sample.adc_mv ? sample : { ...sample, adc_mv: sample.adcMv }; normalized.received_at_ms = num(normalized.received_at_ms ?? normalized.receivedAtMs) ?? Date.now(); normalized.adc_mv = normalized.adc_mv.slice(0, 4).map(value => num(value) ?? 0); if (normalized.adc_mv.length !== 4) return false; samples.push(normalized); dirty = true; if (samples.length % 10 === 0) notify(); return true; },
      finish() { if (!active && !samples.length) return null; active = false; report = analyze(samples, metadata); dirty = false; lastAnalysisAt = Date.now(); notify(); return report; },
      clear() { active = false; samples = []; metadata = {}; report = null; dirty = true; notify(); },
      snapshot,
      report: () => report || (samples.length ? analyze(samples, metadata) : null),
      getReport: () => report || (samples.length ? analyze(samples, metadata) : null),
      exportJson() { const value = report || (samples.length ? analyze(samples, metadata) : null); if (!value) return false; const stamp = new Date().toISOString().replaceAll(':', '-').replace(/\.\d+Z$/, 'Z'); download(`air-light-calibration-analysis-${options.transport || 'unknown'}-${stamp}.json`, JSON.stringify(value, null, 2), 'application/json;charset=utf-8'); return true; },
      subscribe(listener) { listeners.add(listener); listener(snapshot()); return () => listeners.delete(listener); },
      analyze,
      channels: CHANNELS,
      expectedSequence: options.expectedSequence || EXPECTED_SEQUENCE,
      format: FORMAT,
    };
  }

  global.CalibrationAnalyzer = { create, analyze, toDeviceConfig, channels: CHANNELS, expectedSequence: EXPECTED_SEQUENCE, guidedSequence: GUIDED_SEQUENCE, format: FORMAT, modeLabels: MODE_LABELS };
})(window);
