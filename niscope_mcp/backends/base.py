"""ScopeBackend protocol — typed interface for oscilloscope backends."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Protocol, Any


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class DeviceInfo:
    """Summary of a discovered oscilloscope device."""
    resource_name: str
    model: str
    channels: int
    max_sample_rate: float       # samples/sec
    max_input_frequency: float   # Hz
    faulty: bool = False
    fault_reason: str = ""


@dataclass
class ChannelConfig:
    """Per-channel configuration (read + write)."""
    enabled: bool = False
    vertical_range: float = 5.0         # V peak-to-peak
    vertical_offset: float = 0.0        # V
    vertical_coupling: str = "DC"       # AC, DC, GND
    probe_attenuation: float = 1.0
    input_impedance: float = 1e6        # ohms (50.0 or 1e6)
    bandwidth_filter: str = "FULL"      # FULL, 20MHz, 100MHz, 200MHz (device-dependent)


@dataclass
class TriggerConfig:
    """Trigger settings (read + write)."""
    source: str = "VAL_IMMEDIATE"       # channel name, VAL_EXTERNAL, VAL_IMMEDIATE
    level: float = 0.0                  # V
    slope: str = "POSITIVE"             # POSITIVE, NEGATIVE
    coupling: str = "DC"                # AC, DC, HF_REJECT, LF_REJECT
    holdoff: float = 0.0                # seconds
    type: str = "EDGE"                  # EDGE, WINDOW, RUNT, WIDTH, GLITCH, DIGITAL, SOFTWARE
    # Window trigger extras
    window_low: float = 0.0
    window_high: float = 0.0
    # Runt trigger extras
    runt_low: float = 0.0
    runt_high: float = 0.0
    runt_polarity: str = "POSITIVE"
    # Width/Glitch extras
    width_condition: str = "WITHIN"
    width_low: float = 0.0
    width_high: float = 1e-6
    width_polarity: str = "POSITIVE"


@dataclass
class HorizontalConfig:
    """Horizontal / timing settings (read + write)."""
    min_sample_rate: float = 1e6        # samples/sec
    min_num_pts: int = 10000
    num_records: int = 1
    ref_position: float = 50.0          # percent
    enforce_realtime: bool = True
    acquisition_type: str = "NORMAL"    # NORMAL, FLEX_RES, DDC


@dataclass
class AcquisitionResult:
    """Result of a single-channel acquisition."""
    channel: str
    time: list[float]           # time axis (seconds), downsampled if needed
    voltage: list[float]        # voltage axis (volts), downsampled if needed
    raw_samples: int             # actual number of points acquired
    sample_interval: float       # seconds between samples
    trigger_offset: float        # seconds from trigger to first sample
    stats: dict[str, float]      # min, max, mean, std, peak_to_peak


@dataclass
class MeasurementResult:
    """Advanced waveform measurements."""
    channel: str
    frequency_hz: float
    period_sec: float
    amplitude_vpp: float
    rms_v: float
    min_v: float
    max_v: float
    mean_v: float
    rise_time_sec: float
    fall_time_sec: float
    duty_cycle_pct: float
    num_samples: int
    sample_rate_sps: float


@dataclass
class AutoMeasureResult:
    """Combined acquisition + measurement — one-call signal analysis."""
    channel: str
    # Waveform data (downsampled for transport)
    time: list[float]
    voltage: list[float]
    raw_samples: int
    # Statistics
    stats: dict[str, float]
    # Measurements (may be zero if DC)
    frequency_hz: float
    period_sec: float
    amplitude_vpp: float
    rms_v: float
    min_v: float
    max_v: float
    mean_v: float
    rise_time_sec: float
    fall_time_sec: float
    duty_cycle_pct: float
    sample_rate_sps: float
    # Diagnostic
    signal_type: str          # "periodic", "dc", "noise"
    adapt_history: list[str]  # sampling attempts log


# ── Backend Protocol ──────────────────────────────────────────────────────────

class ScopeBackend(Protocol):
    """Protocol for oscilloscope backends.

    Implementations: DirectBackend, GrpcBackend, MockBackend.
    """

    backend_name: str

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def scan_devices(self) -> list[DeviceInfo]:
        """Discover all connected/available oscilloscopes."""
        ...

    def open_device(self, resource_name: str) -> None:
        """Acquire (or re-use) a persistent session to a device."""
        ...

    def close_device(self, resource_name: str) -> None:
        """Release a device session."""
        ...

    def close_all(self) -> None:
        """Release all open sessions."""
        ...

    # ── Configuration ─────────────────────────────────────────────────────

    def configure_channel(self, resource_name: str, channel: str, config: ChannelConfig) -> None:
        """Apply per-channel settings."""
        ...

    def configure_trigger(self, resource_name: str, config: TriggerConfig) -> None:
        """Apply trigger settings."""
        ...

    def configure_horizontal(self, resource_name: str, config: HorizontalConfig) -> None:
        """Apply horizontal/timing settings."""
        ...

    def auto_setup(self, resource_name: str) -> None:
        """Auto-configure vertical, horizontal, and trigger."""
        ...

    def commit(self, resource_name: str) -> None:
        """Commit all pending configuration to hardware."""
        ...

    # ── Acquisition ───────────────────────────────────────────────────────

    def read_waveform(self, resource_name: str, channel: str,
                      num_samples: int = 10000, timeout: float = 5.0) -> AcquisitionResult:
        """Acquire a single waveform from one channel."""
        ...

    def measure_waveform(self, resource_name: str, channel: str,
                         num_samples: int = 100000, timeout: float = 5.0) -> MeasurementResult:
        """Acquire and compute advanced measurements."""
        ...

    def auto_measure(self, resource_name: str, channel: str,
                     timeout: float = 10.0) -> AutoMeasureResult:
        """Adaptive acquisition: tries multiple sample rates to find the signal,
        returns waveform + statistics + measurements in one call."""
        ...

    # ── State read-back ───────────────────────────────────────────────────

    def get_current_config(self, resource_name: str) -> dict[str, Any]:
        """Return full current configuration as a dict (for sync with InstrumentStudio)."""
        ...
