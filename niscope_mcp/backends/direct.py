"""DirectBackend — uses the niscope Python package to talk to real NI hardware.

Requires: NI-SCOPE driver + niscope pip package installed on the local machine.
"""

from __future__ import annotations
import logging
import numpy as np
import niscope

from .base import (
    ScopeBackend,
    DeviceInfo,
    ChannelConfig,
    TriggerConfig,
    HorizontalConfig,
    AcquisitionResult,
    MeasurementResult,
)

log = logging.getLogger("niscope-mcp.direct")


class DirectBackend:
    """Backend that drives real NI oscilloscopes via the niscope Python API."""

    backend_name = "direct"

    def __init__(self):
        self._sessions: dict[str, niscope.Session] = {}
        self._bad_devices: set[str] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def scan_devices(self) -> list[DeviceInfo]:
        devices: list[DeviceInfo] = []
        empty_slots_in_a_row = 0
        for chassis in range(0, 4):
            for slot in range(2, 19):
                name = f"PXI{chassis}Slot{slot}"
                try:
                    s = niscope.Session(name, reset_device=False)
                    devices.append(DeviceInfo(
                        resource_name=name,
                        model=s.instrument_model,
                        channels=s.channel_count,
                        max_sample_rate=s.max_real_time_sampling_rate,
                        max_input_frequency=s.max_input_frequency,
                        faulty=(name in self._bad_devices),
                        fault_reason="FPGA calibration error" if name in self._bad_devices else "",
                    ))
                    s.close()
                    empty_slots_in_a_row = 0
                except niscope.errors.DriverError:
                    empty_slots_in_a_row += 1
                    # After 4 consecutive empty slots, assume no more devices in this chassis
                    if empty_slots_in_a_row >= 4 and slot > 5:
                        break
            empty_slots_in_a_row = 0
        return devices

    def open_device(self, resource_name: str) -> None:
        if resource_name in self._bad_devices:
            raise niscope.errors.DriverError(
                -1, f"Device {resource_name} previously marked faulty. PXI power cycle needed."
            )
        if resource_name not in self._sessions:
            log.info("Opening persistent session to %s", resource_name)
            self._sessions[resource_name] = niscope.Session(resource_name, reset_device=False)

    def close_device(self, resource_name: str) -> None:
        if resource_name in self._sessions:
            try:
                self._sessions[resource_name].close()
            except Exception as e:
                log.warning("Error closing %s: %s", resource_name, e)
            del self._sessions[resource_name]

    def close_all(self) -> None:
        for name in list(self._sessions):
            self.close_device(name)

    def mark_bad(self, resource_name: str, reason: str) -> None:
        self._bad_devices.add(resource_name)
        self.close_device(resource_name)

    def _session(self, resource_name: str) -> niscope.Session:
        self.open_device(resource_name)
        return self._sessions[resource_name]

    # ── Configuration ─────────────────────────────────────────────────────────

    def configure_channel(self, resource_name: str, channel: str, config: ChannelConfig) -> None:
        s = self._session(resource_name)
        ch = s.channels[channel]
        ch.channel_enabled = config.enabled
        ch.vertical_range = config.vertical_range
        ch.vertical_offset = config.vertical_offset
        ch.vertical_coupling = _to_niscope_enum(niscope.VerticalCoupling, config.vertical_coupling)
        ch.probe_attenuation = config.probe_attenuation
        if hasattr(ch, 'input_impedance'):
            ch.input_impedance = config.input_impedance
        if config.bandwidth_filter != "FULL" and hasattr(ch, 'bandpass_filter_enabled'):
            ch.bandpass_filter_enabled = (config.bandwidth_filter != "FULL")

    def configure_trigger(self, resource_name: str, config: TriggerConfig) -> None:
        s = self._session(resource_name)
        # NI-SCOPE: trigger_source accepts "0", "1", "TRIG" — not "VAL_IMMEDIATE"
        if config.source in ("VAL_IMMEDIATE", "IMMEDIATE"):
            s.configure_trigger_immediate()
        else:
            s.trigger_source = config.source
        s.trigger_level = config.level

        slope_map = {"POSITIVE": niscope.TriggerSlope.POSITIVE, "NEGATIVE": niscope.TriggerSlope.NEGATIVE}
        s.trigger_slope = slope_map.get(config.slope, niscope.TriggerSlope.POSITIVE)

        if hasattr(s, 'trigger_coupling'):
            s.trigger_coupling = _to_niscope_enum(niscope.TriggerCoupling, config.coupling)
        if hasattr(s, 'trigger_holdoff'):
            s.trigger_holdoff = config.holdoff

        # Advanced trigger type
        if config.type != "EDGE":
            try:
                _apply_advanced_trigger(s, config)
            except Exception:
                log.debug("Advanced trigger type %s not supported, falling back to EDGE", config.type)
                s.trigger_type = niscope.TriggerType.EDGE

    def configure_horizontal(self, resource_name: str, config: HorizontalConfig) -> None:
        s = self._session(resource_name)
        s.min_sample_rate = config.min_sample_rate
        s.horz_min_num_pts = config.min_num_pts
        s.horz_num_records = config.num_records
        s.horz_record_ref_position = config.ref_position
        s.horz_enforce_realtime = config.enforce_realtime

    def auto_setup(self, resource_name: str) -> None:
        s = self._session(resource_name)
        s.auto_setup()

    def commit(self, resource_name: str) -> None:
        self._session(resource_name).commit()

    # ── Acquisition ───────────────────────────────────────────────────────────

    def read_waveform(self, resource_name: str, channel: str,
                      num_samples: int = 10000, timeout: float = 5.0) -> AcquisitionResult:
        s = self._session(resource_name)
        s.channels[channel].channel_enabled = True
        s.horz_min_num_pts = num_samples
        s.commit()

        with s.initiate():
            waveforms = s.channels[channel].fetch(num_samples=num_samples, timeout=timeout)

        data = waveforms[0]
        samples = np.array(data.samples, dtype=np.float64)
        t0 = data.relative_initial_x
        dt = data.x_increment

        # Downsample to ~2000 points for transport
        time_arr = [float(t0 + i * dt) for i in range(len(samples))]
        volt_arr = [float(v) for v in samples]
        if len(time_arr) > 2000:
            step = len(time_arr) // 2000
            time_arr = time_arr[::step]
            volt_arr = volt_arr[::step]

        return AcquisitionResult(
            channel=channel,
            time=time_arr,
            voltage=volt_arr,
            raw_samples=len(samples),
            sample_interval=float(dt),
            trigger_offset=float(t0),
            stats={
                "min": float(np.min(samples)),
                "max": float(np.max(samples)),
                "mean": float(np.mean(samples)),
                "std": float(np.std(samples)),
                "peak_to_peak": float(np.max(samples) - np.min(samples)),
            },
        )

    def measure_waveform(self, resource_name: str, channel: str,
                         num_samples: int = 100000, timeout: float = 5.0) -> MeasurementResult:
        s = self._session(resource_name)
        s.channels[channel].channel_enabled = True
        s.horz_min_num_pts = num_samples
        s.commit()

        with s.initiate():
            waveforms = s.channels[channel].fetch(num_samples=num_samples, timeout=timeout)

        data = waveforms[0]
        samples = np.array(data.samples, dtype=np.float64)
        dt = data.x_increment

        meas = _compute_measurements(samples, dt)

        return MeasurementResult(
            channel=channel,
            frequency_hz=round(meas.get("frequency_hz", 0.0), 3),
            period_sec=round(meas.get("period_sec", 0.0), 9),
            amplitude_vpp=round(meas.get("amplitude_vpp", 0.0), 6),
            rms_v=round(meas.get("rms_v", 0.0), 6),
            min_v=round(meas.get("min_v", 0.0), 6),
            max_v=round(meas.get("max_v", 0.0), 6),
            mean_v=round(meas.get("mean_v", 0.0), 6),
            rise_time_sec=round(meas.get("rise_time_sec", 0.0), 9),
            fall_time_sec=round(meas.get("fall_time_sec", 0.0), 9),
            duty_cycle_pct=round(meas.get("duty_cycle_pct", 0.0), 2),
            num_samples=len(samples),
            sample_rate_sps=round(1.0 / dt, 1),
        )

    def auto_measure(self, resource_name: str, channel: str,
                     timeout: float = 10.0, preserve_trigger: bool = False) -> "AutoMeasureResult":
        """Acquire and measure with minimal latency. Uses auto_setup for reliable triggering.

        Set preserve_trigger=True to keep the user's configuration.
        Otherwise calls auto_setup to let the hardware find the signal.
        """
        from .base import AutoMeasureResult

        s = self._session(resource_name)
        s.channels[channel].channel_enabled = True
        history: list[str] = []

        if not preserve_trigger:
            s.auto_setup()
        s.horz_min_num_pts = 100000  # fast: 100k points
        s.commit()

        try:
            with s.initiate():
                wfms = s.channels[channel].fetch(num_samples=100000, timeout=min(timeout, 5.0))
        except Exception as e:
            # Fallback: try once more with 10k points
            s.horz_min_num_pts = 10000
            s.commit()
            with s.initiate():
                wfms = s.channels[channel].fetch(num_samples=10000, timeout=timeout)
            history.append(f"fallback after {e}")

        data = wfms[0]
        samples = np.array(data.samples, dtype=np.float64)
        dt = data.x_increment
        meas = _compute_measurements(samples, dt)
        freq = meas.get("frequency_hz", 0.0)
        pp = float(np.max(samples) - np.min(samples))
        std = float(np.std(samples))
        vrange = s.channels[channel].vertical_range
        min_pp = vrange * 0.01

        if freq > 1000 and pp > min_pp:
            signal_type = "periodic"
        elif pp < min_pp:
            signal_type = "dc"
        else:
            signal_type = "noise"

        history.append(f"{1.0/dt/1e6:.0f}MS/s: freq={freq/1e6:.3f}MHz Vpp={pp:.3f}V ({signal_type})")

        # Downsample for transport (max 2000 pts for fast MCP stdio transfer)
        vlist = [float(v) for v in samples]
        tlist = [float(i * dt) for i in range(len(vlist))]
        if len(tlist) > 2000:
            step = len(tlist) // 2000
            tlist = tlist[::step]
            vlist = vlist[::step]

        meas_final = meas if freq > 0 else {
            "frequency_hz": 0.0, "period_sec": 0.0, "amplitude_vpp": 0.0,
            "rms_v": 0.0, "min_v": 0.0, "max_v": 0.0, "mean_v": 0.0,
            "rise_time_sec": 0.0, "fall_time_sec": 0.0, "duty_cycle_pct": 0.0,
        }

        return AutoMeasureResult(
            channel=channel,
            time=tlist,
            voltage=vlist,
            raw_samples=len(samples),
            stats={
                "min": float(np.min(samples)),
                "max": float(np.max(samples)),
                "mean": float(np.mean(samples)),
                "std": float(std),
                "peak_to_peak": float(pp),
            },
            frequency_hz=freq,
            period_sec=1.0 / freq if freq > 0 else 0.0,
            amplitude_vpp=meas_final.get("amplitude_vpp", pp),
            rms_v=meas_final.get("rms_v", std),
            min_v=float(np.min(samples)),
            max_v=float(np.max(samples)),
            mean_v=float(np.mean(samples)),
            rise_time_sec=meas_final.get("rise_time_sec", 0.0),
            fall_time_sec=meas_final.get("fall_time_sec", 0.0),
            duty_cycle_pct=meas_final.get("duty_cycle_pct", 0.0),
            sample_rate_sps=1.0 / dt if dt > 0 else 0.0,
            signal_type=signal_type,
            adapt_history=history,
        )

    def get_current_config(self, resource_name: str) -> dict:
        s = self._session(resource_name)
        config: dict[str, Any] = {
            "resource_name": resource_name,
            "model": s.instrument_model,
            "channels": {},
            "horizontal": {
                "sample_rate_sps": s.min_sample_rate,
                "record_length": s.horz_min_num_pts,
                "actual_sample_rate_sps": getattr(s, 'horz_sample_rate', 0),
            },
            "trigger": {
                "source": s.trigger_source,
                "level_v": s.trigger_level,
                "slope": str(s.trigger_slope),
            },
        }
        for i in range(s.channel_count):
            ch = s.channels[str(i)]
            config["channels"][str(i)] = {
                "enabled": ch.channel_enabled,
                "vertical_range_vpp": ch.vertical_range,
                "vertical_offset_v": ch.vertical_offset,
                "vertical_coupling": str(ch.vertical_coupling),
                "probe_attenuation": ch.probe_attenuation,
                "input_impedance_ohms": getattr(ch, 'input_impedance', 0),
            }
        return config


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_niscope_enum(enum_cls, value: str):
    """Convert a string to a niscope enum value, with prefix search and fallback."""
    try:
        return getattr(enum_cls, value)
    except AttributeError:
        for prefix in ["VAL_", ""]:
            try:
                return getattr(enum_cls, f"{prefix}{value}")
            except AttributeError:
                pass
        first = next(iter(enum_cls.__members__.values()))
        log.warning("Unknown enum value %r for %s, falling back to %s", value, enum_cls.__name__, first)
        return first


def _apply_advanced_trigger(session, config: TriggerConfig) -> None:
    """Apply non-EDGE trigger types."""
    ttype = config.type.upper()
    if ttype == "WINDOW":
        session.configure_trigger_window(
            config.source, config.window_low, config.window_high,
            _to_niscope_enum(niscope.TriggerWindowMode, "ENTERING")
        )
    elif ttype == "RUNT":
        session.configure_trigger_runt(
            config.source, config.runt_low, config.runt_high,
            _to_niscope_enum(niscope.RuntPolarity, config.runt_polarity)
        )
    elif ttype == "WIDTH":
        session.configure_trigger_width(
            config.source, config.level,
            config.width_low, config.width_high,
            _to_niscope_enum(niscope.WidthCondition, config.width_condition),
            _to_niscope_enum(niscope.WidthPolarity, config.width_polarity)
        )
    elif ttype == "GLITCH":
        session.configure_trigger_glitch(
            config.source, config.level,
            config.width_low,
            _to_niscope_enum(niscope.GlitchPolarity, config.width_polarity)
        )
    elif ttype == "DIGITAL":
        session.configure_trigger_digital(config.source)
    elif ttype == "SOFTWARE":
        session.configure_trigger_software()


def _compute_measurements(samples: np.ndarray, dt: float) -> dict:
    """Compute signal measurements from waveform samples.

    Uses (max+min)/2 as the crossing threshold (50% amplitude),
    which is more robust than mean for non-symmetric waveforms.
    """
    N = len(samples)
    min_val = float(np.min(samples))
    max_val = float(np.max(samples))
    amplitude = max_val - min_val
    mean_val = float(np.mean(samples))
    rms = float(np.sqrt(np.mean((samples - mean_val) ** 2)))

    # 50% amplitude threshold — handles non-50% duty cycle correctly
    mid = (max_val + min_val) / 2.0

    crossings_up = []
    crossings_down = []
    for i in range(1, N):
        if samples[i - 1] < mid <= samples[i]:
            crossings_up.append(i)
        elif samples[i - 1] > mid >= samples[i]:
            crossings_down.append(i)

    freq = 0.0
    period = 0.0
    duty = 0.0
    rise_time_s = 0.0
    fall_time_s = 0.0

    if len(crossings_up) >= 2:
        periods_sample = np.diff(crossings_up)
        avg_period = float(np.mean(periods_sample))
        period = avg_period * dt
        if period > 0:
            freq = 1.0 / period

        lo = min_val + 0.1 * amplitude
        hi = min_val + 0.9 * amplitude

        # Average rise times across multiple edges for robustness
        rise_times = []
        for ci in crossings_up:
            if ci >= 1:
                rs = ci - 1
                while rs > 0 and samples[rs] > lo:
                    rs -= 1
                re = ci
                while re < N - 1 and samples[re] < hi:
                    re += 1
                rise_times.append(float((re - rs) * dt))
        if rise_times:
            rise_time_s = float(np.mean(rise_times))

        # Average fall times
        fall_times = []
        for ci in crossings_down:
            if ci < N - 1:
                fs = ci - 1
                while fs > 0 and samples[fs] < hi:
                    fs -= 1
                fe = ci
                while fe < N - 1 and samples[fe] > lo:
                    fe += 1
                fall_times.append(float((fe - fs) * dt))
        if fall_times:
            fall_time_s = float(np.mean(fall_times))

        # Duty cycle from first complete period
        first_up = crossings_up[0]
        first_down = None
        for cd in crossings_down:
            if cd > first_up:
                first_down = cd
                break
        if first_down and len(crossings_up) > 1:
            high_samples = first_down - first_up
            total = crossings_up[1] - first_up
            if total > 0:
                duty = min(high_samples / total * 100.0, 99.9)

    return {
        "frequency_hz": freq,
        "period_sec": period,
        "amplitude_vpp": amplitude,
        "rms_v": rms,
        "min_v": min_val,
        "max_v": max_val,
        "mean_v": mean_val,
        "rise_time_sec": rise_time_s,
        "fall_time_sec": fall_time_s,
        "duty_cycle_pct": duty,
        "num_crossings": len(crossings_up) + len(crossings_down),
    }
