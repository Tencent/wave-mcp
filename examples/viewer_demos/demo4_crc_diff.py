#!/usr/bin/env python3
"""Demo 4: pass/fail diff with the dual-waveform viewer.

Scenario
--------
Two builds of the SAME RTL with the SAME stimulus; the fail build carries
a one-typo CRC tap bug (`^ data[0]` missing). The agent:

1. runs diff_waveforms -> first_divergence + diverging_signals
2. backtracks the earliest diverger with signal_fanin
3. opens a dual-waveform diff view (labels pass/fail, diff reference,
   auto red marker at the divergence, cursor parked there)
4. adds the residue mismatch as a second marker + the fanin conclusion
   as an annotation with evidence

Run:  python3 demo4_crc_diff.py
"""
import json
import sys

from common import DemoDriver, HERE, as_time

WAVES = HERE / "waves"
PASS_FST = WAVES / "crc_pass.fst"
FAIL_FST = WAVES / "crc_fail.fst"
SESSION = WAVES / "session_crc_diff" / "session.json"


def main() -> int:
    d = DemoDriver()
    d.start()

    print(d.call("open_session", {"session_path": str(SESSION)}))

    # ---- 1. locate the first divergence --------------------------------
    d.call("diff_waveforms", {
        "fst_a": str(PASS_FST),          # convention: pass first
        "fst_b": str(FAIL_FST),
        "clock": "crc_diff_tb.clk",      # clock-aligned sampling
        "after": "15ns",                 # skip reset
    })
    r = d.last_structured()
    fd = r.get("first_divergence", {})
    div_t = fd.get("time", "35")
    earliest = (r.get("diverging_signals") or [{}])[0]
    print(f"[demo4] first divergence at {div_t}: "
          f"{earliest.get('path')} {earliest.get('value_a')} -> "
          f"{earliest.get('value_b')}")
    print(f"[demo4] compared: {r.get('compared')}, "
          f"coverage: {r.get('coverage')}")

    # ---- 2. causal backtracking on the earliest diverger ---------------
    d.call("signal_fanin", {"signal_path": "crc_diff_tb.dut.crc"})
    fanin = d.last_structured()
    print(f"[demo4] crc fan-in: {json.dumps(fanin)[:200]}")

    # residue mismatch time (crc_err asserted only in the fail run)
    d.call("signal_values", {"full_path": "crc_diff_tb.crc_err"})
    err_rows = [v for v in d.last_structured().get("values", [])
                if v["value"] == "1"]
    err_t = as_time(err_rows[0]["time"]) if err_rows else "845s"

    # ---- 3. dual-waveform diff view ------------------------------------
    d.call("open_wave_view", {
        "fst_paths": [str(PASS_FST), str(FAIL_FST)],
        "labels": ["pass", "fail"],
        "signals": [
            {"path": "crc_diff_tb.clk",            "group": "时钟",
             "source": "a"},
            {"path": "crc_diff_tb.valid",          "group": "激励",
             "source": "a", "color": "yellow"},
            {"path": "crc_diff_tb.data",           "group": "激励",
             "source": "a", "format": "bin"},
            {"path": "crc_diff_tb.eop",            "group": "激励",
             "source": "a"},
            {"path": "crc_diff_tb.dut.crc",        "group": "CRC（分叉点）",
             "source": "b", "color": "red", "format": "bin"},
            {"path": "crc_diff_tb.crc_residue",    "group": "CRC（分叉点）",
             "source": "b", "color": "red", "format": "bin"},
            {"path": "crc_diff_tb.crc_err",        "group": "校验结果",
             "source": "b", "color": "red"},
        ],
        "cursor": {"time": div_t, "unit": "ns"},
        "viewport": {"from": "0", "to": "1000", "unit": "ns"},
        "diff": {
            "source_a": "a", "source_b": "b",
            "first_divergence": {"time": div_t, "unit": "ns"},
        },
        "annotation": {
            "markdown": (
                f"**首个分叉出现在 {div_t}s 的 `dut.crc` 上**\n\n"
                f"在激励完全相同的情况下，LFSR 状态从第一个 `valid` 节拍"
                f"就开始分叉：fail 版本少了一个 `crc[0]` 的 `^ data[0]` 抽头。"
                f"下游所有东西（eop 处的残差、`crc_err`）都继承了这个偏差。\n\n"
                f"`crc_err` 只在 fail 版本中于 {err_t} 拉高。"
            ),
            "confidence": "high",
            "evidence": [
                f"diff_waveforms：{earliest.get('path')} 在 {div_t} 处分叉"
                f"（{earliest.get('value_a')} vs {earliest.get('value_b')}）",
                "signal_fanin(crc)：data 与 crc 抽头，无时钟/复位偏移",
                f"crc_err=1 出现在 {err_t}，仅在 fail 波形中",
            ],
        },
    })

    # ---- 4. follow-up marker where the check fires ---------------------
    d.call("update_wave_view", {
        "view_id": d.view_id,
        "markers": [
            {"time": div_t, "unit": "ns",
             "label": "首个分叉（dut.crc）", "color": "red"},
            {"time": err_t, "unit": "ns",
             "label": "crc_err 拉高（仅 fail）", "color": "orange"},
        ],
        "viewport": {"from": "0", "to": "1000", "unit": "ns"},
    })

    state = d.call("get_view_state", {"view_id": d.view_id})
    print("[demo4] desired markers:",
          json.dumps(state.get("desired_summary", {}).get("markers")))

    d.hold()
    return 0


if __name__ == "__main__":
    sys.exit(main())
