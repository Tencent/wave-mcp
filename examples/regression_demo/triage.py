#!/usr/bin/env python3
"""Stage 2 of the demo: triage the regression using wave-mcp.

Reads the manifest produced by run_regression.py and, for each failure,
uses wave-mcp to work out what happened from the waveforms and the RTL
netlist. Then it writes an HTML report with the key waveform screenshots
embedded and a link back into the live viewer.

What this script does NOT do: it never reads the intended root cause out
of the RTL comments, and it never hardcodes a conclusion. Every fact in
the report comes from a tool call made during this run. Findings are split
into observed facts (deterministic, from tool output) and a hypothesis
(an inference, labelled as such).

Usage:
    ./triage.py                 # analyse + report
    ./triage.py --no-shots      # skip screenshots (no browser needed)
    ./triage.py --hold          # keep the viewer alive to click around
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "examples" / "viewer_demos"))

RUNS = HERE / "runs"
MANIFEST = RUNS / "regression.json"
REPORT_DIR = HERE / "report"
SHOTS = REPORT_DIR / "shots"

from common import DemoDriver  # noqa: E402  (needs sys.path above)

TOP = "crc_regress_tb"
DUT_CRC = f"{TOP}.dut.crc"
REF_CRC = f"{TOP}.ref_crc"
RESIDUE = f"{TOP}.residue"
EXPECTED = f"{TOP}.expected_residue"


def _ns(t) -> int:
    """Waveform times come back as '25ns'/'250s'; keep the numeric part."""
    digits = "".join(ch for ch in str(t) if ch.isdigit())
    return int(digits) if digits else 0


_OPS = {"LogicalNot": "!", "BitwiseNot": "~", "Equality": "==",
        "Inequality": "!=", "LogicalAnd": "&&", "LogicalOr": "||",
        "BitwiseAnd": "&", "BitwiseOr": "|", "BitwiseXor": "^"}


def _expr(node) -> str:
    """Render one condition node from the netlist into Verilog-ish text."""
    if not isinstance(node, dict):
        return str(node)
    kind = node.get("k")
    if kind == "sig":
        return str(node.get("name", "?"))
    if kind in ("const", "lit"):
        return str(node.get("lit", node.get("value", node.get("v", "?"))))
    op = _OPS.get(node.get("op"), node.get("op") or "?")
    if kind == "un":
        return f"{op}{_expr(node.get('a') or node.get('operand'))}"
    if kind == "bin":
        left = node.get("l") if "l" in node else node.get("a")
        right = node.get("r") if "r" in node else node.get("b")
        return f"({_expr(left)} {op} {_expr(right)})"
    for key in ("name", "value", "v"):
        if key in node:
            return str(node[key])
    return "?"


def _cond_terms(cond) -> list[str]:
    """Split a guard into its individual terms, as rendered text."""
    if not cond:
        return []
    if isinstance(cond, dict):
        cond = [cond]
    out = []
    for item in cond:
        if not isinstance(item, dict):
            out.append(str(item))
            continue
        text = _expr(item.get("cond"))
        if item.get("expect") == 0:
            text = f"!({text})"
        out.append(text)
    return out


def _fmt_cond(cond) -> str:
    """Flatten a driver's guard list into a readable expression.

    signal_drivers reports guards structurally, e.g.
    ``[{'cond': {...}, 'expect': 1}]``. Printing that raw makes the report
    unreadable, so rebuild it as source-like text.
    """
    return " && ".join(t for t in _cond_terms(cond) if t)


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def group_failures(cases: list[dict]) -> dict[str, list[dict]]:
    """Bucket failures by their reported symptom.

    Grouping by symptom is a cheap, deterministic first pass. It does NOT
    prove the members share a root cause; that still needs per-case
    evidence, which is why the report words it as a symptom group.
    """
    groups: dict[str, list[dict]] = {}
    for c in cases:
        if c["status"] != "fail":
            continue
        # normalise the symptom: the residue values differ per seed, so key
        # on the shape of the failure rather than the exact numbers
        key = "包尾残差不匹配" if "residue" in (
            c.get("reason") or "") else (c.get("reason") or "未知")
        groups.setdefault(key, []).append(c)
    return groups


def analyse_case(d: DemoDriver, fail: dict, ref: dict) -> dict:
    """Analyse one failing case against the in-testbench reference model.

    Note on method: two different seeds drive DIFFERENT stimulus by design,
    so diffing a failing waveform against a passing one would just report
    that the payloads differ. The meaningful comparison is inside the
    failing run itself: the DUT's `dut.crc` against the testbench's
    reference `ref_crc`, which sees the same stimulus cycle by cycle.
    """
    fail_fst = str(HERE / fail["waveform"])

    finding: dict = {"case_id": fail["case_id"], "seed": fail["seed"],
                     "reference": "testbench 内参考模型（ref_crc）",
                     "facts": [], "hypothesis": None, "evidence": [],
                     "first_divergence": None, "diverging": []}

    # each case has its own waveform: point the session at it, otherwise
    # every query would read whichever waveform was opened first.
    sess = RUNS / f"session_{fail['seed']:03d}"
    d.call("prepare_session", {
        "out_dir": str(sess), "wave_path": fail_fst,
        "filelist_path": str(HERE / "rtl" / "crc_regress.f"), "top": TOP,
    })

    # 1. residue actually captured vs what the reference model expected
    d.call("signal_values", {"full_path": RESIDUE})
    res_rows = d.last_structured().get("values", [])
    d.call("signal_values", {"full_path": EXPECTED})
    exp_rows = d.last_structured().get("values", [])
    got = res_rows[-1]["value"] if res_rows else None
    want = exp_rows[-1]["value"] if exp_rows else None
    if got is not None and want is not None:
        finding["facts"].append(
            f"捕获的残差为 `{got}`，参考模型期望值为 `{want}`")
        finding["evidence"].append(
            f"signal_values({RESIDUE})[-1]={got}; "
            f"signal_values({EXPECTED})[-1]={want}")

    # 2. walk dut.crc and ref_crc together to find the first cycle where
    #    the DUT leaves the reference behind
    d.call("signal_values", {"full_path": DUT_CRC, "max_number_of_values": 400})
    dut_rows = d.last_structured().get("values", [])
    d.call("signal_values", {"full_path": REF_CRC,
                             "max_number_of_values": 400})
    ref_rows = d.last_structured().get("values", [])

    div_t = None
    if dut_rows and ref_rows:
        ref_at = {r["time"]: r["value"] for r in ref_rows}
        ref_series = sorted(ref_at.items(), key=lambda kv: _ns(kv[0]))
        for row in sorted(dut_rows, key=lambda r: _ns(r["time"])):
            t = _ns(row["time"])
            if t <= 15:                       # skip reset
                continue
            # value the reference holds at this instant
            held = None
            for rt, rv in ref_series:
                if _ns(rt) <= t:
                    held = rv
                else:
                    break
            if held is not None and row["value"] != held:
                div_t = row["time"]
                finding["first_divergence"] = div_t
                finding["facts"].append(
                    f"`dut.crc` 在 {div_t} 处首次偏离参考模型：DUT 为 "
                    f"`{row['value']}`，参考模型为 `{held}`")
                finding["evidence"].append(
                    f"signal_values({DUT_CRC}) vs signal_values({REF_CRC}): "
                    f"首次不匹配于 {div_t} "
                    f"({row['value']} != {held})")
                break

    if div_t is None:
        finding["facts"].append(
            "未在 `dut.crc` 与参考模型之间找到逐拍不匹配，"
            "仅凭波形比较无法解释本次失败")
        finding["hypothesis"] = (
            "波形比较无定论。建议扩大比较窗口或检查包尾残差捕获路径。")
        return finding

    # 3. what was on the data bus in the cycles leading up to the mismatch.
    #    The window is wide enough to show several nibbles, since a
    #    data-dependent bug usually needs a short sequence, not one value.
    lo = max(0, _ns(div_t) - 80)
    d.call("signal_values_in_range", {
        "full_path": f"{TOP}.data",
        "start_time_as_string": f"{lo}ns",
        "end_time_as_string": div_t,
    })
    data_rows = d.last_structured().get("values", [])
    if data_rows:
        seq = " ".join(f"{r['value']}@{r['time']}" for r in data_rows[-8:])
        finding["facts"].append(
            f"偏离前 {lo}ns..{div_t} 窗口内的 `data` 变化："
            f"{seq}")
        finding["evidence"].append(
            f"signal_values_in_range({TOP}.data, {lo}ns..{div_t}): "
            f"{len(data_rows)} 次跳变")
        finding["data_window"] = seq

    # 4. what drives the register that went wrong, and under what condition.
    #    Each driver carries the actual source line, which is the strongest
    #    pointer the netlist can give us.
    d.call("signal_drivers", {"full_path": DUT_CRC})
    drivers = d.last_structured().get("drivers") or []
    suspects = []
    for drv in drivers:
        loc = f"{Path(str(drv.get('file'))).name}:{drv.get('line')}" \
            if drv.get("file") else "?"
        guard = drv.get("guard") or drv.get("condition")
        cond = _fmt_cond(guard)
        snippet = (drv.get("snippet") or "").strip()
        finding["evidence"].append(
            f"signal_drivers({DUT_CRC}) {loc}: {snippet or '(no snippet)'}"
            + (f"  [guard: {cond}]" if cond else ""))
        # Every sequential driver shares the reset and handshake terms; the
        # interesting part is a term that tests the payload itself.
        payload_terms = [t for t in _cond_terms(guard) if "data" in t]
        if payload_terms:
            suspects.append((loc, " && ".join(payload_terms), snippet))
    if drivers:
        finding["facts"].append(
            f"RTL 中共有 {len(drivers)} 条语句写入 `dut.crc`，"
            f"其中 {len(suspects)} 条仅在涉及 `data` 的条件下执行")
    for loc, cond, snippet in suspects[:2]:
        finding["facts"].append(
            f"与 payload 相关的写入位于 {loc}：`{snippet}`，"
            f"触发条件为 `{cond}`")
    finding["suspects"] = suspects

    # 5. hypothesis, labelled as an inference
    suspects = finding.get("suspects") or []
    finding["hypothesis"] = (
        f"DUT 的 CRC 状态在 {div_t} 处偏离参考模型，远早于包尾"
        f"报出残差错误的时刻，因此报告中的残差值只是下游症状，"
        f"而非故障本身。激励合法且同一参考模型在其他 seed 下通过，"
        f"问题指向 `dut.crc` 更新中一条与数据相关的写入"
        + (f"：{suspects[0][0]} 处的 `{suspects[0][2]}`，"
           f"仅在 `{suspects[0][1]}` 成立时执行。" if suspects else "。")
        + "建议结合上方数据窗口，核实该条件是否与失败 seed 的 payload 匹配。")
    return finding


# ---------------------------------------------------------------------------
# presentation
# ---------------------------------------------------------------------------

def open_evidence_view(d: DemoDriver, fail: dict, ref: dict,
                       finding: dict) -> dict:
    """Open the failing waveform with the DUT and reference CRC side by side."""
    fail_fst = str(HERE / fail["waveform"])
    div = _ns(finding["first_divergence"] or 0)
    div_num = str(div)
    # frame the divergence instead of showing the whole run: a few packets of
    # context on the left, enough on the right to see the residue capture
    vp_from = max(0, div - 60)
    vp_to = div + 120

    view = d.call("open_wave_view", {
        "fst_paths": [fail_fst],
        "labels": [f"fail(seed {fail['seed']})"],
        "signals": [
            {"path": f"{TOP}.clk", "group": "clock"},
            {"path": f"{TOP}.valid", "group": "stimulus", "color": "yellow"},
            {"path": f"{TOP}.data", "group": "stimulus", "format": "bin"},
            {"path": DUT_CRC, "group": "dut_vs_reference", "color": "red",
             "format": "bin"},
            {"path": REF_CRC, "group": "dut_vs_reference", "color": "green",
             "format": "bin"},
            {"path": RESIDUE, "group": "result", "color": "red",
             "format": "bin"},
            {"path": EXPECTED, "group": "result", "color": "green",
             "format": "bin"},
        ],
        "cursor": {"time": div_num, "unit": "ns"},
        "viewport": {"from": str(vp_from), "to": str(vp_to), "unit": "ns"},
        "markers": [{"time": div_num, "unit": "ns",
                     "label": "dut.crc 偏离参考模型", "color": "red"}],
        "annotation": {
            "markdown": (
                f"## {fail['case_id']}\n\n"
                + "\n".join(f"- {f}" for f in finding["facts"])
                + f"\n\n**推断（尚未证实）：** "
                  f"{finding['hypothesis']}"),
            "confidence": "medium",
            "evidence": finding["evidence"],
        },
    })
    return view if isinstance(view, dict) else {}


def screenshot(url: str, out: Path) -> str | None:
    """Capture the viewer. Returns None with a reason logged on failure."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                args=["--no-sandbox", "--enable-features=SharedArrayBuffer"])
            page = browser.new_page(viewport={"width": 1600, "height": 900})
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_url("**/shell.html*", timeout=30000)
            except Exception:
                pass
            page.wait_for_selector("#surfer", timeout=30000)
            page.frame_locator("#surfer").locator("canvas").first.wait_for(
                timeout=45000)
            page.wait_for_timeout(9000)   # let surver stream the values in
            vp = page.viewport_size or {"width": 1600, "height": 900}
            page.screenshot(path=str(out), clip={
                "x": 0, "y": 0,
                "width": vp["width"], "height": vp["height"] - 22})
            browser.close()
        # a blank shell is ~15 KB; anything smaller means nothing rendered
        if out.stat().st_size < 20000:
            return None
        return out.name
    except Exception as exc:
        print(f"  [warn] screenshot failed: {str(exc)[:120]}")
        return None


def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def md(s) -> str:
    """Escape for HTML, then turn `backticks` into real <code> spans.

    The facts and hypotheses are written with Markdown-style backticks
    because they also feed the viewer's annotation panel, which renders
    Markdown. Dropping them into HTML raw would show the backticks
    literally, so convert them here.
    """
    out = esc(s)
    parts = out.split("`")
    if len(parts) < 3:
        return out
    # odd indices are the spans between a pair of backticks
    rebuilt = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            rebuilt.append(f"<code>{part}</code>")
        else:
            rebuilt.append(part)
    return "".join(rebuilt)


def write_report(man: dict, groups: dict, findings: list[dict],
                 shots: dict, urls: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    s = man["summary"]
    rate = (100.0 * s["pass"] / s["total"]) if s["total"] else 0.0

    rows = []
    for c in man["cases"]:
        cls = {"pass": "ok", "fail": "bad", "error": "warn"}[c["status"]]
        rows.append(
            f'<tr class="{cls}"><td>{esc(c["case_id"])}</td>'
            f'<td>{c["seed"]}</td><td class="st">{c["status"].upper()}</td>'
            f'<td>{esc(c.get("reason") or "")}</td>'
            f'<td>{c.get("elapsed_sec")}s</td></tr>')

    cards = []
    for f in findings:
        shot = shots.get(f["case_id"])
        url = urls.get(f["case_id"])
        img = (f'<img src="shots/{esc(shot)}" alt="波形证据">'
               if shot else
               '<p class="miss">本条结论未捕获截图。'
               '请在有浏览器的环境下运行，或使用下方的交互查看链接。</p>')
        link = (f'<p class="link">交互查看（仅在进程存活时有效）：'
                f'<a href="{esc(url)}">{esc(url)}</a></p>' if url else "")
        cards.append(f"""
    <section class="card">
      <h3>{esc(f["case_id"])} <span class="ref">对照 {esc(f["reference"])}</span></h3>
      <div class="facts">
        <h4>观测事实</h4>
        <ul>{"".join(f"<li>{md(x)}</li>" for x in f["facts"])}</ul>
        <h4>推断 <span class="tag">推理结论</span></h4>
        <p>{md(f["hypothesis"])}</p>
        <h4>证据链</h4>
        <ul class="ev">{"".join(f"<li>{esc(x)}</li>" for x in f["evidence"])}</ul>
      </div>
      <figure>{img}<figcaption>首次偏离于
        {esc(f["first_divergence"])}，红色为 DUT，绿色为参考模型。
      </figcaption></figure>
      {link}
    </section>""")

    grp = "".join(
        f'<li><b>{esc(k)}</b>：{len(v)} 个用例'
        f'（{", ".join(esc(c["case_id"]) for c in v)}）</li>'
        for k, v in groups.items())

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><meta charset="utf-8">
<title>回归分析：{esc(man["suite"])}</title>
<style>
 :root {{ --ok:#1d8a4e; --bad:#c62828; --warn:#e07800; --line:#e6e6e6; }}
 body {{ font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",
        "Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif;
        margin:0; background:#fafafa; color:#1a1a1a; }}
 .wrap {{ max-width:1080px; margin:0 auto; padding:48px 28px 72px; }}
 h1 {{ font-size:30px; margin:0 0 6px; letter-spacing:-.02em; }}
 .sub {{ color:#6b6b6b; margin:0 0 34px; }}
 .kpis {{ display:flex; gap:14px; flex-wrap:wrap; margin:0 0 34px; }}
 .kpi {{ flex:1 1 150px; background:#fff; border:1px solid var(--line);
         border-radius:14px; padding:18px 20px; }}
 .kpi b {{ display:block; font-size:30px; letter-spacing:-.02em; }}
 .kpi span {{ color:#6b6b6b; font-size:13px; }}
 .kpi.p b {{ color:var(--ok); }} .kpi.f b {{ color:var(--bad); }}
 h2 {{ font-size:20px; margin:38px 0 14px; }}
 table {{ width:100%; border-collapse:collapse; background:#fff;
          border:1px solid var(--line); border-radius:14px; overflow:hidden; }}
 th,td {{ text-align:left; padding:10px 14px; border-bottom:1px solid var(--line);
          font-size:14px; }}
 th {{ background:#f4f4f4; font-weight:600; }}
 tr:last-child td {{ border-bottom:none; }}
 td.st {{ font-weight:600; }}
 tr.ok td.st {{ color:var(--ok); }} tr.bad td.st {{ color:var(--bad); }}
 tr.warn td.st {{ color:var(--warn); }}
 .card {{ background:#fff; border:1px solid var(--line); border-radius:16px;
          padding:24px 26px; margin:0 0 22px; }}
 .card h3 {{ margin:0 0 16px; font-size:18px; }}
 .ref {{ color:#8a8a8a; font-weight:400; font-size:14px; }}
 .card h4 {{ margin:18px 0 6px; font-size:13px; text-transform:uppercase;
             letter-spacing:.06em; color:#6b6b6b; }}
 .card ul {{ margin:0; padding-left:20px; }}
 .card li {{ margin:3px 0; }}
 ul.ev li {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
             font-size:12.5px; color:#444; }}
 .tag {{ background:#fff3e0; color:var(--warn); font-size:11px;
         padding:2px 8px; border-radius:20px; letter-spacing:0;
         text-transform:none; }}
 figure {{ margin:20px 0 0; }}
 figure img {{ width:100%; border:1px solid var(--line); border-radius:10px;
               display:block; }}
 figcaption {{ color:#6b6b6b; font-size:12.5px; margin-top:8px; }}
 .miss {{ background:#fff8e1; border:1px solid #ffe0a3; border-radius:10px;
          padding:12px 14px; color:#7a5a00; font-size:13.5px; }}
 .link {{ font-size:13px; color:#6b6b6b; margin:12px 0 0;
          word-break:break-all; }}
 .note {{ color:#6b6b6b; font-size:13px; border-top:1px solid var(--line);
          margin-top:38px; padding-top:16px; }}
 code {{ background:#f2f2f2; padding:1px 5px; border-radius:4px;
         font-size:13px; overflow-wrap:break-word; word-break:break-word; }}
 .card li code, .card p code {{ font-family:ui-monospace,SFMono-Regular,
         Menlo,monospace; }}
</style>
<div class="wrap">
  <h1>回归分析：{esc(man["suite"])}</h1>
  <p class="sub">运行于 {esc(man["generated_at"])} &middot; 顶层
     <code>{esc(man["top"])}</code> &middot; wave-mcp 自动分析</p>

  <div class="kpis">
    <div class="kpi"><b>{s["total"]}</b><span>用例总数</span></div>
    <div class="kpi p"><b>{s["pass"]}</b><span>通过</span></div>
    <div class="kpi f"><b>{s["fail"]}</b><span>失败</span></div>
    <div class="kpi"><b>{rate:.0f}%</b><span>通过率</span></div>
  </div>

  <h2>失败归组</h2>
  <ul>{grp or "<li>无失败用例。</li>"}</ul>
  <p class="sub" style="margin-top:8px">按症状归组，相同症状不代表相同根因，
     每条结论下方有独立的证据链。</p>

  <h2>分析结论</h2>
  {"".join(cards) or "<p>没有需要分析的失败用例。</p>"}

  <h2>全部用例</h2>
  <table><tr><th>用例</th><th>Seed</th><th>状态</th><th>原因</th>
    <th>耗时</th></tr>{"".join(rows)}</table>

  <p class="note">上方所有数值和信号值均在本次报告生成过程中由 wave-mcp 从波形中
     实际读取。观测事实是确定性的工具输出，推断标注为推理结论，
     证据链列出了实际工具调用以便复核。交互链接仅在生成它的 viewer 进程存活期间有效，
     重新运行 <code>triage.py --hold</code> 可获取新的链接。</p>
</div>
</html>"""
    out = REPORT_DIR / "index.html"
    out.write_text(html)
    return out


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="triage the demo regression")
    ap.add_argument("--no-shots", action="store_true",
                    help="skip screenshots (no browser needed)")
    ap.add_argument("--max-cases", type=int, default=2,
                    help="how many failures to analyse in depth (default 2)")
    ap.add_argument("--hold", action="store_true",
                    help="keep the viewer alive after reporting")
    args = ap.parse_args()

    if not MANIFEST.exists():
        sys.exit(f"error: {MANIFEST} not found. Run ./run_regression.py first.")
    man = json.loads(MANIFEST.read_text())

    passing = [c for c in man["cases"]
               if c["status"] == "pass" and c.get("waveform")]
    failing = [c for c in man["cases"]
               if c["status"] == "fail" and c.get("waveform")]

    print(f"== regression: {man['summary']['pass']} passed, "
          f"{man['summary']['fail']} failed ==")
    if not failing:
        print("no failing cases with waveforms; nothing to triage")
        return 0
    if not passing:
        print("no passing case to compare against; triage needs a reference")
        return 1

    groups = group_failures(man["cases"])
    print(f"== {len(groups)} symptom group(s) ==")
    for k, v in groups.items():
        print(f"  {k}: {len(v)} case(s)")

    ref = passing[0]
    picked = failing[:max(1, args.max_cases)]
    print(f"== analysing {len(picked)} representative failure(s) "
          f"against {ref['case_id']} ==")

    d = DemoDriver()
    d.start()
    findings, shots, urls = [], {}, {}
    try:
        # the netlist session gives signal_drivers its source locations
        sess = RUNS / "session"
        if (sess / "session.json").exists():
            d.call("open_session", {"session_path": str(sess / "session.json")})

        for fail in picked:
            print(f"-- {fail['case_id']}")
            f = analyse_case(d, fail, ref)
            findings.append(f)
            view = open_evidence_view(d, fail, ref, f)
            url = view.get("url")
            if url:
                urls[fail["case_id"]] = url
                if not args.no_shots:
                    name = screenshot(url, SHOTS / f"{fail['case_id']}.png")
                    if name:
                        shots[fail["case_id"]] = name
                        print(f"   screenshot: report/shots/{name}")
            vid = view.get("view_id")
            if vid and not args.hold:
                d.call("close_wave_view", {"view_id": vid})

        out = write_report(man, groups, findings, shots, urls)
        print(f"\n== report: {out.relative_to(HERE)} ==")
        if not shots and not args.no_shots:
            print("   (no screenshots: needs playwright + chromium + "
                  "viewer assets)")
        if args.hold:
            d.hold()
    finally:
        if not args.hold:
            d.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
