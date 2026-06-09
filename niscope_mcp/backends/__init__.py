"""NI-SCOPE MCP Server — Hardware backend for NI oscilloscopes.

Only one backend: ``direct`` — uses the niscope Python package to talk to
real NI hardware (PXIe-5160/5164). Requires NI-SCOPE driver on Windows.
"""

from __future__ import annotations
import logging

from .base import (
    ScopeBackend, ChannelConfig, TriggerConfig, HorizontalConfig,
    AcquisitionResult, MeasurementResult, AutoMeasureResult, DeviceInfo,
)

__all__ = [
    "ScopeBackend", "ChannelConfig", "TriggerConfig", "HorizontalConfig",
    "AcquisitionResult", "MeasurementResult", "AutoMeasureResult", "DeviceInfo",
]

logger = logging.getLogger(__name__)
_BACKENDS: dict[str, type[ScopeBackend]] = {}


def get_backend(name: str) -> ScopeBackend:
    if name not in _BACKENDS:
        raise ValueError(f"Unknown backend: {name}. Available: {list(_BACKENDS)}")
    return _BACKENDS[name]()


def register_direct() -> bool:
    try:
        from .direct import DirectBackend
        _BACKENDS["direct"] = DirectBackend
        __all__.append("DirectBackend")
        logger.info("DirectBackend registered (NI-SCOPE driver ready)")
        return True
    except ModuleNotFoundError as e:
        missing = str(e).removeprefix("No module named '").removesuffix("'")
        logger.warning("DirectBackend unavailable: '%s' not installed.", missing)
        return False


def try_install_niscope() -> bool:
    import subprocess, sys
    logger.info("Installing niscope hardware driver (this may take 30-60s)...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "niscope>=1.4"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            logger.info("niscope package installed successfully")
            return register_direct()
        else:
            logger.error("pip install failed (code %s):\n%s\n%s",
                         result.returncode, result.stdout, result.stderr)
            return False
    except Exception as e:
        logger.error("Auto-install failed: %s", e)
        return False
