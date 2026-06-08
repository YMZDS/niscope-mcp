"""NI-SCOPE MCP Server — Hardware backend for NI oscilloscopes.

Only one backend: ``direct`` — uses the niscope Python package to talk to
real NI hardware (PXIe-5160/5164). Requires NI-SCOPE driver on Windows.

If the niscope package is not installed, the server auto-installs it on startup.
See ``niscope_mcp.__main__`` for the auto-setup flow.
"""

from __future__ import annotations
import logging

from .base import ScopeBackend, ChannelConfig, AcquisitionResult, MeasurementResult, DeviceInfo

__all__ = [
    "ScopeBackend", "ChannelConfig", "AcquisitionResult", "MeasurementResult", "DeviceInfo",
]

logger = logging.getLogger(__name__)

_BACKENDS: dict[str, type[ScopeBackend]] = {}


def get_backend(name: str) -> ScopeBackend:
    """Factory: return a ScopeBackend instance by name."""
    if name not in _BACKENDS:
        raise ValueError(f"Unknown backend: {name}. Available: {list(_BACKENDS)}")
    return _BACKENDS[name]()


def register_direct() -> bool:
    """Register the DirectBackend. Returns True if successful, False otherwise."""
    try:
        from .direct import DirectBackend  # noqa: F811
        _BACKENDS["direct"] = DirectBackend
        __all__.append("DirectBackend")
        logger.info("DirectBackend registered (NI-SCOPE driver ready)")
        return True
    except ModuleNotFoundError as e:
        missing = str(e).removeprefix("No module named '").removesuffix("'")
        logger.warning("DirectBackend unavailable: '%s' not installed.", missing)
        return False


def try_install_niscope() -> bool:
    """Attempt to pip install the niscope hardware package."""
    import subprocess
    import sys
    logger.info("Attempting to install niscope package (NI-SCOPE driver)...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "niscope-mcp[hardware]"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            logger.info("niscope package installed successfully")
            # Try registering again
            return register_direct()
        else:
            logger.error("pip install failed:\n%s\n%s", result.stdout, result.stderr)
            return False
    except Exception as e:
        logger.error("Auto-install failed: %s", e)
        return False
