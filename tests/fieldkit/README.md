# fieldkit — 加密网现场测试与反馈工具

在无外网、仅可"限长复制粘贴"带出信息的环境中运行 wave-mcp 测试，
并产出**完全脱敏**的结果反馈。

## 脱敏保证

L0/L1/L2 输出中**永不出现**：信号名、模块名、实例路径、文件路径、RTL 文本。
只包含：计数、比例、耗时、版本号、错误分类码。异常消息一律折叠为
`异常类名@8位摘要`（如 `FileNotFoundError@a1b2c3d4`），原文不外泄。

## 用法（网内）

```bash
# 1) 环境 + 自检（不需要任何业务数据；安装后先跑这个）
python3 tests/fieldkit/run_fieldkit.py

# 2) 真实项目测试（业务波形 + filelist；结果自动脱敏）
python3 tests/fieldkit/run_fieldkit.py \
    --wave sim/dump.vcd --filelist rtl.f --top top_tb
```

## 反馈方式（按通道容量选一）

| 级别 | 载体 | 大小 | 何时用 |
|---|---|---|---|
| L0 | 报告末尾单行（`WMFK1.0\|py3.11...`） | ~120 字符 | 粘贴额度极小时 |
| L1 | 终端整块文本 | ≤40 行 | 常规反馈（推荐） |
| L2 | `fieldkit_report.json` | 数 KB | 有文件外发通道时 |

把 L1（或至少 L0）带出后原样提供给外网开发侧即可定位问题。

## 错误分类码

| 码 | 含义 | 外网侧动作 |
|---|---|---|
| E-ENV-PYVER / E-ENV-IMPORT | Python 版本 / 二进制依赖导入失败 | 换 bundle 的 Python/wheel 平台 |
| E-ENV-CMD | console 命令不在 PATH | 检查 install.sh / PATH（非致命）|
| E-VCD-CONVERT | VCD→FST 转换失败 | 排查 xrun VCD 方言 |
| E-VCD-DIALECT | FST 打开但层次/信号异常 | 同上，需 L2 的计数辅助 |
| E-FST-OPEN | 波形打不开 | 检查文件完整性/格式 |
| E-NETLIST-BUILD / -PARTIAL | 网表完全失败 / 带错误降级 | 看 elab_errors 数值 |
| E-NETLIST-PROTECT | 检出加密源（pragma protect） | 预期降级，确认不 crash |
| E-ALIGN-COVERAGE | 波形↔网表对齐率 <50% | trace 类工具受限，需排查命名 |
| E-TOOL-CRASH / -EMPTY | 某工具异常 / 预期有数据却为空 | 按工具名+异常码定位 |
| E-PERF-SLOW | 超时间预算（默认 30s/工具） | 看 slow_tools 条目 |

## 判读速查

- `selftest 4/4` 失败 → 工具本体/依赖坏了，先解决环境再谈业务测试
- `netlist_trust=full` + `def_coverage_pct≥90` → 全部 27 工具可信
- `trust=partial` → 值查询类仍可信；连接/trace 类结果可能不完整
- `trust=none` → 只有波形类工具可用（预期行为，非 crash 即正常）
