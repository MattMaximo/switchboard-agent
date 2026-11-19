"""Switchboard extension for the Claude desktop app: the switchboard-agent MCP server in app mode.

Claude starts this with `uv run --directory <extension> src/server.py`. Keys live in ~/.switchboard on this computer.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from switchboard_agent.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["mcp", "--app", "claude-desktop"]))
