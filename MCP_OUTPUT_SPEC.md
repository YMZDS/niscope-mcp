# NI-SCOPE MCP 输出格式规范 v2.1

> 定义 `read_waveform`、`measure_waveform`、`read_all_channels` 的输出格式。
> 所有修改必须严格遵循此规范。

---

## 1. 公共头部 — 设备列表

所有读取工具（`read_waveform` / `measure_waveform` / `read_all_channels`）输出均以设备列表开头：

```
## Device List

| Slot   | Device     | Model              | Channels | Max SR   | Status    |
|--------|-----------|--------------------|----------|----------|-----------|
| Slot 3 | PXI1Slot3 | NI PXIe-5160 (2CH) | 2 CH     | 1.2 GS/s | OK        |
| Slot 4 | PXI1Slot4 | NI PXIe-5160 (2CH) | 2 CH     | 1.2 GS/s | OK        |
```

- 故障设备显示 `FAULT (原因)`
- 由 `_format_device_list()` 统一生成

---

## 2. `read_waveform` / `measure_waveform` — 单通道

```
## Measurement — CH0

| Parameter       | Value              |
|-----------------|--------------------|
| Frequency       | 10.0000 MHz        |
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
| RMS / Mean      | 1.1274 / +1.5395 V |
| Sample Rate     | 312.5 MS/s         |
| Signal Levels   | 4 levels detected  |

{ASCII waveform}

### Signal Analysis
- CH0: {signal_fingerprint}
```

### 参数表规则

**周期性信号** — 显示全部 16 行：
| # | 参数 | 来源字段 |
|---|------|---------|
| 1 | Frequency | `frequency_hz` |
| 2 | Period | `period_sec` |
| 3 | Amplitude | `vpp` (Vpp) |
| 4 | Top / Base | `top_level` / `base_level` |
| 5 | Duty Cycle | `duty_cycle_pct` |
| 6 | Pos / Neg Width | `pos_width_ns` / `neg_width_ns` |
| 7 | Rise 10-90% | `rise_10_90_ns` |
| 8 | Fall 10-90% | `fall_10_90_ns` |
| 9 | Overshoot | `overshoot_pct` |
| 10 | Undershoot | `undershoot_pct` |
| 11 | Jitter (RMS) | `jitter_ps` |
| 12 | SNR (est.) | `snr_db` |
| 13 | RMS / Mean | `rms_v` / `mean_v` |
| 14 | Sample Rate | `sample_rate_sps` |
| 15 | Signal Levels | `num_levels` (if > 2) |
| 16 | Mode | `burst_info` (if `is_burst`) |

**DC/Noise 信号** — 精简表：
| # | 参数 | 来源 |
|---|------|------|
| 1 | Type | `signal_type` |
| 2 | Mean | `mean_v` |
| 3 | RMS | `rms_v` |
| 4 | Min / Max | `min_v` / `max_v` |
| 5 | Vpp | `stats.peak_to_peak` |
| 6 | SNR (est.) | `snr_db` |
| 7 | Sample Rate | `sample_rate_sps` |

### 波形图

- 宽度: 100 (`read_waveform`) / 80 (`read_all_channels`)
- 高度: 16 (`read_waveform`) / 14 (`read_all_channels`)
- 垂直范围: `base_level - span*0.2` 到 `top_level + span*0.2`
- 触发电平: 周期信号取 `(top_level + base_level) / 2` 标记 `╌`
- 连线式波形 `╱╲│─●` + 水平网格 `┼┄` + 电压标签 + 时间轴

---

## 3. `read_all_channels` — 全通道

```
## Device List

{_format_device_list(devices)}

## Measurement Results

| Device    | CH  | Type     | Frequency/Vpp            | Time |
|-----------|-----|----------|--------------------------|------|
| PXI1Slot3 | CH0 | PERIODIC | 10.0000 MHz / 3.3307Vpp  | 1.5s |
| PXI1Slot3 | CH1 | PERIODIC | 274.6720 MHz / 0.4736Vpp | 2.6s |

### Skipped Channels (timeout >10s)  [if any]
- **PXI1Slot3 CH2**: TIMEOUT after 12s — channel skipped

## CH 3-0

{same per-channel detail as read_waveform}

## CH 3-1
...
```

### 汇总表规则

| 列 | 内容 |
|----|------|
| Device | `resource_name` |
| CH | `CH0`-`CH3` |
| Type | `PERIODIC` / `DC` / `NOISE` / `SKIP` / `ERROR` |
| Frequency/Vpp | 周期: `freq / Vpp`, DC: `DC meanV`, noise: `noise vppVpp` |
| Time | `elapsed_sec` |

### 进程清理

输出末尾显示: `No residual processes found — all clean.` 或 `Final cleanup: killed N residual process(es)`

---

## 4. 信号分析格式

```
{freq} {vpp}Vpp {logic_family} D={duty}% → {guess1}; {guess2}; {guess3}
```

### 逻辑电平识别 (`_identify_logic`)

| 条件 | 输出 |
|------|------|
| top ~3.3V, base ~0V | `3.3V CMOS` |
| top ~5.0V, base ~0V | `5V TTL` |
| top ~1.8V, base ~0V | `1.8V LVCMOS` |
| top ~2.5V, base ~0V | `2.5V LVCMOS` |
| Vpp < 0.5V | `LVDS/diff.` |
| Vpp > 10V | `RS-232` or `HV industrial` |

### 信号智能识别 (`_signal_guess`)

按优先级输出最多 4 条猜测，用 `; ` 分隔：

1. **时钟识别** — 32 种常见频率 (32.768kHz ~ 200MHz)
2. **协议检测** — UART/SPI/I2C/CAN/RS-232
3. **PWM 检测** — LED/电机/加热器/DC-DC/VRM
4. **质量分析** — 抖动分级 (excellent <10ps / good <100ps / moderate <500ps / high)，边沿占比警告

---

## 5. 数值格式

| 参数 | 格式 | 示例 |
|------|------|------|
| 频率 | `{:.4f} MHz` / `{:.2f} kHz` / `{:.2f} Hz` | `10.0000 MHz` |
| 周期 | `{:.1f} ns` | `100.0 ns` |
| 电压 | `{:.4f} V` / `{:+.4f} V` (均值) | `3.3307 V` / `+1.5462 V` |
| 百分比 | `{:.1f}%` / `{:.2f}%` (过冲) | `47.3%` |
| 时间(ns) | `{:.3f} ns` | `29.007 ns` |
| 抖动 | `{:.1f} ps` | `762.5 ps` |
| SNR | `{:.1f} dB` | `21.8 dB` |
| 采样率 | `{:.1f} MS/s` / `{:.1f} GS/s` | `312.5 MS/s` |

---

## 6. 代码位置

| 功能 | 文件 | 函数/位置 |
|------|------|----------|
| 设备列表 | `__main__.py` | `_format_device_list()` |
| 单通道格式化 | `__main__.py` | `_format_auto_measure()` |
| 参数表 | `__main__.py` | `_build_param_rows()` |
| 全通道输出 | `__main__.py` | `_read_all()` |
| 波形渲染 | `__main__.py` | `_fmt_ascii_waveform()` |
| 信号分析 | `__main__.py` | `_signal_fingerprint()` / `_signal_guess()` |
| 逻辑电平 | `__main__.py` | `_identify_logic()` |
| 协议检测 | `__main__.py` | `_identify_protocol()` |
| PWM 检测 | `__main__.py` | `_identify_pwm()` |
| 时钟识别 | `__main__.py` | `_identify_clock()` |
| 高级测量 | `backends/_worker.py` | `_compute_all()` |
| Level 检测 | `backends/_worker.py` | `_find_levels()` |
