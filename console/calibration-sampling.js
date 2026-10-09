/*
 * Phase-one light-sensor calibration recorder.
 *
 * This component deliberately records exactly what arrived at the console.  It
 * does not smooth ADC values, decide lamp state, segment the exercise, or send
 * an appliance-control command.  Those decisions belong to the later,
 * reviewable calibration-analysis phase.
 */
(function attachCalibrationSampling(global) {
  'use strict';

  const FORMAT = 'air-light-calibration-sample-v1';
  const STATUS_KEY = 'air-light-calibration-session-status';
  const MODE_SEQUENCE = ['low', 'medium', 'high', 'auto', 'low'];
  const CHANNEL_NAMES = ['low', 'medium', 'high', 'auto'];

  function makeId() {
    if (global.crypto?.randomUUID) return global.crypto.randomUUID();
    return `cal-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function safeNumber(value) {
    return Number.isFinite(Number(value)) ? Number(value) : null;
  }

  function durationText(milliseconds) {
    const seconds = Math.max(0, Math.floor(milliseconds / 1000));
    const minutes = Math.floor(seconds / 60);
    return minutes ? `${minutes} 分 ${String(seconds % 60).padStart(2, '0')} 秒` : `${seconds} 秒`;
  }

  function csvCell(value) {
    if (value === null || value === undefined) return '';
    return `"${String(value).replaceAll('"', '""')}"`;
  }

  function download(name, content, mimeType) {
    const blob = new Blob([content], { type: mimeType });
    const anchor = document.createElement('a');
    anchor.href = URL.createObjectURL(blob);
    anchor.download = name;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(anchor.href), 1000);
  }

  function create(options) {
    const configuration = {
      transport: options.transport,
      getDeviceId: options.getDeviceId || (() => 'unknown'),
      getRequestedRateHz: options.getRequestedRateHz || (() => null),
      maxSamples: Math.max(1000, options.maxSamples || 120000),
      transportMetadata: options.transportMetadata || {},
    };
    let session = null;
    let active = false;
    let lastSequence = null;
    let sequenceGaps = 0;
    let sequenceResets = 0;
    let localDrops = 0;
    const receivedTimes = [];
    const listeners = new Set();
    let changeTimer = null;

    function snapshot() {
      const samples = session?.samples || [];
      const startedAt = session?.started_at_ms || 0;
      const endedAt = session?.ended_at_ms || Date.now();
      const latest = samples.length ? samples[samples.length - 1] : null;
      return {
        active,
        sessionId: session?.session_id || '',
        transport: configuration.transport,
        deviceId: session?.device_id || '',
        startedAt,
        endedAt: active ? null : session?.ended_at_ms || null,
        durationMs: startedAt ? Math.max(0, endedAt - startedAt) : 0,
        sampleCount: samples.length,
        receivedHz: calculateReceivedHz(),
        sequenceGaps,
        sequenceResets,
        localDrops,
        latest,
        stopReason: session?.stop_reason || null,
      };
    }

    function calculateReceivedHz() {
      if (receivedTimes.length < 2) return 0;
      const elapsed = receivedTimes[receivedTimes.length - 1] - receivedTimes[0];
      return elapsed > 0 ? Math.round((receivedTimes.length - 1) * 10000 / elapsed) / 10 : 0;
    }

    function persistStatus() {
      const state = snapshot();
      try {
        localStorage.setItem(STATUS_KEY, JSON.stringify({
          format: 'air-light-calibration-status-v1',
          updated_at_ms: Date.now(),
          active: state.active,
          transport: state.transport,
          device_id: state.deviceId,
          duration_ms: state.durationMs,
          sample_count: state.sampleCount,
          received_hz: state.receivedHz,
          sequence_gaps: state.sequenceGaps,
          local_drops: state.localDrops,
          latest_adc_mv: state.latest?.adc_mv || null,
          stop_reason: state.stopReason,
        }));
      } catch (_) {
        // Recording stays in memory even if browser storage is unavailable.
      }
    }

    function notify(immediate = false) {
      const publish = () => {
        changeTimer = null;
        persistStatus();
        const state = snapshot();
        listeners.forEach(listener => listener(state));
      };
      if (immediate) {
        if (changeTimer) clearTimeout(changeTimer);
        publish();
      } else if (!changeTimer) {
        // A table does not need 120 DOM rebuilds per second. Raw samples are
        // still retained individually; only screen repaint is coalesced.
        changeTimer = setTimeout(publish, 100);
      }
    }

    function start(extra = {}) {
      if (active) return snapshot();
      const startedAt = Date.now();
      const deviceId = String(extra.deviceId || configuration.getDeviceId() || 'unknown').trim() || 'unknown';
      const requestedRate = safeNumber(extra.requestedRateHz ?? configuration.getRequestedRateHz());
      session = {
        format: FORMAT,
        session_id: makeId(),
        started_at_ms: startedAt,
        started_at_iso: new Date(startedAt).toISOString(),
        device_id: deviceId,
        transport: configuration.transport,
        requested_rate_hz: requestedRate,
        phase: 'raw_capture_only',
        plan: {
          expected_mode_sequence: MODE_SEQUENCE,
          dark_reference_required: true,
          automatic_startup_transient_analysis: true,
          fixed_dwell_required: false,
          manual_switch_markers_required: false,
          automatic_segmentation_applied: false,
          raw_transition_samples_preserved: true,
          controls_optocouplers: false,
          writes_device_configuration: false,
        },
        transport_metadata: { ...configuration.transportMetadata, ...(extra.transportMetadata || {}) },
        samples: [],
        statistics: {},
      };
      active = true;
      lastSequence = null;
      sequenceGaps = 0;
      sequenceResets = 0;
      localDrops = 0;
      receivedTimes.length = 0;
      notify(true);
      return snapshot();
    }

    function append(input) {
      if (!active || !session || !Array.isArray(input?.adcMv) || input.adcMv.length !== 4) return false;
      if (session.samples.length >= configuration.maxSamples) {
        localDrops += 1;
        stop('local_sample_limit_reached');
        return false;
      }
      const receivedAt = safeNumber(input.receivedAtMs) || Date.now();
      const adcMv = input.adcMv.map(safeNumber);
      if (adcMv.some(value => value === null)) return false;
      const sequence = Number.isInteger(input.sequence) ? input.sequence : null;
      let gapBefore = null;
      if (sequence !== null) {
        if (lastSequence !== null && sequence > lastSequence + 1) {
          gapBefore = sequence - lastSequence - 1;
          sequenceGaps += gapBefore;
        } else if (lastSequence !== null && sequence < lastSequence) {
          sequenceResets += 1;
        }
        lastSequence = sequence;
      }
      receivedTimes.push(receivedAt);
      const windowStart = receivedAt - 3000;
      while (receivedTimes.length > 2 && receivedTimes[0] < windowStart) receivedTimes.shift();
      const requestedRate = safeNumber(input.requestedRateHz ?? session.requested_rate_hz ?? configuration.getRequestedRateHz());
      const deviceId = String(input.deviceId || configuration.getDeviceId() || session.device_id || 'unknown').trim() || 'unknown';
      if (session.device_id === 'unknown' && deviceId !== 'unknown') session.device_id = deviceId;
      const sample = {
        sample_index: session.samples.length + 1,
        device_id: deviceId,
        transport: configuration.transport,
        received_at_ms: receivedAt,
        received_at_iso: new Date(receivedAt).toISOString(),
        firmware_uptime_ms: safeNumber(input.firmwareUptimeMs),
        sequence,
        sequence_gap_before: gapBefore,
        sequence_gaps_total: sequenceGaps,
        requested_rate_hz: requestedRate,
        received_rate_hz: calculateReceivedHz(),
        adc_mv: adcMv,
      };
      session.samples.push(sample);
      session.statistics = {
        sample_count: session.samples.length,
        sequence_gaps: sequenceGaps,
        sequence_resets: sequenceResets,
        local_drops: localDrops,
        last_received_at_ms: receivedAt,
      };
      notify();
      return true;
    }

    function stop(reason = 'manual_stop') {
      if (!session || !active) return snapshot();
      active = false;
      const endedAt = Date.now();
      session.ended_at_ms = endedAt;
      session.ended_at_iso = new Date(endedAt).toISOString();
      session.stop_reason = reason;
      session.statistics = {
        ...session.statistics,
        sample_count: session.samples.length,
        duration_ms: endedAt - session.started_at_ms,
        final_received_hz: calculateReceivedHz(),
        sequence_gaps: sequenceGaps,
        sequence_resets: sequenceResets,
        local_drops: localDrops,
      };
      notify(true);
      return snapshot();
    }

    function clear() {
      if (active) stop('cleared_by_operator');
      session = null;
      lastSequence = null;
      sequenceGaps = 0;
      sequenceResets = 0;
      localDrops = 0;
      receivedTimes.length = 0;
      notify(true);
    }

    function payload() {
      if (!session) return null;
      const state = snapshot();
      return {
        format: FORMAT,
        exported_at_ms: Date.now(),
        exported_at_iso: new Date().toISOString(),
        session: {
          ...session,
          statistics: {
            ...session.statistics,
            export_received_hz: state.receivedHz,
            export_sequence_gaps: state.sequenceGaps,
          },
          samples: undefined,
        },
        samples: session.samples,
      };
    }

    function csv() {
      if (!session) return '';
      const header = [
        'session_id', 'device_id', 'transport', 'received_at_iso', 'received_at_ms',
        'firmware_uptime_ms', 'sequence', 'sequence_gap_before', 'sequence_gaps_total',
        'requested_rate_hz', 'received_rate_hz', 'adc_low_mv', 'adc_medium_mv', 'adc_high_mv', 'adc_auto_mv',
      ];
      const rows = session.samples.map(sample => [
        session.session_id, sample.device_id, sample.transport, sample.received_at_iso, sample.received_at_ms,
        sample.firmware_uptime_ms, sample.sequence, sample.sequence_gap_before, sample.sequence_gaps_total,
        sample.requested_rate_hz, sample.received_rate_hz, ...sample.adc_mv,
      ].map(csvCell).join(','));
      return `\ufeff${header.join(',')}\r\n${rows.join('\r\n')}`;
    }

    function filename(extension) {
      const stamp = new Date().toISOString().replaceAll(':', '-').replace(/\.\d+Z$/, 'Z');
      return `air-light-calibration-${configuration.transport}-${stamp}.${extension}`;
    }

    return {
      start,
      append,
      stop,
      clear,
      snapshot,
      payload,
      exportCsv: () => {
        if (!session?.samples.length) return false;
        download(filename('csv'), csv(), 'text/csv;charset=utf-8');
        return true;
      },
      exportJson: () => {
        const data = payload();
        if (!data?.samples.length) return false;
        download(filename('json'), JSON.stringify(data, null, 2), 'application/json;charset=utf-8');
        return true;
      },
      subscribe(listener) {
        listeners.add(listener);
        listener(snapshot());
        return () => listeners.delete(listener);
      },
      durationText,
      channelNames: CHANNEL_NAMES,
      modeSequence: MODE_SEQUENCE,
    };
  }

  global.CalibrationSampling = { create, durationText, modeSequence: MODE_SEQUENCE, channelNames: CHANNEL_NAMES };
})(window);
