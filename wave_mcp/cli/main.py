"""``wave-mcp`` entrypoint dispatch.

``wave-mcp query ...`` routes to the CLI surface for all MCP tools;
anything else starts the MCP server exactly as before.
"""
from __future__ import annotations

import sys


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "query":
        from .query import main as query_main
        sys.exit(query_main(argv[1:]))
    from ..server import main as server_main
    sys.exit(server_main())


if __name__ == "__main__":
    main()
