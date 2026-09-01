#!/usr/bin/env python3
"""Demo 1: X-propagation debug with viewer presentation.

Scenario
--------
data_out carries X in its upper bits on EVERY packet. The agent:

1. opens the session and finds the X with signal_values
2. traces the root cause with trace_x (RTL backtracking)
3. presents the finding in the browser viewer: cursor at the first X,
   a marker there, the trace conclusion as an annotation with evidence

Run:  python3 demo1_xprop.py
"""
import json
import subprocess
import sys

from common import DemoDriver, HERE, as_time

WAVES = HERE / "waves"
SESSION = WAVES / "session_xprop" / "session.json"


def main() -> int:
    d = DemoDriver()
    d.start()

    # ---- 1. open the session and locate the X --------------------------
    print(d.call("open_session", {"session_path": str(SESSION)}))
    d.call("signal_values", {"full_path": "xprop_tb.data_out"})
    values = d.last_structured().get("values", [])
    x_rows = [v for v in values if "x" in v.get("value", "")]
    assert x_rows, "expected X values on data_out"
    first_x_time = as_time(x_rows[0]["time"])
    print(f"[demo1] data_out X on every packet; first X at {first_x_time}")

    # ---- 2. root cause: trace_x backtracks through the RTL -------------
    d.call("trace_x", {"signal_path": "xprop_tb.dut.data_out",
                       "time_point": first_x_time})
    trace = d.last_structured()
    suspects = json.dumps(trace)
    cause = ("byte_cnt never reset" if "byte_cnt" in suspects
             else "un-reset counter feeding data_out[7:5]")
    print(f"[demo1] trace_x root cause: {cause}")

    # also show WHERE the counter is driven (declaration + assignments)
    d.call("signal_drivers", {"full_path": "xprop_tb.dut.byte_cnt"})

    # ---- 3. present in the viewer --------------------------------------
    # signals: grouped datapath vs control, X-carrying bus in hex
    r = d.call("open_wave_view", {
        "fst_paths": [str(WAVES / "xprop.fst")],
        "signals": [
            {"path": "xprop_tb.clk",               "group": "时钟与复位"},
            {"path": "xprop_tb.rst_n",             "group": "时钟与复位"},
            {"path": "xprop_tb.start",             "group": "控制通路",
             "color": "yellow"},
            {"path": "xprop_tb.dut.fsm_state",     "group": "控制通路",
             "color": "purple", "format": "signed"},
            {"path": "xprop_tb.dut.byte_cnt",      "group": "数据通路",
             "color": "red", "format": "bin"},
            {"path": "xprop_tb.din",               "group": "数据通路",
             "format": "hex"},
            {"path": "xprop_tb.data_out",          "group": "数据通路",
             "color": "red", "format": "hex"},
            {"path": "xprop_tb.done",              "group": "控制通路"},
        ],
        "cursor": {"time": x_rows[0]["time"], "unit": "ns"},
        "viewport": {"from": "0", "to": "460", "unit": "ns"},
        "markers": [
            {"time": x_rows[0]["time"], "unit": "ns",
             "label": "data_out 上第一个 X", "color": "red"},
        ],
        "annotation": {
            "markdown": (
                f"**data_out 每个包都带 X**\n\n"
                f"`trace_x` 从 {first_x_time} 回溯到 `dut.byte_cnt`，"
                f"这个计数器**从未被复位**：初值是 X，而 `X+1=X` 会一直保持下去。"
                f"`data_out[7:5]` 里嵌了它，所以 X 一路传播到输出；"
                f"而 FSM 本身跑得完全正常（状态来自一个正确复位的 tick 计数器）。\n\n"
                f"**修法：** 在 `!rst_n` 分支里复位 `byte_cnt`（或者在 `start` "
                f"时给它初值）。"
            ),
            "confidence": "high",
            "evidence": [
                f"signal_values(data_out)：{first_x_time} 及之后一直为 X",
                "trace_x：X 路径终止于 dut.byte_cnt",
                "signal_drivers(byte_cnt)：只有 SHIFT 里的赋值",
            ],
        },
    })
    print(json.dumps(r, indent=2))

    # ---- 4. incremental refinement: zoom into the corrupted packet -----
    d.call("update_wave_view", {
        "view_id": d.view_id,
        "viewport": {"from": "70", "to": "130", "unit": "ns"},
        "markers": [
            {"time": x_rows[0]["time"], "unit": "ns",
             "label": "data_out 上第一个 X", "color": "red"},
            {"time": "75", "unit": "ns",
             "label": "rst_n 在运行中途释放", "color": "orange"},
        ],
        "annotation": {
            "markdown": (
                "已放大到第一个被破坏的包。注意 `fsm_state` 正常地走完 "
                "IDLE->SHIFT->FLUSH，而 `byte_cnt` 全程一直是 `xxx`："
                "控制通路是健康的，坏掉的只有那个没复位的计数器。"
            ),
            "confidence": "high",
            "evidence": ["fsm_state transitions normal; byte_cnt constant xxx"],
        },
    })

    # ---- 5. bidirectional awareness: read what the user sees -----------
    state = d.call("get_view_state", {"view_id": d.view_id})
    print("[demo1] get_view_state.actual:",
          json.dumps(state.get("actual"), indent=2)[:400])

    d.hold()
    return 0


if __name__ == "__main__":
    sys.exit(main())
