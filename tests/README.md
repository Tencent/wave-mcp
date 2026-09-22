# tests/ 目录结构

以下内容随仓库公开：

```
tests/
  run_regression.py         # 统一回归入口：python3 tests/run_regression.py [--quick]
  unit/                     # 轻量单测（仅依赖 examples/sample，秒级）
    smoke_test.py           #   端到端冒烟
    test_definition_name.py #   definition_name 三层解析单测

  fourstate/                # 四态(X/Z)专项（依赖 iverilog，回归时自动重建波形）
    rtl/  tb/               #   自带小设计 + testbench
    run_fourstate_test.py   #   基础套件：X 传播/三态 Z/冲突 X/部分 dump/负路径
    run_fourstate_ext_test.py #  扩展套件：guard-X/casez/位选/锁存器/generate/wor

  fieldkit/                 # 部署环境自检套件（隔离网现场用）

  functional_verify.py      # 项目级功能正确性验证（需自备项目波形数据）
  full_quality_check_all_tools.py # 项目级全量质量检查（需自备项目波形数据）
```

> 项目级测试数据与历史报告（tests/projects/、tests/reports/ 等）属内部
> 测试资产，不随仓库公开（见 .gitignore）；上面两个验证脚本本身随仓库
> 公开，任何人可用自己的项目数据运行。

## 项目级验证的数据配置

`functional_verify.py` 与 `full_quality_check_all_tools.py` 通过
`WAVE_MCP_PROJECT_ASSETS` 环境变量（`os.pathsep` 分隔）或
`tests/project_assets.txt`（每行一个条目，支持 `#` 注释）定位数据。
每个条目是以下两种之一：

- 一个 `*.fst` 波形文件，其网表 maps 位于同目录的
  `session/netlist/maps.json`
- 一个多 IP 构建目录，其中每个子目录含一个 `*.fst` 和
  `session/netlist/maps.json`

两者都未配置时，回归入口自动跳过项目级套件。

## 常用命令

```bash
# 日常回归（改完代码必跑；unit + 四态，秒级）
python3 tests/run_regression.py --quick

# 完整回归（含项目级验证，需按上文配置项目数据；未配置则自动跳过）
python3 tests/run_regression.py
```
