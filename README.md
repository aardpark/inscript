# inscript

Session memory for AI coding agents. Records what happens across Claude Code sessions so you never lose context.

## Install

```bash
pip install inscript
inscript setup
```

That's it. `inscript setup` configures Claude Code hooks (passive recording), registers the MCP server (agent tools), and creates a default config. No manual JSON editing.

## What it does

**Passive recording** — Hooks silently capture every prompt, file touch, edit, and diff. When a new session starts, inscript compresses the previous session into a handoff summary that the new agent receives automatically. The agent doesn't know inscript exists — it just starts with context.

**Agent tools** — An MCP server gives agents 10 tools they can call on demand:

| Tool | What it does |
|------|-------------|
| `status` | Current project, active session, other running sessions |
| `sessions` | List all recorded sessions with IDs and timestamps |
| `log` | Activity log — every prompt with files touched and token usage |
| `replay` | Compact context summary of a previous session |
| `message` | Drill into a specific prompt — full response text, file touches, diffs |
| `thread` | Cross-session timeline — last 30min of work across all parallel sessions |
| `file_history` | Complete diff arc for one file across a session |
| `commits` | Git commits linked to the prompts that produced them |
| `note` | Save a note to the session (surfaces in future replays) |
| `notes` | List all notes for a session |

Agents discover these automatically through MCP. No CLAUDE.md instructions needed.

## The session continuity problem

Every new Claude Code session starts blind. You re-explain context, re-describe decisions, re-establish what was tried and rejected. Inscript fixes this:

1. **Automatic handoff** — new sessions receive a summary of the previous session's work
2. **Cross-session awareness** — `thread` shows what all parallel sessions are doing
3. **Full history** — `message` retrieves any prompt + response from any session
4. **Notes persist** — decisions and ideas survive session boundaries

## For humans

```bash
inscript explore [id]     # interactive TUI — arrow keys to browse prompts
inscript viz [id]         # visual heatmap — files x prompts
```

## CLI reference

```bash
# Setup
inscript setup            # configure hooks + MCP server
inscript init             # configure retention and storage

# Session tools
inscript log [id]         # activity log
inscript replay [id]      # context summary

# Human tools
inscript explore [id]     # interactive browser
inscript viz [id]         # visual heatmap

# Notes
inscript note "text"      # save a note (optionally --ref FILE)
inscript notes [id]       # list notes

# Organization
inscript tag <name>       # tag current work
inscript untag            # clear tag
inscript time [tag]       # time spent by tag
inscript branch "why"     # start a scoped detour
inscript resume           # end detour, return to trunk
inscript branches         # list branches

# Export & maintenance
inscript export <id>      # export session as markdown
inscript chat-export <id> # full conversation export with responses
inscript permissions [id] # generate permission profile from usage
inscript overlap          # file collisions across sessions
inscript cleanup          # enforce retention policy
```

## How it works

Four Claude Code hooks write to `~/.inscript/`:

- **SessionStart** — creates session directory, finalizes previous session, injects handoff context
- **UserPromptSubmit** — records user prompts with timestamps
- **PostToolUse** — records file touches, diffs, detects git commits
- **Stop** — writes summary snapshot with token usage

```
~/.inscript/
  config.toml                # retention policy, storage settings
  active_session             # current session ID
  active_sessions/           # per-process session tracking
  sessions/
    <session-id>/
      meta.json              # start time, project, status, transcript path
      prompts.jsonl          # every user prompt
      touches.jsonl          # every file read/edit/write
      diffs.jsonl            # raw edit diffs (configurable)
      summary.json           # stats, token usage, duration
      commits.jsonl          # git commits linked to prompts
      notes.jsonl            # user notes
      branches.jsonl         # detour tracking
```

All data is plain JSON/JSONL. Any tool can read it.

## Python API

```python
from inscript_pkg import active_project, active_session, list_sessions
from inscript_pkg.replay import generate_replay, generate_log

project = active_project()      # Path or None
session = active_session()      # session ID or None
sessions = list_sessions()      # [{session_id, start_time, project, status}, ...]
replay = generate_replay(sid)   # compact session summary
log = generate_log(sid)         # activity log
```

## License

MIT
