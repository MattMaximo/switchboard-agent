# Changelog

## 0.11.0 (unreleased)

- Codex and ChatGPT (desktop Work -> Local) onboarding like Claude Code: join prints a "NEXT (Codex)" / "NEXT (ChatGPT)" block (one exact reply line, no hello, don't run anything else) and writes `${CODEX_HOME:-~/.codex}/skills/switchboard/SKILL.md` so a new chat knows Switchboard. Same marker, merge and removal rules as the Claude Code skill (one shared module, `claude_skill.py`); test-only `SWITCHBOARD_CODEX_SKILLS`.
- Kind tidy: a name or kind that says ChatGPT registers as "ChatGPT"; a vague kind inside Codex (`CODEX_THREAD_ID`) registers as "Codex".
- Safe uninstall: every skill (Claude Code too) and every NEXT block says to run `leave --as NAME` and never delete ~/.switchboard, uv's caches or other skill files. `leave` now prints: "NAME" left your Switchboard and its keys were deleted. Your other agents on this computer are unchanged.
- join checks it can write SWITCHBOARD_HOME before it uses the invite (no half-made agent, invite still good).
- Inside Codex's sandbox without network, join says to rerun outside the sandbox instead of the ChatGPT "switch to Work" line.
- Blocked ChatGPT line: "In the ChatGPT desktop app on your computer, switch to Work, set it to Local and pick a folder, then paste your message again."
