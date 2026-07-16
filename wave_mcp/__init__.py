"""wave_mcp — open-source MCP server for xrun (Xcelium) waveform debug.

A license-free alternative to Cadence Indago / Verisium Debug MCP.

Data sources aggregated by the server:
  * FST waveform (pylibfst) -> hierarchy, signals, values
  * xrun.log parser         -> errors / warnings / messages
  * RTL static refinement   -> connectivity / driver / trace (pyslang netlist)
  * coverage / assertion    -> urg report + CSV
"""

__version__ = "0.1.0"
