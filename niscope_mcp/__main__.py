#!/usr/bin/env python3
"""
NI-SCOPE MCP Server — AI-controlled oscilloscope for any MCP-compatible agent.

Usage:
    python -m niscope_mcp                       # direct NI hardware backend
    niscope-mcp                                 # after pip install

REQUIRES:
    - Windows with NI-SCOPE driver
    - niscope Python package (auto-installed on first start if missing)

The server auto-installs the required NI hardware driver (niscope package)
when started for the first time. After installation, you MUST register this
MCP server in your AI assistant's configuration (see printed instructions).

Installation lifecycle:
    1. Run `python -m niscope_mcp` — auto-installs niscope driver if missing
    2. Add the printed MCP entry to your AI assistant config
    3. Restart the AI assistant
"""

from __future__ import annotations
import argparse
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from niscope_mcp.backends import get_backend, ScopeBackend
from niscope_mcp.backends.base import ChannelConfig, TriggerConfig, HorizontalConfig

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("niscope-mcp")

# ── Global backend (set via CLI) ─────────────────────────────────────────────
_backend: ScopeBackend | None = None


def backend() -> ScopeBackend:
    assert _backend is not None, "Backend not initialized"
    return _backend


# ── Formatters ───────────────────────────────────────────────────────────────

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
    """Oscilloscope-style waveform: connected line trace + grid + time axis.

    Uses Unicode box-drawing chars (╱╲│─┼┄) for a clean oscilloscope look.
    Height is adaptive; width ~100 chars fits chat windows well."""
    if len(voltage) < 2:
        return "(insufficient data points)"
    if vmin is None:
        vmin = min(voltage)
    if vmax is None:
        vmax = max(voltage)
    span = vmax - vmin
    if span < 1e-12:
        span = 0.1; vmax = vmin + 0.1
    # Add 5% padding
    pad = span * 0.05
    vmin -= pad; vmax += pad; span = vmax - vmin

    # Downsample: 1 point per column
    if len(voltage) > width * 4:
        voltage = voltage[:width * 4]
    step = max(1, len(voltage) // width)
    points = []
    for xi in range(width):
        chunk = voltage[xi * step: (xi + 1) * step]
        if chunk:
            points.append((sum(chunk) / len(chunk) - vmin) / span)  # normalize 0..1
        else:
            points.append(0.5)

    # Grid settings
    n_grid_lines = 4  # number of horizontal grid divisions
    grid_rows = [int(height * i / n_grid_lines) for i in range(1, n_grid_lines)]

    # --- Draw to canvas ---
    # canvas[y][x] = char; y=0 is top (vmax), y=height-1 is bottom (vmin)
    canvas = [[" "] * width for _ in range(height)]

    # Trace the waveform line
    for xi in range(width):
        y_float = (1.0 - points[xi]) * (height - 1)
        yi = max(0, min(height - 1, int(round(y_float))))
        if xi == 0:
            canvas[yi][xi] = "●"
        else:
            # Draw connection from previous point
            prev_y_float = (1.0 - points[xi - 1]) * (height - 1)
            prev_yi = max(0, min(height - 1, int(round(prev_y_float))))
            if yi == prev_yi:
                canvas[yi][xi] = "─"
            elif yi < prev_yi:
                # Moving up (lower y index = higher voltage)
                dy = prev_yi - yi
                for y in range(yi, prev_yi + 1):
                    if dy == 1:
                        ch = "╱" if y == yi else " "  # single slope at start
                    elif dy == 2:
                        ch = "╱"  # two-row slope
                    elif y == yi:
                        ch = "╱"
                    elif y == prev_yi:
                        ch = "╲"  # end of steep rise tilts opposite
                    else:
                        ch = "│"
                    if 0 <= y < height and ch != " ":
                        canvas[y][xi] = ch
            else:
                # Moving down
                dy = yi - prev_yi
                for y in range(prev_yi, yi + 1):
                    if dy == 1:
                        ch = "╲" if y == prev_yi else " "
                    elif dy == 2:
                        ch = "╲"
                    elif y == prev_yi:
                        ch = "╲"
                    elif y == yi:
                        ch = "╱"  # end of steep drop tilts opposite
                    else:
                        ch = "│"
                    if 0 <= y < height and ch != " ":
                        canvas[y][xi] = ch

    # Draw horizontal grid lines
    for gy in grid_rows:
        for xi in range(width):
            if canvas[gy][xi] == " ":
                # Dashed grid: alternate dash and space
                canvas[gy][xi] = "┄" if xi % 3 != 0 else " "

    # Draw trigger level marker
    if trigger_level is not None and vmin <= trigger_level <= vmax:
        t_y_float = (1.0 - (trigger_level - vmin) / span) * (height - 1)
        t_yi = max(0, min(height - 1, int(round(t_y_float))))
        if 0 <= t_yi < height:
            for xi in range(0, width, 4):
                if canvas[t_yi][xi] == " " or canvas[t_yi][xi] == "┄":
                    canvas[t_yi][xi] = "╌"

    # --- Render ---
    label_width = 9  # "+1.234V"
    lines = []
    # Top voltage label
    lines.append(f"  {vmax:+.3f}V")
    lines.append(f"  ┌{'─' * width}┐")

    for yi in range(height):
        row_chars = "".join(canvas[yi])
        # Voltage labels at grid lines and top/bottom
        is_grid = yi in grid_rows
        is_top = yi == 0
        is_bot = yi == height - 1
        if is_top:
            prefix = f"{vmax:+.3f}V".rjust(label_width) + " ┤"
        elif is_bot:
            prefix = f"{vmin:+.3f}V".rjust(label_width) + " ┤"
        elif is_grid:
            v_at = vmax - (yi / (height - 1)) * span
            prefix = f"{v_at:+.3f}V".rjust(label_width) + " ┼"
        else:
            prefix = " " * label_width + " │"
        lines.append(f"{prefix}{row_chars}│")

    lines.append(f"  └{'─' * width}┘")
    lines.append(f"  {vmin:+.3f}V")

    # Time axis
    if period_sec > 0:
        total_time = period_sec * (width / max(1, _count_periods(points)))
        if total_time <= 0:
            total_time = len(voltage) * period_sec / width
        # Show time markers
        n_ticks = 5
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
    """Count zero-crossings to estimate number of periods in the data."""
    if len(points) < 2:
        return 1
    mid = 0.5
    crossings = 0
    for i in range(1, len(points)):
        if (points[i - 1] < mid <= points[i]) or (points[i - 1] > mid >= points[i]):
            crossings += 1
    return max(1, crossings // 2)


def _fmt_status_icon(ok: bool) -> str:
    return "✓ OK" if ok else "✗ FAULT"


# ── MCP Server ────────────────────────────────────────────────────────────────

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
horizontal timebase, and trigger setup. Use this to see what the scope is
currently doing, including changes made via InstrumentStudio.""",
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
            description="""Configure oscilloscope settings. You can set any subset of parameters;
omitted ones keep their current value.

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
ASCII waveform, and signal fingerprint. For reading ALL channels at once with
chassis diagram, use read_all_channels instead.""",
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
channel with adaptive sampling, outputs a measurement table, ASCII waveform
previews for active signals, and search-friendly signal fingerprints for web
identification. One call does everything.""",
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


# ── Tool dispatcher ───────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
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
        resource = arguments.get("resource_name", "")
        if resource and ("TopazADCPhaseParityCalibration" in err_str or "Internal Hardware Error" in err_str):
            backend().mark_bad(resource, err_str[:120])
        # Close stale session so next call gets a fresh one (prevents persistent bad state)
        if resource and "Failed to retrieve error description" in err_str:
            try:
                backend().close_device(resource)
            except Exception:
                pass
        log.exception("Error in %s", name)
        tb = _tb.format_exc()
        tb_short = '\n'.join(tb.strip().split('\n')[-6:])
        return [TextContent(type="text", text=f"❌ Error: {e}\n\nTraceback (last 6 lines):\n{tb_short}")]


# ── Handlers ──────────────────────────────────────────────────────────────────

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
                    float(args[k]) if k == "min_sample_rate" else (int(args[k]) if k == "min_record_length" else args[k]))
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
    """Convenience: auto_setup + single-shot measure. Fast, reliable."""
    r = args["resource_name"]
    ch = args.get("channel", "0")
    timeout = float(args.get("timeout_seconds", 10.0))
    backend().open_device(r)
    result = backend().auto_measure(r, ch, timeout, preserve_trigger=False)
    return [_format_auto_measure(result)]


async def _measure(args: dict) -> list[TextContent]:
    """Same as read_waveform — single-shot measure for 'show me the signal'."""
    r = args["resource_name"]
    ch = args.get("channel", "0")
    timeout = float(args.get("timeout_seconds", 10.0))
    backend().open_device(r)
    result = backend().auto_measure(r, ch, timeout, preserve_trigger=False)
    return [_format_auto_measure(result)]


def _build_param_rows(m) -> list[list[str]]:
    """Build parameter table rows for a measurement result (spec-compliant)."""
    sr = m.sample_rate_sps / 1e6
    if m.signal_type == "periodic":
        f_hz = m.frequency_hz
        if f_hz >= 1e6:      f_str = f"{f_hz/1e6:.4f} MHz"
        elif f_hz >= 1e3:    f_str = f"{f_hz/1e3:.2f} kHz"
        else:                f_str = f"{f_hz:.2f} Hz"
        return [
            ["频率",       f_str],
            ["周期",       f"{m.period_sec*1e9:.1f} ns"],
            ["幅度 (Vpp)", f"{m.amplitude_vpp:.4f} V"],
            ["RMS",        f"{m.rms_v:.4f} V"],
            ["均值",       f"{m.mean_v:+.4f} V"],
            ["最小值",     f"{m.min_v:.4f} V"],
            ["最大值",     f"{m.max_v:.4f} V"],
            ["占空比",     f"{m.duty_cycle_pct:.1f}%"],
            ["上升时间",   f"{m.rise_time_sec*1e9:.1f} ns"],
            ["下降时间",   f"{m.fall_time_sec*1e9:.1f} ns"],
            ["采样率",     f"{sr:.1f} MS/s"],
        ]
    else:
        return [
            ["类型",   m.signal_type.upper()],
            ["均值",   f"{m.mean_v:.4f} V"],
            ["RMS",    f"{m.rms_v:.4f} V"],
            ["最小值", f"{m.min_v:.4f} V"],
            ["最大值", f"{m.max_v:.4f} V"],
            ["Vpp",    f"{m.stats['peak_to_peak']:.4f} V"],
            ["采样率", f"{sr:.1f} MS/s"],
        ]


def _format_auto_measure(m) -> TextContent:
    is_periodic = m.signal_type == "periodic"
    trigger_level = None
    if is_periodic:
        trigger_level = (m.max_v + m.min_v) / 2  # mid-point as trigger reference

    ascii_preview = _fmt_ascii_waveform(
        m.voltage, width=100, height=16,
        vmin=m.stats.get("min"), vmax=m.stats.get("max"),
        period_sec=m.period_sec if is_periodic else 0.0,
        trigger_level=trigger_level,
    )

    # Header
    lines = [f"## 测量结果 — 通道 {m.channel}", ""]
    # Parameter table
    lines.append(_fmt_table(["参数", "值"], _build_param_rows(m)))
    lines.append("")
    # ASCII waveform
    lines.append(ascii_preview)
    # Signal inference
    fp = _signal_fingerprint(m)
    lines.append(f"\n### 信号分析\n- {m.channel}: {fp}")

    return TextContent(type="text", text="\n".join(lines))


async def _read_all(args: dict) -> list[TextContent]:
    timeout = float(args.get("timeout_seconds", 10.0))
    b = backend()
    # Scan first before opening any persistent sessions, to avoid resource conflicts
    devices = b.scan_devices()

    if not devices:
        return [TextContent(type="text", text="No devices found.")]

    # ── Chassis map ──
    lines = ["## 机箱结构", ""]
    chassis_rows = []
    for d in devices:
        slot = d.resource_name.replace("PXI1Slot", "")
        if "PXI1Slot" not in d.resource_name:
            slot = d.resource_name
        status = "✓" if not d.faulty else f"✗ FAULTY"
        ch_str = f"{d.channels} CH"
        chassis_rows.append([slot, d.resource_name, d.model, ch_str, status])
    lines.append(_fmt_table(["插槽", "设备名", "型号", "通道", "状态"], chassis_rows))

    # ── Acquire ──
    lines.append("")
    lines.append("## 测量结果")
    lines.append("")
    header = ["CH", "类型", "频率", "Vpp", "占空比", "采样率"]
    summary_rows = []
    dev_results: dict[str, Any] = {}

    for d in devices:
        if d.faulty:
            dev_results[d.resource_name] = {"status": "FAULTY", "reason": d.fault_reason}
            continue
        try:
            b.auto_setup(d.resource_name)
            b.commit(d.resource_name)
            # Read every channel the device has
            ch_data = {}
            ch_icons = ["🔵", "🟠", "🟢", "🔴"]
            for i in range(d.channels):
                try:
                    m = b.auto_measure(d.resource_name, str(i), timeout)
                    ch_data[str(i)] = m
                    icon = ch_icons[i] if i < len(ch_icons) else "⚪"
                    f_str = ""
                    if m.signal_type == "periodic":
                        f_mhz = m.frequency_hz / 1e6
                        f_str = f"{f_mhz:.4f} MHz" if f_mhz >= 1 else f"{m.frequency_hz/1e3:.2f} kHz"
                    elif m.signal_type == "dc":
                        f_str = "DC"
                    else:
                        f_str = "noise"
                    summary_rows.append([
                        f"CH{i}", icon,
                        f_str,
                        f"{m.amplitude_vpp:.4f} V" if m.signal_type == "periodic" else f"{m.stats['peak_to_peak']:.4f} V",
                        f"{m.duty_cycle_pct:.1f}%" if m.signal_type == "periodic" else "-",
                        f"{m.sample_rate_sps/1e6:.0f}M",
                    ])
                except Exception as e:
                    ch_data[str(i)] = {"error": str(e)}
                    summary_rows.append([f"CH{i}", "🔴", "ERROR", "-", "-", "-"])
            dev_results[d.resource_name] = {"status": "OK", "ch_data": ch_data}
        except Exception as e:
            dev_results[d.resource_name] = {"status": "ERROR", "reason": str(e)}

    if summary_rows:
        lines.append(_fmt_table(header, summary_rows))

    # ── Waveform previews for active channels ──
    active_channels = []
    for name in sorted(dev_results.keys()):
        dr = dev_results[name]
        if dr["status"] != "OK":
            continue
        for ch in sorted(dr.get("ch_data", {}).keys()):
            cd = dr["ch_data"][ch]
            if isinstance(cd, dict):
                continue
            if cd.signal_type in ("periodic", "noise"):
                active_channels.append((name, ch, cd))

    if active_channels:
        lines.append("")
        for name, ch, cd in active_channels:
            is_periodic = cd.signal_type == "periodic"
            trig = (cd.max_v + cd.min_v) / 2 if is_periodic else None
            slot_num = name.replace("PXI1Slot", "") if "PXI1Slot" in name else name
            title = f"## 波型显示: 插槽{slot_num} 通道{ch}"
            lines.append(title)
            lines.append("")

            # Per-channel parameter table (shared builder)
            lines.append(_fmt_table(["参数", "值"], _build_param_rows(cd)))
            lines.append("")

            wf = _fmt_ascii_waveform(cd.voltage, width=80, height=14,
                                     vmin=cd.stats.get("min"), vmax=cd.stats.get("max"),
                                     period_sec=cd.period_sec if is_periodic else 0.0,
                                     trigger_level=trig)
            lines.append(wf)
            lines.append("")

    # ── Signal analysis ──
    lines.append("## 信号分析")
    lines.append("")

    for name in sorted(dev_results.keys()):
        dr = dev_results[name]
        if dr["status"] != "OK":
            slot_num = name.replace("PXI1Slot", "") if "PXI1Slot" in name else name
            lines.append(f"- **插槽{slot_num}**: ⚠ FAULTY — {dr.get('reason', 'hardware error')[:80]}")
            continue
        for ch in sorted(dr.get("ch_data", {}).keys()):
            cd = dr["ch_data"][ch]
            if isinstance(cd, dict) and "error" in cd:
                lines.append(f"- **通道{ch}**: ❌ {cd['error']}")
                continue
            fp = _signal_fingerprint(cd)
            slot_num = name.replace("PXI1Slot", "") if "PXI1Slot" in name else name
            lines.append(f"- **插槽{slot_num} 通道{ch}**: {fp}")

    return [TextContent(type="text", text="\n".join(lines))]


def _signal_guess(m) -> str:
    """Generate a specific signal identification guess based on measurements."""
    if m.signal_type == "dc":
        v = m.mean_v
        if abs(v) < 0.05:
            return "GND (接地)"
        elif abs(v - 3.3) < 0.35:
            return "3.3V 上拉/空闲逻辑高电平"
        elif abs(v - 5.0) < 0.5:
            return "5V 上拉/空闲逻辑高电平"
        elif abs(v - 1.8) < 0.2:
            return "1.8V 上拉/空闲逻辑高电平"
        else:
            return f"DC {v:.1f}V — 可能是电源轨或空闲IO"

    if m.signal_type == "noise":
        rms_mv = m.stats['std'] * 1000
        if rms_mv < 10:
            return "低幅度噪声 — 可能是开路输入或微弱串扰"
        elif rms_mv < 100:
            return "中等噪声 — 可能是EMI干扰或地环路"
        else:
            return "强噪声 — 可能是天线效应或严重干扰"

    # Periodic signal analysis
    f_hz = m.frequency_hz
    f_mhz = f_hz / 1e6
    f_khz = f_hz / 1e3
    vpp = m.amplitude_vpp
    duty = m.duty_cycle_pct
    vhi = m.max_v
    vlo = m.min_v
    is_33v = abs(vhi - 3.3) < 0.4 and abs(vlo) < 0.3
    is_5v = abs(vhi - 5.0) < 0.5 and abs(vlo) < 0.3
    is_18v = abs(vhi - 1.8) < 0.2 and abs(vlo) < 0.2
    is_square = 45 < duty < 55

    guesses = []

    # Frequency-based identification
    if 0.9 <= f_mhz <= 1.1:
        guesses.append("1MHz 参考时钟")
    elif 3.2 <= f_mhz <= 3.4:
        guesses.append("3.3MHz 系统时钟")
    elif 4.9 <= f_mhz <= 5.1:
        guesses.append("5MHz 参考时钟")
    elif 7.9 <= f_mhz <= 8.1:
        guesses.append("8MHz 晶振 (常见MCU时钟)")
    elif 9.9 <= f_mhz <= 10.1:
        guesses.append("10MHz 基准时钟/FPGA主时钟")
    elif 11.9 <= f_mhz <= 12.1:
        guesses.append("12MHz 晶振 (USB/MCU常用)")
    elif 15.9 <= f_mhz <= 16.1:
        guesses.append("16MHz 晶振 (Arduino/AVR常用)")
    elif 19.9 <= f_mhz <= 20.1:
        guesses.append("20MHz 系统时钟")
    elif 23.9 <= f_mhz <= 24.1:
        guesses.append("24MHz 晶振 (USB全速/STM32常用)")
    elif 24.9 <= f_mhz <= 25.1:
        guesses.append("25MHz 晶振 (以太网PHY常用)")
    elif 26.9 <= f_mhz <= 27.1:
        guesses.append("27MHz 晶振 (视频/DVB常用)")
    elif 32.7 <= f_khz <= 32.8:
        guesses.append("32.768kHz 手表晶振 (RTC实时时钟)")
    elif 49.9 <= f_mhz <= 50.1:
        guesses.append("50MHz 系统时钟/FPGA时钟")
    elif 99.9 <= f_mhz <= 100.1:
        guesses.append("100MHz 高速时钟 (PCIe/SGMII参考)")
    elif 124.9 <= f_mhz <= 125.1:
        guesses.append("125MHz 以太网GMII时钟")
    elif 147.9 <= f_mhz <= 148.1:
        guesses.append("148.5MHz 视频像素时钟")
    elif f_hz < 100:
        guesses.append(f"极低频 — 可能是PWM或控制信号")
    elif f_khz < 1:
        guesses.append(f"低频 — 可能是PWM、电源开关频率或慢速通信")
    elif f_khz < 100:
        guesses.append(f"中低频 — 可能是I²C/SPI时钟、PWM或音频")
    elif f_mhz < 10:
        guesses.append(f"{f_mhz:.1f}MHz — 可能是MCU时钟或低速通信时钟")
    elif f_mhz < 50:
        guesses.append(f"{f_mhz:.0f}MHz — 可能是系统时钟或FPGA输出")
    elif f_mhz < 200:
        guesses.append(f"{f_mhz:.0f}MHz — 可能是高速通信时钟 (DDR/SGMII)")
    else:
        guesses.append(f"{f_mhz:.0f}MHz — 可能是SerDes/高速ADC时钟")

    # Voltage + duty based refinement
    if is_33v and is_square:
        guesses.append("3.3V CMOS 方波 — 典型数字逻辑信号")
    elif is_5v and is_square:
        guesses.append("5V TTL 方波 — 传统数字逻辑信号")
    elif is_18v:
        guesses.append("1.8V LVCMOS — 低功耗数字信号")
    elif vpp < 0.5:
        guesses.append("低摆幅 — 可能是差分信号或50Ω端接")
    elif vpp > 10:
        guesses.append("高电压 — 可能是RS-232 (±12V) 或工业24V信号")

    # Duty cycle hints
    if duty > 90:
        guesses.append("极窄正脉冲 — 可能是触发/复位信号")
    elif duty < 10:
        guesses.append("极窄负脉冲 — 可能是触发/复位信号")
    elif 35 < duty < 45 or 55 < duty < 65:
        guesses.append(f"非对称占空比({duty:.0f}%) — 可能不是标准时钟")

    return "；".join(guesses[:3])  # Top 3 most relevant


def _signal_fingerprint(m) -> str:
    """Generate signal description with measurement data + identification guess."""
    guess = _signal_guess(m)
    if m.signal_type == "dc":
        return f"DC {m.mean_v:.2f}V → {guess}"
    if m.signal_type == "noise":
        return f"噪声 {m.stats['std']*1000:.0f}mV RMS → {guess}"
    # Periodic
    f_str = f"{m.frequency_hz/1e6:.4f} MHz" if m.frequency_hz >= 1e6 else f"{m.frequency_hz/1e3:.2f} kHz"
    logic = ""
    if abs(m.max_v - 3.3) < 0.4 and abs(m.min_v) < 0.3:
        logic = " 3.3V CMOS"
    elif abs(m.max_v - 5.0) < 0.5 and abs(m.min_v) < 0.3:
        logic = " 5V TTL"
    elif abs(m.max_v - 1.8) < 0.2:
        logic = " 1.8V"
    return f"{f_str} {m.amplitude_vpp:.2f}Vpp{logic} {m.duty_cycle_pct:.0f}% → {guess}"

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
- **"Read all channels"** → `read_all_channels("PXI1Slot3")`
- **"Measure CH0 frequency"** → `read_waveform("PXI1Slot3", channel="0")`
- **"Set up for 10MHz TTL"** → `configure_scope(resource_name="...", vertical_range=5, input_impedance=50, min_sample_rate=100e6, trigger_source="0", trigger_level=1.65)`

### Parameter Cheat Sheet
| Setting | Common Values | Notes |
|---|---|---|
| vertical_range | 0.1–50 | Full-scale Vpp |
| input_impedance | 50, 1000000 | 50Ω (RF), 1MΩ (probes) |
| min_sample_rate | 1e6–1.25e9 | >10× signal freq |
| trigger_type | EDGE | EDGE works for 95% of cases |

### Installation (first time only)
The server auto-installs the NI hardware driver on first start.
After that, add this MCP entry to your AI assistant.

Claude Desktop / Cursor / Proma mcp.json:
```json
{{
  "servers": {{
    "niscope": {{
      "type": "stdio",
      "command": "python",
      "args": ["-u", "-m", "niscope_mcp"]
    }}
  }}
}}
```

Then **restart the AI assistant**.

### Troubleshooting
- **niscope package missing** → auto-installed on first start, or manually: `pip install "niscope-mcp[hardware]"`
- **FAULTY device** → power-cycle PXI chassis
- **Timeout** → check trigger source/level; try free-run
- **Flat waveform** → try larger vertical_range or check connection"""
    return [TextContent(type="text", text=text)]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _backend
    import asyncio

    parser = argparse.ArgumentParser(
        description="NI-SCOPE MCP Server — AI-controlled oscilloscope (NI PXIe-516x)",
        epilog="""
INSTALLATION STEPS (run once):
  1. First start:  python -m niscope_mcp
     - Auto-installs the niscope hardware driver package if missing
     - Prints the MCP config entry you need to add to your AI assistant

  2. Add to Reasonix config (C:\\Users\\<username>\\.reasonix\\config.json):
     "mcp": [
       "niscope=python -u -m niscope_mcp"
     ]

  3. Add to Claude Desktop / Cursor:
     "mcpServers": {
       "niscope": {
         "command": "python",
         "args": ["-u", "-m", "niscope_mcp"]
       }
     }

  4. Restart your AI assistant — the tools will appear automatically.

PREREQUISITES:
  - Windows OS (NI driver requirement)
  - NI-SCOPE runtime driver installed from ni.com
  - NI oscilloscope hardware connected (PXIe-5160 / 5164 / 5110)

TROUBLESHOOTING:
  - If auto-install fails, run manually:
      pip install "niscope-mcp[hardware]"
  - If import fails after install, the NI-SCOPE driver may not be installed
  - If no devices found, check PXI chassis connection and power cycle
""")

    # ── Auto-install niscope if missing ──────────────────────────────────
    import sys
    from niscope_mcp.backends import register_direct, try_install_niscope

    if not register_direct():
        log.warning("=" * 60)
        log.warning("NI-SCOPE hardware driver (niscope) not found.")
        log.warning("Auto-installing now...")
        log.warning("=" * 60)
        if try_install_niscope():
            log.info("niscope package installed. Starting server...")
        else:
            sys.stderr.write("=" * 60 + "\n")
            sys.stderr.write("FAILED: niscope hardware driver could not be installed.\n")
            sys.stderr.write("Run: pip install \"niscope-mcp[hardware]\"\n")
            sys.stderr.write("=" * 60 + "\n")
            sys.exit(1)

    _backend = get_backend("direct")
    log.info("Backend: %s — ready (lazy device scan on first request)", _backend.backend_name)

    # ── Log MCP config registration instructions (stderr, not stdout) ────
    log.info("=" * 58)
    log.info("  NI-SCOPE MCP Server - Installation Complete")
    log.info("=" * 58)
    log.info(" [OK] niscope-mcp package + hardware driver installed")
    log.info("")
    log.info(" To use this MCP server, register it in your AI assistant config:")
    log.info("")
    log.info(" --- Reasonix Desktop (config.json) ---")
    log.info('   "mcp": ["niscope=python -u -m niscope_mcp"]')
    log.info("")
    log.info(" --- Claude Desktop / Cursor ---")
    log.info('   "mcpServers": { "niscope": { "command": "python", "args": ["-u", "-m", "niscope_mcp"] } }')
    log.info("")
    log.info(" Then RESTART the AI assistant.")
    log.info("=" * 58)

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
