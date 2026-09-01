"""wave_mcp — open-source MCP server for RTL waveform debug.

A license-free alternative to Cadence Indago / Verisium Debug MCP.

Data sources aggregated by the server:
  * FST waveform (pylibfst) -> hierarchy, signals, values
  * RTL static analysis    -> connectivity / driver / trace (pyslang netlist)
"""

__version__ = "0.2.0"
