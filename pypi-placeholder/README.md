# wave-mcp

**This is a name-reservation placeholder (v0.0.1). The full release is coming soon.**

`wave-mcp` is an open-source, **license-free** MCP server for RTL waveform debug,
backed entirely by open-source data sources — **FST waveforms + a pyslang RTL
netlist**. It gives an LLM a set of waveform-debug tools (design hierarchy,
signals, signal values, connectivity/drivers, and cross-module value tracing)
with **no commercial license required and unlimited concurrency**.

It does **not** run a simulator — you produce the waveform with your own flow
(Verilator `--trace-fst`, or convert a VCD to FST) and hand the result to it.

- Source & docs: https://github.com/Tencent/wave-mcp
- License: MIT

> `pip install wave-mcp` will install the real package once the first functional
> release (`0.1.0`) is published. This `0.0.1` release only reserves the name.
