"""MockBackend — simulated oscilloscope for testing without hardware.

Generates synthetic waveforms so the full MCP tool-chain can be tested
and demonstrated on any machine, including CI/CD environments.
"""

from __future__ import annotations
import math
import time
import random
import logging

from .base import (
    ScopeBackend, DeviceInfo, ChannelConfig, TriggerConfig, HorizontalConfig,
    AcquisitionResult, MeasurementResult,
)

log = logging.getLogger("niscope-mcp.mock")

# Pre-defined mock devices
_MOCK_DEVICES = [
    DeviceInfo("MockDev1", "NI PXIe-5160 (2CH) Simulated", 2, 1.25e9, 500e6),
    DeviceInfo("MockDev2", "NI PXIe-5164 (4CH) Simulated", 4, 2.5e9, 1e9),
]


class MockBackend:
    """Simulated oscilloscope backend — generates realistic test waveforms."""

    backend_name = "mock"

    def __init__(self):
        self._configs: dict[str, dict] = {}
        # Per-device signal shape
        self._signal = {
            "MockDev1": {"shape": "sine", "freq": 1e6, "amp": 1.0, "offset": 0.0, "noise": 0.02},
            "MockDev2": {"shape": "square", "freq": 10e3, "amp": 3.3, "offset": 1.65, "noise": 0.05},
        }
        self._sessions: set[str] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def scan_devices(self) -> list[DeviceInfo]:
        return _MOCK_DEVICES

    def open_device(self, resource_name: str) -> None:
        self._sessions.add(resource_name)
        if resource_name not in self._configs:
            self._configs[resource_name] = {
                "channels": {
                    str(i): ChannelConfig(enabled=True, vertical_range=5.0)
                    for i in range(2)
                },
                "trigger": TriggerConfig(),
                "horizontal": HorizontalConfig(),
            }

    def close_device(self, resource_name: str) -> None:
        self._sessions.discard(resource_name)

    def close_all(self) -> None:
        self._sessions.clear()

    # ── Configuration ─────────────────────────────────────────────────────────

    def configure_channel(self, resource_name: str, channel: str, config: ChannelConfig) -> None:
        self._configs[resource_name]["channels"][channel] = config

    def configure_trigger(self, resource_name: str, config: TriggerConfig) -> None:
        self._configs[resource_name]["trigger"] = config

    def configure_horizontal(self, resource_name: str, config: HorizontalConfig) -> None:
        self._configs[resource_name]["horizontal"] = config

    def auto_setup(self, resource_name: str) -> None:
        cfg = self._configs[resource_name]
        sig = self._signal.get(resource_name, {"amp": 1.0})
        cfg["channels"]["0"].vertical_range = max(sig["amp"] * 2.2, 0.01)
        cfg["horizontal"].min_sample_rate = sig.get("freq", 1e6) * 20
        cfg["horizontal"].min_num_pts = 10000

    def commit(self, resource_name: str) -> None:
        pass  # Mock devices are always ready

    # ── Acquisition ───────────────────────────────────────────────────────────

    def read_waveform(self, resource_name: str, channel: str,
                      num_samples: int = 10000, timeout: float = 5.0) -> AcquisitionResult:
        sig = self._signal.get(resource_name, {"shape": "sine", "freq": 1e6, "amp": 1.0, "offset": 0.0, "noise": 0.02})
        hcfg = self._configs[resource_name]["horizontal"]
        sr = hcfg.min_sample_rate
        n = min(num_samples, hcfg.min_num_pts)
        dt = 1.0 / sr

        t = [i * dt for i in range(n)]
        v = _generate_waveform(t, sig["shape"], sig["freq"], sig["amp"], sig["offset"], sig.get("noise", 0.01))

        # Downsample for transport
        if len(t) > 2000:
            step = len(t) // 2000
            t = t[::step]
            v = v[::step]

        v_arr = list(v)
        return AcquisitionResult(
            channel=channel,
            time=t,
            voltage=v_arr,
            raw_samples=len(v_arr),
            sample_interval=dt,
            trigger_offset=0.0,
            stats={
                "min": min(v_arr), "max": max(v_arr),
                "mean": sum(v_arr) / len(v_arr),
                "std": _std(v_arr),
                "peak_to_peak": max(v_arr) - min(v_arr),
            },
        )

    def measure_waveform(self, resource_name: str, channel: str,
                         num_samples: int = 100000, timeout: float = 5.0) -> MeasurementResult:
        result = self.read_waveform(resource_name, channel, num_samples, timeout)
        sig = self._signal.get(resource_name, {"freq": 1e6, "amp": 1.0})
        return MeasurementResult(
            channel=channel,
            frequency_hz=sig["freq"],
            period_sec=1.0 / sig["freq"] if sig["freq"] > 0 else 0,
            amplitude_vpp=sig["amp"] * 2,
            rms_v=sig["amp"] * 0.707 if sig["shape"] == "sine" else sig["amp"],
            min_v=result.stats["min"],
            max_v=result.stats["max"],
            mean_v=result.stats["mean"],
            rise_time_sec=1e-9,
            fall_time_sec=1e-9,
            duty_cycle_pct=50.0,
            num_samples=result.raw_samples,
            sample_rate_sps=1.0 / result.sample_interval,
        )

    def auto_measure(self, resource_name: str, channel: str,
                     timeout: float = 10.0) -> "AutoMeasureResult":
        from .base import AutoMeasureResult
        sig = self._signal.get(resource_name, {"shape": "sine", "freq": 1e6, "amp": 1.0, "offset": 0.0, "noise": 0.02})
        result = self.read_waveform(resource_name, channel, 2000, timeout)
        return AutoMeasureResult(
            channel=channel,
            time=result.time,
            voltage=result.voltage,
            raw_samples=result.raw_samples,
            stats=result.stats,
            frequency_hz=sig["freq"],
            period_sec=1.0 / sig["freq"] if sig["freq"] > 0 else 0.0,
            amplitude_vpp=sig["amp"] * 2,
            rms_v=sig["amp"] * 0.707 if sig["shape"] == "sine" else sig["amp"],
            min_v=result.stats["min"],
            max_v=result.stats["max"],
            mean_v=result.stats["mean"],
            rise_time_sec=1e-9,
            fall_time_sec=1e-9,
            duty_cycle_pct=50.0,
            sample_rate_sps=1.0 / result.sample_interval,
            signal_type="periodic",
            adapt_history=["mock: synthetic signal"],
        )

    def get_current_config(self, resource_name: str) -> dict:
        cfg = self._configs.get(resource_name, {})
        return {
            "resource_name": resource_name,
            "model": "Simulated Oscilloscope",
            "channels": {
                ch: {
                    "enabled": c.enabled,
                    "vertical_range_vpp": c.vertical_range,
                    "vertical_offset_v": c.vertical_offset,
                    "vertical_coupling": c.vertical_coupling,
                    "probe_attenuation": c.probe_attenuation,
                    "input_impedance_ohms": c.input_impedance,
                }
                for ch, c in cfg.get("channels", {}).items()
            },
            "horizontal": {
                "sample_rate_sps": cfg.get("horizontal", HorizontalConfig()).min_sample_rate,
                "record_length": cfg.get("horizontal", HorizontalConfig()).min_num_pts,
            },
            "trigger": {
                "source": cfg.get("trigger", TriggerConfig()).source,
                "level_v": cfg.get("trigger", TriggerConfig()).level,
            },
        }


# ── Waveform generators ──────────────────────────────────────────────────────

def _generate_waveform(t: list[float], shape: str, freq: float, amp: float, offset: float, noise: float) -> list[float]:
    w = 2 * math.pi * freq
    if shape == "sine":
        values = [amp * math.sin(w * ti) + offset for ti in t]
    elif shape == "square":
        values = [amp * (1.0 if math.sin(w * ti) >= 0 else -1.0) + offset for ti in t]
    elif shape == "triangle":
        values = [amp * (2.0 * abs(2.0 * (ti * freq - math.floor(ti * freq + 0.5))) - 1.0) + offset for ti in t]
    elif shape == "sawtooth":
        values = [amp * (2.0 * (ti * freq - math.floor(ti * freq)) - 1.0) + offset for ti in t]
    elif shape == "dc":
        values = [offset] * len(t)
    elif shape == "noise":
        values = [random.gauss(offset, amp) for _ in t]
    else:
        values = [offset] * len(t)

    # Add noise
    if noise > 0:
        values = [v + random.gauss(0, noise) for v in values]
    return values


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
