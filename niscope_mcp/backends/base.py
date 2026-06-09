"""Data models and ScopeBackend protocol for oscilloscope backends."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Protocol, Any


@dataclass
class DeviceInfo:
    """Summary of a discovered oscilloscope device."""
    resource_name: str
    model: str
    channels: int
    max_sample_rate: float
    max_input_frequency: float
    faulty: bool = False
    fault_reason: str = ""


@dataclass
class ChannelConfig:
    """Per-channel configuration."""
    enabled: bool = False
    vertical_range: float = 5.0          # Vpp
    vertical_offset: float = 0.0         # V
    vertical_coupling: str = "DC"        # AC, DC, GND
    probe_attenuation: float = 1.0
    input_impedance: float = 1e6         # ohms (50 or 1e6)
    bandwidth_filter: str = "FULL"       # FULL, 20MHZ, 100MHZ, 200MHZ


@dataclass
class TriggerConfig:
    """Trigger settings."""
    source: str = "VAL_IMMEDIATE"
    level: float = 0.0
    slope: str = "POSITIVE"
    coupling: str = "DC"
    holdoff: float = 0.0
    type: str = "EDGE"
    window_low: float = 0.0
    window_high: float = 0.0
    runt_low: float = 0.0
    runt_high: float = 0.0
    runt_polarity: str = "POSITIVE"
    width_condition: str = "WITHIN"
    width_low: float = 0.0
    width_high: float = 1e-6
    width_polarity: str = "POSITIVE"


@dataclass
class HorizontalConfig:
    """Horizontal / timing settings."""
    min_sample_rate: float = 1e6
    min_num_pts: int = 10000
    num_records: int = 1
    ref_position: float = 50.0
    enforce_realtime: bool = True
    acquisition_type: str = "NORMAL"


@dataclass
class AcquisitionResult:
    """Single-channel acquisition result."""
    channel: str
    time: list[float]
    voltage: list[float]
    raw_samples: int
    sample_interval: float
    trigger_offset: float
    stats: dict[str, float]


@dataclass
class MeasurementResult:
    """Advanced waveform measurements."""
    channel: str
    frequency_hz: float = 0.0
    period_sec: float = 0.0
    amplitude_vpp: float = 0.0
    rms_v: float = 0.0
    min_v: float = 0.0
    max_v: float = 0.0
    mean_v: float = 0.0
    rise_time_sec: float = 0.0
    fall_time_sec: float = 0.0
    duty_cycle_pct: float = 0.0
    num_samples: int = 0
    sample_rate_sps: float = 0.0


@dataclass
class AutoMeasureResult(MeasurementResult):
    """Combined acquisition + measurement result."""
    time: list[float] = field(default_factory=list)
    voltage: list[float] = field(default_factory=list)
    raw_samples: int = 0
    stats: dict[str, float] = field(default_factory=dict)
    signal_type: str = "dc"
    adapt_history: list[str] = field(default_factory=list)

    @property
    def vpp(self) -> float:
        return self.amplitude_vpp or self.stats.get("peak_to_peak", 0.0)


class ScopeBackend(Protocol):
    """Protocol for oscilloscope backends."""

    backend_name: str

    def scan_devices(self) -> list[DeviceInfo]: ...
    def open_device(self, resource_name: str) -> None: ...
    def close_device(self, resource_name: str) -> None: ...
    def close_all(self) -> None: ...
    def configure_channel(self, resource_name: str, channel: str, config: ChannelConfig) -> None: ...
    def configure_trigger(self, resource_name: str, config: TriggerConfig) -> None: ...
    def configure_horizontal(self, resource_name: str, config: HorizontalConfig) -> None: ...
    def auto_setup(self, resource_name: str) -> None: ...
    def commit(self, resource_name: str) -> None: ...
    def read_waveform(self, resource_name: str, channel: str,
                      num_samples: int = 10000, timeout: float = 5.0) -> AcquisitionResult: ...
    def measure_waveform(self, resource_name: str, channel: str,
                         num_samples: int = 100000, timeout: float = 5.0) -> MeasurementResult: ...
    def auto_measure(self, resource_name: str, channel: str,
                     timeout: float = 10.0) -> AutoMeasureResult: ...
    def get_current_config(self, resource_name: str) -> dict[str, Any]: ...
