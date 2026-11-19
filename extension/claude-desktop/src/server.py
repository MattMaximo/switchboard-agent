"""Switchboard extension for the Claude desktop app: the switchboard-agent MCP server in app mode.

Claude starts this with `uv run --directory <extension> src/server.py`. Keys live in ~/.switchboard on this computer.
A download from /claude?invite=CODE also has switchboard-invite.json at the extension root: the server joins with it
on its first start and deletes it.
"""
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)

from switchboard_agent.cli import main  # noqa: E402
from switchboard_agent.mcp import BUNDLE_FILE  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["mcp", "--app", "claude-desktop", "--bundle-invite", os.path.join(os.path.dirname(here), BUNDLE_FILE)]))
