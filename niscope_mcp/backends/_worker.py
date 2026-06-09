#!/usr/bin/env python3
"""Single-channel read worker — advanced measurements + adaptive sampling.

Usage: python _worker.py <resource_name> <channel> [timeout_seconds]
Outputs JSON to stdout.
"""

import sys
import json
import time
import os
import threading


def _watchdog(timeout: float):
    time.sleep(timeout)
    print(json.dumps({
        "ok": False, "channel": sys.argv[2] if len(sys.argv) > 2 else "?",
        "resource_name": sys.argv[1] if len(sys.argv) > 1 else "?",
        "error": f"WORKER SELF-TIMEOUT after {timeout:.0f}s", "skipped": True,
    }))
    sys.stdout.flush()
    os._exit(0)


def read_channel(resource_name: str, channel: str, timeout: float = 10.0) -> dict:
    t_start = time.time()
    import numpy as np
    import niscope as _niscope

    s = _niscope.Session(resource_name, reset_device=True)
    try:
        s.channels[channel].channel_enabled = True
        s.configure_trigger_immediate()
        s.horz_min_num_pts = 100000
        s.commit()

        with s.initiate():
            wfms = s.channels[channel].fetch(num_samples=100000, timeout=min(timeout, 5.0))

        data = wfms[0]
        samples = np.array(data.samples, dtype=np.float64)
        dt = data.x_increment
        sr = 1.0 / dt

        # Quick frequency estimate for adaptive re-sampling
        arr_min, arr_max = float(samples.min()), float(samples.max())
        mid = (arr_max + arr_min) / 2.0
        crossings = 0
        for i in range(1, len(samples)):
            if (samples[i - 1] < mid <= samples[i]) or (samples[i - 1] > mid >= samples[i]):
                crossings += 1
        est_freq = (crossings / 2) / (len(samples) * dt) if crossings >= 2 else 0.0

        # Re-acquire at higher rate for high-frequency signals (>1MHz, <20 pts/period)
        if est_freq > 1e6 and sr < est_freq * 20:
            target_sr = min(est_freq * 30, 1.25e9)
            s.min_sample_rate = target_sr
            s.horz_min_num_pts = 100000
            s.commit()
            with s.initiate():
                wfms = s.channels[channel].fetch(num_samples=100000, timeout=min(timeout, 3.0))
            data = wfms[0]
            samples = np.array(data.samples, dtype=np.float64)
            dt = data.x_increment
            sr = 1.0 / dt
            arr_min, arr_max = float(samples.min()), float(samples.max())

        return _compute_all(samples, dt, channel, resource_name, t_start, est_freq, sr)
    finally:
        s.close()


def _compute_all(samples, dt, channel, resource_name, t_start, est_freq, sr):
    import numpy as np
    N = len(samples)
    arr_min = float(samples.min())
    arr_max = float(samples.max())
    amplitude = arr_max - arr_min
    arr_mean = float(samples.mean())
    arr_std = float(samples.std())

    # ── Percentile-based top/base level detection ─────────────────────────
    top_level, base_level, num_levels = _find_levels(samples, amplitude)

    # ── Edge detection with interpolation ─────────────────────────────────
    lo10 = base_level + 0.10 * (top_level - base_level)
    hi90 = base_level + 0.90 * (top_level - base_level)
    lo20 = base_level + 0.20 * (top_level - base_level)
    hi80 = base_level + 0.80 * (top_level - base_level)
    mid50 = (top_level + base_level) / 2.0

    rise_edges = []  # (start_sample_idx, end_sample_idx) 10%-90%
    fall_edges = []
    rise20_80 = []
    fall20_80 = []
    cross50_up = []  # interpolated crossing times
    cross50_down = []

    for i in range(1, N):
        # Rising edge
        if samples[i - 1] < mid50 <= samples[i]:
            t_cross = i - 1 + (mid50 - samples[i - 1]) / (samples[i] - samples[i - 1]) if samples[i] != samples[i - 1] else i
            cross50_up.append(t_cross)
        # Falling edge
        elif samples[i - 1] > mid50 >= samples[i]:
            t_cross = i - 1 + (mid50 - samples[i - 1]) / (samples[i] - samples[i - 1]) if samples[i] != samples[i - 1] else i
            cross50_down.append(t_cross)

    # 10%-90% rise/fall time using interpolated crossings
    rise_times_10_90 = []
    fall_times_10_90 = []
    rise_times_20_80 = []
    fall_times_20_80 = []

    for ci in range(len(cross50_up)):
        idx_f = cross50_up[ci]
        i = int(idx_f)
        if i < 1 or i >= N - 1:
            continue
        # Search backward for 10% crossing
        r10, r20 = None, None
        for j in range(i, 0, -1):
            if r10 is None and samples[j] <= lo10 < samples[j + 1]:
                r10 = j + (lo10 - samples[j]) / (samples[j + 1] - samples[j])
            if r20 is None and samples[j] <= lo20 < samples[j + 1]:
                r20 = j + (lo20 - samples[j]) / (samples[j + 1] - samples[j])
            if r10 is not None and r20 is not None:
                break
        # Search forward for 90% crossing
        h90, h80 = None, None
        for j in range(i, N - 1):
            if h90 is None and samples[j] < hi90 <= samples[j + 1]:
                h90 = j + (hi90 - samples[j]) / (samples[j + 1] - samples[j])
            if h80 is None and samples[j] < hi80 <= samples[j + 1]:
                h80 = j + (hi80 - samples[j]) / (samples[j + 1] - samples[j])
            if h90 is not None and h80 is not None:
                break
        if r10 is not None and h90 is not None:
            rise_times_10_90.append((h90 - r10) * dt)
        if r20 is not None and h80 is not None:
            rise_times_20_80.append((h80 - r20) * dt)

    for ci in range(len(cross50_down)):
        idx_f = cross50_down[ci]
        i = int(idx_f)
        if i < 1 or i >= N - 1:
            continue
        h90, h80 = None, None
        for j in range(i, 0, -1):
            if h90 is None and samples[j] > hi90 >= samples[j + 1]:
                h90 = j + (hi90 - samples[j]) / (samples[j + 1] - samples[j])
            if h80 is None and samples[j] > hi80 >= samples[j + 1]:
                h80 = j + (hi80 - samples[j]) / (samples[j + 1] - samples[j])
            if h90 is not None and h80 is not None:
                break
        r10, r20 = None, None
        for j in range(i, N - 1):
            if r10 is None and samples[j] > lo10 >= samples[j + 1]:
                r10 = j + (lo10 - samples[j]) / (samples[j + 1] - samples[j])
            if r20 is None and samples[j] > lo20 >= samples[j + 1]:
                r20 = j + (lo20 - samples[j]) / (samples[j + 1] - samples[j])
            if r10 is not None and r20 is not None:
                break
        if h90 is not None and r10 is not None:
            fall_times_10_90.append((r10 - h90) * dt)
        if h80 is not None and r20 is not None:
            fall_times_20_80.append((r20 - h80) * dt)

    # ── Frequency & period from interpolated crossings ────────────────────
    freq = 0.0
    period = 0.0
    duty = 0.0
    jitter_ps = 0.0
    if len(cross50_up) >= 2:
        periods_sample = np.diff(cross50_up)
        period = float(np.mean(periods_sample)) * dt
        if period > 0:
            freq = 1.0 / period
        if len(periods_sample) >= 2:
            jitter_ps = float(np.std(periods_sample)) * dt * 1e12
    if freq == 0 and est_freq > 0:
        freq = est_freq
        period = 1.0 / freq if freq > 0 else 0.0

    # Duty cycle from first complete period
    if len(cross50_up) >= 2 and len(cross50_down) >= 1:
        first_up = cross50_up[0]
        first_down = next((cd for cd in cross50_down if cd > first_up), None)
        second_up = cross50_up[1] if len(cross50_up) > 1 else None
        if first_down and second_up and second_up > first_down:
            duty = (first_down - first_up) / (second_up - first_up) * 100.0
            duty = max(0.5, min(99.5, duty))

    # ── Rise/Fall time averages ───────────────────────────────────────────
    rise_10_90 = float(np.mean(rise_times_10_90)) * 1e9 if rise_times_10_90 else 0.0
    fall_10_90 = float(np.mean(fall_times_10_90)) * 1e9 if fall_times_10_90 else 0.0
    rise_20_80 = float(np.mean(rise_times_20_80)) * 1e9 if rise_times_20_80 else 0.0
    fall_20_80 = float(np.mean(fall_times_20_80)) * 1e9 if fall_times_20_80 else 0.0

    # ── Pulse widths ──────────────────────────────────────────────────────
    pos_width_ns = 0.0
    neg_width_ns = 0.0
    if len(cross50_up) >= 1 and len(cross50_down) >= 1:
        pw_pos = []
        pw_neg = []
        # First edge determines polarity
        if cross50_up[0] < (cross50_down[0] if cross50_down else float('inf')):
            for i in range(min(len(cross50_up), len(cross50_down))):
                if cross50_down[i] > cross50_up[i]:
                    pw_pos.append((cross50_down[i] - cross50_up[i]) * dt)
                if i + 1 < len(cross50_up) and cross50_up[i + 1] > cross50_down[i]:
                    pw_neg.append((cross50_up[i + 1] - cross50_down[i]) * dt)
        else:
            for i in range(min(len(cross50_down), len(cross50_up))):
                if cross50_up[i] > cross50_down[i]:
                    pw_neg.append((cross50_up[i] - cross50_down[i]) * dt)
                if i + 1 < len(cross50_down) and cross50_down[i + 1] > cross50_up[i]:
                    pw_pos.append((cross50_down[i + 1] - cross50_up[i]) * dt)
        if pw_pos:
            pos_width_ns = float(np.mean(pw_pos)) * 1e9
        if pw_neg:
            neg_width_ns = float(np.mean(pw_neg)) * 1e9

    # ── Overshoot / Undershoot ────────────────────────────────────────────
    overshoot_pct = 0.0
    undershoot_pct = 0.0
    if amplitude > 0.01 and num_levels >= 2:
        overshoot_pct = max(0.0, (arr_max - top_level) / (top_level - base_level) * 100.0) if (top_level - base_level) > 0 else 0.0
        undershoot_pct = max(0.0, (base_level - arr_min) / (top_level - base_level) * 100.0) if (top_level - base_level) > 0 else 0.0

    # ── SNR estimate ──────────────────────────────────────────────────────
    snr_db = 0.0
    # Estimate noise as std within each level (exclude transitions)
    if num_levels >= 2 and top_level - base_level > 0.02:
        lo_mask = (samples >= base_level - 0.05) & (samples <= base_level + (top_level - base_level) * 0.15)
        hi_mask = (samples <= top_level + 0.05) & (samples >= top_level - (top_level - base_level) * 0.15)
        noise_samples = np.concatenate([
            samples[lo_mask] - base_level,
            top_level - samples[hi_mask],
        ]) if lo_mask.any() and hi_mask.any() else samples - arr_mean
        noise_rms = float(np.std(noise_samples)) if len(noise_samples) > 10 else arr_std
        if noise_rms > 1e-12:
            signal_rms = (top_level - base_level) / 2.0
            snr_db = 20.0 * np.log10(signal_rms / noise_rms)
    elif arr_std > 1e-12:
        snr_db = 10.0 * np.log10((amplitude / 2.0) ** 2 / arr_std ** 2)

    # ── Burst detection ───────────────────────────────────────────────────
    is_burst = False
    burst_info = ""
    if num_levels >= 2:
        # Look for sustained high periods (>10us) alternating with toggling
        idle_hi = samples > (top_level - 0.1 * (top_level - base_level))
        # Find runs of consecutive idle samples
        run_lengths = []
        run_start = None
        for i, v in enumerate(idle_hi):
            if v and run_start is None:
                run_start = i
            elif not v and run_start is not None:
                run_lengths.append(i - run_start)
                run_start = None
        if run_start is not None:
            run_lengths.append(N - run_start)
        # Burst mode: has long idle runs AND active toggling
        long_idle = [rl for rl in run_lengths if rl * dt > 5e-6]  # >5us idle
        if long_idle and len(run_lengths) > 2:
            idle_time = sum(run_lengths) * dt
            total_time = N * dt
            idle_pct = idle_time / total_time * 100
            if 10 < idle_pct < 90:
                is_burst = True
                burst_info = f"Burst mode — active {100-idle_pct:.0f}% of time, {len(long_idle)} idle periods"

    # ── Signal type classification ────────────────────────────────────────
    # Key insight: frequency + amplitude = periodic, regardless of RMS/mean ratio
    if freq > 500 and amplitude > 0.03:
        signal_type = "burst" if is_burst else "periodic"
    elif amplitude < 0.02:
        signal_type = "dc"
    else:
        signal_type = "noise"

    # ── Downsample for transport ──────────────────────────────────────────
    vlist = [float(v) for v in samples]
    tlist = [float(i * dt) for i in range(N)]
    max_pts = 800
    if len(tlist) > max_pts:
        step = len(tlist) // max_pts
        tlist = tlist[::step]
        vlist = vlist[::step]

    elapsed = time.time() - t_start
    return {
        "ok": True, "channel": channel, "resource_name": resource_name,
        "signal_type": signal_type,
        # Basic
        "frequency_hz": round(freq, 6), "vpp": round(amplitude, 6),
        "min_v": round(arr_min, 6), "max_v": round(arr_max, 6),
        "mean_v": round(arr_mean, 6), "rms_v": round(arr_std, 6),
        "sample_rate_sps": round(sr, 1), "raw_samples": N,
        # Levels
        "top_level": round(top_level, 6), "base_level": round(base_level, 6),
        "num_levels": num_levels,
        # Timing
        "period_sec": round(period, 12), "duty_cycle_pct": round(duty, 2),
        "rise_10_90_ns": round(rise_10_90, 3), "fall_10_90_ns": round(fall_10_90, 3),
        "rise_20_80_ns": round(rise_20_80, 3), "fall_20_80_ns": round(fall_20_80, 3),
        "pos_width_ns": round(pos_width_ns, 3), "neg_width_ns": round(neg_width_ns, 3),
        # Quality
        "overshoot_pct": round(overshoot_pct, 2), "undershoot_pct": round(undershoot_pct, 2),
        "jitter_ps": round(jitter_ps, 2), "snr_db": round(snr_db, 1),
        "edge_count": len(cross50_up) + len(cross50_down),
        # Burst
        "is_burst": is_burst, "burst_info": burst_info,
        # Waveform
        "time": tlist, "voltage": vlist, "elapsed_sec": round(elapsed, 2),
    }


def _find_levels(samples, amplitude):
    """Percentile-based detection of signal levels.
    Returns (top_level, base_level, num_levels)."""
    import numpy as np
    if amplitude < 0.02:
        return float(np.mean(samples)), float(np.mean(samples)), 0

    N = len(samples)
    sorted_s = np.sort(samples)

    # Use tighter percentiles for more accurate level detection
    k_lo = max(1, N // 30)  # bottom ~3%
    k_hi = max(1, N // 30)  # top ~3%
    lo_cluster = sorted_s[:k_lo]
    hi_cluster = sorted_s[-k_hi:]

    base_level = float(np.mean(lo_cluster))
    top_level = float(np.mean(hi_cluster))
    level_gap = top_level - base_level

    if level_gap < amplitude * 0.2:
        return float(np.mean(samples)), float(np.mean(samples)), 1

    # Check for additional levels
    mid = sorted_s[k_lo:-k_hi]
    if len(mid) < 30:
        return top_level, base_level, 2

    hist, edges = np.histogram(mid, bins=min(64, max(8, len(mid) // 30)))
    extra_peaks = 0
    threshold = len(mid) * 0.04  # Need 4% of mid samples for a distinct level
    for i in range(1, len(hist) - 1):
        if hist[i] > hist[i - 1] and hist[i] > hist[i + 1]:
            if hist[i] > threshold:
                extra_peaks += 1

    return top_level, base_level, min(2 + extra_peaks, 5)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({"ok": False, "error": "usage: _worker.py <resource_name> <channel> [timeout]"}))
        sys.exit(1)

    resource_name = sys.argv[1]
    channel = sys.argv[2]
    timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0

    watchdog_timeout = timeout + 3.0
    t = threading.Thread(target=_watchdog, args=(watchdog_timeout,), daemon=True)
    t.start()

    try:
        result = read_channel(resource_name, channel, timeout)
        print(json.dumps(result))
        sys.exit(0)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e), "channel": channel, "resource_name": resource_name}))
        sys.exit(1)
