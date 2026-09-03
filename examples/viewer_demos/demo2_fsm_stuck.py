#!/usr/bin/env python3
"""Demo 2: FSM deadlock localization with viewer presentation.

Scenario
--------
rd_count advances for three transactions, then freezes forever while req
stays high. The agent:

1. notices the flatline in signal_values(rd_count)
2. checks the handshake pair: req stuck 1, ack never came for tx #4
3. inspects the FSM state at the stuck time (signal_value_at + drivers)
4. presents it in the viewer: cursor at the deadlock, markers at the last
   completed tx and the req-rise of the stuck one, annotation concluding
   "WAIT_ACK has no timeout escape"
5. simulates the user staring at the handshake by zooming there, then
   reads back the actual view state (bidirectional awareness)

Run:  python3 demo2_fsm_stuck.py
"""
import json
import sys

from common import DemoDriver, HERE, as_time

WAVES = HERE / "waves"
SESSION = WAVES / "session_fsm_stuck" / "session.json"


def main() -> int:
    d = DemoDriver()
    d.start()

    print(d.call("open_session", {"session_path": str(SESSION)}))

    # ---- 1. flatline detection -----------------------------------------
    d.call("signal_values", {"full_path": "fsm_stuck_tb.rd_count"})
    rows = d.last_structured().get("values", [])
    last_change = rows[-1]
    last_t = as_time(last_change["time"])
    print(f"[demo2] rd_count stops advancing after {last_t} "
          f"(value {last_change['value']})")

    # ---- 2. handshake pair at the stuck point --------------------------
    d.call("signal_values", {"full_path": "fsm_stuck_tb.req"})
    req_rows = d.last_structured().get("values", [])
    stuck_req = [r for r in req_rows if r["time_units"] >= last_change["time_units"]
                 and r["value"] == "1"]
    stuck_t = as_time(stuck_req[-1]["time"]) if stuck_req else "145s"
    d.call("signal_value_at", {"full_path": "fsm_stuck_tb.ack",
                               "time_as_string": stuck_t})
    ack_val = d.last_structured().get("value", "?")
    print(f"[demo2] at {stuck_t}: req=1, ack={ack_val} -> handshake stalled")

    # ---- 3. FSM state + drivers at the stuck point ---------------------
    d.call("signal_value_at", {"full_path": "fsm_stuck_tb.dut.state",
                               "time_as_string": stuck_t})
    state_val = d.last_structured()
    d.call("signal_drivers", {"full_path": "fsm_stuck_tb.dut.state"})
    print(f"[demo2] FSM at {stuck_t}: {state_val}")

    # ---- 4. viewer presentation ----------------------------------------
    d.call("open_wave_view", {
        "fst_paths": [str(WAVES / "fsm_stuck.fst")],
        "signals": [
            {"path": "fsm_stuck_tb.clk",             "group": "clock"},
            {"path": "fsm_stuck_tb.rst_n",           "group": "clock"},
            {"path": "fsm_stuck_tb.rd_en",           "group": "handshake",
             "color": "yellow"},
            {"path": "fsm_stuck_tb.req",             "group": "handshake",
             "color": "red"},
            {"path": "fsm_stuck_tb.ack",             "group": "handshake",
             "color": "green"},
            {"path": "fsm_stuck_tb.dut.state",       "group": "fsm",
             "color": "purple", "format": "signed"},
            {"path": "fsm_stuck_tb.rd_count",        "group": "status",
             "format": "dec"},
        ],
        "cursor": {"time": stuck_req[-1]["time"] if stuck_req else "145",
                   "unit": "ns"},
        "viewport": {"from": "0", "to": "240", "unit": "ns"},
        "markers": [
            {"time": "125", "unit": "ns",
             "label": "最后一笔完成的事务（rd_count=3）", "color": "green"},
            {"time": stuck_req[-1]["time"] if stuck_req else "145",
             "unit": "ns", "label": "req 拉高但 ack 始终不来",
             "color": "red"},
        ],
        "annotation": {
            "markdown": (
                f"**FSM 死锁：WAIT_ACK 没有超时**\n\n"
                f"`rd_count` 从 {last_t} 起一直卡在 3。{stuck_t} 时 `req` "
                f"为第 4 次读拉高，但 `ack` 始终没有应答（从端反压），"
                f"而 FSM 只在 `WAIT_ACK` 状态里采样 `ack`，没有任何退出路径。\n\n"
                f"**修法：** 在 `WAIT_ACK` 里加一个超时计数器，"
                f"连续 N 拍等不到 `ack` 就回到 `IDLE`（并上报错误）。"
            ),
            "confidence": "high",
            "evidence": [
                f"rd_count 在 {last_t} 之后不再变化",
                f"req 从 {stuck_t} 起一直为 1；{stuck_t} 处 ack={ack_val}",
                "signal_drivers(state)：单个 always 块，case WAIT_ACK",
            ],
        },
    })

    # ---- 5. zoom where the user would look, then read back actual ------
    d.call("update_wave_view", {
        "view_id": d.view_id,
        "viewport": {"from": "100", "to": "240", "unit": "ns"},
    })
    state = d.call("get_view_state", {"view_id": d.view_id})
    print("[demo2] actual viewport (user sees):",
          json.dumps(state.get("actual", {}).get("viewport")))

    d.hold()
    return 0


if __name__ == "__main__":
    sys.exit(main())
