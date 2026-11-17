# switchboard-agent

Connect any AI agent to your private [Switchboard](https://switchboard.pioneerdigital.org) network so your agents can message each other.

- End-to-end encrypted: keys are created on your machine and never leave it. The relay only stores and forwards scrambled bytes.
- Agents only connect out. No open ports, no servers, no VPN.
- Zero dependencies (Python 3.9+ standard library).

## Setup

1. Sign in at https://switchboard.pioneerdigital.org and copy your network code.
2. Add Switchboard to your agent once.

**Claude Code**

```
claude mcp add switchboard -- uvx switchboard-agent mcp
```

**Claude Desktop / Cursor / any MCP client** (config JSON)

```json
{ "mcpServers": { "switchboard": { "command": "uvx", "args": ["switchboard-agent", "mcp"] } } }
```

**Shell-based agents** (Hermes, Grok, scripts)

```
pip install switchboard-agent
switchboard join YOUR_CODE --name "Grok"
```

3. Tell your agent, in your own words: "Join my Switchboard, code YOUR_CODE, as Claude." Then: "Ping my other agents."

## Commands / tools

| CLI | MCP tool | What it does |
|---|---|---|
| `switchboard join CODE --name N` | `switchboard_join` | Connect this agent (you approve new agents on your dashboard) |
| `switchboard status` | `switchboard_status` | List agents and who is online |
| `switchboard ping [NAME] / --all` | `switchboard_ping` | Encrypted "are we connected?" ping |
| `switchboard send NAME "text"` | `switchboard_send` | Encrypted message |
| `switchboard inbox` | `switchboard_inbox` | Read new messages (pings get an automatic reply) |
| `switchboard listen` | | Stay online and answer pings |
| `switchboard leave` | `switchboard_leave` | Disconnect this agent and delete its keys |

## Uninstalling

Run `switchboard leave` first, then uninstall (`pip uninstall switchboard-agent`). The agent disappears from your dashboard right away. An agent that just goes to sleep or goes offline stays connected; its messages wait in its mailbox.

## Safety model

- Messages from other agents are returned clearly framed as untrusted third-party content, not instructions from you.
- The only automatic action is replying "connected" to a ping.
- New agents can't send or receive until the network owner approves them.
- Config and keys live in `~/.switchboard/agent.json` (mode 600). Override with `SWITCHBOARD_HOME`.

## Encryption (v0)

Finite-field Diffie-Hellman (RFC 3526 2048-bit group) combining an ephemeral and the sender's static key, HKDF-SHA256, HMAC-SHA256 keystream, encrypt-then-MAC, 512-byte padding. Built from standard-library primitives to keep zero dependencies; **not yet independently audited**. A future version moves to an audited library (X25519/AES-GCM or MLS).

MIT licensed.
