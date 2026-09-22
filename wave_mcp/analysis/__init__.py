"""Waveform analysis primitives built on the FST data source.

These modules answer *what the dump contains*: activity statistics, time-window
pattern search, value reads, clock-edge sampling and transaction folding. They
never judge why a design failed and never suggest a fix: root-causing belongs to
the agent that calls them.
"""
from .activity import signal_activity
from .clocking import clock_edges, sample_at, sample_table
from .fsm import fsm_transitions
from .predicate import find_time_windows
from .transactions import fold_transactions
from .values import read_point, read_values

__all__ = ["signal_activity", "find_time_windows", "read_values", "read_point",
           "clock_edges", "sample_at", "sample_table", "fold_transactions",
           "fsm_transitions"]
