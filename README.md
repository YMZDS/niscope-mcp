# NI-SCOPE MCP Server 🎛️

AI 控制的示波器 — 通过 MCP (Model Context Protocol) 让 AI 助手直接操作 NI 示波器。

支持 **NI PXIe-5160 / 5164 / 5110** 等设备。仅支持 Windows + NI 硬件。

---

## 📦 快速安装（两步）

### 第 1 步：安装包 + 硬件驱动

```bash
pip install -e .
pip install "niscope-mcp[hardware]"    # 安装 NI-SCOPE 硬件驱动
```

或 Windows 双击 `install.bat`。

> **首次启动会自动安装硬件驱动** — 直接运行 `python -m niscope_mcp`，如果缺少 `niscope` 包会自动 pip install。

### 第 2 步：注册 MCP 服务器（❗ 必须）

pip install **只是安装了 Python 包**，要让 AI 助手能调用示波器工具，还需要在 AI 助手的配置中注册 MCP 入口。

#### Proma / Claude Desktop / Cursor

编辑 MCP 配置文件，添加：

```json
{
  "servers": {
    "niscope": {
      "type": "stdio",
      "command": "python",
      "args": ["-u", "-m", "niscope_mcp"],
      "enabled": true
    }
  }
}
```

**重启 AI 助手**后生效。

---

## ⚠️ 安装经验（踩坑记录）

这是总结的安装流程和经验教训：

| 步骤 | 做什么 | 容易忘的点 |
|------|--------|-----------|
| 1 | `pip install -e .` | 只装了 Python 包，**不是 MCP 配置** |
| 2 | `pip install "niscope-mcp[hardware]"` | 安装 `niscope` 硬件驱动包，否则 direct 后端报 `ModuleNotFoundError` |
| 3 | 编辑 `config.json` 添加 MCP 入口 | ❗ **这是最容易被忽视的一步** — 不注册 AI 助手根本不知道有这个 MCP |
| 4 | 重启 AI 助手 | MCP 配置变更必须重启才能生效 |
| 5 | 验证 | 启动后调用 `list_devices` 确认连接 |

**关键教训**：pip install 和 MCP 注册是**两回事**，两步都要做。

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
| `help` | 参数参考 + 安装指引 |

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

## 典型工作流

### "检查通道 0 有没有信号"
```
list_devices → read_waveform(resource_name="PXI1Slot3", channel="0")
→ 报告均值和峰峰值
```

### "测量通道 1 的信号频率"
```
list_devices → configure_scope(resource_name="PXI1Slot3", trigger_source="1", trigger_level=0.5)
→ measure_waveform(resource_name="PXI1Slot3", channel="1")
→ 从 frequency_hz 读取结果
```

### "设置 10 MHz TTL 信号，CH0，50Ω 终端"
```
configure_scope(
    resource_name="PXI1Slot3",
    channel="0",
    vertical_range=5.0,
    input_impedance=50.0,
    min_sample_rate=100e6,
    trigger_source="0",
    trigger_level=1.5
)
→ read_waveform(...)
```

---

## 参数参考

### 垂直 (Vertical)
| 参数 | 说明 | 示例 |
|------|------|------|
| `vertical_range` | 满刻度 Vpp (`0.1`=±50mV, `5.0`=±2.5V, `50.0`=±25V) | `5.0` |
| `vertical_coupling` | AC / DC / GND | `DC` |
| `vertical_offset` | DC 偏移量 (V) | `0.0` |
| `input_impedance` | 50Ω (RF) 或 1MΩ (标准探头) | `50` / `1000000` |
| `probe_attenuation` | 探头倍率 | `1` / `10` / `100` |
| `bandwidth_filter` | FULL / 20MHZ / 100MHZ / 200MHZ — 降噪 | `FULL` |

### 水平 (Horizontal)
| 参数 | 说明 | 示例 |
|------|------|------|
| `min_sample_rate` | 采样率 S/s (≥ 10× 信号频率) | `100e6` (100 MS/s) |
| `min_record_length` | 采样点数 (1000~1e8) | `10000` |
| `acquisition_type` | NORMAL / FLEX_RES (增强分辨率) / DDC (数字下变频) | `NORMAL` |

### 触发 (Trigger)
| 参数 | 说明 | 示例 |
|------|------|------|
| `trigger_source` | `"0"`/`"1"` (通道), `"VAL_EXTERNAL"`, `"VAL_IMMEDIATE"` (自由运行) | `"0"` |
| `trigger_level` | 触发电平 (V) | `1.5` |
| `trigger_slope` | POSITIVE (上升沿) / NEGATIVE (下降沿) | `POSITIVE` |
| `trigger_coupling` | DC / AC / HF_REJECT / LF_REJECT | `DC` |
| `trigger_type` | EDGE (95%情况) / WINDOW / RUNT / WIDTH / GLITCH / SOFTWARE | `EDGE` |

---

## 故障排查

| 问题 | 解决 |
|------|------|
| **niscope 包找不到** | 首次启动自动安装，或手动 `pip install "niscope-mcp[hardware]"` |
| **设备标记 FAULTY** | PXI 机箱需要断电重启 (power cycle) |
| **超时 (Timeout)** | 检查触发源和触发电平是否正确；尝试 `VAL_IMMEDIATE` 自由运行 |
| **波形平直/零电压** | 检查探头连接和 `vertical_range` 设置 |
| **波形噪声大** | 启用 `bandwidth_filter` 或增加 `min_sample_rate` |
| **AI 助手找不到示波器工具** | 检查 `config.json` 是否添加了 MCP 入口，重启 AI 助手 |

---

## 项目结构

```
niscope-mcp/
├── niscope_mcp/          # Python 包
│   ├── __init__.py
│   ├── __main__.py        # MCP 服务器 + 自安装逻辑
│   └── backends/
│       ├── __init__.py     # 后端工厂 + 懒加载 + 自安装
│       ├── base.py         # 数据类型定义
│       └── direct.py       # NI-SCOPE 硬件后端
├── niscope-mcp/server.py  # 兼容入口
├── pyproject.toml
├── MCP_OUTPUT_SPEC.md     # 输出格式规范
├── install.bat
└── README.md
```

## 许可

MIT
