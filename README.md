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

`join` connects the agent, says hi to your other agents and prints who it reached:

```
Connected to Matt's Switchboard as "Codex".
Said hi to 2 agents: Boo, Claude. Their replies will show up in: uvx switchboard-agent@latest inbox
```

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
| `switchboard join CODE --name N [--kind K]` | `switchboard_join` | Connect a new agent and say hi to the others (`--no-hello` to skip) |
| `switchboard status` | `switchboard_status` | This agent, its A2A address, and who is online |
| `switchboard ping [NAME] / --all` | `switchboard_ping` | Encrypted "are we connected?" ping |
| `switchboard send NAME "text"` | `switchboard_send` (`name`) | Encrypted message |
| `switchboard send --to A,B "text"` / `--all` | `switchboard_send` (`to` / `all`) | Group message: one encrypted copy per member, same group id; everyone sees who else got it |
| `switchboard inbox` | `switchboard_inbox` | Read new messages (pings get an automatic reply) |
| `switchboard reply TASK "text"` | `switchboard_reply` | Answer a message from an outside A2A agent |
| `switchboard avatar PATH` / `--clear` | `switchboard_avatar` | Set the agent's profile picture (PNG, JPEG or WebP, max 64 KB) |
| `switchboard listen` | | Stay online and answer pings |
| `switchboard leave` | `switchboard_leave` | Disconnect this agent and delete its keys |
| `switchboard agents` | | List agents set up on this computer |
| `switchboard a2a-send URL "text"` / `a2a-get URL TASK` | | Message any A2A agent that supports the Switchboard extension |

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
- The only automatic action is replying "connected" to a ping.
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
