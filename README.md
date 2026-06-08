# NI-SCOPE MCP Server 🎛️

AI 控制的示波器 — 通过 MCP (Model Context Protocol) 让 AI 助手直接操作 NI 示波器。

支持 **NI PXIe-5160 / 5164 / 5110** 等设备。无需硬件？用 `--backend mock` 体验模拟波形。

---

## 快速安装

```bash
pip install -e .
```

或 Windows 双击 `install.bat`。

## 添加到 AI 助手

### Reasonix Desktop

编辑 `C:\Users\<用户名>\.reasonix\config.json`，在 `mcp` 数组中添加：

```json
{
  "mcp": [
    "niscope=python -u -m niscope_mcp --backend direct"
  ]
}
```

> 无硬件时改用 `--backend mock`。重启 Reasonix 生效。

### Claude Desktop / Cursor

```json
{
  "mcpServers": {
    "niscope": {
      "command": "python",
      "args": ["-u", "-m", "niscope_mcp", "--backend", "direct"]
    }
  }
}
```

---

## 后端选择

| 后端 | 命令 | 需要 |
|------|------|------|
| `direct` | `--backend direct` | Windows + NI-SCOPE 驱动 + `niscope` 包 |
| `mock` | `--backend mock` | 仅 Python 3.11+ |

---

## 可用工具

| 工具 | 功能 |
|------|------|
| `list_devices` | 扫描所有示波器 |
| `read_waveform` | 单通道采集 + 自动测量 |
| `read_all_channels` | 全通道采集 + 机箱图 + 信号分析 |
| `measure_waveform` | 同 read_waveform |
| `configure_scope` | 配置通道/时基/触发 |
| `auto_setup` | 自动设置（Autoset） |
| `get_current_config` | 读取当前硬件配置 |
| `help` | 参数参考 |

---

## 效果示例

AI 说"读取示波器"后输出：

```
## 机箱结构
| 插槽 | 设备名     | 型号              | 状态 |
|------|-----------|-------------------|------|
| 3    | PXI1Slot3 | NI PXIe-5160 (2CH) | ✓   |

## 测量结果
| CH | 类型 | 频率 | Vpp | 占空比 | 采样率 |
|----|------|------|-----|--------|--------|
| CH0| 🔵   | 10.00 MHz | 3.09V | 50% | 104M |

## 波型显示: 插槽3 通道0
  +3.306V ┤    ╱╲        ╱─╲        ╱╲
  +2.521V ┼ ──╱╱─ ╲─ ── ─│─ ╲─ ── ─│── ╲
  +1.474V ┼ ──│── ─│─ ── │─ ──╱╲─ ──│── ─│
          │●        ╱─          ─
  -0.097V ┤
           0       229ns      457ns

## 信号分析
- CH0: 10MHz 3.3V CMOS → FPGA主时钟/基准时钟
```

---

## 参数参考

**垂直 (Vertical)**
| 参数 | 说明 | 示例 |
|------|------|------|
| `vertical_range` | 满刻度 Vpp | `5.0` |
| `vertical_coupling` | AC / DC / GND | `DC` |
| `input_impedance` | 50Ω 或 1MΩ | `50` / `1000000` |
| `probe_attenuation` | 探头倍率 | `1` / `10` |

**水平 (Horizontal)**
| 参数 | 说明 | 示例 |
|------|------|------|
| `min_sample_rate` | 采样率 S/s | `100e6` (100 MS/s) |
| `min_record_length` | 采样点数 | `10000` |

**触发 (Trigger)**
| 参数 | 说明 | 示例 |
|------|------|------|
| `trigger_source` | 触发源 | `"0"` / `"1"` / `"VAL_IMMEDIATE"` |
| `trigger_level` | 触发电平 V | `1.5` |
| `trigger_slope` | 边沿 | `POSITIVE` / `NEGATIVE` |
| `trigger_type` | 类型 | `EDGE` |

---

## 项目结构

```
niscope-mcp/
├── niscope_mcp/          # Python 包
│   ├── __init__.py
│   ├── __main__.py        # MCP 服务器 + 输出渲染
│   └── backends/
│       ├── base.py         # 数据类型定义
│       ├── direct.py       # NI-SCOPE 硬件后端
│       └── mock.py         # 模拟后端（无需硬件）
├── niscope-mcp/server.py  # 兼容入口
├── pyproject.toml
├── MCP_OUTPUT_SPEC.md     # 输出格式规范
├── install.bat
└── README.md
```

## 许可

MIT
