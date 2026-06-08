"""NI-SCOPE MCP Server — Pluggable instrument backends.

Each backend implements the ScopeBackend protocol defined in base.py.
The server selects a backend via the --backend CLI flag:
    direct  — local niscope Python package (requires NI driver)
    mock    — simulated device for testing (no hardware needed)
    grpc    — (future) NI gRPC Device Server
"""

from __future__ import annotations

from .base import ScopeBackend, ChannelConfig, AcquisitionResult, MeasurementResult, DeviceInfo
from .direct import DirectBackend
from .mock import MockBackend

__all__ = [
    "ScopeBackend", "ChannelConfig", "AcquisitionResult", "MeasurementResult", "DeviceInfo",
    "DirectBackend", "MockBackend",
]

_BACKENDS = {
    "direct": DirectBackend,
    "mock": MockBackend,
}


def get_backend(name: str) -> ScopeBackend:
    """Factory: return a ScopeBackend instance by name."""
    if name not in _BACKENDS:
        raise ValueError(f"Unknown backend: {name}. Available: {list(_BACKENDS)}")
    return _BACKENDS[name]()
