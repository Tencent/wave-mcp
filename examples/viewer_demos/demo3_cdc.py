#!/usr/bin/env python3
"""Demo 3: CDC pulse-crossing loss with both clocks on screen.

Scenario
--------
Six pulses cross from the 50 MHz domain into the 20 MHz domain with NO
synchronizer. Two get captured, four vanish. The agent:

1. counts pulses in both domains from the waveform and spots the mismatch
2. checks signal_connectivity(pulse_seen): driven straight from the other
   clock domain, no synchronizer stage
3. presents it in the viewer with BOTH clocks visible, markers on a
   captured and a missed pulse, annotation recommending a 2-FF sync
4. walks the user through one missed pulse by moving the cursor there

Run:  python3 demo3_cdc.py
"""
import json
import sys

from common import DemoDriver, HERE, as_time

WAVES = HERE / "waves"
SESSION = WAVES / "session_cdc" / "session.json"


def rising_edges(rows) -> list:
    edges, prev = [], None
    for r in rows:
        if prev is not None and prev == "0" and r["value"] == "1":
            edges.append(r["time"])
        prev = r["value"]
    return edges


def main() -> int:
    d = DemoDriver()
    d.start()

    print(d.call("open_session", {"session_path": str(SESSION)}))

    # ---- 1. pulse accounting across domains ----------------------------
    d.call("signal_values", {"full_path": "cdc_tb.dut.pulse_fast"})
    sent = rising_edges(d.last_structured().get("values", []))
    d.call("signal_values", {"full_path": "cdc_tb.pulse_seen"})
    seen = rising_edges(d.last_structured().get("values", []))
    d.call("signal_values", {"full_path": "cdc_tb.pulse_count"})
    final_count = d.last_structured().get("values", [{}])[-1].get("value", "?")
    print(f"[demo3] pulses sent: {len(sent)}, captured: {len(seen)} "
          f"(count register says {int(final_count, 2)})")
    missed = [t for t in sent if not any(s >= t for s in seen[:sent.index(t) + 1])]
    print(f"[demo3] missed pulse times: {missed}")

    # ---- 2. connectivity proves the missing synchronizer ---------------
    d.call("signal_connectivity", {"full_path": "cdc_tb.dut.pulse_seen"})
    conn = d.last_structured()
    print(f"[demo3] pulse_seen connectivity: "
          f"{json.dumps(conn)[:220]}")

    # ---- 3. viewer: both clocks + captured vs missed markers -----------
    captured_t, missed_t = seen[0], missed[0]
    d.call("open_wave_view", {
        "fst_paths": [str(WAVES / "cdc.fst")],
        "signals": [
            {"path": "cdc_tb.clk_fast",           "group": "clock",
             "color": "blue"},
            {"path": "cdc_tb.clk_slow",           "group": "clock",
             "color": "purple"},
            {"path": "cdc_tb.rst_n",              "group": "clock"},
            {"path": "cdc_tb.trigger",            "group": "fast_domain",
             "color": "yellow"},
            {"path": "cdc_tb.dut.pulse_fast",     "group": "fast_domain",
             "color": "orange"},
            {"path": "cdc_tb.pulse_seen",         "group": "slow_domain",
             "color": "green"},
            {"path": "cdc_tb.pulse_count",        "group": "slow_domain",
             "format": "dec"},
        ],
        "viewport": {"from": "0", "to": "1100", "unit": "ns"},
        "markers": [
            {"time": captured_t, "unit": "ns",
             "label": "采到的脉冲", "color": "green"},
            {"time": missed_t, "unit": "ns",
             "label": "丢失的脉冲（窗口内无慢时钟沿）", "color": "red"},
        ],
        "annotation": {
            "markdown": (
                "**跨时钟域脉冲丢失**\n\n"
                f"快时钟域发出了 {len(sent)} 个脉冲，慢时钟域只收到了 "
                f"{len(seen)} 个。`pulse_seen` 直接在 `clk_slow` 上采样 "
                f"`pulse_fast`，中间没有任何同步器，因此任何在两个慢时钟"
                f"上升沿之间开始并结束的脉冲，对慢时钟域来说根本不存在。\n\n"
                f"**修法：** 在这条跨时钟路径上加两级触发器同步器，或者改用"
                f"握手/翻转（toggle）方案，不要直接传裸脉冲。"
            ),
            "confidence": "high",
            "evidence": [
                f"pulse_fast 上升沿 {len(sent)} 个；"
                f"pulse_seen 上升沿 {len(seen)} 个",
                "signal_connectivity(pulse_seen)：由 dut.pulse_fast（快时钟域）"
                "驱动，中间无同步级",
            ],
        },
    })

    # ---- 4. cursor walkthrough of one missed pulse ---------------------
    mt = int("".join(ch for ch in missed_t if ch.isdigit()))
    d.call("update_wave_view", {
        "view_id": d.view_id,
        "cursor": {"time": missed_t, "unit": "ns"},
        "viewport": {"from": str(mt - 60),
                     "to": str(mt + 60), "unit": "ns"},
        "annotation": {
            "markdown": (
                f"游标停在丢失脉冲的位置 {missed_t}。在框出的窗口内，"
                f"`pulse_fast` 为高期间 `clk_slow` 没有任何上升沿，"
                f"所以慢时钟域完全没有观测到这个脉冲。"
            ),
            "confidence": "high",
            "evidence": [f"[{missed_t}, {missed_t}+20] 窗口内无 "
                         f"clk_slow 上升沿"],
        },
    })

    state = d.call("get_view_state", {"view_id": d.view_id})
    print("[demo3] user cursor now at:",
          json.dumps(state.get("desired_summary", {}).get("cursor")))

    d.hold()
    return 0


if __name__ == "__main__":
    sys.exit(main())
