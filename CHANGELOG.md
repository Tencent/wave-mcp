# Changelog

All notable changes to wave-mcp are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.0]: https://github.com/Tencent/wave-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/Tencent/wave-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/Tencent/wave-mcp/releases/tag/v0.1.0
