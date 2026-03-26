"""Inscript — universal agent activity ledger.

Records what AI agents do: which project they're in, what files they
touch, what they change. Any tool reads ~/.inscript/ for context.

Usage:
    from inscript_pkg import active_project, active_session
    from inscript_pkg import generate_replay, generate_log

    project = active_project()   # Path or None
    session = active_session()   # session ID or None
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

__version__ = "0.9.1"

INSCRIPT_DIR = Path.home() / ".inscript"
ACTIVE_PROJECT_FILE = INSCRIPT_DIR / "active_project"
ACTIVE_SESSION_FILE = INSCRIPT_DIR / "active_session"
ACTIVE_SESSIONS_DIR = INSCRIPT_DIR / "active_sessions"  # per-PID session tracking
SESSIONS_DIR = INSCRIPT_DIR / "sessions"
OVERLAP_DIR = INSCRIPT_DIR / "overlap"
CONFIG_FILE = INSCRIPT_DIR / "config.toml"

DEFAULT_CONFIG = {
    "retention": {"policy": "30d", "max_storage": "1GB", "store_diffs": True},
    "overlap": {"enabled": True},
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        return DEFAULT_CONFIG
    try:
        import sys
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            try:
                import tomli as tomllib
            except ImportError:
                return DEFAULT_CONFIG
        with CONFIG_FILE.open("rb") as f:
            return tomllib.load(f)
    except Exception:
        return DEFAULT_CONFIG


def store_diffs() -> bool:
    return _load_config().get("retention", {}).get("store_diffs", True)


# ---------------------------------------------------------------------------
# Active project
# ---------------------------------------------------------------------------

def active_project() -> Path | None:
    try:
        text = ACTIVE_PROJECT_FILE.read_text().strip()
        if text:
            p = Path(text)
            if p.is_dir():
                return p
    except (OSError, ValueError):
        pass
    return None


def set_active_project(path: str | Path) -> None:
    INSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_PROJECT_FILE.write_text(str(Path(path).resolve()) + "\n")


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def active_session() -> str | None:
    """Get active session. Used by CLI tools — returns global singleton."""
    try:
        return ACTIVE_SESSION_FILE.read_text().strip() or None
    except OSError:
        return None


def _get_ppid() -> int:
    """Get parent process ID, with Windows fallback."""
    import os
    try:
        return os.getppid()
    except AttributeError:
        return os.getpid()


def _process_exists(pid: int) -> bool:
    """Check if a process exists, cross-platform."""
    import os
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        # PermissionError = exists but can't signal
        # AttributeError = os.kill not available (some Windows configs)
        # OSError = Windows may raise this instead of ProcessLookupError
        return False


def active_session_for_hook() -> str | None:
    """Get active session for the calling Claude Code instance (by PPID).
    Falls back to global active_session if no per-PID file exists."""
    ppid = _get_ppid()
    pid_file = ACTIVE_SESSIONS_DIR / str(ppid)
    try:
        return pid_file.read_text().strip() or None
    except OSError:
        return active_session()


def set_active_session_for_hook(session_id: str) -> None:
    """Set active session for the calling Claude Code instance (by PPID).
    Also updates global active_session for CLI compatibility."""
    ppid = _get_ppid()
    ACTIVE_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    (ACTIVE_SESSIONS_DIR / str(ppid)).write_text(session_id + "\n")
    # Also update global for CLI tools
    ACTIVE_SESSION_FILE.write_text(session_id + "\n")
    # Clean stale PID files
    _cleanup_stale_pid_files()


def _cleanup_stale_pid_files() -> None:
    """Remove active_sessions/ entries for PIDs that no longer exist."""
    if not ACTIVE_SESSIONS_DIR.exists():
        return
    for f in ACTIVE_SESSIONS_DIR.iterdir():
        try:
            pid = int(f.name)
            if not _process_exists(pid):
                f.unlink(missing_ok=True)
        except ValueError:
            f.unlink(missing_ok=True)


def session_dir(session_id: str) -> Path:
    result = (SESSIONS_DIR / session_id).resolve()
    # Prevent path traversal (e.g. session_id = "../../.ssh")
    if not str(result).startswith(str(SESSIONS_DIR.resolve())):
        return SESSIONS_DIR / "invalid"
    return result


def _rel_path(file_path: str, project: str | None) -> str:
    """Relativize a file path against the project root for display."""
    if project and file_path.startswith(project):
        rel = file_path[len(project):]
        rel = rel.lstrip("/").lstrip("\\")
        return rel or file_path
    return file_path


def _format_tokens(n: int) -> str:
    """Format token count: 1234 -> 1.2k, 1234567 -> 1.2M."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def list_sessions() -> list[dict]:
    sessions = []
    if not SESSIONS_DIR.exists():
        return sessions
    for d in sorted(SESSIONS_DIR.iterdir(), reverse=True):
        meta_file = d / "meta.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
                meta["session_id"] = d.name
                sessions.append(meta)
            except (json.JSONDecodeError, OSError):
                pass
    return sessions


def active_sessions() -> list[dict]:
    return [s for s in list_sessions() if s.get("status") == "active"]


# ---------------------------------------------------------------------------
# Overlap
# ---------------------------------------------------------------------------

def project_hash(project_path: str) -> str:
    return hashlib.sha256(project_path.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Data loading helpers (used by multiple modules)
# ---------------------------------------------------------------------------

def _append_jsonl(path: Path, data: dict) -> None:
    """Append a single JSON object as a line to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(data, default=str) + "\n")


def _load_jsonl(path: Path) -> list[dict]:
    """Load all entries from a JSONL file."""
    if not path.exists():
        return []
    entries = []
    for line in path.open():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return entries


def _load_prompts(sdir: Path) -> list[dict]:
    """Load prompts for a session."""
    return _load_jsonl(sdir / "prompts.jsonl")


# ---------------------------------------------------------------------------
# Public API re-exports
# ---------------------------------------------------------------------------

def generate_replay(session_id: str) -> str | None:
    from .replay import generate_replay as _gen
    return _gen(session_id)


def generate_log(session_id: str) -> str | None:
    from .replay import generate_log as _gen
    return _gen(session_id)


def generate_file_history(session_id: str, file_query: str) -> str | None:
    from .replay import generate_file_history as _gen
    return _gen(session_id, file_query)


def infer_branches(prompts, touches_by_prompt):
    from .inference import infer_branches as _infer
    return _infer(prompts, touches_by_prompt)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _dispatch_permissions(args: list[str]) -> None:
    apply = "--apply" in args
    positional = [a for a in args if not a.startswith("--")]
    session_id = positional[0] if positional else None
    mod = __import__("inscript_pkg.commands", fromlist=["cmd_permissions"])
    mod.cmd_permissions(session_id, apply=apply)


def _dispatch_chat_export(args: list[str]) -> None:
    import sys
    snapshot = "--snapshot" in args
    positional = [a for a in args if not a.startswith("--")]
    if not positional:
        print("Usage: inscript chat-export <session-id> [output-dir] [--snapshot]", file=sys.stderr)
        return
    session_id = positional[0]
    output_dir = positional[1] if len(positional) > 1 else None
    mod = __import__("inscript_pkg.commands", fromlist=["cmd_chat_export"])
    mod.cmd_chat_export(session_id, output_dir, snapshot=snapshot)


def cli() -> None:
    import sys
    args = sys.argv[1:]
    if not args:
        _cmd_status()
        return

    commands = {
        "init": _cmd_init,
        "setup": _cmd_setup,
        "status": lambda: _cmd_status(),
        "log": lambda: _cmd_log(args[1] if len(args) > 1 else None),
        "replay": lambda: _cmd_replay(args[1] if len(args) > 1 else None),
        "overlap": lambda: __import__("inscript_pkg.commands", fromlist=["cmd_overlap"]).cmd_overlap(),
        "cleanup": lambda: __import__("inscript_pkg.commands", fromlist=["cmd_cleanup"]).cmd_cleanup(),
        "export": lambda: __import__("inscript_pkg.commands", fromlist=["cmd_export"]).cmd_export(args[1]) if len(args) > 1 else print("Usage: inscript export <session-id>", file=sys.stderr),
        "chat-export": lambda: _dispatch_chat_export(args[1:]),
        "permissions": lambda: _dispatch_permissions(args[1:]),
        "set": lambda: (set_active_project(args[1]), print(f"Active project: {Path(args[1]).resolve()}")) if len(args) > 1 else print("Usage: inscript set <path>", file=sys.stderr),
        "note": lambda: _cmd_note(args[1:]),
        "notes": lambda: _cmd_notes(args[1:]),
        "tag": lambda: __import__("inscript_pkg.commands", fromlist=["cmd_tag"]).cmd_tag(args[1] if len(args) > 1 else None),
        "untag": lambda: __import__("inscript_pkg.commands", fromlist=["cmd_tag"]).cmd_tag(None),
        "time": lambda: __import__("inscript_pkg.commands", fromlist=["cmd_time"]).cmd_time(args[1] if len(args) > 1 else None),
        "branch": lambda: __import__("inscript_pkg.commands", fromlist=["cmd_branch"]).cmd_branch(args[1] if len(args) > 1 else None),
        "resume": lambda: __import__("inscript_pkg.commands", fromlist=["cmd_resume"]).cmd_resume(),
        "branches": lambda: __import__("inscript_pkg.commands", fromlist=["cmd_branches"]).cmd_branches(),
        "viz": lambda: __import__("inscript_pkg.viz", fromlist=["dispatch_viz"]).dispatch_viz(args[1:]),
        "map": lambda: __import__("inscript_pkg.viz", fromlist=["dispatch_viz"]).dispatch_viz(args[1:]),
        "explore": lambda: __import__("inscript_pkg.explore", fromlist=["cmd_explore"]).cmd_explore(args[1] if len(args) > 1 else None),
        "dashboard": lambda: __import__("inscript_pkg.explore", fromlist=["cmd_dashboard"]).cmd_dashboard(),
        "concepts": lambda: _cmd_concepts(args[1] if len(args) > 1 else None),
        "categories": lambda: _cmd_categories(args[1] if len(args) > 1 else None),
        "help": _cmd_help, "--help": _cmd_help, "-h": _cmd_help,
    }

    handler = commands.get(args[0])
    if handler:
        handler()
    else:
        print(f"Unknown command: {args[0]}", file=sys.stderr)
        _cmd_help()
        sys.exit(1)


def _cmd_help():
    print("""inscript — universal agent activity ledger

Commands:
  inscript setup        Configure Claude Code hooks + MCP server (run this first)
  inscript              Show status
  inscript log [id]     Activity log for a session (latest if omitted)
  inscript replay [id]  Context summary for handoff to next session
  inscript explore [id] Interactive timeline explorer (arrow keys)
  inscript viz [id]     Visual session map (files × prompts heatmap)
  inscript note "text"  Save a thought, linked to current session/prompt
  inscript note "text" --ref FILE  Save with a file reference
  inscript notes [id]   List notes for a session (or 'all' for every session)
  inscript tag <name>   Tag current work with a feature/task name
  inscript untag        Clear the current tag
  inscript time [tag]   Show time spent, optionally filtered by tag
  inscript branch "why" Start a scoped detour (debugging, refactoring, etc.)
  inscript resume       End the current branch, return to trunk
  inscript branches     Show branches for the current session
  inscript overlap      File collisions across concurrent sessions
  inscript export <id>  Export session as markdown
  inscript chat-export <id> [dir] [--snapshot]  Full bundle: chat + activity + notes + files
                                    --snapshot: include codebase working directory
  inscript permissions [id] [--apply]  Generate permissions profile from tool usage
                                    --apply: write to ~/.claude/settings.local.json
  inscript concepts     Cross-session concept clusters (recurring file groups)
  inscript concepts <f> Show history for the concept containing a file
  inscript categories [id]  Prompt category analysis for a session (structural shapes)
  inscript categories all   Recurring workflow patterns across sessions
  inscript cleanup      Enforce retention policy
  inscript init         Advanced: configure retention and storage""")


def _cmd_init():
    INSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    print("inscript setup\n")
    sd = input("Store raw diffs? [Y/n]: ").strip().lower()
    policy = input("Retention [forever/30d/7d] (default 30d): ").strip() or "30d"
    max_storage = input("Max storage [unlimited/1GB/500MB] (default 1GB): ").strip() or "1GB"
    CONFIG_FILE.write_text(f"""[retention]
policy = "{policy}"
max_storage = "{max_storage}"
store_diffs = {'true' if sd != 'n' else 'false'}

[overlap]
enabled = true
""")
    print(f"\nConfig written to {CONFIG_FILE}")


def _cmd_setup():
    """One-command setup: configure Claude Code hooks + MCP server."""
    import shutil
    import sys

    INSCRIPT_DIR.mkdir(parents=True, exist_ok=True)

    # Verify inscript-hook is installed and findable
    hook_bin_abs = shutil.which("inscript-hook")
    if not hook_bin_abs:
        py_bin_dir = Path(sys.executable).parent
        candidate = py_bin_dir / "inscript-hook"
        if candidate.exists():
            hook_bin_abs = str(candidate)
    if not hook_bin_abs:
        inscript_bin = shutil.which("inscript")
        if inscript_bin:
            candidate = Path(inscript_bin).parent / "inscript-hook"
            if candidate.exists():
                hook_bin_abs = str(candidate)

    if not hook_bin_abs:
        print("Error: inscript-hook not found on PATH.", file=sys.stderr)
        print("Make sure inscript is installed: pip install inscript", file=sys.stderr)
        return

    # Use absolute path if in a virtualenv (Claude Code won't inherit the venv PATH),
    # bare command name otherwise (survives Python upgrades, system installs).
    in_venv = (
        hasattr(sys, "real_prefix")  # old-style virtualenv
        or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)  # venv
    )
    hook_bin = hook_bin_abs if in_venv else "inscript-hook"

    py_bin_dir = Path(sys.executable).parent

    print(f"inscript setup")
    print(f"  hook binary: {hook_bin_abs}")
    if in_venv:
        print(f"  hook command: {hook_bin} (absolute — virtualenv detected)")
    else:
        print(f"  hook command: {hook_bin} (PATH-relative)")
    print()

    # Read existing settings
    settings_path = Path.home() / ".claude" / "settings.json"
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    hooks = settings.setdefault("hooks", {})
    inscript_hooks = {
        "SessionStart": {"hooks": [{"type": "command", "command": hook_bin}]},
        "UserPromptSubmit": {"hooks": [{"type": "command", "command": hook_bin}]},
        "PostToolUse": {"matcher": "Read|Edit|Write|Glob|Grep|Bash", "hooks": [{"type": "command", "command": hook_bin, "async": True}]},
        "Stop": {"hooks": [{"type": "command", "command": hook_bin}]},
    }

    already_configured = True
    for event, hook_config in inscript_hooks.items():
        event_hooks = hooks.get(event, [])
        has_inscript = any(
            any("inscript" in h.get("command", "") for h in entry.get("hooks", []))
            for entry in event_hooks
        )
        if not has_inscript:
            already_configured = False
            break

    if already_configured:
        print("Claude Code hooks already configured.")
    else:
        for event, hook_config in inscript_hooks.items():
            event_hooks = hooks.get(event, [])
            event_hooks = [
                entry for entry in event_hooks
                if not any("inscript" in h.get("command", "") for h in entry.get("hooks", []))
            ]
            event_hooks.append(hook_config)
            hooks[event] = event_hooks
        settings["hooks"] = hooks
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        print(f"  Hooks written to {settings_path}")

    # Configure MCP server (also PATH-relative)
    mcp_bin_abs = shutil.which("inscript-mcp")
    if not mcp_bin_abs:
        candidate = py_bin_dir / "inscript-mcp"
        if candidate.exists():
            mcp_bin_abs = str(candidate)
    if mcp_bin_abs:
        mcp_bin = mcp_bin_abs if in_venv else "inscript-mcp"
        mcp_json_path = Path.home() / ".claude" / ".mcp.json"
        mcp_config: dict = {}
        if mcp_json_path.exists():
            try:
                mcp_config = json.loads(mcp_json_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        mcp_servers = mcp_config.setdefault("mcpServers", {})
        if "inscript" not in mcp_servers:
            mcp_servers["inscript"] = {"type": "stdio", "command": mcp_bin}
            mcp_json_path.parent.mkdir(parents=True, exist_ok=True)
            mcp_json_path.write_text(json.dumps(mcp_config, indent=2) + "\n")
            print(f"  MCP server configured in {mcp_json_path}")
        else:
            print("  MCP server already configured.")

    # Write default config if none exists
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text("""[retention]
policy = "30d"
max_storage = "1GB"
store_diffs = true

[overlap]
enabled = true
""")
        print(f"  Config written to {CONFIG_FILE}")

    print()
    print("Done. Inscript will now record all Claude Code sessions.")
    print("  Agents can use inscript tools: replay, log, sessions, status, message, file_history")
    print("  Humans can run: inscript explore, inscript viz")


def _cmd_status():
    proj = active_project()
    sess = active_session()
    print(f"Project: {proj or 'none'}")
    if sess:
        sdir = session_dir(sess)
        meta_file = sdir / "meta.json"
        if meta_file.exists():
            meta = json.loads(meta_file.read_text())
            touches_file = sdir / "touches.jsonl"
            touch_count = sum(1 for _ in touches_file.open()) if touches_file.exists() else 0
            print(f"Session: {sess} (started {meta.get('start_time', '?')}, {touch_count} touches)")
        else:
            print(f"Session: {sess}")
    else:
        print("Session: none")
    others = [s for s in active_sessions() if s.get("session_id") != sess]
    if others:
        print(f"\nOther active sessions: {len(others)}")
        for s in others[:5]:
            print(f"  {s['session_id']} — {s.get('project', '?')}")


def _cmd_log(session_id: str | None):
    from .replay import generate_log as _gen
    if session_id is None:
        session_id = active_session()
    if session_id is None:
        sessions = list_sessions()
        if not sessions:
            print("No sessions found", file=__import__("sys").stderr)
            return
        session_id = sessions[0]["session_id"]
    result = _gen(session_id)
    if result:
        print(result)
    else:
        print(f"No activity log for {session_id}", file=__import__("sys").stderr)


def _cmd_replay(session_id: str | None):
    from .replay import generate_replay as _gen
    if session_id is None:
        current = active_session()
        sessions = list_sessions()
        for s in sessions:
            sid = s["session_id"]
            if sid == current:
                continue
            sdir = session_dir(sid)
            if (sdir / "summary.json").exists():
                session_id = sid
                break
        if session_id is None:
            session_id = current
    if session_id is None:
        print("No sessions found", file=__import__("sys").stderr)
        return
    result = _gen(session_id)
    if result:
        print(result)
    else:
        print(f"No replay data for {session_id}", file=__import__("sys").stderr)


def _cmd_concepts(file_query: str | None):
    from .concepts import detect_concepts, format_concepts, concept_for_file, concept_history

    concepts = detect_concepts()
    if file_query:
        c = concept_for_file(file_query, concepts)
        if c:
            print(concept_history(c))
        else:
            print(f"No concept found for '{file_query}'")
    else:
        proj = active_project()
        print(format_concepts(concepts, str(proj) if proj else None))


def _cmd_notes(argv: list[str]):
    from .commands import cmd_notes
    session_id = None
    page = 0
    i = 0
    while i < len(argv):
        if argv[i] == "--older":
            page += 1
            i += 1
        elif session_id is None:
            session_id = argv[i]
            i += 1
        else:
            i += 1
    cmd_notes(session_id, page=page)


def _cmd_note(argv: list[str]):
    from .commands import cmd_note
    text = None
    ref = None
    i = 0
    while i < len(argv):
        if argv[i] == "--ref" and i + 1 < len(argv):
            ref = argv[i + 1]
            i += 2
        elif text is None:
            text = argv[i]
            i += 1
        else:
            i += 1
    if not text:
        import sys
        print('Usage: inscript note "your thought" [--ref file.md]', file=sys.stderr)
        sys.exit(1)
    cmd_note(text, ref)


def _cmd_categories(session_id: str | None):
    from .categories import format_session_categories, format_workflow_patterns

    if session_id == "all":
        print(format_workflow_patterns())
    else:
        if session_id is None:
            session_id = active_session()
        if session_id is None:
            sessions = list_sessions()
            if not sessions:
                print("No sessions found", file=__import__("sys").stderr)
                return
            session_id = sessions[0]["session_id"]
        print(format_session_categories(session_id))
