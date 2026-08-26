"""``wave-mcp query`` — command-line access to every wave-mcp MCP tool.

The exact same functions the MCP server exposes, callable from a shell:

    wave-mcp query --list
    wave-mcp query signal_values --session sessions/my_module \\
        --full_path top.u_tx.tx_serial --t0 100ns --t1 200ns
    wave-mcp query signal_drivers --session sessions/my_module \\
        --json-args '{"full_path": "top.u_tx.tx_serial"}'

Arguments are derived from each tool's signature automatically, so every
current and future tool gets a CLI surface with zero extra maintenance.

Default output is human-readable text (the same renderer used for MCP
``content[].text``); pass ``--json`` for the full structured result.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from typing import Any, get_args, get_origin, get_type_hints

from .. import server


def _tool_registry() -> dict[str, Any]:
    """name -> Tool metadata (pydantic model with .name / .description)."""
    tools = asyncio.run(server.mcp.list_tools())
    return {t.name: t for t in tools}


def _split_list(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def _resolve_hint(hints: dict[str, Any], name: str):
    hint = hints.get(name, str)
    return hint


def _is_bool(hint: Any) -> bool:
    return hint is bool or (get_origin(hint) is not None
                            and bool in get_args(hint))


def _is_int(hint: Any) -> bool:
    return hint is int or (get_origin(hint) is not None
                           and int in get_args(hint))


def _is_list(hint: Any) -> bool:
    if get_origin(hint) is list:
        return True
    return any(get_origin(a) is list for a in get_args(hint))


def _type_label(hint: Any) -> str:
    if hint is bool:
        return "flag"
    if hint is int:
        return "int"
    if _is_list(hint):
        return "list (comma-separated)"
    return "str"


def _build_fn_parser(fn: Any, meta: Any, prog: str) -> argparse.ArgumentParser:
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}
    desc = (meta.description or "").strip().splitlines()
    desc = desc[0] if desc else "(no description)"
    p = argparse.ArgumentParser(
        prog=prog, description=desc, add_help=True, allow_abbrev=False)
    for name, param in sig.parameters.items():
        hint = _resolve_hint(hints, name)
        has_default = param.default is not inspect.Parameter.empty
        opts: dict[str, Any] = {
            "default": None,
            "help": f"({_type_label(hint)})"
                    + (f" default: {param.default}" if has_default else ""),
        }
        if _is_bool(hint):
            opts["action"] = "store_true"
        elif _is_int(hint):
            opts["type"] = int
        elif _is_list(hint):
            opts["type"] = _split_list
        p.add_argument(f"--{name}", **opts)
    return p


def _print_tools() -> None:
    registry = _tool_registry()
    print(f"{len(registry)} tools:")
    width = max(len(n) for n in registry)
    for name in sorted(registry):
        desc = (registry[name].description or "").strip().splitlines()
        first = desc[0] if desc else ""
        print(f"  {name:<{width}}  {first}")
    print("\nusage: wave-mcp query <tool> [--session PATH] [--json] "
          "[--json-args JSON] [--<param> value ...]")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    parser = argparse.ArgumentParser(
        prog="wave-mcp query",
        description="Run any wave-mcp MCP tool from the command line.",
        add_help=False)
    parser.add_argument("-h", "--help", action="store_true",
                        help="show this help message and exit")
    parser.add_argument("tool", nargs="?",
                        help="tool name (see --list)")
    parser.add_argument("--list", action="store_true",
                        help="list all available tools")
    parser.add_argument("--session", metavar="PATH",
                        help="open this session dir first (becomes the "
                             "default session for session_id-less tools)")
    parser.add_argument("--json", action="store_true",
                        help="print the full structured result as JSON")
    parser.add_argument("--json-args", metavar="JSON",
                        help="pass tool arguments as a JSON object; "
                             "explicit --<param> flags override its keys")
    known, rest = parser.parse_known_args(argv)

    if known.help and not known.tool:
        parser.print_help()
        return 0

    if known.list or not known.tool:
        _print_tools()
        return 0

    name = known.tool
    registry = _tool_registry()
    if name not in registry:
        print(f"error: unknown tool {name!r}. "
              f"Run 'wave-mcp query --list' to see all tools.",
              file=sys.stderr)
        return 1
    fn = getattr(server, name)

    fn_parser = _build_fn_parser(fn, registry[name],
                                 prog=f"wave-mcp query {name}")
    if known.help:
        fn_parser.print_help()
        return 0
    args = fn_parser.parse_args(rest)

    kwargs = {k: v for k, v in vars(args).items() if v is not None}
    if known.json_args:
        try:
            base = json.loads(known.json_args)
        except json.JSONDecodeError as exc:
            print(f"error: bad --json-args: {exc}", file=sys.stderr)
            return 1
        if not isinstance(base, dict):
            print("error: --json-args must be a JSON object", file=sys.stderr)
            return 1
        kwargs = {**base, **kwargs}

    if known.session:
        try:
            server.SESSIONS.open(known.session)
        except Exception as exc:  # noqa: BLE001 — surface any open failure
            print(f"error: cannot open session {known.session!r}: {exc}",
                  file=sys.stderr)
            return 1

    try:
        result = fn(**kwargs)
    except TypeError as exc:
        print(f"error: bad arguments for {name}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — CLI must report tool failures
        print(f"error: {name} failed: {exc}", file=sys.stderr)
        return 1

    if known.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("\n".join(server._render_text(result)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
