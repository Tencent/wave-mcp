# Viewer Debug Scenario Demos

Four worked examples that show how an agent combines wave-mcp's analysis
tools with the viewer to debug real RTL problems. Each demo ships the
buggy RTL, a reproducible build, and a driver script that plays out the
full debug conversation: the agent detects the bug from the waveform,
tracks down the root cause, then presents the finding in the browser
viewer with cursor, markers, zoom and evidence-backed annotations.

## Directory layout

```
rtl/          buggy RTL sources, one scenario per directory
  xprop/        data_out carries X on every packet
  fsm_stuck/    read FSM deadlocks in WAIT_ACK
  cdc/          clock-domain pulse crossings silently lost
  crc_diff/     same RTL, same stimulus, one CRC tap typo -> pass/fail pair
*.f           filelists used by the build
make_all.sh   builds every scenario: iverilog -> vcd2fst -> build_session
common.py     DemoDriver: a minimal MCP stdio client for the demos
demo1_xprop.py      ... demo4_crc_diff.py
```

## Build

Prerequisites on PATH: `iverilog`, `vcd2fst`, and wave-mcp on PYTHONPATH
(`pip install -e .` in the repo root works). Then:

```
./make_all.sh
```

Waves land in `waves/*.fst` and prebuilt sessions in
`waves/session_<name>/`. Every driver script re-opens its session and
launches `python -m wave_mcp.server` over stdio itself; nothing else is
needed:

```
python3 demo1_xprop.py
python3 demo2_fsm_stuck.py
python3 demo3_cdc.py
python3 demo4_crc_diff.py
```

Each script prints the viewer URL. Open it (or `ssh -L` forward, as
printed) to watch the presented state; on a headless box you can still
follow along in the script output, which narrates every tool call.

## How to actually LOOK at a demo

The viewer lives inside the MCP server process that the demo script
spawns. The moment the script exits, its viewer URL dies with it, so
use one of these:

1. **Hold the view open while the demo runs.** Add `--hold` (or set
   `DEMO_HOLD=1`): after the scripted flow finishes, the script keeps
   the server up, prints `viewer stays up`, and waits for Enter, so you
   can open the URL, zoom, toggle signals, and read the annotation at
   your own pace.

   ```
   python3 demo1_xprop.py --hold
   # ... follow along in the script, then press Enter in the terminal
   #     to close the viewer and exit
   ```

2. **Reopen the demo waveform later with wave-view.** The built
   waveforms stay in `waves/`, so at any time:

   ```
   wave-view examples/viewer_demos/waves/cdc.fst \
       --signals cdc_tb.clk_fast cdc_tb.clk_slow \
                 cdc_tb.dut.pulse_fast cdc_tb.pulse_seen \
       --cursor 830s
   ```

   (use `python3 -m wave_mcp.viewer.cli` if `wave-view` is not on
   PATH). This gives you the plain viewer without the scripted
   markers/annotations; wave-view keeps running until Ctrl-C.

3. **Watch pass/fail side by side:**

   ```
   wave-view examples/viewer_demos/waves/crc_pass.fst \
             examples/viewer_demos/waves/crc_fail.fst \
       --labels pass fail
   ```

`--hold` is the way to see exactly what the agent presented (markers,
cursor, annotations, diff view); `wave-view` is the way to browse the
same waveforms freely afterwards.

## The scenarios

### Demo 1, X-propagation (`demo1_xprop.py`)

A byte counter is never reset, stays X forever (`X+1=X`), and is embedded
into `data_out[7:5]`, so every packet leaves the output corrupted while
the FSM runs perfectly. Classic "why is my output X" debug.

Agent flow:

1. `open_session`, `signal_values(data_out)` finds X on every packet
2. `trace_x(data_out, first X time)` backtracks the X through the RTL to
   `byte_cnt`; `signal_drivers(byte_cnt)` shows it has no reset driver
3. `open_wave_view` presents: clock/reset/control/datapath groups,
   hex bus formatting, cursor parked on the first X, a red marker there,
   and an annotation carrying the fix plus evidence from steps 1 and 2
4. `update_wave_view` zooms to the corrupted packet for a closer look
5. `get_view_state` reads back what the user currently sees

### Demo 2, FSM deadlock (`demo2_fsm_stuck.py`)

The read FSM's `WAIT_ACK` state has no timeout. The fourth transaction's
`req` rises, `ack` never comes, `rd_count` freezes at 3 forever. Classic
"bus hangs" debug.

Agent flow:

1. `signal_values(rd_count)` spots the flatline
2. `signal_values(req)` plus `signal_value_at(ack, stuck time)` shows
   req=1 with no ack: the handshake stalled
3. `signal_value_at(state)` + `signal_drivers(state)` pinpoints the FSM
   sitting in WAIT_ACK
4. `open_wave_view` with a handshake signal group, green marker on the
   last completed transaction, red marker where req rises forever,
   annotation concluding "add a WAIT_ACK timeout"
5. `update_wave_view` zooms to the hang; `get_view_state` reads the
   actual viewport back

### Demo 3, CDC pulse loss (`demo3_cdc.py`)

Six pulses cross from a 50 MHz domain into a 20 MHz domain with no
synchronizer. Two arrive, four silently vanish. Classic silent data-loss
debug that fails no assertion.

Agent flow:

1. `signal_values` on both sides of the crossing: 6 sent, 2 seen, and
   the counter register in the slow domain agrees (2)
2. `signal_connectivity(pulse_seen)` proves the slow-domain flop is
   clocked by clk_slow but driven straight from the fast domain: no
   synchronizer stage
3. `open_wave_view` shows BOTH clocks side by side, a green marker on a
   captured pulse and a red one on a missed pulse, and an annotation
   recommending a 2-FF synchronizer
4. `update_wave_view` walks the cursor to the missed pulse and zooms in:
   pulse_fast goes high and falls entirely between two clk_slow edges
5. `get_view_state` confirms the cursor position

### Demo 4, pass/fail diff (`demo4_crc_diff.py`)

Two builds of the same RTL with identical stimulus; the fail build drops
one CRC tap (`^ data[0]`). The agent locates the first cycle where the
two runs diverge and backtracks it to the tap bug. Classic regression
triage debug.

Agent flow:

1. `diff_waveforms(pass.fst, fail.fst, clock-aligned, after reset)`:
   first divergence at 35s on `dut.crc` (0110 vs 0111), 22 signals
   compared, 3 diverging
2. `signal_fanin(crc)` shows the combinational cone: data + feedback
   taps, pointing at the missing tap
3. `open_wave_view` with BOTH waveforms (`labels: [pass, fail]`,
   per-signal `source: a/b`), a `diff` block that highlights the first
   divergence, cursor parked on it, and an annotation with the tap-bug
   conclusion
4. `update_wave_view` adds a second marker where `crc_err` asserts (fail
   build only, 845s); `get_view_state` confirms both markers

## Tool coverage across the demos

Analysis tools: `open_session`, `signal_values`, `signal_value_at`,
`signal_connectivity`, `signal_drivers`, `signal_fanin`, `trace_x`,
`diff_waveforms`. Viewer tools: `open_wave_view`, `update_wave_view`,
`get_view_state`. That is every major workflow an agent needs for
waveform debugging: detect, localize, prove, present, and hand the
finding to the user.

## Writing your own demo

Follow the shape of the bundled ones:

1. Build a session with `build_session --fst ... --filelist ... --top
   ... --out ...`, or point `open_session` at an existing one.
2. Drive detection with the analysis tools, and capture structured
   results with `d.last_structured()`.
3. Present with `open_wave_view`: group signals, color the ones that
   matter, park the cursor on the finding, drop markers, and attach an
   annotation whose `evidence` cites the tool results that prove it.
4. Refine with `update_wave_view` (zoom, extra markers, follow-up
   annotations) as the conversation deepens.
5. Close the loop with `get_view_state`: it is how the agent stays aware
   of what the user is actually looking at.
