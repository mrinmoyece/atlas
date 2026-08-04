"""Atlas as an MCP server.

Exposes the repository-analysis toolkit over MCP so any host can drive it.
The tool definitions come from `atlas.tools.repo.tool_specs()` - the same
source the in-process agents use - so the two transports can never drift.

Security posture (this is a server accepting calls from outside the
process, so it is a real attack surface):

  * The repository root is fixed at construction. A caller cannot pass a
    root; the confinement check in `RepoToolkit` then makes traversal
    impossible rather than merely discouraged.
  * Only the five whitelisted tools are registered. There is no generic
    "run" or "exec" tool, by design.
  * Errors are returned as text, never raised across the protocol boundary
    with a stack trace (which would leak absolute paths).

The `mcp` package is an optional dependency: importing this module without
it raises a clear message, and the rest of Atlas keeps working, so `pip
install atlas-dd` stays light.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atlas.tools.repo import RepoToolkit, tool_specs

SERVER_NAME = "atlas-repo-tools"
SERVER_VERSION = "0.1.0"


def mcp_tool_definitions() -> list[dict[str, Any]]:
    """Tool list in MCP's `tools/list` shape.

    Exposed as plain dicts (not SDK objects) so this can be asserted in
    tests and served by any transport, with or without the SDK installed.
    """
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "inputSchema": spec.parameters or {"type": "object", "properties": {}},
        }
        for spec in tool_specs()
    ]


def build_mcp_server(repo_root: str | Path):  # pragma: no cover - needs mcp SDK
    """Construct a FastMCP server bound to one repository root.

    Run it with:
        python -m atlas.mcp_layer.server /path/to/repo
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        raise RuntimeError("MCP server support requires: pip install 'atlas-dd[mcp]'") from e

    toolkit = RepoToolkit(repo_root)
    server = FastMCP(SERVER_NAME)

    @server.tool()
    def list_files(pattern: str = "*") -> str:
        """List repository files matching a glob pattern."""
        return toolkit.call("list_files", {"pattern": pattern})

    @server.tool()
    def read_file(path: str, start_line: int = 1, max_lines: int = 200) -> str:
        """Read a slice of a file, with line numbers for citation."""
        return toolkit.call(
            "read_file", {"path": path, "start_line": start_line, "max_lines": max_lines}
        )

    @server.tool()
    def grep(pattern: str, glob: str = "*") -> str:
        """Regex search across the repository; returns path:line:text."""
        return toolkit.call("grep", {"pattern": pattern, "glob": glob})

    @server.tool()
    def dependency_manifest() -> str:
        """Return dependency manifests found in the repository."""
        return toolkit.call("dependency_manifest", {})

    @server.tool()
    def repo_stats() -> str:
        """Language mix, file count, and CI/test/docs signals."""
        return toolkit.call("repo_stats", {})

    return server


def main() -> None:  # pragma: no cover
    import sys

    root = sys.argv[1] if len(sys.argv) > 1 else "."
    build_mcp_server(root).run()


if __name__ == "__main__":  # pragma: no cover
    main()
