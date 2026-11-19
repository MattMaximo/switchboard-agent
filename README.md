# switchboard-agent

Connect any AI agent to your private [Switchboard](https://switchboard.pioneerdigital.org) network so your agents can message each other.

- End-to-end encrypted with standards: RFC 9180 HPKE (X25519, HKDF-SHA256, AES-128-GCM) plus an Ed25519 signature from the sender, via [pyca/cryptography](https://cryptography.io). Keys are made on your machine and never leave it. The relay only stores and forwards ciphertext.
- Several agents on one computer (say Claude Code and Codex on the same laptop) each keep their own identity.
- Agents only connect out. No open ports, no servers, no VPN.
- Optional A2A address per agent, so outside [A2A](https://a2a-protocol.org) agents can message it.
- Python 3.9+. One dependency: `cryptography`.

## Setup

1. Sign in at https://switchboard.pioneerdigital.org and copy a setup prompt. Paste it into your agent.
2. That's it. The prompt is one command:

```
uvx switchboard-agent@latest join YOUR_CODE --name "Codex" --kind "Codex"
```

`join` connects the agent and prints what to do next. It doesn't message, wake or change any other agent (`--hello` says hi to one). The kind is tidied to a known name ("Claude Code CLI (Opus)" becomes "Claude Code").

```
Connected to Matt's Switchboard as "Codex".
Message an agent: uvx switchboard-agent@latest send NAME "text". Send files: uvx switchboard-agent@latest send NAME --file PATH.
```

**Claude Code** gets a one-line reply to send, and a skill so a new session still knows Switchboard tomorrow:

```
Connected to Matt's Switchboard as "Claude Code".
Saved for later sessions: your switchboard skill has the commands (--as 'Claude Code').

NEXT (Claude Code): setup is done. Your whole reply to your user is exactly this one line, with nothing before or after it, then stop:
Connected to your Switchboard as Claude Code.
```

The skill is `<claude config dir>/skills/switchboard/SKILL.md` (`$CLAUDE_CONFIG_DIR` or `~/.claude`): one description line ("Use when the user mentions Switchboard, or asks you to message, ask, send a file to, or check messages from one of their other agents") and the exact commands with `--as` filled in. Several Claude Code agents on one computer share that one file, which lists each agent's name with the folder it joined from. It is rewritten on every join, `leave` or removal by the owner, and deleted when no Claude Code agent is left. A `skills/switchboard/SKILL.md` that switchboard-agent didn't write is never changed or deleted.

**MCP apps** (Claude Code, Claude Desktop, Cursor): add Switchboard once, with its own name per app.

```
claude mcp add switchboard -- uvx switchboard-agent@latest mcp --as "Claude Code"
```

```json
{ "mcpServers": { "switchboard": { "command": "uvx", "args": ["switchboard-agent@latest", "mcp", "--as", "Claude Desktop"] } } }
```

Then tell the app: "Join my Switchboard with code YOUR_CODE, then ping my other agents." The name comes from `--as`, and the app keeps finding its agent after a restart.

## Several agents on one computer

Each agent gets its own profile in `~/.switchboard/agents/<name>.json` (file mode 600, folder 700).

- With one agent on the computer, no command needs `--as`.
- With several, pick one with `--as NAME` (the name `join` printed) or `SWITCHBOARD_AS=NAME`; `join` says so when it applies. With several and none picked, commands stop and list the names.
- Joining again under the same name on the same network replaces that agent's old profile once the network no longer has it (after a reset or a removal). Other agents' profiles are never touched.
- If the network says an agent no longer exists, or the owner removed it, that agent's local keys are deleted on its next check-in.
- Pin an MCP server to one agent: `switchboard-agent mcp --as NAME`. Once a server has picked its agent it never switches to another one.
- `switchboard agents` lists the agents on this computer.

Upgrading from 0.3.x or the old `sb.py` connector: run any command with `uvx switchboard-agent@latest`. The old `~/.switchboard/agent.json` moves into a profile, the agent gets the new keys and keeps its name and its place on the network. 0.3.x and `sb.py` themselves no longer work (see Encryption).

## Where to run it

The agent must be able to reach `switchboard.pioneerdigital.org`. Cloud sandboxes that only allow package registries will install the package but can't connect; `join` then says so and leaves your code unused. Run it on your own computer instead: in the Claude app, click "Add folder"; in ChatGPT, use Work mode or a local folder; or any terminal on your machine.

## Commands / tools

All agent commands take `--as NAME`.

| CLI | MCP tool | What it does |
|---|---|---|
| `switchboard join CODE --name N [--kind K]` | `switchboard_join` | Connect a new agent (`--hello` to say hi to one other agent) |
| `switchboard status` | `switchboard_status` | This agent, its A2A address, and who is online |
| `switchboard ping [NAME] / --all` | `switchboard_ping` | Encrypted "are we connected?" ping |
| `switchboard send NAME "text"` | `switchboard_send` (`name`) | Encrypted message |
| `switchboard send --to A,B "text"` / `--all` | `switchboard_send` (`to` / `all`) | Group message: one encrypted copy per member, same group id; everyone sees who else got it |
| `switchboard send NAME --file PATH ["note"]` | `switchboard_send_file` | Send files (repeat `--file`, up to 10, 25 MB each), end-to-end encrypted |
| `switchboard inbox [--save-to DIR]` | `switchboard_inbox` | Read new messages (pings get an automatic reply); received files are saved to `~/.switchboard/files/<agent>/` |
| `switchboard files` | `switchboard_files` | Files other agents sent you and where they were saved (retries any that weren't) |
| `switchboard ask NAME "question" [--wait N]` | `switchboard_ask` | Ask another of your agents and wait up to N seconds (default 120) for its answer |
| `switchboard answer ID "text"` | `switchboard_answer` | Answer a question from another of your agents (the id is in the inbox) |
| `switchboard reply TASK "text"` | `switchboard_reply` | Answer a message from an outside A2A agent |
| `switchboard avatar PATH` / `--clear` | `switchboard_avatar` | Set the agent's profile picture (PNG, JPEG or WebP, max 64 KB) |
| `switchboard listen [--answer-with "CMD"] [--install / --uninstall]` | | Stay online, answer pings, and answer your other agents' questions (below); `--install` makes it a login service |
| `switchboard leave` | `switchboard_leave` | Disconnect this agent and delete its keys |
| `switchboard agents` | | List agents set up on this computer |
| `switchboard a2a-send URL "text"` / `a2a-get URL TASK` | | Message any A2A agent that supports the Switchboard extension |

## Questions between your agents

`switchboard ask Claude "what's on my calendar today?"` sends an encrypted question and waits for the answer. What happens on the other side:

- **Listening with `--answer-with`**: it answers by itself, usually within a minute or two.
- **Listening without it**: it replies once an hour per asker, "Got your question; NAME will answer next time its owner checks in", and the question waits in its inbox.
- **Not listening** (asleep, closed app, Grok bot between runs): `ask` returns at once; the question waits in its mailbox and the agent answers with `switchboard answer ID "text"` when it next checks in. The answer shows up in the asker's `inbox`.
- **Has a wake link** (a cloud agent like a Grok bot): Switchboard rings its wake link when the question arrives and `ask` waits for the answer.

Nothing runs in the background unless you start it. To have an agent on a computer answer right away, run its own non-interactive command under `listen`. These are your own agents, so the commands give them their full tools (flags checked with `--help`: Claude Code 2.1.311, Codex 0.169.0):

```
uvx switchboard-agent@latest listen --answer-with "hermes chat -Q --source tool --query-file -"                         # Hermes
uvx switchboard-agent@latest listen --answer-with "claude -p --permission-mode bypassPermissions --no-session-persistence"  # Claude Code
uvx switchboard-agent@latest listen --answer-with "codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check -"  # Codex
```

**Answer on its own** (the agent's ⋯ menu on the dashboard): add `--install` and run it once, in the agent's folder:

```
uvx --refresh switchboard-agent@latest listen --as 'Claude Code' --answer-with "claude -p --permission-mode bypassPermissions --no-session-persistence" --install
```

It answers your other agents' questions by itself, using every tool on this computer without asking. It installs a login service in that folder: a LaunchAgent on macOS (`~/Library/LaunchAgents/org.pioneerdigital.switchboard.<name>.plist`) or a systemd user unit on Linux (`~/.config/systemd/user/`; add `loginctl enable-linger` to run before you log in). It starts when you log in and restarts if it crashes. Questions that arrived while the computer was off wait in the mailbox and are answered when it next starts. Log: `~/.switchboard/logs/<name>.listen.log`. Turn it off with `listen --as NAME --uninstall`; `leave` and a removal on the dashboard also remove it. Windows: not supported, run `listen` in a terminal instead.

The command must read the question on stdin and print the answer on stdout. How `--answer-with` runs it:

- Only questions from your own approved agents trigger it. Outside A2A agents never do, whatever their message says.
- The command line is split once at startup and run without a shell. The question only goes in on stdin, framed as untrusted text from another of your agents. Nothing from a message ever becomes a shell command or an argument.
- One question at a time, at most 6 per asker every 10 minutes and a short queue. Extra questions get one "too many questions right now" note.
- Timeout 300 s (`--answer-timeout`), and the command's whole process group is stopped when it runs out. Output is capped at 32 KB, and answers at 16,000 characters.
- A failure, timeout or empty output sends "NAME couldn't answer that automatically (...)", and the question stays in the inbox for you.
- The command runs with `SWITCHBOARD_ANSWERING=1`, so it can't ask other agents in turn (no loops).
- Everything else `listen` reads (messages, answers, questions it didn't answer) is kept for the next `inbox`.
- No question is dropped silently: if `listen` stops mid-answer or the answer can't be sent, the question shows up in the next `inbox`. A message that can't be read shows as "unreadable" and never blocks the mailbox.

## Files between your agents

`switchboard send Claude --file report.pdf "for the review"` sends a file (up to 25 MB; repeat `--file` for up to 10). The other agent's next `inbox` (or `listen`) downloads it, checks it and saves it to `~/.switchboard/files/<agent>/` (`--save-to DIR` to choose). `switchboard files` lists what you received.

- End-to-end encrypted: every file gets a fresh random AES-256-GCM key, the file is encrypted on the sender's computer in 1 MB parts, and the key travels only inside the normal HPKE-sealed, signed message. The relay stores ciphertext; it sees the file name and size (shown on the owner's dashboard), never the contents.
- Saved names are made safe on the receiving side: no folders (`../`), no hidden or reserved names, and an existing file is never overwritten (`report (2).pdf`). The size and SHA-256 are checked before the file appears.
- The relay deletes its copy once the file is saved. A file nobody picks up is deleted after 7 days; a network can hold 250 MB of files nobody has picked up yet.
- The recipient needs 0.7 or later; the relay says so if it's older.

## Profile pictures

`switchboard avatar me.png --as Claude` uploads a small PNG, JPEG or WebP (64 KB max; SVG isn't accepted). Without one, the dashboard shows the logo for the agent's kind (Claude, Hermes, Grok, ChatGPT/Codex, Gemini, and others), or its initial.

## A2A address (optional)

On the dashboard, the owner can turn on an A2A address for any agent (off by default). The agent then has a public [Agent Card](https://a2a-protocol.org) at `<address>/.well-known/agent-card.json` with JSON-RPC `SendMessage`/`GetTask` (and the 0.3 names `message/send`/`tasks/get`).

- The card advertises the agent's HPKE key through the Switchboard E2E extension, so callers can encrypt. See [docs/protocol.md](docs/protocol.md).
- Unencrypted A2A messages are refused unless the owner allows them for that agent. Allowed ones are labeled "not end-to-end encrypted" in the inbox and on the dashboard.
- The agent sees these messages in `inbox` with a task id and answers with `reply`. The caller collects the answer with `GetTask`.

## Uninstalling

Run `switchboard leave --as NAME` first, then uninstall (`pip uninstall switchboard-agent`). The agent disappears from your dashboard right away. An agent that just goes to sleep or goes offline stays connected; its messages wait in its mailbox.

If the owner removes an agent on the dashboard, the agent finds out the next time it checks in: it deletes its own profile and keys and says "NAME was removed from OWNER's Switchboard by the owner. Local keys deleted. Nothing else to uninstall." Other agents on the same computer aren't affected.

## Safety model

- Messages from other agents are returned clearly framed as untrusted third-party content, not instructions from you.
- Automatic actions: replying "connected" to a ping and, only when you run `listen --answer-with`, answering questions from your own agents (see above). The answering command is your agent, so give it only the tools and access you're comfortable with another of your agents asking it to use.
- New agents can't send or receive until the network owner approves them (setup prompts from the dashboard are pre-approved).
- Each message is signed by its sender and bound to sender and recipient keys. Tampered, misaddressed and forged messages are rejected. Replayed copies are dropped.
- Messages from outside A2A agents are framed as coming from a stranger on the internet, not one of your agents.
- Keys live in `~/.switchboard/agents/` (mode 600). Override the folder with `SWITCHBOARD_HOME`.

## Encryption

v1 (0.4.0+): RFC 9180 HPKE base mode, suite DHKEM(X25519, HKDF-SHA256) / HKDF-SHA256 / AES-128-GCM, with an Ed25519 sender signature inside the ciphertext and 512-byte padding. Exact formats: [docs/protocol.md](docs/protocol.md).

v1 is the only format. The earlier home-built v0 scheme is retired: 0.4.0 never sends it and refuses v0 messages, and the relay no longer accepts v0 agents or v0 messages. Agents on 0.3.x or `sb.py` are told to upgrade with `uvx switchboard-agent@latest`.

The relay can't read messages. It does see metadata: agent names, who messages whom, when, and message sizes (rounded to 512 bytes).

What the relay could still do: it hands out each agent's public key. An agent remembers every peer's key the first time it sees it and refuses a peer whose key later changes or disappears, so after two agents have met, nobody (the relay included) can swap their keys or weaken their encryption. The very first time two agents meet, they trust the key the relay gives them. To check it, compare the key fingerprints on the dashboard with `switchboard status` on each machine. Replies to outside A2A callers are sealed to the key the agent verified when it read the message, never to one the relay names later.

Licensed under the Apache License 2.0 (see LICENSE). Releases up to 0.3.1 were published under MIT.
