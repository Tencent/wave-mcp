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
```

> 面向大型真实项目的功能验证、质量检查与历史报告属内部测试资产，
> 不随仓库公开（见 .gitignore）。

## 常用命令

```bash
# 日常回归（改完代码必跑；unit + 四态，秒级）
python3 tests/run_regression.py --quick

# 完整回归（公开仓库仅含 unit/四态套件，项目级验证仅在内部环境运行）
python3 tests/run_regression.py
```
