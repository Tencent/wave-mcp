"""pytest setup for tests/unit.

Makes ``import wave_mcp`` resolve to this checkout when the tests run straight
from a clone, from any cwd and without PYTHONPATH or ``pip install -e``:

- the repository root goes on ``sys.path`` for the test process itself;
- it is also prepended to ``PYTHONPATH`` in ``os.environ``, because several
  tests launch ``python -m wave_mcp...`` subprocesses, which do not inherit
  ``sys.path``.

In an offline bundle the bundle root has no ``wave_mcp/`` package, so nothing
is changed there and the installed package is what gets tested.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

if os.path.isdir(os.path.join(_ROOT, "wave_mcp")):
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    _parts = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if _ROOT not in _parts:
        os.environ["PYTHONPATH"] = os.pathsep.join([_ROOT] + _parts)
