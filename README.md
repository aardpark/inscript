# inscript

Universal agent activity ledger. Records what AI agents do — which files they touch, what they change, how they spend their time — so you never lose context between sessions.

## Install

```bash
pip install inscript
inscript setup
```

`inscript setup` does three things:
1. Configures Claude Code hooks (passive recording)
2. Registers the inscript MCP server (agent-facing tools)
3. Creates default retention config

No manual JSON editing required.

## What it does

Inscript has two layers:

**Passive recording** — Hooks silently capture every file read, edit, write, and prompt. When a session ends and a new one starts, inscript compresses the previous session into a compact handoff summary that the new session receives automatically.

**Agent tools** — An MCP server exposes four tools that agents can call directly:

| Tool | What it does |
|------|-------------|
| `replay` | Compact context summary of a previous session — files, prompts, detours, where work left off |
| `log` | Activity log with every prompt, file touched, and token usage |
| `sessions` | List all recorded sessions with IDs and timestamps |
| `status` | Current project, active session, other running sessions |

Agents discover these tools automatically through MCP. No CLAUDE.md instructions needed.

## For humans

### Interactive explorer

```bash
inscript explore          # current session
inscript explore <id>     # specific session
```

Terminal UI for browsing sessions. Arrow keys to scrub through prompts, tabs for chat/diffs/files views.

### Session visualization

```bash
inscript viz              # heatmap: files x prompts
```

Visual map of where an agent spent its time across files and prompts.

## CLI reference

```bash
inscript setup            # configure hooks + MCP server
inscript log [id]         # activity log
inscript replay [id]      # context summary for handoff
inscript explore [id]     # interactive browser
inscript viz [id]         # visual heatmap
inscript tag <name>       # tag current work
inscript untag            # clear tag
inscript time [tag]       # time spent by tag
inscript branch "why"     # start a scoped detour
inscript resume           # end detour, return to trunk
inscript branches         # list branches
inscript overlap          # file collisions across sessions
inscript export <id>      # export session as markdown
inscript cleanup          # enforce retention policy
inscript init             # configure retention and storage
```

## How it works

Four Claude Code hooks write to `~/.inscript/`:

- **SessionStart** — creates session, finalizes previous session, injects handoff context
- **UserPromptSubmit** — records prompts with tags and branch IDs
- **PostToolUse** — records file touches, diffs, and project context
- **Stop** — writes running summary snapshot

An MCP server (`inscript-mcp`) wraps the session data as callable tools. Agents see `replay`, `log`, `sessions`, and `status` in their tool list and can call them when they need context.

## Data stored

```
~/.inscript/
  sessions/
    <session-id>/
      meta.json          # start time, project, status
      prompts.jsonl      # every user prompt
      touches.jsonl      # every file read/edit/write
      diffs.jsonl        # raw changes (configurable)
      summary.json       # stats, token usage
  config.toml            # retention policy
```

## Python API

```python
from inscript_pkg import active_project, active_session, list_sessions
from inscript_pkg import generate_replay, generate_log

project = active_project()      # Path or None
session = active_session()      # session ID or None
sessions = list_sessions()      # [{session_id, start_time, project, status}, ...]
replay = generate_replay(sid)   # compact session summary as string
log = generate_log(sid)         # activity log as string
```

## License

MIT
