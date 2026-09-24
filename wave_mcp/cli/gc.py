"""``wave-mcp gc``: show and reclaim disk used by sessions and derived caches.

Only wave-mcp's own roots are considered (``$WAVE_MCP_SESSION_ROOT`` |
``~/.wave-mcp/sessions`` and ``$WAVE_MCP_CACHE_ROOT`` | ``~/.wave-mcp/cache``).
Sessions placed with an explicit ``out_dir`` live elsewhere and are never
touched. Without ``--apply`` nothing is deleted.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from ..runtime import storage

_UNITS = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}


def _size(text: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?\s*", text.lower())
    if not m:
        raise argparse.ArgumentTypeError(f"not a size: {text!r} (e.g. 20G, 500M)")
    return int(float(m.group(1)) * _UNITS[m.group(2)])


def _fmt(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="wave-mcp gc",
        description="Show or reclaim disk used by wave-mcp sessions and caches. "
                    "Dry run unless --apply is given.")
    p.add_argument("--older-than", type=float, metavar="DAYS",
                   help="remove entries not opened for more than DAYS days")
    p.add_argument("--max-size", type=_size, metavar="SIZE",
                   help="then remove least recently used entries until the "
                        "total fits in SIZE (e.g. 20G)")
    p.add_argument("--apply", action="store_true",
                   help="actually delete (default: only report)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    a = p.parse_args(argv)

    res = storage.gc(older_than_days=a.older_than, max_total_bytes=a.max_size,
                     dry_run=not a.apply)
    if a.json:
        print(json.dumps(res, indent=2))
        return 0
    print(f"session root: {res['session_root']}")
    print(f"cache root:   {res['cache_root']}")
    print(f"{res['entries']} entries, {_fmt(res['total_bytes'])} total")
    if a.older_than is None and a.max_size is None:
        biggest = sorted(storage.usage_entries(), key=lambda e: -e["bytes"])[:10]
        for e in biggest:
            print(f"  {_fmt(e['bytes']):>10}  idle {e['idle_days']:6.1f} d  "
                  f"{e['kind']:<22} {e['path']}")
        print("pass --older-than DAYS and/or --max-size SIZE to select entries")
        return 0
    verb = "removed" if a.apply else "would remove"
    for e in res["removed"]:
        print(f"  {verb} {_fmt(e['bytes']):>10}  idle {e['idle_days']:6.1f} d  "
              f"{e['path']}")
    print(f"{verb} {len(res['removed'])} entries, {_fmt(res['freed_bytes'])}"
          + ("" if a.apply else "  (dry run; add --apply)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
