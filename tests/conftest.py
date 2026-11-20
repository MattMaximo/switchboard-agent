"""Every test runs with the app config folders in a throwaway place, so no test can ever write a skill into the
real ~/.claude or ~/.codex, and with no Codex/Claude Code session variables leaking in from the shell that runs it."""
import os
import tempfile

_root = tempfile.mkdtemp(prefix="sb-tests-")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(_root, "claude")
os.environ["CODEX_HOME"] = os.path.join(_root, "codex")
os.environ["SWITCHBOARD_HOME"] = os.path.join(_root, "switchboard")
for _k in ("SWITCHBOARD_CLAUDE_SKILLS", "SWITCHBOARD_CODEX_SKILLS", "CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SANDBOX",
           "CODEX_SANDBOX_NETWORK_DISABLED", "SWITCHBOARD_AS"):
    os.environ.pop(_k, None)
