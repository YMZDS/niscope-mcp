#!/usr/bin/env python3
"""
NI-SCOPE MCP Server — AI-controlled PXIe oscilloscope.

Process management guarantees:
  1. One channel at a time via short-lived worker process
  2. Before each channel: kill any residual worker processes
  3. After each channel: kill the worker process explicitly
  4. Timeout >10s → skip channel with reason logged
  5. After all channels: final cleanup of all residual processes
  6. Mutex ensures at most one worker process at any moment

Usage:
    python -m niscope_mcp            # direct NI hardware backend
    niscope-mcp                      # after pip install
"""

from __future__ import annotations
import argparse
import asyncio
import json as _json
import logging
import os as _os
import subprocess as _subprocess
import sys
import threading as _threading
import time as _time
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from niscope_mcp.backends import get_backend, ScopeBackend
from niscope_mcp.backends.base import ChannelConfig, TriggerConfig, HorizontalConfig

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("niscope-mcp")

# ── Worker process management ──────────────────────────────────────────────

_WORKER_SCRIPT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "backends", "_worker.py")

# Single-process guard
_active_worker_pid: int | None = None
_worker_lock = _threading.Lock()


def _kill_stale_workers() -> int:
    """Kill any leftover niscope worker processes. Returns count of killed processes.

    Uses WMIC to find all python.exe processes and kills those running _worker.py.
    Called BEFORE each channel read and AFTER all channels.
    """
    killed = 0
    try:
        result = _subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "processid,commandline"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.split("\n"):
            # Look for lines containing the _worker.py script path (real worker processes)
            if "_worker.py" in line and ("backends" in line):
                parts = line.strip().split()
                # First column is ProcessId in WMIC output
                if parts and parts[0].isdigit():
                    pid = int(parts[0])
                    if pid != _os.getpid():
                        try:
                            _os.kill(pid, 9)
                            killed += 1
                        except Exception:
                            pass
    except Exception:
        pass
    if killed:
        log.info("Killed %d stale worker process(es)", killed)
    return killed


def _run_worker(resource_name: str, channel: str, timeout: float = 10.0) -> dict:
    """Spawn a single-channel reader worker process.

    Protocol:
      1. Kill any stale workers from previous runs
      2. Spawn new worker
      3. Wait for result (with timeout)
      4. Explicitly kill worker process
      5. Timeout >10s → kill + skip + reason

    Returns parsed JSON dict.
    """
    global _active_worker_pid

    # Step 1: Kill residual processes before starting
    _kill_stale_workers()

    # Brief delay to allow NI-SCOPE driver to fully release from previous session
    _time.sleep(0.3)

    t0 = _time.time()
    log.info("Starting worker for %s CH%s (timeout=%.0fs)", resource_name, channel, timeout)

    proc = _subprocess.Popen(
        [sys.executable, _WORKER_SCRIPT, resource_name, channel, str(timeout)],
        stdin=_subprocess.DEVNULL,
        stdout=_subprocess.PIPE, stderr=_subprocess.PIPE,
        env={**_os.environ, "PYTHONUNBUFFERED": "1"},
    )

    # Register active worker PID under lock
    with _worker_lock:
        _active_worker_pid = proc.pid

    try:
        stdout, stderr = proc.communicate(timeout=timeout + 2)
        elapsed = _time.time() - t0
        result = _json.loads(stdout.decode("utf-8", errors="replace"))
        result["elapsed_sec"] = round(elapsed, 2)
        return result
    except _subprocess.TimeoutExpired:
        # Timeout: kill worker, skip channel
        proc.kill()
        proc.wait(timeout=2)
        elapsed = _time.time() - t0
        return {
            "ok": False,
            "channel": channel,
            "resource_name": resource_name,
            "error": f"TIMEOUT after {elapsed:.0f}s — channel skipped",
            "skipped": True,
            "elapsed_sec": round(elapsed, 2),
        }
    except _json.JSONDecodeError:
        return {
            "ok": False,
            "channel": channel,
            "resource_name": resource_name,
            "error": f"Worker output parse error: {stderr.decode('utf-8', errors='replace')[:200]}",
            "skipped": True,
        }
    finally:
        # Step 4: Explicitly kill worker after reading (regardless of success/failure)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
            log.info("Killed worker PID %d for %s CH%s", proc.pid, resource_name, channel)
        with _worker_lock:
            _active_worker_pid = None


def _cleanup_all() -> int:
    """Kill ALL stale workers. Called after reading all channels.

    Also verifies no worker is registered as active.
    """
    global _active_worker_pid
    with _worker_lock:
        if _active_worker_pid is not None:
            try:
                _os.kill(_active_worker_pid, 9)
                log.info("Killed active worker PID %d during final cleanup", _active_worker_pid)
            except Exception:
                pass
            _active_worker_pid = None
    return _kill_stale_workers()


# ── Global backend ─────────────────────────────────────────────────────────
_backend: ScopeBackend | None = None


def backend() -> ScopeBackend:
    assert _backend is not None, "Backend not initialized"
    return _backend


# ── Formatters ─────────────────────────────────────────────────────────────

def _fmt_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a GitHub-flavored markdown table."""
    max_w = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            max_w[i] = max(max_w[i], len(cell))
    sep = "| " + " | ".join("-" * w for w in max_w) + " |"
    header = "| " + " | ".join(h.ljust(w) for h, w in zip(headers, max_w)) + " |"
    body = "\n".join(
        "| " + " | ".join(c.ljust(w) for c, w in zip(row, max_w)) + " |"
        for row in rows
    )
    return f"{header}\n{sep}\n{body}"


def _fmt_ascii_waveform(voltage: list[float], width: int = 100, height: int = 16,
                         vmin: float | None = None, vmax: float | None = None,
                         period_sec: float = 0.0, trigger_level: float | None = None) -> str:
    """Oscilloscope-style waveform: connected line trace + grid + time axis."""
    if len(voltage) < 2:
        return "(insufficient data points)"
    if vmin is None:
        vmin = min(voltage)
    if vmax is None:
        vmax = max(voltage)
    span = vmax - vmin
    if span < 1e-12:
        span = 0.1; vmax = vmin + 0.1
    pad = span * 0.05
    vmin -= pad; vmax += pad; span = vmax - vmin

    if len(voltage) > width * 4:
        voltage = voltage[:width * 4]
    step = max(1, len(voltage) // width)
    points = []
    for xi in range(width):
        chunk = voltage[xi * step: (xi + 1) * step]
        if chunk:
            points.append((sum(chunk) / len(chunk) - vmin) / span)
        else:
            points.append(0.5)

    n_grid_lines = 4
    grid_rows = [int(height * i / n_grid_lines) for i in range(1, n_grid_lines)]

    canvas = [[" "] * width for _ in range(height)]

    for xi in range(width):
        y_float = (1.0 - points[xi]) * (height - 1)
        yi = max(0, min(height - 1, int(round(y_float))))
        if xi == 0:
            canvas[yi][xi] = "●"  # ●
        else:
            prev_y_float = (1.0 - points[xi - 1]) * (height - 1)
            prev_yi = max(0, min(height - 1, int(round(prev_y_float))))
            if yi == prev_yi:
                canvas[yi][xi] = "─"  # ─
            elif yi < prev_yi:
                dy = prev_yi - yi
                for y in range(yi, prev_yi + 1):
                    if dy == 1:
                        ch = "╱" if y == yi else " "  # ╱
                    elif y == yi:
                        ch = "╱"  # ╱
                    elif y == prev_yi:
                        ch = "╲"  # ╲
                    else:
                        ch = "│"  # │
                    if 0 <= y < height and ch != " ":
                        canvas[y][xi] = ch
            else:
                dy = yi - prev_yi
                for y in range(prev_yi, yi + 1):
                    if dy == 1:
                        ch = "╲" if y == prev_yi else " "  # ╲
                    elif y == prev_yi:
                        ch = "╲"  # ╲
                    elif y == yi:
                        ch = "╱"  # ╱
                    else:
                        ch = "│"  # │
                    if 0 <= y < height and ch != " ":
                        canvas[y][xi] = ch

    for gy in grid_rows:
        for xi in range(width):
            if canvas[gy][xi] == " ":
                canvas[gy][xi] = "┄" if xi % 3 != 0 else " "  # ┄

    if trigger_level is not None and vmin <= trigger_level <= vmax:
        t_y_float = (1.0 - (trigger_level - vmin) / span) * (height - 1)
        t_yi = max(0, min(height - 1, int(round(t_y_float))))
        if 0 <= t_yi < height:
            for xi in range(0, width, 4):
                if canvas[t_yi][xi] in (" ", "┄"):
                    canvas[t_yi][xi] = "╌"  # ╌

    label_width = 9
    lines = []
    lines.append(f"  {vmax:+.3f}V")
    lines.append(f"  ┌{'─' * width}┐")  # ┌─...─┐

    for yi in range(height):
        row_chars = "".join(canvas[yi])
        is_grid = yi in grid_rows
        is_top = yi == 0
        is_bot = yi == height - 1
        if is_top:
            prefix = f"{vmax:+.3f}V".rjust(label_width) + " ┤"  # ┤
        elif is_bot:
            prefix = f"{vmin:+.3f}V".rjust(label_width) + " ┤"  # ┤
        elif is_grid:
            v_at = vmax - (yi / (height - 1)) * span
            prefix = f"{v_at:+.3f}V".rjust(label_width) + " ┼"  # ┼
        else:
            prefix = " " * label_width + " │"  # │
        lines.append(f"{prefix}{row_chars}│")  # │

    lines.append(f"  └{'─' * width}┘")  # └─...─┘
    lines.append(f"  {vmin:+.3f}V")

    if period_sec > 0:
        n_ticks = 5
        total_time = period_sec * (width / max(1, _count_periods(points)))
        tick_labels = []
        for i in range(n_ticks + 1):
            x = int(i * width / n_ticks)
            t = i * total_time / n_ticks
            if t < 1e-9:
                tick_labels.append((x, "0"))
            elif t < 1e-6:
                tick_labels.append((x, f"{t*1e9:.0f}ns"))
            elif t < 1e-3:
                tick_labels.append((x, f"{t*1e6:.0f}µs"))
            elif t < 1:
                tick_labels.append((x, f"{t*1e3:.0f}ms"))
            else:
                tick_labels.append((x, f"{t:.1f}s"))
        time_line = [" "] * (label_width + 2 + width)
        for x, label in tick_labels:
            pos = label_width + 2 + x
            for j, ch in enumerate(label):
                if pos + j < len(time_line):
                    time_line[pos + j] = ch
        lines.append("".join(time_line).rstrip())

    return "\n".join(lines)


def _count_periods(points: list[float]) -> int:
    if len(points) < 2:
        return 1
    mid = 0.5
    crossings = 0
    for i in range(1, len(points)):
        if (points[i - 1] < mid <= points[i]) or (points[i - 1] > mid >= points[i]):
            crossings += 1
    return max(1, crossings // 2)


def _fmt_status_icon(ok: bool) -> str:
    return "✓ OK" if ok else "✗ FAULT"  # ✓ ✗


# ── MCP Server ─────────────────────────────────────────────────────────────

server = Server("niscope-mcp")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_devices",
            description="""List all available oscilloscopes with model, channel count,
max sample rate, and health status. Call this FIRST before any other tool to
discover device resource names.""",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_current_config",
            description="""Read the current oscilloscope hardware configuration:
ALL channel settings (vertical range, coupling, offset, impedance, probe),
horizontal timebase, and trigger setup.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_name": {"type": "string", "description": "Device resource name from list_devices."},
                },
                "required": ["resource_name"],
            },
        ),
        Tool(
            name="configure_scope",
            description="""Configure oscilloscope settings. Any subset of parameters;
omitted ones keep current value.

Channel: vertical_range (Vpp), vertical_coupling (AC/DC/GND), vertical_offset (V),
  probe_attenuation (1/10/100), input_impedance (50/1e6), bandwidth_filter (FULL/20MHZ/100MHZ/200MHZ)
Horizontal: min_sample_rate (S/s), min_record_length (pts), acquisition_type (NORMAL/FLEX_RES/DDC)
Trigger: trigger_source, trigger_level (V), trigger_slope (POSITIVE/NEGATIVE),
  trigger_coupling (AC/DC/HF_REJECT/LF_REJECT), trigger_holdoff (s),
  trigger_type (EDGE/WINDOW/RUNT/WIDTH/GLITCH/SOFTWARE)""",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_name": {"type": "string"},
                    "channel": {"type": "string", "default": "0"},
                    "vertical_range": {"type": "number"},
                    "vertical_coupling": {"type": "string", "enum": ["AC", "DC", "GND"]},
                    "vertical_offset": {"type": "number"},
                    "probe_attenuation": {"type": "number"},
                    "input_impedance": {"type": "number"},
                    "bandwidth_filter": {"type": "string", "enum": ["FULL", "20MHZ", "100MHZ", "200MHZ"]},
                    "min_sample_rate": {"type": "number"},
                    "min_record_length": {"type": "integer"},
                    "acquisition_type": {"type": "string", "enum": ["NORMAL", "FLEX_RES", "DDC"]},
                    "trigger_source": {"type": "string"},
                    "trigger_level": {"type": "number"},
                    "trigger_slope": {"type": "string", "enum": ["POSITIVE", "NEGATIVE"]},
                    "trigger_coupling": {"type": "string", "enum": ["AC", "DC", "HF_REJECT", "LF_REJECT"]},
                    "trigger_holdoff": {"type": "number"},
                    "trigger_type": {"type": "string", "enum": ["EDGE", "WINDOW", "RUNT", "WIDTH", "GLITCH", "SOFTWARE"]},
                },
                "required": ["resource_name"],
            },
        ),
        Tool(
            name="auto_setup",
            description="Auto-configure the oscilloscope (equivalent to pressing 'Autoset').",
            inputSchema={
                "type": "object",
                "properties": {"resource_name": {"type": "string"}},
                "required": ["resource_name"],
            },
        ),
        Tool(
            name="read_waveform",
            description="""Capture one channel with adaptive sampling. Returns measurements,
ASCII waveform, and signal fingerprint. For reading ALL channels at once, use
read_all_channels instead.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_name": {"type": "string"},
                    "channel": {"type": "string", "default": "0"},
                    "timeout_seconds": {"type": "number", "default": 10.0},
                },
                "required": ["resource_name"],
            },
        ),
        Tool(
            name="measure_waveform",
            description="Same as read_waveform — adaptive acquisition with full measurement suite.",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_name": {"type": "string"},
                    "channel": {"type": "string", "default": "0"},
                    "timeout_seconds": {"type": "number", "default": 10.0},
                },
                "required": ["resource_name"],
            },
        ),
        Tool(
            name="read_all_channels",
            description="""PRIMARY TOOL — call this for any 'read the oscilloscope' request.
Scans ALL devices across ALL slots, draws the PXIe chassis map, captures every
channel with strict process isolation:
- Kill stale workers before each channel read
- Spawn fresh worker per channel, kill it immediately after
- Single-process guard: at most one worker at any moment
- Channels exceeding timeout (default 10s) are skipped with reason
- Final cleanup: kill ALL residual processes""",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_name": {"type": "string"},
                    "timeout_seconds": {"type": "number", "default": 10.0},
                },
                "required": ["resource_name"],
            },
        ),
        Tool(
            name="help",
            description="Show usage guide, parameter reference, and typical workflow examples.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


# ── Tool dispatcher ────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    resource = arguments.get("resource_name", "")
    try:
        match name:
            case "list_devices":      return await _list_devices()
            case "get_current_config": return await _get_config(arguments)
            case "configure_scope":    return await _configure(arguments)
            case "auto_setup":         return await _auto_setup(arguments)
            case "read_waveform":      return await _read_waveform(arguments)
            case "measure_waveform":   return await _measure(arguments)
            case "read_all_channels":  return await _read_all(arguments)
            case "help":               return await _help()
            case _: return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        import traceback as _tb
        err_str = str(e)
        if resource and ("TopazADCPhaseParityCalibration" in err_str or "Internal Hardware Error" in err_str):
            backend().mark_bad(resource, err_str[:120])
        log.exception("Error in %s", name)
        tb = _tb.format_exc()
        tb_short = '\n'.join(tb.strip().split('\n')[-6:])
        return [TextContent(type="text", text=f"Error: {e}\n\nTraceback (last 6 lines):\n{tb_short}")]
    finally:
        if resource:
            try:
                backend().close_device(resource)
            except Exception:
                pass


# ── Handlers ───────────────────────────────────────────────────────────────

async def _list_devices() -> list[TextContent]:
    devices = backend().scan_devices()
    if not devices:
        return [TextContent(type="text", text="No oscilloscopes found.")]
    rows = []
    for d in devices:
        sr_gs = d.max_sample_rate / 1e9
        status = _fmt_status_icon(not d.faulty)
        note = f" ({d.fault_reason})" if d.faulty else ""
        rows.append([d.resource_name, d.model, f"{d.channels} CH", f"{sr_gs:.1f} GS/s", status + note])
    table = _fmt_table(["Device", "Model", "Channels", "Max SR", "Status"], rows)
    return [TextContent(type="text", text=f"## Oscilloscope Devices\n\n{table}")]


async def _get_config(args: dict) -> list[TextContent]:
    r = args["resource_name"]
    backend().open_device(r)
    cfg = backend().get_current_config(r)
    ch_rows = []
    for ch_name in sorted(cfg.get("channels", {}).keys()):
        c = cfg["channels"][ch_name]
        z = c.get('input_impedance_ohms', 0)
        ch_rows.append([
            f"CH{ch_name}", "✓" if c["enabled"] else "✗",
            f"{c['vertical_range_vpp']:.3f} Vpp", f"{c['vertical_offset_v']:+.3f} V",
            c["vertical_coupling"], f"{c['probe_attenuation']}x",
            f"{z:.0f}Ω" if z < 1000 else "1MΩ",
        ])
    ch_table = _fmt_table(["CH", "On", "Vert Range", "Offset", "Coupling", "Probe", "Impedance"], ch_rows)
    h = cfg.get("horizontal", {})
    t = cfg.get("trigger", {})
    lines = [
        f"## {cfg.get('model', r)} — {r}",
        "", "### Channels", ch_table,
        "", "### Horizontal",
        f"| Sample Rate | {h.get('sample_rate_sps', 0):.3e} S/s |",
        f"| Record Length | {h.get('record_length', 0)} pts |",
        "", "### Trigger",
        f"| Source | {t.get('source', 'N/A')} |",
        f"| Level | {t.get('level_v', 0):+.3f} V |",
        f"| Slope | {t.get('slope', 'N/A')} |",
    ]
    return [TextContent(type="text", text="\n".join(lines))]


async def _configure(args: dict) -> list[TextContent]:
    r = args["resource_name"]
    ch = args.get("channel", "0")
    b = backend()
    b.open_device(r)
    ch_cfg = ChannelConfig(enabled=True)
    for k in ["vertical_range", "vertical_coupling", "vertical_offset", "probe_attenuation",
              "input_impedance", "bandwidth_filter"]:
        if k in args:
            setattr(ch_cfg, k, float(args[k]) if k in ("vertical_range", "vertical_offset",
                      "probe_attenuation", "input_impedance") else args[k])
    b.configure_channel(r, ch, ch_cfg)
    h_cfg = HorizontalConfig()
    for k in ["min_sample_rate", "min_record_length", "acquisition_type"]:
        if k in args:
            setattr(h_cfg, "min_num_pts" if k == "min_record_length" else k,
                    float(args[k]) if k == "min_sample_rate" else (
                        int(args[k]) if k == "min_record_length" else args[k]))
    b.configure_horizontal(r, h_cfg)
    t_cfg = TriggerConfig()
    for k in ["trigger_source", "trigger_level", "trigger_slope", "trigger_coupling",
              "trigger_holdoff", "trigger_type"]:
        if k in args:
            setattr(t_cfg, "level" if k == "trigger_level" else "source" if k == "trigger_source"
                    else "slope" if k == "trigger_slope" else "coupling" if k == "trigger_coupling"
                    else "holdoff" if k == "trigger_holdoff" else "type",
                    float(args[k]) if k in ("trigger_level", "trigger_holdoff") else args[k])
    b.configure_trigger(r, t_cfg)
    b.commit(r)
    summary = [f"✓ {r} CH{ch} configured:"]
    if "vertical_range" in args:     summary.append(f"  Range: {args['vertical_range']} Vpp")
    if "vertical_coupling" in args:  summary.append(f"  Coupling: {args['vertical_coupling']}")
    if "min_sample_rate" in args:    summary.append(f"  SR: {float(args['min_sample_rate']):.2e} S/s")
    if "trigger_source" in args:     summary.append(f"  Trigger: {args['trigger_source']} @ {args.get('trigger_level', 0):+.3f}V")
    if "input_impedance" in args:
        z = args["input_impedance"]
        summary.append(f"  Impedance: {'1MΩ' if z >= 1000 else f'{z:.0f}Ω'}")
    return [TextContent(type="text", text="\n".join(summary))]


async def _auto_setup(args: dict) -> list[TextContent]:
    r = args["resource_name"]
    backend().open_device(r)
    backend().auto_setup(r)
    backend().commit(r)
    return [TextContent(type="text", text=f"✓ Autoset completed on {r}.")]


async def _read_waveform(args: dict) -> list[TextContent]:
    r = args["resource_name"]
    ch = args.get("channel", "0")
    timeout = float(args.get("timeout_seconds", 10.0))
    result = await asyncio.to_thread(_run_worker, r, ch, timeout)
    return [_format_worker_result(result)]


async def _measure(args: dict) -> list[TextContent]:
    r = args["resource_name"]
    ch = args.get("channel", "0")
    timeout = float(args.get("timeout_seconds", 10.0))
    result = await asyncio.to_thread(_run_worker, r, ch, timeout)
    return [_format_worker_result(result)]


class _ResultWrapper:
    """Dict wrapper supporting attribute access for formatting functions."""
    def __init__(self, d: dict):
        self._d = d
        for k, v in d.items():
            setattr(self, k, v)
        if not hasattr(self, "stats") or not isinstance(self.stats, dict):
            self.stats = {}
            if hasattr(self, "min_v"):
                self.stats["min"] = self.min_v
                self.stats["max"] = self.max_v
                self.stats["peak_to_peak"] = getattr(self, "vpp", 0)
        if hasattr(self, "frequency_hz") and self.frequency_hz > 0:
            self.period_sec = 1.0 / self.frequency_hz
        if not hasattr(self, "period_sec") or self.period_sec == 0:
            self.period_sec = 0.0
        if not hasattr(self, "duty_cycle_pct"):
            self.duty_cycle_pct = 0.0
        if not hasattr(self, "rise_time_sec"):
            self.rise_time_sec = 0.0
        if not hasattr(self, "fall_time_sec"):
            self.fall_time_sec = 0.0
        if not hasattr(self, "amplitude_vpp"):
            self.amplitude_vpp = getattr(self, "vpp", 0)
        if not hasattr(self, "sample_rate_sps"):
            self.sample_rate_sps = 0.0
        if not hasattr(self, "voltage"):
            self.voltage = []
        if not hasattr(self, "time"):
            self.time = []
        if not hasattr(self, "signal_type"):
            self.signal_type = "dc"
        # New advanced measurement defaults
        for _attr, _def in [
            ("top_level", 0.0), ("base_level", 0.0), ("num_levels", 0),
            ("rise_10_90_ns", 0.0), ("fall_10_90_ns", 0.0),
            ("rise_20_80_ns", 0.0), ("fall_20_80_ns", 0.0),
            ("pos_width_ns", 0.0), ("neg_width_ns", 0.0),
            ("overshoot_pct", 0.0), ("undershoot_pct", 0.0),
            ("jitter_ps", 0.0), ("snr_db", 0.0), ("edge_count", 0),
            ("is_burst", False), ("burst_info", ""),
        ]:
            if not hasattr(self, _attr):
                setattr(self, _attr, _def)


def _format_worker_result(d: dict) -> TextContent:
    if not d.get("ok"):
        err = d.get("error", "unknown")
        ch = d.get("channel", "?")
        return TextContent(type="text", text=f"CH{ch}: {err}")
    m = _ResultWrapper(d)
    return _format_auto_measure(m)


def _build_param_rows(m) -> list[list[str]]:
    sr = m.sample_rate_sps / 1e6
    if m.signal_type in ("periodic", "burst"):
        f_hz = m.frequency_hz
        if f_hz >= 1e6:
            f_str = f"{f_hz/1e6:.4f} MHz"
        elif f_hz >= 1e3:
            f_str = f"{f_hz/1e3:.2f} kHz"
        else:
            f_str = f"{f_hz:.2f} Hz"
        rows = [
            ["Frequency",     f_str],
            ["Period",        f"{m.period_sec*1e9:.1f} ns"],
            ["Amplitude",     f"{m.amplitude_vpp:.4f} Vpp"],
            ["Top / Base",    f"{getattr(m, 'top_level', m.max_v):.4f} / {getattr(m, 'base_level', m.min_v):.4f} V"],
            ["Duty Cycle",    f"{m.duty_cycle_pct:.1f}%"],
            ["Pos / Neg Width", f"{getattr(m, 'pos_width_ns', 0):.2f} / {getattr(m, 'neg_width_ns', 0):.2f} ns"],
            ["Rise 10-90%",   f"{getattr(m, 'rise_10_90_ns', m.rise_time_sec*1e9):.3f} ns"],
            ["Fall 10-90%",   f"{getattr(m, 'fall_10_90_ns', m.fall_time_sec*1e9):.3f} ns"],
            ["Overshoot",     f"{getattr(m, 'overshoot_pct', 0):.2f}%"],
            ["Undershoot",    f"{getattr(m, 'undershoot_pct', 0):.2f}%"],
            ["Jitter (RMS)",  f"{getattr(m, 'jitter_ps', 0):.1f} ps"],
            ["SNR (est.)",    f"{getattr(m, 'snr_db', 0):.1f} dB"],
            ["RMS / Mean",    f"{m.rms_v:.4f} / {m.mean_v:+.4f} V"],
            ["Sample Rate",   f"{sr:.1f} MS/s"],
        ]
        levels = getattr(m, 'num_levels', 0)
        if levels > 2:
            rows.append(["Signal Levels", f"{levels} levels detected"])
        if getattr(m, 'is_burst', False):
            rows.append(["Mode", getattr(m, 'burst_info', 'Burst')])
        return rows
    else:
        return [
            ["Type",        m.signal_type.upper()],
            ["Mean",        f"{m.mean_v:.4f} V"],
            ["RMS",         f"{m.rms_v:.4f} V"],
            ["Min / Max",   f"{m.min_v:.4f} / {m.max_v:.4f} V"],
            ["Vpp",         f"{m.stats['peak_to_peak']:.4f} V"],
            ["SNR (est.)",  f"{getattr(m, 'snr_db', 0):.1f} dB"],
            ["Sample Rate", f"{sr:.1f} MS/s"],
        ]


def _format_auto_measure(m) -> TextContent:
    is_periodic = m.signal_type in ("periodic", "burst")
    top = getattr(m, 'top_level', m.max_v)
    base = getattr(m, 'base_level', m.min_v)
    # Use top/base levels with padding for better waveform visualization
    span = top - base if (top - base) > 0.01 else m.max_v - m.min_v
    v_min = base - span * 0.2 if span > 0 else m.min_v
    v_max = top + span * 0.2 if span > 0 else m.max_v
    trigger_level = (top + base) / 2 if is_periodic else None
    ascii_preview = _fmt_ascii_waveform(
        m.voltage, width=100, height=16,
        vmin=min(v_min, m.stats.get("min", v_min)),
        vmax=max(v_max, m.stats.get("max", v_max)),
        period_sec=m.period_sec if is_periodic else 0.0,
        trigger_level=trigger_level,
    )
    lines = [f"## Measurement — CH{m.channel}", ""]
    lines.append(_fmt_table(["Parameter", "Value"], _build_param_rows(m)))
    lines.append("")
    lines.append(ascii_preview)
    fp = _signal_fingerprint(m)
    lines.append(f"\n### Signal Analysis\n- CH{m.channel}: {fp}")
    return TextContent(type="text", text="\n".join(lines))


async def _read_all(args: dict) -> list[TextContent]:
    """Read ALL channels on ALL devices via independent worker processes.

    Process management guarantees:
      1. Kill stale workers before starting
      2. For each channel: kill stale workers, spawn fresh worker, kill after read
      3. Only one worker at a time (sequential + mutex guard)
      4. Channels >10s are skipped with reason
      5. Final cleanup: kill ALL residual processes
    """
    timeout = float(args.get("timeout_seconds", 10.0))
    b = backend()
    devices = b.scan_devices()

    if not devices:
        return [TextContent(type="text", text="No devices found.")]

    # ── Chassis map ──
    lines = ["## Chassis Map", ""]
    chassis_rows = []
    for d in devices:
        slot = d.resource_name.replace("PXI1Slot", "")
        if "PXI1Slot" not in d.resource_name:
            slot = d.resource_name
        status = "✓" if not d.faulty else "✗ FAULTY"
        chassis_rows.append([slot, d.resource_name, d.model, f"{d.channels} CH", status])
    lines.append(_fmt_table(["Slot", "Device", "Model", "Channels", "Status"], chassis_rows))

    # ── Initial cleanup ──
    killed = _kill_stale_workers()
    if killed:
        lines.append(f"\nInitial cleanup: killed {killed} stale worker(s)")

    # ── Read channels one-by-one ──
    lines.append("")
    lines.append("## Measurement Results")
    lines.append("")

    summary_header = ["Device", "CH", "Type", "Frequency/Vpp", "Time"]
    summary_rows = []
    all_results: list[dict] = []
    skipped: list[dict] = []

    ch_icons = ["CH0", "CH1", "CH2", "CH3"]
    for d in devices:
        if d.faulty:
            lines.append(f"Skipping faulty device: {d.resource_name}")
            continue
        for ch_idx in range(d.channels):
            ch = str(ch_idx)
            icon = ch_icons[ch_idx] if ch_idx < len(ch_icons) else f"CH{ch}"

            # _run_worker handles: kill-stale → spawn → read/kill → cleanup
            result = await asyncio.to_thread(_run_worker, d.resource_name, ch, timeout)

            if result.get("skipped"):
                skipped.append(result)
                summary_rows.append([
                    d.resource_name, icon, "SKIP",
                    result.get("error", "timeout")[:40],
                    f"{result.get('elapsed_sec', 0):.0f}s",
                ])
                lines.append(f"- {icon}: SKIPPED — {result.get('error', 'timeout')}")
                continue

            if not result.get("ok"):
                summary_rows.append([
                    d.resource_name, icon, "ERROR",
                    result.get("error", "?")[:40],
                    f"{result.get('elapsed_sec', 0):.0f}s",
                ])
                lines.append(f"- {icon}: ERROR — {result.get('error', '?')}")
                continue

            all_results.append(result)
            sig = result.get("signal_type", "?")
            if sig == "periodic":
                f_mhz = result["frequency_hz"] / 1e6
                f_str = f"{f_mhz:.4f} MHz" if f_mhz >= 1 else f"{result['frequency_hz']/1e3:.2f} kHz"
                info = f"{f_str} / {result['vpp']:.4f}Vpp"
            elif sig == "dc":
                info = f"DC {result['mean_v']:.3f}V"
            else:
                info = f"noise {result['vpp']:.3f}Vpp"
            summary_rows.append([
                d.resource_name, icon, sig.upper(),
                info,
                f"{result.get('elapsed_sec', 0):.1f}s",
            ])

    lines.append(_fmt_table(summary_header, summary_rows))

    # ── Skipped channels detail ──
    if skipped:
        lines.append("")
        lines.append("### Skipped Channels (timeout >10s)")
        for s in skipped:
            lines.append(f"- **{s.get('resource_name', '?')} CH{s.get('channel', '?')}**: "
                         f"{s.get('error', 'timeout')}")

    # ── Per-channel detail ──
    if all_results:
        lines.append("")
        for r in all_results:
            m = _ResultWrapper(r)
            slot_num = r["resource_name"].replace("PXI1Slot", "")
            lines.append(f"## CH {slot_num}-{r['channel']}")
            lines.append("")
            lines.append(_fmt_table(["Parameter", "Value"], _build_param_rows(m)))
            lines.append("")
            is_periodic = r.get("signal_type") in ("periodic", "burst")
            top = r.get("top_level", r.get("max_v", 0))
            base = r.get("base_level", r.get("min_v", 0))
            span_wf = top - base if (top - base) > 0.01 else r.get("vpp", 1)
            vmin_wf = base - span_wf * 0.2
            vmax_wf = top + span_wf * 0.2
            trig = (top + base) / 2 if is_periodic else None
            wf = _fmt_ascii_waveform(
                r.get("voltage", []), width=80, height=14,
                vmin=vmin_wf, vmax=vmax_wf,
                period_sec=1.0 / r["frequency_hz"] if is_periodic and r.get("frequency_hz", 0) > 0 else 0.0,
                trigger_level=trig,
            )
            lines.append(wf)
            lines.append("")
            fp = _signal_fingerprint(m)
            lines.append(f"**{fp}**")
            lines.append("")

    # ── Final cleanup ──
    cleaned = _cleanup_all()
    if cleaned > 0:
        lines.append(f"\nFinal cleanup: killed {cleaned} residual process(es)")
    else:
        lines.append("\nNo residual processes found — all clean.")

    return [TextContent(type="text", text="\n".join(lines))]


def _signal_fingerprint(m) -> str:
    """Signal description with measurement data + smart identification."""
    guess = _signal_guess(m)
    if m.signal_type == "dc":
        return f"DC {m.mean_v:.2f}V → {guess}"
    if m.signal_type == "noise":
        return f"Noise {m.stats['std']*1000:.0f}mV RMS → {guess}"
    f_str = f"{m.frequency_hz/1e6:.4f} MHz" if m.frequency_hz >= 1e6 else f"{m.frequency_hz/1e3:.2f} kHz"
    top = getattr(m, 'top_level', m.max_v)
    base = getattr(m, 'base_level', m.min_v)
    logic = _identify_logic(top, base)
    if getattr(m, 'is_burst', False):
        logic += " [BURST]"
    return f"{f_str} {m.amplitude_vpp:.2f}Vpp{logic} D={m.duty_cycle_pct:.0f}% → {guess}"


def _identify_logic(top: float, base: float) -> str:
    """Identify logic family from voltage levels."""
    vpp = top - base
    if vpp < 0.01:
        return ""
    if abs(top - 3.3) < 0.5 and abs(base) < 0.3:
        return " 3.3V CMOS"
    if abs(top - 5.0) < 0.7 and abs(base) < 0.3:
        return " 5V TTL"
    if abs(top - 1.8) < 0.3 and abs(base) < 0.2:
        return " 1.8V LVCMOS"
    if abs(top - 2.5) < 0.3 and abs(base) < 0.2:
        return " 2.5V LVCMOS"
    if abs(top - 1.2) < 0.2 and abs(base) < 0.2:
        return " 1.2V SSTL"
    if vpp < 0.5:
        return " LVDS/diff."
    if vpp > 10:
        if abs(top - 12) < 2:
            return " RS-232"
        return " HV industrial"
    return ""


def _signal_guess(m) -> str:
    """Smart signal identification engine: clocks, protocols, PWM, power."""
    guesses = []

    # ── DC analysis ───────────────────────────────────────────────────────
    if m.signal_type == "dc":
        v = m.mean_v
        if abs(v) < 0.05:       return "GND"
        if abs(v - 3.3) < 0.35: return "3.3V rail / pull-up idle"
        if abs(v - 5.0) < 0.5:  return "5.0V rail / pull-up idle"
        if abs(v - 1.8) < 0.2:  return "1.8V rail"
        if abs(v - 2.5) < 0.2:  return "2.5V rail"
        if abs(v - 1.2) < 0.2:  return "1.2V core / VTT"
        if abs(v - 12) < 1:     return "12V rail"
        if abs(v - 3.3) < 0.15: return "3.3V rail (tight)"
        if v < 0.05 and v > -0.05: return "GND (solid)"
        return f"DC {v:.2f}V — power rail or idle IO"

    # ── Noise analysis ────────────────────────────────────────────────────
    if m.signal_type == "noise":
        rms_mv = m.stats['std'] * 1000
        snr = getattr(m, 'snr_db', 0)
        if rms_mv < 10:  return f"Low noise ~{rms_mv:.0f}mVrms — open/floating input"
        if rms_mv < 50:  return f"Moderate noise ~{rms_mv:.0f}mVrms — EMI or ground bounce (SNR={snr:.0f}dB)"
        if rms_mv < 200: return f"High noise ~{rms_mv:.0f}mVrms — strong EMI or switching noise"
        return f"Severe noise ~{rms_mv:.0f}mVrms — antenna effect or PSU ripple"

    # ── Periodic / Burst signal ───────────────────────────────────────────
    f_hz = m.frequency_hz
    f_mhz = f_hz / 1e6
    f_khz = f_hz / 1e3
    vpp = m.amplitude_vpp
    duty = m.duty_cycle_pct
    top = getattr(m, 'top_level', m.max_v)
    base = getattr(m, 'base_level', m.min_v)
    amplitude = top - base
    jitter_ps = getattr(m, 'jitter_ps', 0)
    rise_ns = getattr(m, 'rise_10_90_ns', 0)
    pos_width = getattr(m, 'pos_width_ns', 0)
    neg_width = getattr(m, 'neg_width_ns', 0)
    num_levels = getattr(m, 'num_levels', 2)
    is_burst = getattr(m, 'is_burst', False)

    # ── 1. Clock identification ───────────────────────────────────────────
    _identify_clock(guesses, f_hz, f_mhz, f_khz, jitter_ps, duty, rise_ns)

    # ── 2. Protocol detection ─────────────────────────────────────────────
    _identify_protocol(guesses, f_hz, f_khz, f_mhz, duty, pos_width, neg_width, amplitude, is_burst, num_levels)

    # ── 3. PWM detection ──────────────────────────────────────────────────
    _identify_pwm(guesses, f_hz, f_khz, duty, amplitude, top)

    # ── 4. Signal quality ─────────────────────────────────────────────────
    if jitter_ps > 0:
        if jitter_ps < 10:
            guesses.append(f"Jitter {jitter_ps:.1f}ps RMS — excellent clock quality")
        elif jitter_ps < 100:
            guesses.append(f"Jitter {jitter_ps:.0f}ps RMS — good clock quality")
        elif jitter_ps < 500:
            guesses.append(f"Jitter {jitter_ps:.0f}ps RMS — moderate, may be data-dependent")
        else:
            guesses.append(f"Jitter {jitter_ps/1000:.1f}ns RMS — high jitter, check PLL/source")

    # ── 5. Rise/fall time quality ─────────────────────────────────────────
    period_ns = (1e9 / f_hz) if f_hz > 0 else 0
    if rise_ns > 0 and period_ns > 0:
        edge_ratio = rise_ns / period_ns * 100
        if edge_ratio > 20:
            guesses.append(f"Slow edges ({rise_ns:.1f}ns = {edge_ratio:.0f}% of period) — check drive strength or capacitance")

    if not guesses:
        guesses.append(f"{f_mhz:.2f}MHz periodic signal — unclassified")
    return "; ".join(guesses[:4])


def _identify_clock(guesses: list, f_hz, f_mhz, f_khz, jitter_ps, duty, rise_ns):
    """Identify well-known clock frequencies and crystal oscillators."""
    is_clean = duty > 40 and duty < 60
    quality = ""
    if jitter_ps > 0 and jitter_ps < 50:
        quality = " (low jitter)"

    # Exact crystal frequencies
    if 32.7 <= f_khz <= 32.8:
        guesses.append(f"32.768kHz RTC crystal{quality}")
    elif 0.45 <= f_mhz <= 0.55:
        guesses.append(f"500kHz reference clock")
    elif 0.99 <= f_mhz <= 1.01:
        guesses.append(f"1MHz ref clock — common timebase")
    elif 3.28 <= f_mhz <= 3.32:
        guesses.append(f"3.3MHz clock")
    elif 3.57 <= f_mhz <= 3.58:
        guesses.append(f"3.58MHz NTSC color burst")
    elif 3.99 <= f_mhz <= 4.01:
        guesses.append(f"4MHz MCU clock")
    elif 4.90 <= f_mhz <= 5.10:
        guesses.append(f"5MHz ref clock")
    elif 7.37 <= f_mhz <= 7.38:
        guesses.append(f"7.3728MHz UART baud clock")
    elif 7.90 <= f_mhz <= 8.10:
        guesses.append(f"8MHz MCU crystal")
    elif 9.90 <= f_mhz <= 10.10:
        guesses.append(f"10MHz FPGA ref / GPS disciplined{quality}")
    elif 11.05 <= f_mhz <= 11.06:
        guesses.append(f"11.0592MHz UART baud clock")
    elif 11.90 <= f_mhz <= 12.10:
        guesses.append(f"12MHz USB/MCU crystal")
    elif 14.30 <= f_mhz <= 14.32:
        guesses.append(f"14.318MHz NTSC ref")
    elif 15.90 <= f_mhz <= 16.10:
        guesses.append(f"16MHz AVR/Arduino crystal")
    elif 18.40 <= f_mhz <= 18.44:
        guesses.append(f"18.432MHz audio / baud clock")
    elif 19.90 <= f_mhz <= 20.10:
        guesses.append(f"20MHz system clock")
    elif 22.10 <= f_mhz <= 22.12:
        guesses.append(f"22.1184MHz baud clock")
    elif 23.90 <= f_mhz <= 24.10:
        guesses.append(f"24MHz USB FS / STM32 HSE")
    elif 24.90 <= f_mhz <= 25.10:
        guesses.append(f"25MHz Ethernet PHY ref")
    elif 26.90 <= f_mhz <= 27.10:
        guesses.append(f"27MHz video / DVB clock")
    elif 33.20 <= f_mhz <= 33.35:
        guesses.append(f"33.333MHz PCI clock")
    elif 39.90 <= f_mhz <= 40.10:
        guesses.append(f"40MHz system clock")
    elif 47.90 <= f_mhz <= 48.10:
        guesses.append(f"48MHz USB HS clock")
    elif 49.90 <= f_mhz <= 50.10:
        guesses.append(f"50MHz FPGA / system clock")
    elif 66.50 <= f_mhz <= 66.70:
        guesses.append(f"66.667MHz PCI-X clock")
    elif 74.90 <= f_mhz <= 75.10:
        guesses.append(f"75MHz SGMII ref")
    elif 99.90 <= f_mhz <= 100.10:
        guesses.append(f"100MHz PCIe / SGMII ref{quality}")
    elif 124.90 <= f_mhz <= 125.10:
        guesses.append(f"125MHz Gigabit Ethernet GMII")
    elif 132.90 <= f_mhz <= 133.35:
        guesses.append(f"133.33MHz PCI-X / DDR ref")
    elif 147.90 <= f_mhz <= 148.60:
        guesses.append(f"148.5MHz HD video pixel clock")
    elif 155.90 <= f_mhz <= 156.30:
        guesses.append(f"156.25MHz 10GbE / XAUI ref")
    elif 199.90 <= f_mhz <= 200.10:
        guesses.append(f"200MHz DDR / FPGA clock")
    elif f_hz < 100:
        guesses.append(f"Sub-100Hz — likely PWM or control, not a clock")
    elif f_khz < 1:
        guesses.append(f"Sub-kHz — PSU switching or slow control signal")
    elif f_khz < 50:
        if is_clean:
            guesses.append(f"{f_khz:.1f}kHz — audio range or I2C clock")
        else:
            guesses.append(f"{f_khz:.1f}kHz — slow logic or PWM base freq")
    elif f_mhz < 5:
        guesses.append(f"{f_mhz:.2f}MHz — low-speed clock or MCU peripheral")
    elif f_mhz < 30:
        guesses.append(f"{f_mhz:.1f}MHz — common MCU/FPGA clock range")
    elif f_mhz < 100:
        guesses.append(f"{f_mhz:.0f}MHz — high-speed system / FPGA clock")
    elif f_mhz < 300:
        guesses.append(f"{f_mhz:.0f}MHz — DDR / high-speed SerDes ref")
    else:
        guesses.append(f"{f_mhz:.0f}MHz — ultra-high-speed ADC/DAC clock")


def _identify_protocol(guesses: list, f_hz, f_khz, f_mhz, duty, pos_width, neg_width, amplitude, is_burst, num_levels):
    """Detect communication protocol signatures from signal characteristics."""
    if f_hz < 100:
        return

    # ── SPI clock: 1-50MHz, square, continuous or burst ──────────────────
    if 0.1 <= f_mhz <= 50 and 40 < duty < 60 and amplitude > 0.1:
        if is_burst and num_levels <= 2:
            guesses.append(f"SPI CLK {f_mhz:.1f}MHz — burst-synchronized clock with idle periods")
        elif amplitude < 0.8:
            guesses.append(f"SPI CLK {f_mhz:.1f}MHz — typical SPI bus clock")

    # ── I2C clock: typically 100kHz, 400kHz, 1MHz ────────────────────────
    if num_levels <= 2:
        if 90 < f_khz < 110:
            guesses.append("I2C SCL 100kHz — standard mode")
        elif 380 < f_khz < 420:
            guesses.append("I2C SCL 400kHz — fast mode")
        elif 900 < f_khz < 1100:
            guesses.append("I2C SCL 1MHz — fast mode plus")

    # ── UART baud rate detection from bit width ───────────────────────────
    if pos_width > 0 and pos_width > 10:  # ns
        bit_period_ns = pos_width
        # Check common baud rates
        for baud, period_ns in [
            (115200, 8680), (57600, 17360), (38400, 26040),
            (19200, 52080), (9600, 104170), (230400, 4340),
            (460800, 2170), (921600, 1085), (1000000, 1000),
            (2000000, 500), (3000000, 333),
        ]:
            if abs(bit_period_ns - period_ns) < period_ns * 0.15:
                guesses.append(f"UART {baud} baud — bit period ~{period_ns}ns")
                break

    # ── CAN bus: differential, ~2Vpp, typically 125k/250k/500k/1M ────────
    if 100 < f_khz < 1100 and amplitude > 1.0 and amplitude < 4.0:
        if abs(f_khz - 125) < 15:
            guesses.append("CAN bus 125kbps — differential 2V nominal")
        elif abs(f_khz - 250) < 25:
            guesses.append("CAN bus 250kbps")
        elif abs(f_khz - 500) < 50:
            guesses.append("CAN bus 500kbps")
        elif abs(f_khz - 1000) < 100:
            guesses.append("CAN bus 1Mbps")

    # ── RS-232: ±12V swings ──────────────────────────────────────────────
    if amplitude > 8:
        if f_khz < 200:
            guesses.append(f"RS-232 — {amplitude:.0f}Vpp swing, ~{f_khz:.0f}kHz max toggle")


def _identify_pwm(guesses: list, f_hz, f_khz, duty, amplitude, top):
    """Detect PWM signals based on duty cycle and frequency patterns."""
    if f_hz < 10:
        return

    # PWM signature: non-50% duty cycle, lower frequencies
    if duty < 30 or duty > 70:
        if f_hz < 500:
            if f_hz < 1:
                guesses.append(f"Slow PWM {duty:.0f}% — heater/valve control")
            elif f_hz < 100:
                guesses.append(f"LED PWM {f_hz:.0f}Hz {duty:.0f}% — brightness control")
            elif f_hz < 1000:
                guesses.append(f"Motor PWM {f_hz:.0f}Hz {duty:.0f}% — motor speed / fan control")
            else:
                guesses.append(f"DC-DC PWM {f_khz:.0f}kHz {duty:.0f}% — switching regulator")
        else:
            if 1 < f_khz < 500:
                guesses.append(f"SMPS switching {f_khz:.0f}kHz {duty:.0f}% — PSU controller")
            elif f_khz >= 500:
                guesses.append(f"VRM switching {f_khz:.0f}kHz {duty:.0f}% — voltage regulator module")

    # Extreme duty cycle
    if duty > 95:
        if f_hz < 1000:
            guesses.append(f"Enable/rst pulse — always-high with brief low pulses ({f_hz:.0f}Hz)")
    elif duty < 5:
        if f_hz < 1000:
            guesses.append(f"Trigger/IRQ pulse — normally-low with brief high pulses ({f_hz:.0f}Hz)")


async def _help() -> list[TextContent]:
    try:
        backend_name = backend().backend_name
    except Exception:
        backend_name = "direct"
    text = f"""
## NI-SCOPE MCP Server — Usage Guide

Backend: **{backend_name}**

### Quick Start
1. `list_devices` — find your oscilloscope
2. `read_waveform` — capture + measure in one call
3. `configure_scope` — adjust settings if needed

### Typical Workflows
- **Read all channels** → `read_all_channels("PXI1Slot3")`
- **Measure CH0 frequency** → `read_waveform("PXI1Slot3", channel="0")`
- **Set up for 10MHz TTL** → `configure_scope(resource_name="...", vertical_range=5, input_impedance=50, min_sample_rate=100e6, trigger_source="0", trigger_level=1.65)`

### Process Management
Each channel read follows strict isolation protocol:
1. Kill stale workers before starting
2. Spawn fresh worker process for the channel
3. Kill worker immediately after read (success or failure)
4. Channels >10s are skipped with reason logged
5. Final cleanup: kill ALL residual processes

### Parameter Cheat Sheet
| Setting | Common Values | Notes |
|---|---|---|
| vertical_range | 0.1-50 | Full-scale Vpp |
| input_impedance | 50, 1000000 | 50Ω (RF), 1MΩ (probes) |
| min_sample_rate | 1e6-1.25e9 | >10x signal freq |
| trigger_type | EDGE | EDGE works for 95% of cases |

### MCP Config
```json
{{
  "servers": {{
    "niscope": {{
      "command": "python",
      "args": ["-u", "-m", "niscope_mcp"]
    }}
  }}
}}
```

### Troubleshooting
- **No devices** → check PXI chassis power, NI-SCOPE driver
- **FAULTY device** → power-cycle PXI chassis
- **Timeout** → check trigger source/level; try free-run mode
- **Flat waveform** → try larger vertical_range or check connection
"""
    return [TextContent(type="text", text=text)]


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    global _backend
    import asyncio

    parser = argparse.ArgumentParser(
        description="NI-SCOPE MCP Server — AI-controlled PXIe oscilloscope",
    )
    parser.add_argument("--check", action="store_true", help="Check system readiness")
    parser.add_argument("--setup", action="store_true", help="Guided auto-setup")
    args = parser.parse_args()

    from niscope_mcp.backends import register_direct, try_install_niscope

    if not register_direct():
        log.warning("=" * 60)
        log.warning("NI-SCOPE hardware driver (niscope) not found.")
        log.warning("Auto-installing...")
        log.warning("=" * 60)
        if try_install_niscope():
            log.info("niscope installed. Starting server...")
        else:
            sys.stderr.write("FAILED: niscope driver could not be installed.\n")
            sys.stderr.write('Run: pip install "niscope-mcp[hardware]"\n')
            sys.exit(1)

    if args.check:
        b = get_backend("direct")
        devs = b.scan_devices()
        if not devs:
            print("No devices found. Check PXI chassis power and connections.")
        else:
            for d in devs:
                print(f"  {d.resource_name}: {d.model} ({d.channels} CH, {d.max_sample_rate/1e9:.1f} GS/s)")
        sys.exit(0)

    if args.setup:
        print("Guided setup: run this MCP server, then call 'help' for instructions.")
        print("Add to your MCP config:")
        print('  {"command": "python", "args": ["-u", "-m", "niscope_mcp"]}')
        sys.exit(0)

    _backend = get_backend("direct")
    log.info("Backend: %s — ready", _backend.backend_name)

    async def run():
        try:
            async with stdio_server() as (reader, writer):
                await server.run(reader, writer, server.create_initialization_options())
        finally:
            _backend.close_all()
            log.info("All sessions closed.")

    asyncio.run(run())


if __name__ == "__main__":
    main()

