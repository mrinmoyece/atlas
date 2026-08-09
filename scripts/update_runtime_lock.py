"""Regenerate the hashed dependency snapshot used by the runtime image."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    """Compile core, MCP, and OpenTelemetry dependencies for Python 3.12."""
    repo_root = Path(__file__).resolve().parents[1]
    output = Path("scripts/requirements-runtime.txt")
    command = [
        sys.executable,
        "-m",
        "uv",
        "pip",
        "compile",
        "--generate-hashes",
        "--python-version=3.12",
        "--extra=mcp",
        "--extra=otel",
        f"--output-file={output}",
        "pyproject.toml",
    ]
    return subprocess.run(command, check=False, cwd=repo_root).returncode


if __name__ == "__main__":
    raise SystemExit(main())
