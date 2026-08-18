# tests/ 目录结构

```
tests/
  run_regression.py         # 统一回归入口：python3 tests/run_regression.py [--quick]
  functional_verify.py      # 项目级功能正确性验证（27 工具交叉验证），
                            # 消费 /tmp/ot_build、/tmp/xs_build 构建产物
  full_quality_check_all_tools.py  # 全量质量检查（空结果率维度）

  unit/                     # 轻量单测（仅依赖 examples/sample，秒级）
    smoke_test.py           #   端到端冒烟
    test_definition_name.py #   definition_name 三层解析单测

  fourstate/                # 四态(X/Z)专项（依赖 iverilog，回归时自动重建波形）
    rtl/  tb/               #   自带小设计 + testbench
    run_fourstate_test.py   #   基础套件：X 传播/三态 Z/冲突 X/部分 dump/负路径
    run_fourstate_ext_test.py #  扩展套件：guard-X/casez/位选/锁存器/generate/wor
    sim/ session*/          #   仿真与 session 产物（生成物，不提交）

  projects/                 # 大型开源项目测试（构建耗时长，不进常规回归）
    opentitan_uart/         #   uart 单 IP golden 对比（golden_data.py 定义预期）
    opentitan_multi/        #   OpenTitan 多 IP 构建/穷尽/能力测试 + verilator 兼容层
    xiangshan/              #   香山按 IP 穷尽测试（依赖本地香山仓库）

  reports/                  # 所有测试报告统一归档
    functional/  quality_v1/  quality_v2/   # 项目级三轮报告
    opentitan/   xiangshan/   uart/         # 各项目专项报告（含 HTML 汇总）
    fourstate/                              # 四态套件报告

  archive/                  # 已被取代的历史脚本（保留追溯）
    full_quality_check.py   #   v1 质量检查（被 all_tools 版取代）
    run_test.py             #   uart v1 测试（被 run_test_v2 取代）
```

## 常用命令

```bash
# 日常回归（改完代码必跑；unit + 四态，秒级）
python3 tests/run_regression.py --quick

# 完整回归（若 /tmp 有 OpenTitan/香山构建产物，追加项目级验证）
python3 tests/run_regression.py

# 单独重跑某个项目级测试（先看各脚本头部说明，构建耗时较长）
python3 tests/projects/opentitan_multi/run_multi_scale.py
python3 tests/projects/xiangshan/run_xiangshan_exhaustive.py
```

注意：本目录含 OpenTitan/香山第三方代码与测试数据，整体被 .gitignore 排除，
不推送到公开仓库（unit/ 与本 README 除外，见 .gitignore）。
