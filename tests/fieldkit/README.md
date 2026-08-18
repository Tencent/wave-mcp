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

# 3) 出现问题时追加结构指纹（仍然全脱敏），供外网合成复现用例
python3 tests/fieldkit/run_fieldkit.py \
    --wave sim/dump.vcd --filelist rtl.f --top top_tb --fingerprint
```

## 结构指纹（--fingerprint）

指纹只包含**格式/枚举 token 的统计量**（var 类型直方图、标识符字符集分类、
scope 深度分桶、genblk 命名模式计数、驱动 kind 直方图等），不含任何标识符
原文。作用：外网侧照着指纹特征用开源工具**合成等价用例**复现问题——
例如 `identifier_classes escaped:120` + `E-VCD-DIALECT` 意味着"构造一个含
转义标识符的 VCD 即可复现"。

| 段 | 内容 | 复现用途 |
|---|---|---|
| fingerprint.vcd | $var 类型/标识符类别/scope 类型直方图、嵌套深度、header 指令 | 合成同方言 VCD |
| fingerprint.wave | 信号宽度/scope 深度分桶、块命名模式（genblk vs 命名） | 构造同形层次 |
| fingerprint.netlist | 驱动 kind 直方图、每模块驱动数分桶、含 skip 的模块数 | 定位弹性展开薄弱语法 |

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

## 网内最小化复现指南（当分类码+指纹仍不足以定位时）

人在网内可以看到完整现场——利用这一点把业务问题剥成**可带出的脱敏骨架**。
步骤：

1. **二分缩小**：从触发问题的模块开始，逐步删除无关代码/信号，每删一轮
   重跑一次确认问题仍复现，直到剩下最小触发结构（通常 <30 行）。
2. **脱敏改写**：信号/模块名全部改为 a/b/sel/mod1 这类通用名；常量值改为
   0/1/全F；位宽保留原值（宽度常是触发条件）；逻辑结构**保持原样**——
   触发问题的是结构（如 `assign o = en ? v : 'z` 的三态样式、genblk 嵌套、
   interface modport），不是名字。
3. **自证脱敏**：检查骨架里不含任何业务词汇后，连同 L1 报告一起带出。
4. 外网侧用骨架 + Icarus/Verilator 复现、修复、加进回归套件。

> 判断"能不能带出"的标准：这段代码若出现在任何开源教科书里毫不违和，
> 即可带出。拿不准就再改写一轮，或只带出结构描述文字。

同样的方法适用于公网用户：不愿贴公司代码的用户可按此指南提交最小化
issue（报告 L1 + 脱敏骨架），配合 --fingerprint 输出即可高效定位。
