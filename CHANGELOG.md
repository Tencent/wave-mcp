# Changelog

All notable changes to wave-mcp are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.3] - 2026-09-05

### Fixed

- **Signals declared on the DUT top level reported `unresolved_path`.** Scope
  resolution tried three levels: an exact netlist key, a leaf-name match, then
  the FST `component` field. The last level was dead code, because that field is
  empty for every waveform we produce, both Verilator's native FST and
  VCD-derived ones, so it never resolved anything. Historical sessions never
  noticed, since their netlist was elaborated from the testbench top and its
  root key matched the FST root, which let level one absorb every lookup. A
  DUT-rooted netlist breaks that: the root key is the DUT (`decode`) while the
  FST root is the testbench (`top_tb.U_DECODE`), so levels one and two both miss
  and the root falls through to the branch that never works. Level three now
  also accepts `definition_name`, which the anchor pass derives from the
  netlist, so the root resolves and its direct signals become traceable.
- **Cross-hierarchy tracing only worked in one direction.** `loads()` already
  descended into sub-modules; `drivers()` did not, so the same wire returned a
  connection from `signal_connectivity` and `undriven_signal` from
  `signal_drivers`. Both directions now walk peers, filtered by port direction:
  upstream takes same-level fan-in plus sub-module **output** ports, downstream
  takes same-level loads plus sub-module **input** ports. Direction filtering is
  what keeps a source like a top-level reset from being reported as driven by
  the flops it feeds. Resolved results say which hop they came from.
- **Source paths from the netlist could not be opened.** `modules_in_file`
  always returned 0 and `signal_drivers` handed back paths such as
  `examples/sample/counter.sv` that resolve against nothing, because paths were
  stored relative to the elaboration cwd but looked up against the cwd at query
  time. Netlist builds now record the cwd they ran from and prefer absolute
  paths; every remaining relative path is rewritten once when the netlist loads,
  resolved against the build root, the netlist directory and its ancestors. One
  normalisation point, because these paths reach callers through drivers, loads,
  fan-in, trace results and declarations alike.
- **Filelists ignored environment variables.** `-F $PROJ_FE/rtl/foo.f` was taken
  literally, so an entire file group silently dropped out of elaboration as
  missing files. Tokens now pass through `os.path.expandvars`, and an undefined
  variable is left untouched so it still fails as a missing file rather than
  turning into a path that exists by accident.
- **A misspelled time unit silently moved the cursor.** `--cursor 1000nanoseconds`
  was accepted and landed at an arbitrary time with no warning, because the
  converter fell back to emitting the bare number when unit parsing failed,
  which means something entirely different from converting it. Unknown units are
  now rejected at the CLI and dropped from the generated command batch, and
  remaining markers are renumbered so one bad entry cannot shift the rest onto
  the wrong ids. Accepted units come from a single list in `timeutil`.
- **The viewer stayed blank in IDE-embedded browsers.** The page receives the
  backend token in the URL query string, and some embedded browsers drop the
  query on navigation, leaving the shell requesting a URL that 404s and a canvas
  that never paints. The token is now served alongside the view state and the
  page falls back to it, so the bare URL works. Each server still serves one
  view on localhost only.
- **Views intermittently failed to start.** Opening a view takes two ports, one
  for the shell and one for surver, and both were picked by binding a socket,
  reading the port, then closing it and binding again later. In between, the
  port belongs to nobody and any other process on the host can take it, so the
  second bind fails with the port already in use. It only reproduced under
  load, such as a full regression run starting many viewers in quick
  succession, where it surfaced as a random "surver failed to start". The shell
  server now receives an already-listening socket, so picking and binding are
  one operation. surver is a separate binary that only gets a port number, so
  it cannot inherit a socket and instead retires the port and retries on
  another one when the child fails to come up.

### Added

- `tests/unit/test_dut_root.py`, pinning the DUT-rooted netlist case above on a
  synthetic waveform written by pylibfst, so it needs no simulator and cannot be
  masked by a stale checked-in file. It asserts the conditions that make the bug
  reachable rather than only the outcome, so a later change cannot quietly turn
  it into a test of the leaf-name match. Reverting the fix fails 5 of its 8
  assertions.

## [0.2.2] - 2026-09-05

### Added

- **`WAVE_MCP_SESSION_ROOT` confines where session directories land.** `out_dir`
  is chosen by the calling model, so a drifted prompt could scatter sessions
  across `/tmp`, the cwd, or a shared regression directory, where two users with
  different filelists collide on one directory and silently inherit each other's
  netlist. Set this variable and every `out_dir` resolves inside it: a bare name
  or relative path lands in the root, a path already inside it is kept, and one
  pointing elsewhere is remapped in by basename. Unset, behaviour is unchanged.
  The reply's `session_path` is always the real location.
- **Environment variables documented in one table.** Both READMEs now carry the
  full set (session root, Verdi/FSDB, vcd2fst, viewer, cache) and state that
  these belong in the `env` block of the MCP client config, since an
  agent-spawned server does not inherit an interactive shell's exports.
  `VERDI_HOME` is now explicit about pointing at the install root rather than a
  subdirectory.

### Fixed

- **Viewer backends outlived the process that started them.** `SurverManager`
  relied solely on an `atexit` hook, which Python skips on `SIGTERM`, so
  killing a `wave-view` CLI or an MCP server left one `surver` per open view
  running indefinitely, holding memory and a listening port. Found in the field
  with four backends still alive 23 hours after their servers were abandoned.
  Both entry points now install `SIGTERM` / `SIGINT` / `SIGHUP` handlers that
  close views explicitly, and `surver` children additionally set
  `PR_SET_PDEATHSIG` so the kernel reaps them even when the parent is
  `SIGKILL`ed or crashes, which no in-process handler can cover. The
  server-side cleanup inspects `sys.modules` instead of importing the viewer,
  so installs without the optional assets are unaffected.

### Changed

- **FSDB converter attribution made accurate.** `fsdb2fst` is the newest part of
  wave-mcp and the only place where prior public work was consulted rather than
  starting from scratch: `ParseScaleFs` (FSDB scale string to femtoseconds per
  tick) keeps the error contract and unit table of the public TraceWeave
  implementation, and the offline `ffrAPI` stub mirrors the subset of ffrAPI it
  exercises. Comments naming the project came in with the converter on
  2026-08-31 and were dropped on 2026-09-01 during a broader cleanup of vendor
  references, which left the file described as `original code`. That
  description was inaccurate. `docs/THIRD_PARTY.md` and the headers of both
  source files now carry the project name, author, MIT license, link, and our
  thanks.

## [0.2.1] - 2026-09-04

### Added

- **`WAVE_MCP_SESSION_ROOT` pins where sessions land.** `out_dir` is chosen by
  the calling model, so a drifted prompt could scatter sessions across `/tmp`,
  the cwd, or a shared regression directory, where two users with different
  filelists can collide on one directory and silently inherit each other's
  netlist. Set this variable and every `out_dir` resolves inside it: a bare name
  or relative path lands in the root, a path already inside it is kept, and a
  path pointing elsewhere is remapped in by basename. Unset, behaviour is
  unchanged. The reply's `session_path` is always the real location.
- **Environment variables are documented in one place.** Both READMEs now carry
  the full table (session root, Verdi/FSDB, vcd2fst, viewer, cache) and state
  that these belong in the `env` block of the MCP client config, since an
  agent-spawned server does not inherit an interactive shell's exports.

### Fixed

- **Viewer backends outlived the process that started them.** `SurverManager`
  relied solely on an `atexit` hook, which Python skips on `SIGTERM`, so
  `kill`-ing a `wave-view` CLI or an MCP server left one `surver` per open view
  running indefinitely, holding memory and a listening port. Found in the field
  with four backends still alive 23 hours after their servers were abandoned.
  Both entry points now install `SIGTERM` / `SIGINT` / `SIGHUP` handlers that
  close views explicitly, and `surver` children additionally set
  `PR_SET_PDEATHSIG` so the kernel reaps them even when the parent is
  `SIGKILL`ed or crashes, which no in-process handler can cover. The server-side
  cleanup inspects `sys.modules` instead of importing the viewer, so installs
  without the optional assets are unaffected.
- **Air-gapped launcher could not find its own interpreter.** `install.sh` took
  `--prefix` verbatim, so a relative value baked a relative `RUNTIME` into the
  generated `bin/wave-mcp`. An MCP client spawns that launcher with the user's
  project directory as cwd, not the install directory, so the interpreter path
  resolved to nothing and the client reported only a bare `-32000`. The prefix
  is now absolutized (and probed for write permission) before anything is
  installed, the launcher anchors a relative `RUNTIME` on its bundle, and it
  prints the missing path, the bundle, and the cwd instead of dying silently.
  Reported from an on-site air-gapped deployment.
- **Install-time check now covers the launcher.** The sanity check ran the venv
  interpreter directly, which bypassed the generated launcher entirely, so any
  cwd-dependent path in it survived install and only surfaced in the client. It
  now also executes `bin/wave-mcp` from an unrelated cwd, reproducing how a
  client starts it.
- **`WAVE_MCP_VIEWER_ASSETS` silently ignored when relative.** A relative value
  resolved against whatever cwd the client happened to use and then degraded to
  "viewer unavailable" with a hint telling the user to set the variable they had
  already set. Relative values now resolve against `$HOME`, and the hint names
  the real cause (path missing, or `surver` / `wasm/index.html` absent).
- **Build scripts now absolutize `--out`.** `build_offline_bundle.sh` used
  `dirname "$OUT"` for the tarball step, and the two Docker-based builders pass
  `$OUT` as a `-v` mount source, where a relative path is rejected outright.
- **`open_session` description no longer mentions a sim log**, which it stopped
  loading; the tool description an agent sees now matches what it does.

## [0.2.0] - 2026-09-02

Three new capabilities on top of the 0.1.x tool set: a browser wave viewer the
agent can drive, pass/fail waveform diffing, and an FSDB input path. Tool count
goes from 27 to 34.

### Added

- **Browser wave viewer.** `open_wave_view` renders a session in a real
  waveform GUI (Surfer compiled to WASM, streamed by surver), so an agent can
  show you what it found instead of describing it. `update_wave_view` mutates a
  live view (signals, groups, colors, radix, cursor, markers, viewport,
  annotations) and `get_view_state` reads back what the user has since changed
  by hand, which makes the loop two-way. Install with the `viewer` extra
  (`pip install "wave-mcp[viewer]"`). Guides:
  [docs/WAVE_VIEWER.md](docs/WAVE_VIEWER.md),
  [docs/VIEWER_SCREENSHOTS.md](docs/VIEWER_SCREENSHOTS.md).
- **View lifecycle management.** `list_wave_views` reports the open views
  (id, url, title, waveform paths, revision, backend liveness) and
  `close_wave_view` closes one or all of them, releasing the per-view HTTP
  server. The streaming backend is shared per waveform file set and refcounted,
  so closing one view never cuts off another still reading the same waveform.
  A cap of 8 concurrent views (`WAVE_MCP_MAX_VIEWS`, 0 disables) evicts the
  oldest view so long batch runs cannot pile up views and processes.
- **Pinnable viewer ports.** Views use random high ports by default; setting
  `WAVE_MCP_VIEWER_PORT_BASE` confines them to a 64-port window so a single
  `ssh -L` rule keeps working across views, and several people on one host can
  each take their own window. Allocation falls back to an ephemeral port when
  the window is full.
- **`wave-view` CLI** for opening a waveform in the viewer without an MCP
  client, including `--signals` and remote-friendly port printing.
- **`diff_waveforms`** locates the first divergence between two runs of the
  same design (pass vs fail), reporting per-signal first-difference times and
  coverage of the compared signal set.
- **FSDB input.** `prepare_session` now accepts `.fsdb` directly and converts
  it via the bundled `fsdb2fst`, with `fsdb_scopes` / `fsdb_signals_file` for
  slicing large dumps. `convert_fsdb_to_fst` exposes the converter as its own
  tool, including `info_only` for a fast summary of a huge file before
  committing to a full conversion. See [docs/FSDB_GUIDE.md](docs/FSDB_GUIDE.md).
- **Conversion cache.** Both VCD and FSDB conversions now write the `.fst`
  next to the source waveform and reuse it across sessions. The cache key
  covers the source identity plus the slicing options, so changing the scope
  selection produces a fresh conversion instead of silently reusing a partial
  waveform.
- **Xcelium direct FST output** documented end to end, with the `fstdumper`
  VPI patches required to build it:
  [docs/XCELIUM_FST_GUIDE.md](docs/XCELIUM_FST_GUIDE.md).
- **Simulator compatibility matrix** covering the four ways to get a waveform
  in (FST direct, VCD auto-convert, FSDB conversion, Xcelium direct):
  [docs/SIMULATOR_COMPATIBILITY.md](docs/SIMULATOR_COMPATIBILITY.md).
- **Viewer demos.** Four runnable debug scenarios (X propagation, FSM
  deadlock, CDC pulse loss, pass/fail CRC divergence) under
  [examples/viewer_demos](examples/viewer_demos), plus a screenshot capture
  script.
- **Offline deployment**: one-command Docker pipeline for the air-gapped
  bundle matrix (glibc 2.17 / 2.28), viewer assets packaging, and a vendored
  license directory with a generated crate license report.

### Fixed

- Viewer served its own demo landing page for `/index.html`, shadowing the
  Surfer WASM entry point that the shell loads in an iframe. The viewer would
  come up with an empty waveform pane. Static resolution now always resolves
  the WASM entry from the assets directory.
- `update_wave_view` emitted marker commands with the arguments transposed, so
  markers landed at the wrong time.
- Closing a viewer HTTP server called `shutdown()` without `server_close()`,
  leaking the listening socket and its file descriptor.
- Viewer service worker returned `undefined` on a failed range request instead
  of an error response, stalling waveform streaming.
- Slang lint diagnostics were misclassified as errors, and interface scope
  names failed to resolve during elaboration.
- FST source: definition-name handling for RTL module types, plus scope
  filtering fixes for interface and generate blocks.

### Changed

- Session paths in the shipped demos are stored relative to the session
  directory, so a cloned repository runs the demos without rewriting paths.
- Documentation drops internal test logs, decision history and competitor
  comparisons in favour of support status and known limitations.
- Python interpreter detection in the deploy scripts now gates on the CPython
  version rather than guessing from the binary name.

## [0.1.1] - 2026-08-26

### Added

- `wave-mcp query` CLI subcommand exposing all 27 tools from the shell.
- Validation overview charts in the README.

## [0.1.0] - 2026-08-20

Initial public release: 27 MCP tools for RTL waveform debug over FST plus
SystemVerilog static analysis (pyslang elaboration), covering hierarchy
browsing, signal values, driver/load tracing, X-cause tracing and file-level
queries.

[0.2.2]: https://github.com/Tencent/wave-mcp/releases/tag/v0.2.2
[0.2.1]: https://github.com/Tencent/wave-mcp/releases/tag/v0.2.1
[0.2.0]: https://github.com/Tencent/wave-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/Tencent/wave-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/Tencent/wave-mcp/releases/tag/v0.1.0
