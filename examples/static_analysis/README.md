# Static-analysis example（无波形、无仿真器的静态分析示例）

展示 wave-mcp 的**独有能力**：`open_static_session` 只凭 RTL 源码建网表并打开 session，
**不需要波形、不跑仿真**——仿真前即可分析设计结构、驱动、扇入/扇出和声明位置。

## 一键运行

```bash
python examples/static_analysis/run.py
```

零依赖（纯 Python，不需要 Verilator / vcd2fst / 任何仿真器）。

## 演示内容

| 步骤 | 工具 | 展示 |
| --- | --- | --- |
| 1 | `open_static_session` | RTL-only 打开 session（mode: static） |
| 2 | `list_child_instances` | 两级层次：`uart_top.u_tx` → `u_baud_gen` |
| 3 | `list_modules` | 设计全部 3 个模块定义 |
| 4 | `list_signals` | TX 核 12 个信号（端口 + 内部，含位宽/方向/声明行号） |
| 5 | `signal_drivers` | `tx_serial` 全部驱动 + 分支条件（guard） |
| 6 | `signal_fanin` | `state` 寄存器扇入 |
| 7 | `signal_connectivity` | baud `tick` 的直连信号 |
| 8 | `signal_info` | `bit_cnt` 声明位置（file:line） |
| 9 | `scope_info` | `u_baud_gen` 模块类型 + 声明位置 |
| 10 | `modules_in_file` | 单文件 3 个模块 |
| 11 | `signal_values` | 无波形时正确返回 "needs waveform" 提示 |

## 文件

- `uart_top.sv` — UART TX 设计（顶层 + TX 核 + 波特率分频，两级层次、完整驱动关系）
- `run.py` — 一键演示脚本（生成的 session 在 `session/`，已 gitignore）

## 下一步

仿真产出波形后，用**同一个 out_dir** 调 `prepare_session` 即可升级为完整 session——
已建好的网表直接复用，值查询 / trace 类工具随即可用。
