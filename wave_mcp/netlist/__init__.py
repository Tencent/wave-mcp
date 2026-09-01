"""Static RTL netlist extraction + trace engine.

Built from a pyslang-elaborated design (no Surelog/UHDM, no Verible), persisted
as JSON maps:
  * DriverMap : signal -> [driver records {kind, rhs, control, file, line, snippet}]
  * FanInMap  : signal -> signals that can affect it
  * LoadMap   : signal -> signals it affects (fan-out)
  * LocMap    : signal -> declaration {file, line}
  * instance_tree : hierarchical path -> module definition

The trace engine combines these static maps with FST values to determine the
active driver at a time and to reverse-traverse value / X propagation.
"""
from .slang_netlist import build_netlist, NetlistError  # noqa: F401
from .trace_engine import TraceEngine  # noqa: F401
