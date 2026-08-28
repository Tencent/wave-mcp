# Verilator quickstart（开箱示例，无需 xrun）

用开源 **Verilator** 对一个极小的 counter 设计产出**真实 FST 波形**，再用 wave-mcp
的统一入口 `prepare_session` 直接打开、查询。全程**不依赖任何商用仿真器（xrun），
也不需要 vcd2fst**，Verilator 的 `--trace-fst` 直接写 FST。

## 依赖
- `verilator` >= 5.006（提供 `--binary`；任意 5.x 均可）
- 已安装本仓库 wave-mcp

## 一键运行
```bash
python examples/verilator_quickstart/run.py
```

脚本做四件事：
1. `verilator --binary --trace-fst` 编译 `counter.sv` + `tb_counter.sv`
2. 运行产出的仿真二进制 → 在 `build/` 下 dump 出真实 `counter.fst`
3. `prepare_session(out_dir, wave_path=counter.fst, top=top_tb, filelist=[...])`，
   `.fst` 零转换直读，与 xrun 产出的 `.fst`/`.vcd` 走同一入口
4. 打印 session 摘要 + 层次 / 信号 / 信号值查询结果

## 手动分解（想自己一步步跑）
```bash
cd examples/verilator_quickstart
verilator --binary --trace-fst -j 0 --top-module top_tb -o Vcounter counter.sv tb_counter.sv
./obj_dir/Vcounter          # 产出 counter.fst
# 然后在 MCP 客户端里：
#   prepare_session(out_dir="sess", wave_path="counter.fst", top="top_tb",
#                   filelist=["counter.sv","tb_counter.sv"])
```

## 文件
- `counter.sv`：8 位自增计数器 DUT（带 overflow）
- `tb_counter.sv`：自包含 testbench（时钟/复位 + `$dumpfile/$dumpvars`）
- `run.py`：一键 orchestrator（编译 → 产 FST → prepare_session → 查询）

> 生成物都落在 `build/`（已被 `.gitignore` 忽略），可随时删除重跑。
