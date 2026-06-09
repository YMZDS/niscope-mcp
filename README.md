# NI-SCOPE MCP Server

AI 控制的 PXIe 示波器 — 通过 MCP 协议让 AI 助手直接操作 NI 示波器。

支持 **NI PXIe-5160 / 5164 / 5110**。仅 Windows + NI 硬件。

## 核心特性

- **自适应采样** — 自动提升采样率至信号频率的 30×（最高 1.25 GS/s），消除高频混叠
- **高级测量** — 上升/下降时间 (10-90%, 20-80%)、脉冲宽度、过冲/下冲、周期抖动、SNR
- **信号智能识别** — 30+ 晶振频率、UART/SPI/I2C/CAN 协议检测、PWM/开关电源/触发脉冲分类、边沿质量分析
- **严格进程隔离** — 每通道独立子进程采集，读前查杀残留、读后显式 kill、单进程度量、超时跳过、最终清理

## 快速安装

```bash
pip install -e .
pip install "niscope-mcp[hardware]"
```

或 Windows 双击 `install.bat`。

## MCP 注册

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

重启 AI 助手后生效。

## 可用工具

| 工具 | 功能 |
|------|------|
| `list_devices` | 扫描 PXIe 机箱，列出所有示波器 |
| `read_waveform` | 单通道采集 + 高级测量 + ASCII 波形 |
| `read_all_channels` | 全通道采集 + 机箱图 + 逐通道详细分析 |
| `measure_waveform` | 同 read_waveform |
| `configure_scope` | 配置垂直/水平/触发 |
| `auto_setup` | 自动设置 (Autoset) |
| `get_current_config` | 读取当前硬件配置 |
| `help` | 使用指南 + 参数参考 |

## 输出示例 (v2.1)

```
## Measurement Results

| Device    | CH  | Type     | Frequency/Vpp            | Time |
|-----------|-----|----------|--------------------------|------|
| PXI1Slot3 | CH0 | PERIODIC | 10.0000 MHz / 3.3307Vpp  | 1.5s |
| PXI1Slot3 | CH1 | PERIODIC | 274.6720 MHz / 0.4736Vpp | 2.6s |

## CH 3-0

| Parameter       | Value              |
|-----------------|--------------------|
| Frequency       | 10.0032 MHz        |
| Period          | 100.0 ns           |
| Amplitude       | 3.3307 Vpp         |
| Top / Base      | 3.1433 / 0.1038 V  |
| Duty Cycle      | 47.3%              |
| Pos / Neg Width | 47.84 / 52.13 ns   |
| Rise 10-90%     | 29.007 ns          |
| Fall 10-90%     | 22.412 ns          |
| Overshoot       | 6.16%              |
| Undershoot      | 3.42%              |
| Jitter (RMS)    | 1863.4 ps          |
| SNR (est.)      | 21.8 dB            |
| Sample Rate     | 312.5 MS/s         |
| Signal Levels   | 4 levels detected  |

Signal: 10.0032 MHz 3.33Vpp 3.3V CMOS D=47% →
  10MHz FPGA ref / GPS disciplined
  Jitter 1.9ns RMS — high jitter, check PLL/source
  Slow edges (29.0ns = 29% of period) — check drive strength
```

## 信号智能识别覆盖

| 类别 | 识别项 |
|------|--------|
| 时钟 | 32.768kHz RTC、1/4/5/8/10/12/16/20/24/25/27/33/40/48/50/66/75/100/125/133/148/156/200 MHz |
| 协议 | UART (9600-3M baud)、SPI CLK、I2C (100k/400k/1M)、CAN (125k-1M)、RS-232 |
| PWM | LED/电机/加热器 PWM、DC-DC 开关频率、VRM 控制器 |
| 质量 | 边沿时间 vs 周期比、抖动 RMS 分级 (优秀<10ps / 良好<100ps / 中等<500ps / 高) |

## 进程管理协议

```
每个通道读取流程：
  1. _kill_stale_workers()  — WMIC 扫描并查杀残留 _worker.py 进程
  2. subprocess.Popen       — 启动全新 worker 子进程
  3. proc.communicate(12s)  — 等待结果，10s 超时 → kill + skip + 原因
  4. finally: proc.kill()   — 无论成功失败，显式杀死 worker
  5. _active_worker_pid = None — 释放单进程互斥锁

全通道读取完成后：
  _cleanup_all() → 核验活跃 PID + 全量 WMIC 扫描，确保无残留
```

## 故障排查

| 问题 | 解决 |
|------|------|
| 设备 FAULTY | PXI 机箱断电重启 |
| 超时 (>10s) | 检查触发源/电平；尝试 free-run |
| 波形平直 | 检查连接、增大 vertical_range |
| niscope 包缺失 | `pip install "niscope-mcp[hardware]"` |
| MCP 工具不可见 | 检查 mcp.json 注册、重启 AI 助手 |

## 项目结构

```
niscope-mcp/
├── niscope_mcp/
│   ├── __init__.py
│   ├── __main__.py          # MCP Server + 进程管理 + 信号分析引擎
│   └── backends/
│       ├── __init__.py      # 后端工厂 + 自安装
│       ├── base.py          # 数据模型 + ScopeBackend 协议
│       ├── direct.py        # NI-SCOPE 硬件后端 (niscope API)
│       └── _worker.py       # 单通道子进程采集器 (自适应采样 + 高级测量)
├── pyproject.toml
└── README.md
```

## 许可

MIT
