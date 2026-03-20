"""Inscript — universal agent activity ledger.

Records what AI agents do: which project they're in, what files they
touch, what they change. Any tool reads ~/.inscript/ for context.

Usage:
    from inscript_pkg import active_project, active_session

    project = active_project()   # Path or None
    session = active_session()   # session ID or None
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.8.0"

INSCRIPT_DIR = Path.home() / ".inscript"
ACTIVE_PROJECT_FILE = INSCRIPT_DIR / "active_project"
ACTIVE_SESSION_FILE = INSCRIPT_DIR / "active_session"
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
        import tomllib
        return tomllib.loads(CONFIG_FILE.read_text())
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
    try:
        return ACTIVE_SESSION_FILE.read_text().strip() or None
    except OSError:
        return None


def session_dir(session_id: str) -> Path:
    return SESSIONS_DIR / session_id


def _rel_path(file_path: str, project: str | None) -> str:
    """Relativize a file path against the project root for display."""
    if project and file_path.startswith(project):
        rel = file_path[len(project):]
        if rel.startswith("/"):
            rel = rel[1:]
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
# CLI
# ---------------------------------------------------------------------------

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
        "overlap": _cmd_overlap,
        "cleanup": _cmd_cleanup,
        "export": lambda: _cmd_export(args[1]) if len(args) > 1 else print("Usage: inscript export <session-id>", file=sys.stderr),
        "set": lambda: (set_active_project(args[1]), print(f"Active project: {Path(args[1]).resolve()}")) if len(args) > 1 else print("Usage: inscript set <path>", file=sys.stderr),
        "tag": lambda: _cmd_tag(args[1] if len(args) > 1 else None),
        "untag": lambda: _cmd_tag(None),
        "time": lambda: _cmd_time(args[1] if len(args) > 1 else None),
        "branch": lambda: _cmd_branch(args[1] if len(args) > 1 else None),
        "resume": _cmd_resume,
        "branches": _cmd_branches,
        "viz": lambda: _dispatch_viz(args[1:]),
        "map": lambda: _dispatch_viz(args[1:]),
        "explore": lambda: _cmd_explore(args[1] if len(args) > 1 else None),
        "dashboard": lambda: _cmd_dashboard(),
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
  inscript setup        Configure Claude Code hooks (run this first)
  inscript              Show status
  inscript log [id]     Activity log for a session (latest if omitted)
  inscript replay [id]  Context summary for handoff to next session
  inscript explore [id] Interactive timeline explorer (arrow keys)
  inscript viz [id]     Visual session map (files × prompts heatmap)
  inscript tag <name>   Tag current work with a feature/task name
  inscript untag        Clear the current tag
  inscript time [tag]   Show time spent, optionally filtered by tag
  inscript branch "why" Start a scoped detour (debugging, refactoring, etc.)
  inscript resume       End the current branch, return to trunk
  inscript branches     Show branches for the current session
  inscript overlap      File collisions across concurrent sessions
  inscript export <id>  Export session as markdown
  inscript cleanup      Enforce retention policy
  inscript init         Advanced: configure retention and storage
""")


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
    """One-command setup: configure Claude Code hooks for inscript."""
    import shutil
    import sys

    INSCRIPT_DIR.mkdir(parents=True, exist_ok=True)

    # Find the inscript-hook binary
    hook_bin = shutil.which("inscript-hook")
    if not hook_bin:
        # Look next to the current Python executable (works inside venvs)
        py_bin_dir = Path(sys.executable).parent
        candidate = py_bin_dir / "inscript-hook"
        if candidate.exists():
            hook_bin = str(candidate)
    if not hook_bin:
        # Fall back to looking next to the inscript binary
        inscript_bin = shutil.which("inscript")
        if inscript_bin:
            candidate = Path(inscript_bin).parent / "inscript-hook"
            if candidate.exists():
                hook_bin = str(candidate)

    if not hook_bin:
        print("Error: inscript-hook not found on PATH.", file=sys.stderr)
        print("Make sure inscript is installed: pip install inscript", file=sys.stderr)
        return

    print(f"inscript setup")
    print(f"  hook binary: {hook_bin}")
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

    # Define the hooks inscript needs
    inscript_hooks = {
        "SessionStart": {
            "hooks": [{"type": "command", "command": hook_bin}]
        },
        "UserPromptSubmit": {
            "hooks": [{"type": "command", "command": hook_bin}]
        },
        "PostToolUse": {
            "matcher": "Read|Edit|Write|Glob|Grep",
            "hooks": [{"type": "command", "command": hook_bin, "async": True}]
        },
        "Stop": {
            "hooks": [{"type": "command", "command": hook_bin}]
        },
    }

    # Check for existing inscript hooks
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
        # Add inscript hooks (preserve existing non-inscript hooks)
        for event, hook_config in inscript_hooks.items():
            event_hooks = hooks.get(event, [])
            # Remove any existing inscript hooks
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

    # Configure MCP server
    mcp_bin = shutil.which("inscript-mcp")
    if not mcp_bin:
        candidate = py_bin_dir / "inscript-mcp"
        if candidate.exists():
            mcp_bin = str(candidate)

    if mcp_bin:
        mcp_json_path = Path.home() / ".claude" / ".mcp.json"
        mcp_config: dict = {}
        if mcp_json_path.exists():
            try:
                mcp_config = json.loads(mcp_json_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        mcp_servers = mcp_config.setdefault("mcpServers", {})
        if "inscript" not in mcp_servers:
            mcp_servers["inscript"] = {
                "type": "stdio",
                "command": mcp_bin,
            }
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
    print("  Agents can use inscript tools: replay, log, sessions, status")
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
# Branch inference — purely behavioral, no text matching
# ---------------------------------------------------------------------------


def _is_bookkeeping(f: str) -> bool:
    """Paths that are operational overhead, not real work focus."""
    home = str(Path.home())
    return f.startswith(f"{home}/.claude/") or f.startswith(f"{home}/.inscript/")


def _touch_focus(touches: list[dict]) -> set[str]:
    """Extract focus set (project roots or parent dirs) from touches.

    Excludes bookkeeping paths (~/.claude/, ~/.inscript/) — these are
    operational overhead and shouldn't influence focus/detour detection.
    """
    focus = set()
    for t in touches:
        f = t.get("file", "")
        if _is_bookkeeping(f):
            continue
        proj = t.get("project")
        if proj:
            focus.add(proj)
            continue
        if not f:
            continue
        parent = f.rsplit("/", 1)[0] if "/" in f else f
        parts = parent.split("/")
        if len(parts) > 3:
            focus.add("/".join(parts[:4]))
        elif parts:
            focus.add(parent)
    return focus


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def infer_branches(
    prompts: list[dict],
    touches_by_prompt: dict[int | None, list[dict]],
) -> list[dict]:
    """Infer branches purely from behavioral patterns in touch data.

    No text matching. Structure is derived from what the agent did:
    - Edits establish the trunk (building).
    - Reads in the same area are normal investigation.
    - Activity in a *different* area that ends with a return = detour.
    - A permanent shift is a task change, not a detour.

    Returns list of:
        {"start_idx": int, "end_idx": int, "reason": str, "type": "inferred"}
    """
    if len(prompts) < 3:
        return []

    # Step 1: per-prompt behavioral metadata
    prompt_meta: list[dict] = []
    for p in prompts:
        idx = p.get("idx", 0)
        pts = touches_by_prompt.get(idx, [])
        # Filter bookkeeping for behavioral analysis
        work_pts = [t for t in pts if not _is_bookkeeping(t.get("file", ""))]
        focus = _touch_focus(work_pts)
        has_edits = any(t.get("action") in ("edit", "write") for t in work_pts)
        edit_focus = _touch_focus(
            [t for t in work_pts if t.get("action") in ("edit", "write")]
        )
        prompt_meta.append({
            "idx": idx,
            "focus": focus,
            "edit_focus": edit_focus,
            "has_edits": has_edits,
            "has_touches": len(work_pts) > 0,
        })

    # Step 2: build trunk from the first contiguous block of overlapping edits.
    # Stop absorbing when focus shifts — the shift might be a detour.
    trunk: set[str] = set()
    trunk_edit_count = 0
    for pm in prompt_meta:
        if pm["edit_focus"]:
            if not trunk:
                trunk.update(pm["edit_focus"])
                trunk_edit_count += 1
            elif _jaccard(pm["edit_focus"], trunk) >= 0.5:
                trunk.update(pm["edit_focus"])
                trunk_edit_count += 1
            else:
                break  # focus shifted — stop building trunk

    # If trunk was established from only 1 edit, validate against the majority.
    # A single warm-up edit shouldn't define the trunk for the whole session.
    if trunk_edit_count == 1:
        # Count total edits per focus area across the session
        area_edits: dict[str, int] = {}
        for pm in prompt_meta:
            for area in pm["edit_focus"]:
                area_edits[area] = area_edits.get(area, 0) + 1
        if area_edits:
            majority_area = max(area_edits, key=lambda a: area_edits[a])
            majority_count = area_edits[majority_area]
            # If another area has significantly more edits, use that as trunk
            trunk_area = next(iter(trunk))  # the single-edit trunk area
            if majority_area != trunk_area and majority_count >= 3:
                trunk = {majority_area}

    # Fallback: if no edits at all, use contiguous reads
    if not trunk:
        for pm in prompt_meta:
            if pm["focus"]:
                if not trunk:
                    trunk.update(pm["focus"])
                elif _jaccard(pm["focus"], trunk) >= 0.5:
                    trunk.update(pm["focus"])
                else:
                    break

    if not trunk:
        return []

    # Step 3: single-pass classification + detection with evolving trunk.
    # Trunk evolves as we go so that legitimate work expansion isn't flagged.
    # Don't flag detours until we've seen the first trunk prompt — the session
    # start is "establishing", not "detouring".
    branches: list[dict] = []
    in_shift = False
    shift_start = 0
    trunk_seen = False

    for i, pm in enumerate(prompt_meta):
        if not pm["has_touches"]:
            continue  # idle — doesn't change classification

        # Classify against current trunk
        if pm["has_edits"]:
            sim = _jaccard(pm["edit_focus"], trunk)
        else:
            sim = _jaccard(pm["focus"], trunk)

        is_trunk = sim >= 0.5

        if is_trunk:
            trunk_seen = True
            if in_shift:
                # Shift ended — agent returned to trunk. Record confirmed detour.
                end_i = i - 1
                while end_i > shift_start and not prompt_meta[end_i]["has_touches"]:
                    end_i -= 1
                branches.append(
                    _make_inferred_branch(prompt_meta, trunk, shift_start, end_i)
                )
                in_shift = False
            # Evolve trunk with current focus
            if pm["has_edits"]:
                trunk.update(pm["edit_focus"])
        else:
            # Only start tracking shifts after we've seen trunk work
            if trunk_seen and not in_shift:
                in_shift = True
                shift_start = i

    # Open shift at end of session = permanent task change, NOT a detour
    return branches


def _make_inferred_branch(
    prompt_meta: list[dict], trunk: set[str], start_i: int, end_i: int,
) -> dict:
    """Describe an inferred branch by what areas it touched outside trunk."""
    branch_focus: set[str] = set()
    for pm in prompt_meta[start_i:end_i + 1]:
        branch_focus.update(pm["focus"])

    new_areas = branch_focus - trunk
    if new_areas:
        dirs = sorted(f.split("/")[-1] for f in new_areas)
        reason = f"shifted to {', '.join(dirs)}"
    elif branch_focus:
        dirs = sorted(f.split("/")[-1] for f in branch_focus)
        reason = f"detour in {', '.join(dirs)}"
    else:
        reason = "detour"

    return {
        "start_idx": prompt_meta[start_i]["idx"],
        "end_idx": prompt_meta[end_i]["idx"],
        "reason": reason,
        "type": "inferred",
    }


# ---------------------------------------------------------------------------
# Decision point detection — structural, not semantic
# ---------------------------------------------------------------------------


def detect_decision_points(
    prompts: list[dict],
    responses: dict[int, list[tuple[str, str]]],
) -> list[dict]:
    """Detect prompts where the assistant presented options and asked to choose.

    Structural detection only:
    - Response contains 2+ list items (numbered or bold-bullet)
    - Last text block ends with a question

    Returns list of:
        {"idx": int, "options": list[str], "chosen": str}
    """
    decisions: list[dict] = []

    for p in prompts:
        idx = p.get("idx", 0)
        parts = responses.get(idx, [])
        if not parts:
            continue

        text_parts = [text for ptype, text in parts if ptype == "text"]
        if not text_parts:
            continue

        full_text = "\n".join(text_parts)
        lines = full_text.split("\n")

        # Count list items
        numbered_lines = [l.strip() for l in lines
                          if len(l.strip()) > 3 and l.strip()[0].isdigit() and l.strip()[1] in (".", ")")]
        bold_bullet_lines = [l.strip() for l in lines if l.strip().startswith("- **")]

        option_lines = numbered_lines if len(numbered_lines) >= 2 else bold_bullet_lines
        if len(option_lines) < 2:
            continue

        # Question in the last text block's final paragraph
        last_text = text_parts[-1].strip()
        last_line = last_text.split("\n")[-1].strip() if last_text else ""
        if not last_line.endswith("?"):
            continue

        # Extract option summaries (first ~60 chars of each)
        options = []
        for ol in option_lines:
            # Strip the prefix (1., 2., - **)
            clean = ol
            if clean[:2] in ("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9."):
                clean = clean[2:].strip()
            elif clean.startswith("- **"):
                clean = clean[2:].strip()
            if len(clean) > 60:
                clean = clean[:57] + "..."
            options.append(clean)

        # What did the user pick? (next prompt)
        chosen = ""
        for np in prompts:
            if np.get("idx") == idx + 1:
                chosen = np.get("prompt", "")
                if len(chosen) > 60:
                    chosen = chosen[:57] + "..."
                break

        decisions.append({
            "idx": idx,
            "options": options,
            "chosen": chosen,
        })

    return decisions


def generate_log(session_id: str) -> str | None:
    """Generate activity log for a session. Returns string or None."""
    sdir = session_dir(session_id)
    touches_file = sdir / "touches.jsonl"
    if not touches_file.exists():
        return None

    project = None
    meta_file = sdir / "meta.json"
    if meta_file.exists():
        try:
            project = json.loads(meta_file.read_text()).get("project")
        except (json.JSONDecodeError, OSError):
            pass

    prompts = _load_prompts(sdir)
    out_lines: list[str] = []

    touches_by_prompt: dict[int | None, list[dict]] = {}
    files_seen = set()
    edits = 0
    for line in touches_file.open():
        try:
            e = json.loads(line)
            pidx = e.get("prompt_idx")
            touches_by_prompt.setdefault(pidx, []).append(e)
            files_seen.add(e.get("file"))
            if e.get("action") in ("edit", "write"):
                edits += 1
        except json.JSONDecodeError:
            pass

    out_lines.append(f"Session: {session_id}\n")

    if prompts:
        for p in prompts:
            idx = p.get("idx", 0)
            prompt_text = p.get("prompt", "")
            if len(prompt_text) > 80:
                prompt_text = prompt_text[:77] + "..."
            out_lines.append(f"  [{p.get('ts', '?')}] \"{prompt_text}\"")
            for t in touches_by_prompt.get(idx, []):
                extra = f" ({t['lines_changed']} lines)" if t.get("lines_changed") else ""
                out_lines.append(f"    {t.get('action', '?'):6s}  {_rel_path(t.get('file', '?'), project)}{extra}")
            out_lines.append("")
    else:
        for line in touches_file.open():
            try:
                e = json.loads(line)
                extra = f" ({e['lines_changed']} lines)" if e.get("lines_changed") else ""
                out_lines.append(f"  {e.get('ts', '?')}  {e.get('action', '?'):6s}  {_rel_path(e.get('file', '?'), project)}{extra}")
            except json.JSONDecodeError:
                pass

    summary_file = sdir / "summary.json"
    tokens = None
    if summary_file.exists():
        try:
            s = json.loads(summary_file.read_text())
            tokens = s.get("tokens")
        except (json.JSONDecodeError, OSError):
            pass

    out_lines.append(f"  {len(files_seen)} files, {edits} edits, {len(prompts)} prompts")

    if tokens:
        inp = tokens.get("input_tokens", 0)
        out_ = tokens.get("output_tokens", 0)
        total = tokens.get("total_tokens", 0)
        cache_r = tokens.get("cache_read_tokens", 0)
        cache_w = tokens.get("cache_write_tokens", 0)
        model = tokens.get("model", "?")
        tline = f"  {_format_tokens(total)} tokens ({_format_tokens(inp)} in, {_format_tokens(out_)} out)"
        if cache_r or cache_w:
            tline += f" · cache {_format_tokens(cache_r)} read, {_format_tokens(cache_w)} write"
        tline += f" [{model}]"
        out_lines.append(tline)

    warnings_file = sdir / "warnings.jsonl"
    if warnings_file.exists():
        warnings = []
        for line in warnings_file.open():
            try:
                warnings.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        if warnings:
            out_lines.append("")
            for w in warnings:
                out_lines.append(f"  ⚠ {w.get('message', '?')} ({w.get('ts', '')})")

    return "\n".join(out_lines)


def _cmd_log(session_id: str | None):
    if session_id is None:
        session_id = active_session()
    if session_id is None:
        sessions = list_sessions()
        if not sessions:
            print("No sessions found", file=__import__("sys").stderr)
            return
        session_id = sessions[0]["session_id"]

    result = generate_log(session_id)
    if result:
        print(result)
    else:
        print(f"No activity log for {session_id}", file=__import__("sys").stderr)


def generate_replay(session_id: str) -> str | None:
    """Generate a compact replay summary for a session. Returns string or None."""
    sdir = session_dir(session_id)
    meta_file = sdir / "meta.json"
    if not meta_file.exists():
        return None

    meta = json.loads(meta_file.read_text())
    project = meta.get("project")
    prompts = _load_prompts(sdir)
    touches = _load_jsonl(sdir / "touches.jsonl")
    branches = _load_jsonl(sdir / "branches.jsonl")

    if not prompts:
        return None

    summary = {}
    summary_file = sdir / "summary.json"
    if summary_file.exists():
        try:
            summary = json.loads(summary_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    lines: list[str] = []

    # Header
    duration_str = ""
    if summary.get("duration_seconds"):
        duration_str = f"{_format_duration(summary['duration_seconds'])}, "
    total_edits = summary.get("total_edits", 0)
    lines.append(f"## Previous Session ({duration_str}{total_edits} edits, {len(prompts)} prompts)\n")

    # Tags
    tags_used = sorted({p.get("tag") for p in prompts if p.get("tag")})
    if tags_used:
        lines.append("### Tags")
        for tag in tags_used:
            tagged_prompts = [p for p in prompts if p.get("tag") == tag]
            idxs = [p.get("idx", 0) for p in tagged_prompts]
            if len(idxs) == 1:
                lines.append(f"- {tag} (prompt {idxs[0]})")
            else:
                lines.append(f"- {tag} (prompts {idxs[0]}-{idxs[-1]})")
        lines.append("")

    # Group touches
    touches_by_prompt: dict[int | None, list[dict]] = {}
    for t in touches:
        touches_by_prompt.setdefault(t.get("prompt_idx"), []).append(t)

    # Branch lookup
    branch_map: dict[int, dict] = {}
    for b in branches:
        bid = b.get("id")
        if bid is not None:
            if b.get("type") == "open":
                branch_map[bid] = b
            elif b.get("type") == "close" and bid in branch_map:
                branch_map[bid]["closed"] = True

    inferred = infer_branches(prompts, touches_by_prompt)

    prompt_annotation: dict[int, str] = {}
    for ib in inferred:
        for idx in range(ib["start_idx"], ib["end_idx"] + 1):
            prompt_annotation[idx] = f"detour: {ib['reason']}"
    for p in prompts:
        bid = p.get("branch_id")
        if bid is not None and bid in branch_map:
            prompt_annotation[p.get("idx", 0)] = branch_map[bid].get("reason", "branch")

    # Prompts
    if prompts:
        lines.append("### Prompts")
        for i, p in enumerate(prompts):
            idx = p.get("idx", 0)
            prompt_text = p.get("prompt", "")
            if len(prompt_text) > 80:
                prompt_text = prompt_text[:77] + "..."

            branch_prefix = ""
            annotation = prompt_annotation.get(idx)
            if annotation:
                branch_prefix = f" [{annotation}]"

            prompt_touches = touches_by_prompt.get(idx, [])
            file_actions: dict[str, list[str]] = {}
            for t in prompt_touches:
                f = _rel_path(t.get("file", "?"), project)
                action = t.get("action", "?")
                detail = ""
                if t.get("lines_changed"):
                    detail = f" (+{t['lines_changed']})"
                elif t.get("lines"):
                    detail = f" ({t['lines']} lines)"
                file_actions.setdefault(f, []).append(f"{action}{detail}")

            touch_parts = []
            for f, actions in file_actions.items():
                unique = []
                seen = set()
                for a in actions:
                    if a not in seen:
                        unique.append(a)
                        seen.add(a)
                touch_parts.append(f"{', '.join(unique)} {f}")

            touch_str = " -> " + "; ".join(touch_parts) if touch_parts else ""
            lines.append(f"{idx + 1}. \"{prompt_text}\"{branch_prefix}{touch_str}")

        last_idx = prompts[-1].get("idx", 0)
        last_touches = touches_by_prompt.get(last_idx, [])
        has_edits = any(t.get("action") in ("edit", "write") for t in last_touches)
        if not has_edits and last_touches:
            lines.append(f"   ^ reads only -- may be unfinished")
        elif not last_touches:
            lines.append(f"   ^ no file activity -- may be unfinished")
        lines.append("")

    # Files modified
    file_edits: dict[str, int] = {}
    file_new: set[str] = set()
    for t in touches:
        if t.get("action") in ("edit", "write"):
            f = _rel_path(t.get("file", "?"), project)
            file_edits[f] = file_edits.get(f, 0) + 1
            if t.get("action") == "write":
                file_new.add(f)

    if file_edits:
        lines.append("### Files modified")
        for f, count in sorted(file_edits.items(), key=lambda x: -x[1]):
            new_marker = ", new file" if f in file_new else ""
            lines.append(f"- {f} ({count} edits{new_marker})")
        lines.append("")

    if inferred:
        lines.append("### Detected detours")
        for ib in inferred:
            span = f"prompt {ib['start_idx'] + 1}"
            if ib["start_idx"] != ib["end_idx"]:
                span = f"prompts {ib['start_idx'] + 1}-{ib['end_idx'] + 1}"
            lines.append(f"- {ib['reason']} ({span})")
        lines.append("")

    open_branches = [b for bid, b in branch_map.items() if not b.get("closed")]
    if open_branches:
        lines.append("### Open branches (not resumed)")
        for b in open_branches:
            lines.append(f"- {b.get('reason', '?')} (opened prompt {b.get('prompt_idx', '?')})")
        lines.append("")

    tokens = summary.get("tokens")
    if tokens:
        total = tokens.get("total_tokens", 0)
        model = tokens.get("model", "?")
        lines.append(f"*{_format_tokens(total)} tokens [{model}]*")

    return "\n".join(lines)


def _cmd_replay(session_id: str | None):
    """Produce a compact context summary for handoff to the next session."""
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

    result = generate_replay(session_id)
    if result:
        print(result)
    else:
        print(f"No replay data for {session_id}", file=__import__("sys").stderr)


def _cmd_tag(tag_name: str | None):
    """Set or clear the active tag for the current session."""
    sess = active_session()
    if not sess:
        print("No active session", file=__import__("sys").stderr)
        __import__("sys").exit(1)

    sdir = session_dir(sess)
    tag_file = sdir / "active_tag"

    if tag_name is None:
        # Clear tag
        try:
            tag_file.unlink()
        except OSError:
            pass
        print("Tag cleared")
    else:
        tag_file.write_text(tag_name + "\n")
        print(f"Tagged: {tag_name}")


def _format_duration(seconds: int) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s"
    else:
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        return f"{h}h {m}m"


def _cmd_time(tag_filter: str | None):
    """Show time spent, optionally filtered by tag."""
    all_sessions = list_sessions()
    if not all_sessions:
        print("No sessions found")
        return

    # Collect timing data from prompts across all sessions
    tag_data: dict[str | None, dict] = {}  # tag -> {sessions, prompts, first_ts, last_ts, active_seconds, files, edits}

    for s in all_sessions:
        sid = s["session_id"]
        sdir = session_dir(sid)
        prompts = _load_prompts(sdir)
        touches_file = sdir / "touches.jsonl"

        # Load touches
        touches: list[dict] = []
        if touches_file.exists():
            for line in touches_file.open():
                try:
                    touches.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        if not prompts:
            continue

        # Compute time per prompt block
        for i, p in enumerate(prompts):
            tag = p.get("tag")
            if tag_filter and tag != tag_filter:
                continue

            # Parse this prompt's timestamp
            prompt_ts = p.get("ts", "")

            # Find next prompt's timestamp (or session end) for duration
            if i + 1 < len(prompts):
                next_ts = prompts[i + 1].get("ts", "")
            else:
                # Use last touch timestamp or summary end_time
                summary_file = sdir / "summary.json"
                if summary_file.exists():
                    try:
                        summary = json.loads(summary_file.read_text())
                        end = summary.get("end_time", "")
                        next_ts = end.split("T")[-1] if "T" in end else ""
                    except (json.JSONDecodeError, OSError):
                        next_ts = ""
                else:
                    # Use last touch
                    block_touches = [t for t in touches if t.get("prompt_idx") == p.get("idx")]
                    next_ts = block_touches[-1].get("ts", "") if block_touches else ""

            # Compute duration for this prompt block
            block_seconds = _ts_diff(prompt_ts, next_ts)

            # Count files and edits for this block
            block_touches = [t for t in touches if t.get("prompt_idx") == p.get("idx")]
            block_files = {t.get("file") for t in block_touches}
            block_edits = sum(1 for t in block_touches if t.get("action") in ("edit", "write"))

            # Aggregate by tag
            if tag not in tag_data:
                tag_data[tag] = {
                    "sessions": set(),
                    "prompts": 0,
                    "active_seconds": 0,
                    "first_ts": s.get("start_time", ""),
                    "last_ts": s.get("start_time", ""),
                    "files": set(),
                    "edits": 0,
                    "prompt_texts": [],
                }

            td = tag_data[tag]
            td["sessions"].add(sid)
            td["prompts"] += 1
            td["active_seconds"] += block_seconds
            td["files"].update(block_files)
            td["edits"] += block_edits
            td["prompt_texts"].append(p.get("prompt", ""))
            # Track first/last
            session_time = s.get("start_time", "")
            if session_time < td["first_ts"] or not td["first_ts"]:
                td["first_ts"] = session_time
            if session_time > td["last_ts"]:
                td["last_ts"] = session_time

    if not tag_data:
        if tag_filter:
            print(f"No data for tag: {tag_filter}")
        else:
            print("No prompt data found")
        return

    # Display
    if tag_filter:
        # Single tag detail view
        td = tag_data.get(tag_filter)
        if not td:
            print(f"No data for tag: {tag_filter}")
            return
        print(f"Feature: {tag_filter}\n")
        print(f"  Sessions:     {len(td['sessions'])}")
        print(f"  Prompts:      {td['prompts']}")
        print(f"  Active time:  {_format_duration(td['active_seconds'])}")
        print(f"  Files:        {len(td['files'])}")
        print(f"  Edits:        {td['edits']}")
        print(f"  First:        {td['first_ts']}")
        print(f"  Last:         {td['last_ts']}")
        print(f"\n  Prompts:")
        for pt in td["prompt_texts"]:
            display = pt[:70] + "..." if len(pt) > 70 else pt
            print(f"    - \"{display}\"")
    else:
        # Overview of all tags
        print("Time by tag:\n")
        # Sort: tagged first (alphabetical), then untagged
        sorted_tags = sorted(
            tag_data.keys(),
            key=lambda t: (t is None, t or "")
        )
        for tag in sorted_tags:
            td = tag_data[tag]
            label = tag or "(untagged)"
            print(f"  {label}")
            print(f"    {_format_duration(td['active_seconds'])} active, {td['prompts']} prompts, {td['edits']} edits, {len(td['sessions'])} sessions")
            print()


def _cmd_branch(reason: str | None):
    """Open a scoped detour branch."""
    import sys as _sys
    if not reason:
        print("Usage: inscript branch \"reason for detour\"", file=_sys.stderr)
        _sys.exit(1)

    sess = active_session()
    if not sess:
        print("No active session", file=_sys.stderr)
        _sys.exit(1)

    sdir = session_dir(sess)

    # Check for existing open branch (nesting)
    parent = None
    active_branch_file = sdir / "active_branch"
    try:
        text = active_branch_file.read_text().strip()
        if text:
            parent = int(text)
    except (OSError, ValueError):
        pass

    # Determine next branch ID
    branches = _load_jsonl(sdir / "branches.jsonl")
    next_id = max((b.get("id", -1) for b in branches), default=-1) + 1

    # Get current prompt idx
    prompts = _load_prompts(sdir)
    prompt_idx = prompts[-1].get("idx") if prompts else None

    # Write branch open event
    entry = {
        "type": "open",
        "id": next_id,
        "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "reason": reason,
    }
    if prompt_idx is not None:
        entry["prompt_idx"] = prompt_idx
    if parent is not None:
        entry["parent"] = parent

    _append_jsonl(sdir / "branches.jsonl", entry)

    # Set active branch
    active_branch_file.write_text(str(next_id) + "\n")

    if parent is not None:
        print(f"Branch #{next_id}: {reason} (nested in #{parent})")
    else:
        print(f"Branch #{next_id}: {reason}")


def _cmd_resume():
    """Close the current branch and return to trunk (or parent branch)."""
    import sys as _sys
    sess = active_session()
    if not sess:
        print("No active session", file=_sys.stderr)
        _sys.exit(1)

    sdir = session_dir(sess)
    active_branch_file = sdir / "active_branch"

    # Read current branch
    try:
        text = active_branch_file.read_text().strip()
        branch_id = int(text) if text else None
    except (OSError, ValueError):
        branch_id = None

    if branch_id is None:
        print("No active branch to resume from")
        return

    # Find the branch's parent
    branches = _load_jsonl(sdir / "branches.jsonl")
    parent = None
    reason = "?"
    for b in branches:
        if b.get("id") == branch_id and b.get("type") == "open":
            parent = b.get("parent")
            reason = b.get("reason", "?")
            break

    # Get current prompt idx
    prompts = _load_prompts(sdir)
    prompt_idx = prompts[-1].get("idx") if prompts else None

    # Write close event
    entry = {
        "type": "close",
        "id": branch_id,
        "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    }
    if prompt_idx is not None:
        entry["prompt_idx"] = prompt_idx

    _append_jsonl(sdir / "branches.jsonl", entry)

    # Restore parent branch or clear
    if parent is not None:
        active_branch_file.write_text(str(parent) + "\n")
        print(f"Closed branch #{branch_id} ({reason}), back to branch #{parent}")
    else:
        try:
            active_branch_file.unlink()
        except OSError:
            pass
        print(f"Closed branch #{branch_id} ({reason}), back to trunk")


def _cmd_branches():
    """Show branches for the current session."""
    sess = active_session()
    if not sess:
        sessions = list_sessions()
        if sessions:
            sess = sessions[0]["session_id"]
        else:
            print("No sessions found")
            return

    sdir = session_dir(sess)
    branches = _load_jsonl(sdir / "branches.jsonl")

    if not branches:
        print("No branches")
        return

    # Build branch state
    branch_info: dict[int, dict] = {}
    for b in branches:
        bid = b.get("id")
        if bid is None:
            continue
        if b.get("type") == "open":
            branch_info[bid] = {
                "reason": b.get("reason", "?"),
                "opened": b.get("ts", "?"),
                "prompt_idx": b.get("prompt_idx"),
                "parent": b.get("parent"),
                "closed": None,
            }
        elif b.get("type") == "close" and bid in branch_info:
            branch_info[bid]["closed"] = b.get("ts", "?")

    # Check which is active
    active_bid = None
    try:
        text = (sdir / "active_branch").read_text().strip()
        active_bid = int(text) if text else None
    except (OSError, ValueError):
        pass

    # Count touches per branch
    touches = _load_jsonl(sdir / "touches.jsonl")
    branch_touches: dict[int, int] = {}
    branch_edits: dict[int, int] = {}
    for t in touches:
        bid = t.get("branch_id")
        if bid is not None:
            branch_touches[bid] = branch_touches.get(bid, 0) + 1
            if t.get("action") in ("edit", "write"):
                branch_edits[bid] = branch_edits.get(bid, 0) + 1

    print(f"Session: {sess}\n")
    for bid in sorted(branch_info):
        info = branch_info[bid]
        indent = ""
        if info["parent"] is not None:
            indent = "  "

        status = "open" if info["closed"] is None else f"closed {info['closed']}"
        if bid == active_bid:
            status = "active"

        tc = branch_touches.get(bid, 0)
        ec = branch_edits.get(bid, 0)
        print(f"{indent}#{bid} {info['reason']}")
        print(f"{indent}  {info['opened']} → {status}, {tc} touches, {ec} edits")
    print()


def _append_jsonl(path: Path, data: dict) -> None:
    """Append a JSON line to a file (CLI-side helper, mirrors hook version)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(data, default=str) + "\n")


def _ts_to_secs(ts: str) -> int:
    """Convert HH:MM:SS to seconds since midnight."""
    parts = [int(x) for x in ts.split(":")]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _ts_diff(ts1: str, ts2: str) -> int:
    """Compute seconds between two HH:MM:SS timestamps. Handles midnight crossing."""
    try:
        s1 = _ts_to_secs(ts1)
        s2 = _ts_to_secs(ts2)
        diff = s2 - s1
        if diff < 0:
            diff += 86400  # midnight crossing
        return diff
    except (ValueError, IndexError):
        return 0


def compute_prompt_durations(
    prompts: list[dict],
    touches_by_prompt: dict[int | None, list[dict]],
) -> list[dict]:
    """Compute temporal breakdown for each prompt.

    Returns list of dicts with:
        idx: prompt index
        total: seconds from this prompt to next (or None for last)
        think: seconds from prompt to first touch
        work: seconds from first touch to last touch
        idle: seconds from last touch to next prompt
        touches: number of file touches
    """
    results = []
    for i, p in enumerate(prompts):
        idx = p.get("idx", 0)
        ts = p.get("ts", "")
        pts = touches_by_prompt.get(idx, [])

        # Total duration: time to next prompt
        total = None
        if i + 1 < len(prompts):
            next_ts = prompts[i + 1].get("ts", "")
            if ts and next_ts:
                total = _ts_diff(ts, next_ts)

        entry = {"idx": idx, "total": total, "touches": len(pts)}

        if pts and ts:
            touch_times = [t.get("ts", "") for t in pts if t.get("ts")]
            if touch_times:
                first = touch_times[0]
                last = touch_times[-1]
                think = _ts_diff(ts, first)
                work = _ts_diff(first, last)
                idle = _ts_diff(last, prompts[i + 1]["ts"]) if total and i + 1 < len(prompts) else 0
                entry.update({"think": think, "work": work, "idle": idle})
            else:
                entry.update({"think": 0, "work": 0, "idle": total or 0})
        else:
            entry.update({"think": 0, "work": 0, "idle": total or 0})

        results.append(entry)
    return results


def _format_secs(s: int) -> str:
    """Compact duration: 5s, 1m30s, 12m."""
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if sec == 0:
        return f"{m}m"
    return f"{m}m{sec}s"


def _cmd_overlap():
    if not OVERLAP_DIR.exists():
        print("No overlap data")
        return
    for f in sorted(OVERLAP_DIR.iterdir()):
        if f.suffix != ".jsonl":
            continue
        entries = []
        for line in f.open():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        if entries:
            print(f"Project: {entries[0].get('project', f.stem)}")
            for e in entries[-10:]:
                print(f"  {e.get('ts', '?')}  {e.get('file', '?')}  sessions: {e.get('sessions', [])}")
            print()


def _cmd_cleanup():
    config = _load_config()
    policy = config.get("retention", {}).get("policy", "30d")
    if policy == "forever":
        print("Retention: forever. Nothing to clean.")
        return

    days = 30
    if policy.endswith("d"):
        try:
            days = int(policy[:-1])
        except ValueError:
            pass

    cutoff = time.time() - (days * 86400)
    removed = 0
    if SESSIONS_DIR.exists():
        import shutil
        for d in list(SESSIONS_DIR.iterdir()):
            try:
                if d.stat().st_mtime < cutoff:
                    shutil.rmtree(d)
                    removed += 1
            except OSError:
                pass
    print(f"Removed {removed} sessions older than {days} days")


def _dispatch_viz(args: list[str]):
    """Route viz subcommands: overview, prompt drill-down, file drill-down."""
    import sys as _sys

    # Parse args: viz [--file <path> | <prompt_num> | <session_id>]
    file_filter = None
    prompt_filter = None
    session_id = None

    i = 0
    while i < len(args):
        if args[i] == "--file" and i + 1 < len(args):
            file_filter = args[i + 1]
            i += 2
        elif args[i] == "--detours":
            prompt_filter = "detours"
            i += 1
        elif ":" in args[i]:
            # session_id:prompt_num format (from explore copy)
            parts = args[i].split(":", 1)
            session_id = parts[0]
            if parts[1].isdigit():
                prompt_filter = int(parts[1])
            i += 1
        elif args[i].isdigit():
            prompt_filter = int(args[i])
            i += 1
        else:
            session_id = args[i]
            i += 1

    ctx = _load_viz_context(session_id)
    if ctx is None:
        return

    if prompt_filter == "detours":
        _viz_detours(ctx)
    elif isinstance(prompt_filter, int):
        _viz_prompt(ctx, prompt_filter)
    elif file_filter:
        _viz_file(ctx, file_filter)
    else:
        _viz_overview(ctx)


# -- Viz context: shared data loaded once --

def _cmd_coupling():
    """Show files that are temporally coupled — always edited together."""
    from collections import Counter, defaultdict
    from itertools import combinations

    sessions = list_sessions()
    if not sessions:
        print("No sessions found")
        return

    home = str(Path.home())

    co_touches = Counter()
    file_edit_count = Counter()

    for s in sessions:
        sid = s["session_id"]
        sdir = session_dir(sid)
        touches = _load_jsonl(sdir / "touches.jsonl")

        edits_by_prompt: dict[int, set[str]] = defaultdict(set)
        for t in touches:
            if t.get("action") in ("edit", "write"):
                pidx = t.get("prompt_idx")
                f = t.get("file", "?")
                # Skip bookkeeping
                if _is_bookkeeping(f):
                    continue
                if pidx is not None:
                    edits_by_prompt[pidx].add(f)

        for pidx, files in edits_by_prompt.items():
            for f in files:
                file_edit_count[f] += 1
            for a, b in combinations(sorted(files), 2):
                co_touches[(a, b)] += 1

    # Compute coupling strength
    couplings = []
    for (a, b), count in co_touches.items():
        min_edits = min(file_edit_count[a], file_edit_count[b])
        if count < 2:
            continue  # need at least 2 co-occurrences
        strength = count / min_edits
        couplings.append({
            "a": _viz_short(a, home),
            "b": _viz_short(b, home),
            "co": count,
            "a_edits": file_edit_count[a],
            "b_edits": file_edit_count[b],
            "strength": strength,
        })

    couplings.sort(key=lambda c: (-c["strength"], -c["co"]))

    if not couplings:
        print("Not enough data yet (need 2+ co-occurrences)")
        return

    print(f"Co-touch coupling across {len(sessions)} sessions")
    print(f"{len(file_edit_count)} files edited, {len(couplings)} coupled pairs")
    print()

    # Group into clusters (connected components of strong couplings)
    strong = [c for c in couplings if c["strength"] >= 0.5]
    if strong:
        # Build adjacency
        adj: dict[str, set[str]] = defaultdict(set)
        for c in strong:
            adj[c["a"]].add(c["b"])
            adj[c["b"]].add(c["a"])

        # Find connected components
        visited: set[str] = set()
        clusters: list[set[str]] = []
        for node in adj:
            if node in visited:
                continue
            cluster: set[str] = set()
            stack = [node]
            while stack:
                n = stack.pop()
                if n in visited:
                    continue
                visited.add(n)
                cluster.add(n)
                stack.extend(adj[n] - visited)
            if len(cluster) >= 2:
                clusters.append(cluster)

        if clusters:
            print("Coupled clusters (files that form one 'move'):")
            print()
            for i, cluster in enumerate(clusters):
                files = sorted(cluster)
                total_edits = sum(file_edit_count.get(f, 0) for f in cluster
                                  for ff, home_s in [(f, home)] if True)
                # Find the full path versions
                print(f"  Cluster {i+1}: {len(cluster)} files")
                for f in files:
                    ec = 0
                    for ff, cnt in file_edit_count.items():
                        if _viz_short(ff, home) == f:
                            ec = cnt
                            break
                    print(f"    {f} ({ec} edits)")
                print()

    print("All couplings:")
    print()
    for c in couplings[:20]:
        bar = "█" * int(c["strength"] * 10)
        print(f"  {c['strength']:>3.0%} {bar:10s}  {c['a'].split('/')[-1]} <-> {c['b'].split('/')[-1]}  ({c['co']}x co-touched)")


def _load_viz_context(session_id: str | None) -> dict | None:
    import sys as _sys

    if session_id is None:
        session_id = active_session()
    if session_id is None:
        sessions = list_sessions()
        if not sessions:
            print("No sessions found", file=_sys.stderr)
            return None
        session_id = sessions[0]["session_id"]

    sdir = session_dir(session_id)
    if not (sdir / "touches.jsonl").exists():
        print(f"No activity for {session_id}", file=_sys.stderr)
        return None

    meta = {}
    if (sdir / "meta.json").exists():
        try:
            meta = json.loads((sdir / "meta.json").read_text())
        except (json.JSONDecodeError, OSError):
            pass

    prompts = _load_prompts(sdir)
    touches = _load_jsonl(sdir / "touches.jsonl")
    diffs = _load_jsonl(sdir / "diffs.jsonl")

    if not prompts or not touches:
        print("Not enough data to visualize")
        return None

    touches_by_prompt: dict[int | None, list[dict]] = {}
    for t in touches:
        touches_by_prompt.setdefault(t.get("prompt_idx"), []).append(t)

    inferred = infer_branches(prompts, touches_by_prompt)
    detour_prompts: set[int] = set()
    for ib in inferred:
        for idx in range(ib["start_idx"], ib["end_idx"] + 1):
            detour_prompts.add(idx)

    # Project mapping
    def _infer_project(touch: dict) -> str:
        p = touch.get("project", "")
        if p:
            return p
        f = touch.get("file", "")
        if not f:
            return ""
        parent = f.rsplit("/", 1)[0] if "/" in f else f
        parts = parent.split("/")
        return "/".join(parts[:4]) if len(parts) > 3 else parent

    project_list: list[str] = []
    project_seen: set[str] = set()
    for t in touches:
        p = _infer_project(t)
        if p and p not in project_seen:
            project_list.append(p)
            project_seen.add(p)

    project_letter: dict[str, str] = {}
    for i, p in enumerate(project_list):
        project_letter[p] = chr(ord("A") + i) if i < 26 else chr(ord("a") + i - 26)

    summary = {}
    if (sdir / "summary.json").exists():
        try:
            summary = json.loads((sdir / "summary.json").read_text())
        except (json.JSONDecodeError, OSError):
            pass

    home = str(Path.home())

    # Load transcript responses + detect decision points
    transcript_path = meta.get("transcript_path", "")
    responses = _load_transcript_responses(transcript_path, prompts)
    decisions = detect_decision_points(prompts, responses)
    decision_prompts: set[int] = set()
    for d in decisions:
        decision_prompts.add(d["idx"])

    return {
        "session_id": session_id,
        "sdir": sdir,
        "meta": meta,
        "prompts": prompts,
        "touches": touches,
        "diffs": diffs,
        "touches_by_prompt": touches_by_prompt,
        "inferred": inferred,
        "detour_prompts": detour_prompts,
        "decisions": decisions,
        "decision_prompts": decision_prompts,
        "responses": responses,
        "project_list": project_list,
        "project_letter": project_letter,
        "summary": summary,
        "home": home,
        "_infer_project": _infer_project,
    }


def _diff_summary(old: str, new: str) -> str:
    """Produce a one-line summary of what changed between old and new text."""
    old_lines = old.split("\n")
    new_lines = new.split("\n")

    # Find first differing line
    first_diff_old = None
    first_diff_new = None
    for i in range(max(len(old_lines), len(new_lines))):
        ol = old_lines[i] if i < len(old_lines) else None
        nl = new_lines[i] if i < len(new_lines) else None
        if ol != nl:
            first_diff_old = ol
            first_diff_new = nl
            break

    if first_diff_old is None and first_diff_new is None:
        return "(no change)"

    # If it's a pure insertion (old is subset of new or vice versa)
    if len(old_lines) == 1 and len(new_lines) > 1:
        return f"expanded: {old_lines[0].strip()[:50]} (+{len(new_lines) - 1} lines)"
    if len(new_lines) == 1 and len(old_lines) > 1:
        return f"collapsed: {new_lines[0].strip()[:50]} (-{len(old_lines) - 1} lines)"

    # Show the first meaningful difference
    if first_diff_old is not None and first_diff_new is not None:
        old_s = first_diff_old.strip()[:40]
        new_s = first_diff_new.strip()[:40]
        if old_s and new_s:
            return f"- {old_s}  + {new_s}"
        elif new_s:
            return f"+ {new_s}"
        elif old_s:
            return f"- {old_s}"

    if first_diff_new is not None:
        return f"+ {first_diff_new.strip()[:50]}"
    if first_diff_old is not None:
        return f"- {first_diff_old.strip()[:50]}"

    return f"{len(old_lines)} -> {len(new_lines)} lines"


def _viz_short(f: str, home: str) -> str:
    if f.startswith(home + "/"):
        return "~/" + f[len(home) + 1:]
    return f


def _viz_trunc(s: str, w: int) -> str:
    """Truncate path keeping filename: ~/long/path/file.py → ~/.../file.py"""
    if len(s) <= w:
        return s.ljust(w)
    # Keep the filename (last component), truncate the middle
    if "/" in s:
        parts = s.split("/")
        filename = parts[-1]
        # Try: first component + .. + filename
        head = parts[0] + "/" if parts[0] else "/"
        candidate = head + "../" + filename
        if len(candidate) <= w:
            return candidate.ljust(w)
        # Just .. + filename
        candidate = "../" + filename
        if len(candidate) <= w:
            return candidate.ljust(w)
    # Fallback: chop from the left
    return ".." + s[-(w - 2):]


# -- Overview grid: files × prompts, idle-collapsed, intensity --

def _viz_overview(ctx: dict):
    prompts = ctx["prompts"]
    touches = ctx["touches"]
    touches_by_prompt = ctx["touches_by_prompt"]
    detour_prompts = ctx["detour_prompts"]
    decision_prompts = ctx["decision_prompts"]
    decisions = ctx["decisions"]
    inferred = ctx["inferred"]
    project_list = ctx["project_list"]
    project_letter = ctx["project_letter"]
    summary = ctx["summary"]
    home = ctx["home"]
    _infer_project = ctx["_infer_project"]

    prompt_idxs = [p.get("idx", 0) for p in prompts]

    # Build display columns: collapse idle prompts, but keep decision points visible
    display_cols: list[int | None] = []
    in_gap = False
    for idx in prompt_idxs:
        is_notable = bool(touches_by_prompt.get(idx)) or idx in decision_prompts
        if not is_notable:
            if not in_gap:
                display_cols.append(None)  # gap marker
                in_gap = True
        else:
            display_cols.append(idx)
            in_gap = False

    # Build intensity grid: raw_file → {prompt_idx → (char, touch_count)}
    grid: dict[str, dict[int, tuple[str, int]]] = {}
    for t in touches:
        f = t.get("file", "?")
        pidx = t.get("prompt_idx")
        if pidx is None:
            continue
        proj = _infer_project(t)
        letter = project_letter.get(proj, "?")
        is_edit = t.get("action", "") in ("edit", "write")
        if f not in grid:
            grid[f] = {}
        existing = grid[f].get(pidx)
        if existing:
            old_char, old_count = existing
            new_char = letter.upper() if is_edit else old_char
            # Upgrade to edit if we see one
            if is_edit and old_char.islower():
                new_char = letter.upper()
            grid[f][pidx] = (new_char, old_count + 1)
        else:
            char = letter.upper() if is_edit else letter.lower()
            grid[f][pidx] = (char, 1)

    # File order by first appearance
    file_order: list[str] = []
    file_set: set[str] = set()
    for t in touches:
        f = t.get("file", "?")
        if f not in file_set:
            file_order.append(f)
            file_set.add(f)

    # Intensity: 1 touch = lowercase/uppercase letter, 4+ = bold (doubled)
    def _cell(f: str, idx: int) -> str:
        entry = grid.get(f, {}).get(idx)
        if not entry:
            return "·"
        char, count = entry
        if count >= 4:
            return char.upper()  # always uppercase for heavy activity
        return char

    # Header
    total_edits = sum(1 for t in touches if t.get("action") in ("edit", "write"))
    sid_short = ctx["session_id"]
    if len(sid_short) > 16:
        sid_short = sid_short[:8]

    duration_str = ""
    if summary.get("duration_seconds"):
        duration_str = f" ({_format_duration(summary['duration_seconds'])})"
    tokens_str = ""
    tok = summary.get("tokens")
    if tok:
        tokens_str = f" | {_format_tokens(tok.get('total_tokens', 0))} tokens"

    print(f"{sid_short}{duration_str}")
    print(f"{len(file_order)} files, {total_edits} edits, {len(prompts)} prompts{tokens_str}")
    print()

    # Column headers — numbers right-justified to align with data chars
    label_width = 42
    header = " " * label_width
    for col in display_cols:
        if col is None:
            header += " ~"
        else:
            n = col + 1
            if n == 1 or n % 5 == 0:
                header += str(n % 100).rjust(2)
            elif col in detour_prompts:
                header += " ×"
            elif col in decision_prompts:
                header += " ?"
            else:
                header += " ·"
    print(header)
    sep = " " * label_width
    for col in display_cols:
        sep += "──"
    print(sep)

    # Grid rows — char in position 1, detour marker in position 0
    for f in file_order:
        label = _viz_trunc(_viz_short(f, home), label_width)
        row = label
        for col in display_cols:
            if col is None:
                row += " ~"
            elif col in detour_prompts:
                cell = _cell(f, col)
                row += "│" + cell
            else:
                row += " " + _cell(f, col)
        print(row)

    # Legend
    print()
    parts = []
    for p in project_list:
        letter = project_letter[p]
        name = p.split("/")[-1]
        parts.append(f"{letter}={name}")
    print("  " + "  ".join(parts))
    print("  UPPER=edit  lower=read  ~=idle  │=detour  ?=decision point")

    if inferred:
        print()
        for ib in inferred:
            span = f"prompt {ib['start_idx'] + 1}"
            if ib["start_idx"] != ib["end_idx"]:
                span = f"prompts {ib['start_idx'] + 1}-{ib['end_idx'] + 1}"
            print(f"  × {ib['reason']} ({span})")

    if decisions:
        print()
        for d in decisions:
            print(f"  ? prompt {d['idx'] + 1}: chose \"{d['chosen']}\"")
            for opt in d["options"]:
                print(f"      - {opt}")


# -- Drill-down: single prompt --

def _viz_prompt(ctx: dict, prompt_num: int):
    prompt_idx = prompt_num - 1  # 1-indexed input
    prompts = ctx["prompts"]
    touches_by_prompt = ctx["touches_by_prompt"]
    diffs = ctx["diffs"]
    detour_prompts = ctx["detour_prompts"]
    home = ctx["home"]
    _infer_project = ctx["_infer_project"]
    project_letter = ctx["project_letter"]
    meta = ctx["meta"]

    # Find the prompt
    prompt = None
    for p in prompts:
        if p.get("idx") == prompt_idx:
            prompt = p
            break
    if not prompt:
        print(f"Prompt {prompt_num} not found (session has {len(prompts)} prompts)")
        return

    pts = touches_by_prompt.get(prompt_idx, [])
    decision_prompts = ctx.get("decision_prompts", set())
    decisions = ctx.get("decisions", [])

    tags = []
    if prompt_idx in detour_prompts:
        tags.append("detour")
    if prompt_idx in decision_prompts:
        tags.append("decision point")
    tag_str = f"  [{', '.join(tags)}]" if tags else ""

    # Compute timing
    durations = compute_prompt_durations(prompts, touches_by_prompt)
    dur = next((d for d in durations if d["idx"] == prompt_idx), None)

    timing_str = ""
    if dur and dur.get("total"):
        parts = [f"{_format_secs(dur['total'])} total"]
        if dur.get("think"):
            parts.append(f"{_format_secs(dur['think'])} thinking")
        if dur.get("work"):
            parts.append(f"{_format_secs(dur['work'])} working")
        if dur.get("idle") and dur["idle"] > 5:
            parts.append(f"{_format_secs(dur['idle'])} idle")
        timing_str = f"  ({', '.join(parts)})"

    print(f"Prompt {prompt_num}{tag_str}{timing_str}")

    # Show decision options if this is a decision point
    decision = next((d for d in decisions if d["idx"] == prompt_idx), None)
    if decision:
        print()
        print("  Options presented:")
        for opt in decision["options"]:
            print(f"    - {opt}")
        print(f"  Chosen: \"{decision['chosen']}\"")
    print(f'"{prompt.get("prompt", "")}"')
    if prompt.get("tag"):
        print(f"tag: {prompt['tag']}")
    print()

    if not pts:
        print("  (no file activity)")
        return

    # Group by file
    file_actions: dict[str, list[dict]] = {}
    for t in pts:
        file_actions.setdefault(t.get("file", "?"), []).append(t)

    for f, actions in file_actions.items():
        proj = _infer_project(actions[0])
        letter = project_letter.get(proj, "?")
        reads = sum(1 for a in actions if a.get("action") == "read")
        edits = sum(1 for a in actions if a.get("action") in ("edit", "write"))
        lines = sum(a.get("lines_changed", a.get("lines", 0)) for a in actions)

        parts = []
        if reads:
            parts.append(f"{reads} read")
        if edits:
            parts.append(f"{edits} edit")
        if lines:
            parts.append(f"{lines} lines")
        detail = ", ".join(parts)

        print(f"  [{letter}] {_viz_short(f, home)}  ({detail})")

    # Show diffs for this prompt
    prompt_diffs = [d for d in diffs if d.get("prompt_idx") == prompt_idx]
    if prompt_diffs:
        print()
        for d in prompt_diffs:
            f = _viz_short(d.get("file", "?"), home)
            if d.get("old_string") is not None:
                old = d["old_string"]
                new = d.get("new_string", "")
                summary = _diff_summary(old, new)
                old_lines = old.count("\n") + 1
                new_lines = new.count("\n") + 1
                size = f" ({old_lines} -> {new_lines} lines)" if old_lines != new_lines else ""
                print(f"  {f}:{size}")
                print(f"    {summary}")
            elif d.get("is_new"):
                print(f"  {f}: new file ({d.get('lines', '?')} lines)")

    # Show transcript response
    transcript_path = meta.get("transcript_path", "")
    responses = _load_transcript_responses(transcript_path, prompts)
    parts = responses.get(prompt_idx, [])
    if parts:
        print()
        print("--- Response ---")
        for ptype, ptext in parts:
            if ptype == "thinking":
                print()
                print("[thinking]")
                print(ptext)
                print("[/thinking]")
            elif ptype == "text":
                print()
                print(ptext)
            elif ptype == "tool":
                print(f"  > {ptext}")


# -- Drill-down: single file --

def _viz_file(ctx: dict, file_query: str):
    prompts = ctx["prompts"]
    touches = ctx["touches"]
    diffs = ctx["diffs"]
    detour_prompts = ctx["detour_prompts"]
    home = ctx["home"]

    # Find matching file (substring match)
    matching = set()
    for t in touches:
        f = t.get("file", "")
        if file_query in f or file_query in _viz_short(f, home):
            matching.add(f)

    if not matching:
        print(f'No file matching "{file_query}"')
        return
    if len(matching) > 1:
        print(f'Multiple files match "{file_query}":')
        for f in sorted(matching):
            print(f"  {_viz_short(f, home)}")
        return

    target = matching.pop()
    print(f"File: {_viz_short(target, home)}")
    print()

    # Collect all touches for this file, grouped by prompt
    prompt_touches: dict[int, list[dict]] = {}
    for t in touches:
        if t.get("file") == target:
            pidx = t.get("prompt_idx")
            if pidx is not None:
                prompt_touches.setdefault(pidx, []).append(t)

    total_reads = sum(1 for t in touches if t.get("file") == target and t.get("action") == "read")
    total_edits = sum(1 for t in touches if t.get("file") == target and t.get("action") in ("edit", "write"))
    print(f"  {total_reads} reads, {total_edits} edits across {len(prompt_touches)} prompts")
    print()

    # Compute durations
    touches_by_prompt = ctx["touches_by_prompt"]
    durations = compute_prompt_durations(prompts, touches_by_prompt)
    dur_map = {d["idx"]: d for d in durations}

    # Timeline
    total_time_on_file = 0
    for p in prompts:
        idx = p.get("idx", 0)
        if idx not in prompt_touches:
            continue
        pts = prompt_touches[idx]
        detour = " [detour]" if idx in detour_prompts else ""

        reads = sum(1 for t in pts if t.get("action") == "read")
        edits = sum(1 for t in pts if t.get("action") in ("edit", "write"))
        lines = sum(t.get("lines_changed", t.get("lines", 0)) for t in pts)

        parts = []
        if reads:
            parts.append(f"{reads} read")
        if edits:
            parts.append(f"{edits} edit")
        if lines:
            parts.append(f"{lines} lines")

        dur = dur_map.get(idx)
        dur_str = ""
        if dur and dur.get("work"):
            dur_str = f" [{_format_secs(dur['work'])}]"
            total_time_on_file += dur["work"]

        prompt_text = p.get("prompt", "")
        if len(prompt_text) > 45:
            prompt_text = prompt_text[:42] + "..."

        print(f"  {idx+1:>2}  {', '.join(parts):20s}{dur_str:>8s}  \"{prompt_text}\"{detour}")

    if total_time_on_file:
        print(f"\n  Total work time on this file: {_format_secs(total_time_on_file)}")

    # Diffs for this file
    file_diffs = [d for d in diffs if d.get("file") == target]
    if file_diffs:
        print()
        print("  Changes:")
        for d in file_diffs:
            pidx = d.get("prompt_idx")
            pnum = f"p{pidx+1}" if pidx is not None else "?"
            if d.get("old_string") is not None:
                old = d["old_string"]
                new = d.get("new_string", "")
                summary = _diff_summary(old, new)
                print(f"    [{pnum}] {summary}")
            elif d.get("is_new"):
                print(f"    [{pnum}] new file ({d.get('lines', '?')} lines)")


# -- Drill-down: detours only --

def _viz_detours(ctx: dict):
    inferred = ctx["inferred"]
    prompts = ctx["prompts"]
    touches_by_prompt = ctx["touches_by_prompt"]
    home = ctx["home"]
    _infer_project = ctx["_infer_project"]
    project_letter = ctx["project_letter"]

    if not inferred:
        print("No detours detected")
        return

    print(f"{len(inferred)} detected detour(s)")
    print()

    for ib in inferred:
        span_start = ib["start_idx"]
        span_end = ib["end_idx"]
        span = f"prompt {span_start + 1}"
        if span_start != span_end:
            span = f"prompts {span_start + 1}-{span_end + 1}"
        print(f"  {ib['reason']} ({span})")
        print()

        for p in prompts:
            idx = p.get("idx", 0)
            if idx < span_start or idx > span_end:
                continue
            pts = touches_by_prompt.get(idx, [])
            prompt_text = p.get("prompt", "")
            if len(prompt_text) > 60:
                prompt_text = prompt_text[:57] + "..."

            print(f"    {idx+1}  \"{prompt_text}\"")
            file_actions: dict[str, list[str]] = {}
            for t in pts:
                f = _viz_short(t.get("file", "?"), home)
                action = t.get("action", "?")
                file_actions.setdefault(f, []).append(action)
            for f, actions in file_actions.items():
                reads = actions.count("read")
                edits = sum(1 for a in actions if a in ("edit", "write"))
                parts = []
                if reads:
                    parts.append(f"{reads} read")
                if edits:
                    parts.append(f"{edits} edit")
                print(f"       {f}  ({', '.join(parts)})")
        print()


def _cmd_timeline(max_sessions: int = 10):
    """Cross-session file activity: files as rows, sessions as columns."""
    sessions = list_sessions()
    if not sessions:
        print("No sessions found")
        return

    # Sort by start_time ascending, take last N
    sessions.sort(key=lambda s: s.get("start_time", ""))
    sessions = sessions[-max_sessions:]

    home = str(Path.home())

    # Gather per-session file data
    session_data: list[dict] = []  # [{sid, label, date, files: {path: {reads, edits}}}]
    all_files: dict[str, int] = {}  # file → total edits across all sessions

    for s in sessions:
        sid = s["session_id"]
        sdir = session_dir(sid)
        touches = _load_jsonl(sdir / "touches.jsonl")
        prompts = _load_prompts(sdir)

        # Session label: date + short ID
        start = s.get("start_time", "")
        date = start.split("T")[0] if "T" in start else start[:10]

        # Duration
        summary = {}
        summary_file = sdir / "summary.json"
        if summary_file.exists():
            try:
                summary = json.loads(summary_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        dur = ""
        if summary.get("duration_seconds"):
            dur = _format_duration(summary["duration_seconds"])

        files: dict[str, dict[str, int]] = {}
        for t in touches:
            f = t.get("file", "?")
            short = _viz_short(f, home)
            action = t.get("action", "")
            if short not in files:
                files[short] = {"reads": 0, "edits": 0}
            if action == "read":
                files[short]["reads"] += 1
            elif action in ("edit", "write"):
                files[short]["edits"] += 1

        for f, counts in files.items():
            all_files[f] = all_files.get(f, 0) + counts["edits"]

        session_data.append({
            "sid": sid,
            "date": date,
            "dur": dur,
            "prompts": len(prompts),
            "files": files,
        })

    if not all_files:
        print("No file activity across sessions")
        return

    # Rank files by total edits, take top N that fit
    ranked_files = sorted(all_files.keys(), key=lambda f: -all_files[f])
    # Filter to files with at least 1 edit
    ranked_files = [f for f in ranked_files if all_files[f] > 0]
    max_files = 30
    ranked_files = ranked_files[:max_files]

    # Header
    n_sessions = len(session_data)
    total_edits = sum(all_files.values())
    print(f"Timeline: {n_sessions} sessions, {len(ranked_files)} files, {total_edits} total edits")
    print()

    # Column headers: session dates
    label_width = 42
    header = " " * label_width
    for sd in session_data:
        col = sd["date"][-5:]  # MM-DD
        header += col.rjust(6)
    print(header)

    # Duration row
    dur_row = " " * label_width
    for sd in session_data:
        dur_row += sd["dur"][:5].rjust(6) if sd["dur"] else "     -"
    print(dur_row)

    # Separator
    print(" " * label_width + "------" * n_sessions)

    # Grid rows
    for f in ranked_files:
        label = _viz_trunc(f, label_width)
        row = label
        for sd in session_data:
            fdata = sd["files"].get(f)
            if not fdata:
                row += "     ·"
            else:
                edits = fdata["edits"]
                reads = fdata["reads"]
                if edits > 0:
                    row += str(edits).rjust(6)
                elif reads > 0:
                    row += "     r"
                else:
                    row += "     ·"
        print(row)

    # Footer: session IDs for reference
    print()
    for i, sd in enumerate(session_data):
        sid_short = sd["sid"][:8]
        detour_info = ""
        # Quick detour check
        sdir = session_dir(sd["sid"])
        touches = _load_jsonl(sdir / "touches.jsonl")
        prompts = _load_prompts(sdir)
        if prompts and touches:
            tbp: dict[int | None, list[dict]] = {}
            for t in touches:
                tbp.setdefault(t.get("prompt_idx"), []).append(t)
            inferred = infer_branches(prompts, tbp)
            if inferred:
                detour_info = f"  ({len(inferred)} detour(s))"
        print(f"  {sd['date']} {sid_short}  {sd['prompts']} prompts, {sd['dur'] or '?'}{detour_info}")


def _load_transcript_responses(
    transcript_path: str, prompts: list[dict],
) -> dict[int, list[tuple[str, str]]]:
    """Extract assistant responses from transcript, matched to inscript prompts.

    Returns dict of prompt_idx → list of (type, text) tuples where type is:
      "thinking" — reasoning blocks
      "text"     — response text
      "tool"     — tool usage (compact description)
    """
    responses: dict[int, list[tuple[str, str]]] = {}
    try:
        tp = Path(transcript_path)
        if not tp.exists():
            return responses
    except (OSError, TypeError):
        return responses

    # Build lookup: prompt text prefix → list of inscript prompt indices
    # Normalize smart quotes for matching
    def _normalize(s: str) -> str:
        return s.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')

    prompt_lookup: dict[str, list[int]] = {}
    for p in prompts:
        text = _normalize(p.get("prompt", "").strip())
        if text:
            prompt_lookup.setdefault(text[:60], []).append(p.get("idx", 0))

    current_idx: int | None = None
    current_parts: list[tuple[str, str]] = []

    for line in tp.open():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = d.get("message", {})
        role = msg.get("role")

        if role == "user":
            content = msg.get("content", "")
            prompt_text = ""
            if isinstance(content, str) and content.strip():
                prompt_text = content.strip()
            elif isinstance(content, list):
                has_tool_result = any(
                    b.get("type") == "tool_result" for b in content
                )
                if not has_tool_result:
                    for b in content:
                        if b.get("type") == "text" and b.get("text", "").strip():
                            prompt_text = b["text"].strip()
                            break

            if prompt_text:
                if current_idx is not None and current_parts:
                    responses[current_idx] = current_parts
                # Pop next matching index (handles duplicate prompts)
                key = _normalize(prompt_text)[:60]
                candidates = prompt_lookup.get(key, [])
                current_idx = candidates.pop(0) if candidates else None
                current_parts = []

        elif role == "assistant" and current_idx is not None:
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    btype = block.get("type", "")
                    if btype == "thinking" and block.get("thinking"):
                        current_parts.append(("thinking", block["thinking"]))
                    elif btype == "text" and block.get("text"):
                        current_parts.append(("text", block["text"]))
                    elif btype == "tool_use":
                        tool = block.get("name", "?")
                        inp = block.get("input", {})
                        if tool in ("Read", "Glob", "Grep"):
                            target = inp.get("file_path") or inp.get("path") or inp.get("pattern", "")
                            current_parts.append(("tool", f"{tool} {target}"))
                        elif tool in ("Edit", "Write"):
                            target = inp.get("file_path", "?")
                            current_parts.append(("tool", f"{tool} {target}"))
                        elif tool == "Bash":
                            cmd = inp.get("command", "?")[:80]
                            current_parts.append(("tool", f"Bash: {cmd}"))
                        elif tool == "Agent":
                            desc = inp.get("description", inp.get("prompt", "?"))[:60]
                            current_parts.append(("tool", f"Agent: {desc}"))
                        else:
                            current_parts.append(("tool", tool))
            elif isinstance(content, str) and content:
                current_parts.append(("text", content))

    if current_idx is not None and current_parts:
        responses[current_idx] = current_parts

    return responses


def _cmd_dashboard():
    """Session picker dashboard. Select a session to explore."""
    import curses

    sessions = list_sessions()
    if not sessions:
        print("No sessions found")
        return

    home = str(Path.home())

    # Pre-compute session info (need this before sorting)
    session_info: list[dict] = []
    for s in sessions:
        sid = s["session_id"]
        sdir = session_dir(sid)
        start = s.get("start_time", "?")
        status = s.get("status", "?")

        prompts = _load_prompts(sdir)
        touches = _load_jsonl(sdir / "touches.jsonl")

        summary = {}
        if (sdir / "summary.json").exists():
            try:
                summary = json.loads((sdir / "summary.json").read_text())
            except (json.JSONDecodeError, OSError):
                pass

        # Find last activity time for sorting
        last_time = start
        summary_end = summary.get("end_time", "")
        if summary_end:
            # end_time is a full ISO timestamp — most reliable
            last_time = summary_end
        elif prompts:
            # Fallback: combine date from start + time from last prompt
            last_prompt_ts = prompts[-1].get("ts", "")
            if last_prompt_ts and "T" in start:
                start_hour = int(start.split("T")[1].split(":")[0])
                prompt_hour = int(last_prompt_ts.split(":")[0])
                date = start.split("T")[0]
                # If prompt hour < start hour, it's past midnight — bump date
                if prompt_hour < start_hour:
                    from datetime import datetime as _dt, timedelta as _td
                    d = _dt.fromisoformat(date) + _td(days=1)
                    date = d.strftime("%Y-%m-%d")
                last_time = date + "T" + last_prompt_ts

        dur = ""
        if summary.get("duration_seconds"):
            dur = _format_duration(summary["duration_seconds"])

        total_edits = sum(1 for t in touches if t.get("action") in ("edit", "write"))

        # Top files
        file_edits: dict[str, int] = {}
        for t in touches:
            if t.get("action") in ("edit", "write"):
                f = t.get("file", "?").split("/")[-1]
                file_edits[f] = file_edits.get(f, 0) + 1
        top = sorted(file_edits.items(), key=lambda x: -x[1])[:3]
        top_str = ", ".join(f"{name}" for name, _ in top) if top else ""

        session_info.append({
            "sid": sid,
            "start": start,
            "last_time": last_time,
            "status": status,
            "dur": dur,
            "prompts": len(prompts),
            "edits": total_edits,
            "top_files": top_str,
        })

    # Sort by last activity, most recent first
    session_info.sort(key=lambda s: s.get("last_time", ""), reverse=True)

    def _run_dashboard(stdscr):
        curses.curs_set(0)
        curses.set_escdelay(25)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)

        cursor = 0
        scroll = 0

        while True:
            stdscr.clear()
            height, width = stdscr.getmaxyx()

            # Header
            try:
                stdscr.addnstr(0, 0, f"inscript dashboard — {len(session_info)} sessions", width - 1, curses.A_BOLD)
                stdscr.addnstr(1, 0, "", width - 1)
            except curses.error:
                pass

            # Session list
            list_start = 2
            visible_rows = height - 4  # room for header + footer
            if cursor < scroll:
                scroll = cursor
            if cursor >= scroll + visible_rows:
                scroll = cursor - visible_rows + 1

            for i in range(scroll, min(len(session_info), scroll + visible_rows)):
                si = session_info[i]
                y = list_start + (i - scroll)
                if y >= height - 2:
                    break

                date = si["start"].split("T")[0] if "T" in si["start"] else si["start"][:10]
                time_part = si["start"].split("T")[1][:5] if "T" in si["start"] else ""
                sid_short = si["sid"][:8]

                line = f"  {date} {time_part}  {sid_short}  "
                line += f"{si['dur'] or '-':>6s}  "
                line += f"{si['prompts']:>3} prompts  "
                line += f"{si['edits']:>3} edits"
                if si["top_files"]:
                    remaining = width - len(line) - 4
                    if remaining > 5:
                        files = si["top_files"][:remaining]
                        line += f"  {files}"

                attr = curses.color_pair(5) | curses.A_BOLD if i == cursor else curses.A_NORMAL
                if si["status"] == "active":
                    attr = curses.color_pair(5) | curses.A_BOLD if i == cursor else curses.color_pair(2)

                try:
                    stdscr.addnstr(y, 0, line, width - 1, attr)
                except curses.error:
                    pass

            # Footer
            try:
                footer = " up/dn select  enter> explore  q quit"
                stdscr.addnstr(height - 1, 0, footer, width - 1, curses.A_DIM | curses.A_REVERSE)
            except curses.error:
                pass

            stdscr.refresh()

            key = stdscr.getch()
            if key == ord("q") or key == 27:
                break
            elif key == curses.KEY_DOWN or key == ord("j"):
                if cursor < len(session_info) - 1:
                    cursor += 1
            elif key == curses.KEY_UP or key == ord("k"):
                if cursor > 0:
                    cursor -= 1
            elif key == ord("\n") or key == curses.KEY_RIGHT:
                # Open explore for selected session
                selected_sid = session_info[cursor]["sid"]
                _run_explore_inner(stdscr, selected_sid)
                # After explore returns, we're back at dashboard
            elif key == curses.KEY_HOME or key == ord("g"):
                cursor = 0
            elif key == curses.KEY_END or key == ord("G"):
                cursor = len(session_info) - 1

    curses.wrapper(_run_dashboard)


def _run_explore_inner(stdscr, session_id: str):
    """Run the explore TUI within an existing curses context."""
    ctx = _load_viz_context(session_id)
    if ctx is None:
        return

    # Import everything the explore loop needs
    _explore_loop(stdscr, ctx)


def _cmd_explore(session_id: str | None):
    """Interactive curses TUI: scrub timeline with arrow keys, switch panels."""
    import curses

    ctx = _load_viz_context(session_id)
    if ctx is None:
        return

    curses.wrapper(lambda stdscr: _explore_loop(stdscr, ctx))


def _explore_loop(stdscr, ctx: dict):
    """The explore TUI main loop. Can be called from curses.wrapper or dashboard."""
    import curses

    prompts = ctx["prompts"]
    touches = ctx["touches"]
    touches_by_prompt = ctx["touches_by_prompt"]
    diffs = ctx["diffs"]
    detour_prompts = ctx["detour_prompts"]
    decision_prompts = ctx.get("decision_prompts", set())
    inferred = ctx["inferred"]
    project_letter = ctx["project_letter"]
    summary = ctx["summary"]
    home = ctx["home"]
    _infer_project = ctx["_infer_project"]

    all_prompts = list(enumerate(prompts))
    if not all_prompts:
        return

    file_order: list[str] = []
    file_set: set[str] = set()
    for t in touches:
        f = t.get("file", "?")
        if f not in file_set:
            file_order.append(f)
            file_set.add(f)

    detour_reason: dict[int, str] = {}
    for ib in inferred:
        for idx in range(ib["start_idx"], ib["end_idx"] + 1):
            detour_reason[idx] = ib["reason"]

    transcript_path = ctx["meta"].get("transcript_path", "")
    responses = _load_transcript_responses(transcript_path, prompts)

    PANELS = ["Files", "Chat", "Diffs"]

    def _run_inner():
        curses.curs_set(0)
        curses.set_escdelay(25)  # 25ms instead of default 1000ms
        curses.use_default_colors()

        def safe_addstr(y, x, text, maxw=None, attr=0):
            """Write to screen, silently ignoring out-of-bounds."""
            h, w = stdscr.getmaxyx()
            if y < 0 or y >= h or x >= w:
                return
            if maxw is None:
                maxw = w - x - 1
            maxw = max(0, min(maxw, w - x - 1))
            if maxw <= 0:
                return
            try:
                stdscr.addnstr(y, x, text, maxw, attr)
            except curses.error:
                pass

        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_GREEN, -1)
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_RED)
        curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_WHITE)  # active tab
        curses.init_pair(8, curses.COLOR_YELLOW, -1)                # decision point
        curses.init_pair(9, curses.COLOR_BLACK, curses.COLOR_YELLOW) # selected decision

        cursor = 0
        panel = 0  # 0=Files, 1=Chat, 2=Diffs
        panel_scroll = 0
        focus = 0  # 0=timeline, 1=tabs, 2=content
        view_mode = 0  # 0=detail, 1=grid

        # Pre-build grid data for grid view
        grid_data: dict[str, dict[int, str]] = {}
        for t in touches:
            f = t.get("file", "?")
            pidx = t.get("prompt_idx")
            if pidx is None:
                continue
            proj = _infer_project(t)
            letter = project_letter.get(proj, "?")
            is_edit = t.get("action", "") in ("edit", "write")
            char = letter.upper() if is_edit else letter.lower()
            if f not in grid_data:
                grid_data[f] = {}
            existing = grid_data[f].get(pidx)
            if existing is None or is_edit:
                grid_data[f][pidx] = char

        while True:
            stdscr.clear()
            height, width = stdscr.getmaxyx()

            pos, prompt = all_prompts[cursor]
            idx = prompt.get("idx", 0)
            pts = touches_by_prompt.get(idx, [])

            # Panel content availability (needed for scroll clamping + input gating)
            has_files = bool(pts)
            has_chat = bool(responses.get(idx, []))
            prompt_diffs_exist = any(d.get("prompt_idx") == idx for d in diffs)
            panel_has_content = [has_files, has_chat, prompt_diffs_exist]

            # -- Header --
            sid = ctx["session_id"][:8] if len(ctx["session_id"]) > 16 else ctx["session_id"]
            dur = ""
            if summary.get("duration_seconds"):
                dur = f" ({_format_duration(summary['duration_seconds'])})"
            total_edits = sum(1 for t in touches if t.get("action") in ("edit", "write"))
            header_text = f"{sid}{dur} | {len(file_order)} files, {total_edits} edits, {len(prompts)} prompts"
            safe_addstr(0, 0, header_text, width - 1, curses.A_BOLD)

            # -- Timeline bar (row 2) --
            bar_y = 2
            label = "> " if focus == 0 else "  "

            bar_tokens: list[tuple[str, int, bool]] = []
            for ai, (apos, ap) in enumerate(all_prompts):
                aidx = ap.get("idx", 0)
                has_activity = bool(touches_by_prompt.get(aidx))
                is_notable = has_activity or aidx in decision_prompts
                num = str(aidx + 1)
                if ai == cursor:
                    if aidx in detour_prompts:
                        attr = curses.color_pair(6)
                    elif aidx in decision_prompts:
                        attr = curses.color_pair(9)
                    else:
                        attr = curses.color_pair(5)
                    bar_tokens.append((f">{num}", attr | curses.A_BOLD, True))
                elif not is_notable:
                    bar_tokens.append((" ·", curses.A_DIM, False))
                elif aidx in detour_prompts:
                    bar_tokens.append((f" {num}", curses.color_pair(3), False))
                elif aidx in decision_prompts:
                    bar_tokens.append((f" {num}", curses.color_pair(8), False))
                else:
                    bar_tokens.append((f" {num}", curses.A_NORMAL, False))

            token_positions: list[int] = []
            x = 0
            cursor_token_start = 0
            cursor_token_end = 0
            for i, (text, attr, is_cur) in enumerate(bar_tokens):
                token_positions.append(x)
                if is_cur:
                    cursor_token_start = x
                    cursor_token_end = x + len(text)
                x += len(text)

            bar_width = width - len(label) - 1
            scroll = 0
            if cursor_token_end > bar_width:
                scroll = cursor_token_start - bar_width // 3
                scroll = max(0, min(scroll, x - bar_width))

            safe_addstr(bar_y, 0, label, width - 1, curses.A_DIM)
            for i, (text, attr, is_cur) in enumerate(bar_tokens):
                tok_start = token_positions[i]
                tok_end = tok_start + len(text)
                if tok_end <= scroll:
                    continue
                if tok_start - scroll + len(label) >= width - 1:
                    break
                visible_start = max(0, scroll - tok_start)
                visible_text = text[visible_start:]
                draw_x = len(label) + tok_start - scroll + visible_start
                if draw_x < len(label):
                    draw_x = len(label)
                if draw_x < width - 1:
                    safe_addstr(bar_y, draw_x, visible_text, width - draw_x - 1, attr)

            if scroll > 0:
                safe_addstr(bar_y, len(label), "<", 1, curses.A_DIM)
            if x - scroll > bar_width:
                safe_addstr(bar_y, width - 2, ">", 1, curses.A_DIM)

            # -- Content area (row 4+) --
            if view_mode == 1:
                # GRID VIEW
                grid_y = 4
                label_w = min(35, width // 3)
                cell_w = 2
                cols_visible = (width - label_w - 2) // cell_w
                cursor_prompt_idx = all_prompts[cursor][1].get("idx", 0)
                all_idxs = [pp.get("idx", 0) for pp in prompts]
                cursor_col = all_idxs.index(cursor_prompt_idx) if cursor_prompt_idx in all_idxs else 0
                grid_scroll_x = max(0, cursor_col - cols_visible // 3)
                visible_idxs = all_idxs[grid_scroll_x:grid_scroll_x + cols_visible]

                h_line = " " * label_w + "  "
                for vidx in visible_idxs:
                    n = vidx + 1
                    if vidx == cursor_prompt_idx:
                        h_line += str(n % 100).rjust(2)
                    elif n == 1 or n % 5 == 0:
                        h_line += str(n % 100).rjust(2)
                    elif vidx in detour_prompts:
                        h_line += " ×"
                    elif vidx in decision_prompts:
                        h_line += " ?"
                    else:
                        h_line += " ·"
                safe_addstr(grid_y, 0, h_line, attr=curses.A_DIM)
                safe_addstr(grid_y + 1, 0, " " * label_w + "  " + "──" * len(visible_idxs))

                max_file_rows = height - grid_y - 4
                for fi, f in enumerate(file_order[:max_file_rows]):
                    if grid_y + 2 + fi >= height - 2:
                        break
                    short = _viz_short(f, home)
                    label = _viz_trunc(short, label_w)
                    row = label + "  "
                    for vidx in visible_idxs:
                        char = grid_data.get(f, {}).get(vidx, "·")
                        if vidx == cursor_prompt_idx:
                            row += f">{char}"
                        elif vidx in detour_prompts:
                            row += f"│{char}"
                        else:
                            row += f" {char}"
                    safe_addstr(grid_y + 2 + fi, 0, row)

                legend_y = min(grid_y + 2 + min(len(file_order), max_file_rows) + 1, height - 3)
                parts = []
                for p_name in ctx["project_list"]:
                    letter = project_letter[p_name]
                    name = p_name.split("/")[-1]
                    parts.append(f"{letter}={name}")
                safe_addstr(legend_y, 0, "  " + "  ".join(parts) + "  UPPER=edit  lower=read", attr=curses.A_DIM)

            else:
                # DETAIL VIEW
                info_y = 4
                prompt_label = f"Prompt {idx + 1}/{len(prompts)}"
                if idx in detour_prompts:
                    prompt_label += f"  [detour: {detour_reason.get(idx, '?')}]"
                if idx in decision_prompts:
                    prompt_label += "  [decision point]"
                safe_addstr(info_y, 0, prompt_label, width - 1, curses.A_BOLD)

                prompt_text = prompt.get("prompt", "")
                line = 0
                remaining = prompt_text
                while remaining and line < 2:
                    chunk = remaining[:width - 4]
                    safe_addstr(info_y + 1 + line, 2, chunk, width - 3)
                    remaining = remaining[width - 4:]
                    line += 1

                tag = prompt.get("tag")
                if tag:
                    safe_addstr(info_y + 1 + line, 2, f"tag: {tag}", width - 3, curses.A_DIM)
                    line += 1

                # -- Tab bar --
                tab_y = info_y + 2 + line + 1
                tab_indicator = "> " if focus == 1 else "  "
                content_indicator = "> " if focus == 2 else "  "
                safe_addstr(tab_y, 0, tab_indicator, width - 1)
                tab_x = len(tab_indicator)
                for pi, pname in enumerate(PANELS):
                    if pi == panel and focus == 1:
                        safe_addstr(tab_y, tab_x, f"[{pname}]", width - tab_x - 1, curses.color_pair(7))
                    elif pi == panel:
                        safe_addstr(tab_y, tab_x, f"[{pname}]", width - tab_x - 1, curses.A_NORMAL)
                    else:
                        safe_addstr(tab_y, tab_x, f" {pname} ", width - tab_x - 1, curses.A_DIM)
                    tab_x += len(pname) + 3

                # -- Panel content --
                safe_addstr(tab_y + 1, 0, content_indicator.rstrip(), width - 1, curses.A_DIM)
                content_y = tab_y + 2
                max_rows = height - content_y - 2  # leave room for footer

                # Count content lines per panel for scroll clamping
                content_lines = [0, 0, 0]
                if has_files:
                    _fa: dict[str, list] = {}
                    for t in pts:
                        _fa.setdefault(t.get("file", "?"), []).append(t)
                    content_lines[0] = len(_fa)
                if has_chat:
                    _ww = max(1, width - 6)
                    _cl = 0
                    for _ptype, _ptext in responses.get(idx, []):
                        if _ptype == "tool":
                            _cl += 1
                        else:
                            for _rl in _ptext.split("\n"):
                                _cl += max(1, (len(_rl) + _ww - 1) // _ww)
                            if _ptype == "thinking":
                                _cl += 2  # header + blank line
                    content_lines[1] = _cl
                if prompt_diffs_exist:
                    _dlc = 0
                    for d in diffs:
                        if d.get("prompt_idx") != idx:
                            continue
                        if d.get("old_string") is not None:
                            _dlc += 2
                            _ol = d["old_string"].split("\n")
                            _nl = d.get("new_string", "").split("\n")
                            _shown = 0
                            for _oi, _o in enumerate(_ol):
                                _n = _nl[_oi] if _oi < len(_nl) else None
                                if _o != _n:
                                    _dlc += 2 if _n is not None else 1
                                    _shown += 1
                                    if _shown >= 3:
                                        break
                            if len(_nl) > len(_ol):
                                _dlc += min(3 - _shown, len(_nl) - len(_ol))
                            _dlc += 1
                        elif d.get("is_new"):
                            _dlc += 2
                    content_lines[2] = _dlc

                # Clamp scroll to actual content
                max_scroll = max(0, content_lines[panel] - max(1, max_rows))
                if panel_scroll > max_scroll:
                    panel_scroll = max_scroll

                if panel == 0:
                    # Files panel
                    if not pts:
                        safe_addstr(content_y, 2, "(no file activity)", width - 3, curses.A_DIM)
                    else:
                        file_actions: dict[str, list[dict]] = {}
                        for t in pts:
                            file_actions.setdefault(t.get("file", "?"), []).append(t)
                        entries = list(file_actions.items())
                        visible = entries[panel_scroll:panel_scroll + max(1, max_rows)]
                        for fi, (f, actions) in enumerate(visible):
                            if content_y + fi >= height - 2:
                                break
                            proj = _infer_project(actions[0])
                            letter = project_letter.get(proj, "?")
                            reads = sum(1 for a in actions if a.get("action") == "read")
                            edits = sum(1 for a in actions if a.get("action") in ("edit", "write"))
                            lines_changed = sum(a.get("lines_changed", a.get("lines", 0)) for a in actions)
                            parts = []
                            if reads:
                                parts.append(f"{reads}r")
                            if edits:
                                parts.append(f"{edits}e")
                            if lines_changed:
                                parts.append(f"{lines_changed}L")
                            short = _viz_short(f, home)
                            if len(short) > width - 22:
                                short = _viz_trunc(short, width - 22)
                            detail = " ".join(parts)
                            attr = curses.color_pair(4) if edits else curses.A_NORMAL
                            safe_addstr(content_y + fi, 2, f"[{letter}] {short}  ({detail})", width - 3, attr)

                elif panel == 1:
                    # Chat panel — thinking/text prominent, tools dimmed
                    parts = responses.get(idx, [])
                    if not parts:
                        safe_addstr(content_y, 2, "(no response in transcript)", width - 3, curses.A_DIM)
                    else:
                        wrap_width = width - 6
                        # Build rendered lines: (text, attr)
                        chat_rendered: list[tuple[str, int]] = []
                        for ptype, ptext in parts:
                            if ptype == "thinking":
                                chat_rendered.append(("-- thinking --", curses.color_pair(1)))
                                for raw_line in ptext.split("\n"):
                                    while len(raw_line) > wrap_width:
                                        chat_rendered.append((raw_line[:wrap_width], curses.color_pair(1)))
                                        raw_line = raw_line[wrap_width:]
                                    chat_rendered.append((raw_line, curses.color_pair(1)))
                                chat_rendered.append(("", curses.A_NORMAL))
                            elif ptype == "text":
                                for raw_line in ptext.split("\n"):
                                    while len(raw_line) > wrap_width:
                                        chat_rendered.append((raw_line[:wrap_width], curses.A_BOLD))
                                        raw_line = raw_line[wrap_width:]
                                    chat_rendered.append((raw_line, curses.A_BOLD))
                            elif ptype == "tool":
                                chat_rendered.append((f"  > {ptext}", curses.A_DIM))

                        visible = chat_rendered[panel_scroll:panel_scroll + max(1, max_rows)]
                        for li, (text, attr) in enumerate(visible):
                            if content_y + li >= height - 2:
                                break
                            safe_addstr(content_y + li, 2, text, width - 3, attr)

                elif panel == 2:
                    # Diffs panel
                    prompt_diffs = [d for d in diffs if d.get("prompt_idx") == idx]
                    if not prompt_diffs:
                        safe_addstr(content_y, 2, "(no diffs)", width - 3, curses.A_DIM)
                    else:
                        diff_lines: list[tuple[str, int]] = []
                        for d in prompt_diffs:
                            f = _viz_short(d.get("file", "?"), home)
                            fname = f.split("/")[-1]
                            if d.get("old_string") is not None:
                                old = d["old_string"]
                                new = d.get("new_string", "")
                                old_lc = old.count("\n") + 1
                                new_lc = new.count("\n") + 1
                                diff_lines.append((f"{fname} ({old_lc} -> {new_lc} lines):", curses.A_BOLD))
                                diff_lines.append((f"  {_diff_summary(old, new)}", curses.A_NORMAL))
                                # Show a few lines of actual diff
                                old_lines = old.split("\n")
                                new_lines = new.split("\n")
                                shown = 0
                                for oi, ol in enumerate(old_lines):
                                    nl = new_lines[oi] if oi < len(new_lines) else None
                                    if ol != nl:
                                        diff_lines.append((f"  - {ol[:width-8]}", curses.color_pair(3)))
                                        if nl is not None:
                                            diff_lines.append((f"  + {nl[:width-8]}", curses.color_pair(4)))
                                        shown += 1
                                        if shown >= 3:
                                            break
                                # Show new lines beyond old length
                                if len(new_lines) > len(old_lines) and shown < 3:
                                    for nl in new_lines[len(old_lines):len(old_lines) + 3 - shown]:
                                        diff_lines.append((f"  + {nl[:width-8]}", curses.color_pair(4)))
                                diff_lines.append(("", curses.A_NORMAL))
                            elif d.get("is_new"):
                                diff_lines.append((f"{fname}: new file ({d.get('lines', '?')} lines)", curses.A_BOLD))
                                diff_lines.append(("", curses.A_NORMAL))
                        visible = diff_lines[panel_scroll:panel_scroll + max(1, max_rows)]
                        for li, (text, attr) in enumerate(visible):
                            if content_y + li >= height - 2:
                                break
                            safe_addstr(content_y + li, 2, text, width - 3, attr)

            # -- Footer --
            if focus == 0:
                footer = " <-/-> prompts  dn> panels  v grid  c copy  q quit"
            elif focus == 1:
                if panel_has_content[panel]:
                    footer = " <-/-> panels  dn> content  esc> timeline  q quit"
                else:
                    footer = " <-/-> panels  esc> timeline  q quit"
            else:
                footer = " up/dn scroll  esc> timeline  q quit"
            safe_addstr(height - 1, 0, footer, width - 1, curses.A_DIM | curses.A_REVERSE)

            stdscr.refresh()

            # -- Input --
            key = stdscr.getch()
            if key == ord("q"):
                break

            elif key == ord("v"):
                view_mode = 1 - view_mode  # toggle

            elif key == ord("c"):
                # Copy session:prompt reference to clipboard
                import subprocess
                ref = f"{ctx['session_id']}:{idx + 1}"
                try:
                    subprocess.run(
                        ["pbcopy"], input=ref.encode(),
                        check=True, timeout=2,
                    )
                    safe_addstr(height - 1, 0, f" copied: {ref}", width - 1, curses.A_REVERSE)
                    stdscr.refresh()
                    curses.napms(500)
                except (subprocess.SubprocessError, FileNotFoundError):
                    pass

            elif key == curses.KEY_DOWN or key == ord("j"):
                if focus == 0:
                    focus = 1
                    panel_scroll = 0
                elif focus == 1:
                    if panel_has_content[panel]:
                        focus = 2
                        panel_scroll = 0
                    # else: no content, stay on tabs
                else:  # focus == 2
                    panel_scroll += 1

            elif key == 27 or key == ord("t"):  # ESC or t → jump to timeline
                if focus > 0:
                    focus = 0
                    panel = 0
                    panel_scroll = 0

            elif key == curses.KEY_UP or key == ord("k"):
                if focus == 2:
                    if panel_scroll > 0:
                        panel_scroll -= 1
                    else:
                        focus = 1
                elif focus == 1:
                    focus = 0

            elif key == curses.KEY_SRIGHT or key == ord("L"):  # shift+right
                if focus == 0:
                    cursor = min(cursor + 5, len(all_prompts) - 1)
                    panel_scroll = 0

            elif key == curses.KEY_SLEFT or key == ord("H"):  # shift+left
                if focus == 0:
                    cursor = max(cursor - 5, 0)
                    panel_scroll = 0

            elif key == curses.KEY_RIGHT or key == ord("l"):
                if focus == 0:
                    if cursor < len(all_prompts) - 1:
                        cursor += 1
                        panel_scroll = 0
                elif focus == 1:
                    panel = (panel + 1) % len(PANELS)
                    panel_scroll = 0

            elif key == curses.KEY_LEFT or key == ord("h"):
                if focus == 0:
                    if cursor > 0:
                        cursor -= 1
                        panel_scroll = 0
                elif focus == 1:
                    panel = (panel - 1) % len(PANELS)
                    panel_scroll = 0

            elif key == curses.KEY_HOME or key == ord("g"):
                cursor = 0
                panel_scroll = 0
                focus = 0
            elif key == curses.KEY_END or key == ord("G"):
                cursor = len(all_prompts) - 1
                panel_scroll = 0
                focus = 0

    _run_inner()


def _cmd_export(session_id: str):
    sdir = session_dir(session_id)
    meta_file = sdir / "meta.json"
    if not meta_file.exists():
        print(f"Session {session_id} not found", file=__import__("sys").stderr)
        __import__("sys").exit(1)

    meta = json.loads(meta_file.read_text())
    summary_file = sdir / "summary.json"
    touches_file = sdir / "touches.jsonl"
    diffs_file = sdir / "diffs.jsonl"

    print(f"# Session {session_id}\n")
    print(f"- **Project**: {meta.get('project', '?')}")
    print(f"- **Started**: {meta.get('start_time', '?')}")

    if summary_file.exists():
        s = json.loads(summary_file.read_text())
        print(f"- **Ended**: {s.get('end_time', '?')}")
        if s.get("duration_seconds"):
            print(f"- **Duration**: {_format_duration(s['duration_seconds'])}")
        print(f"- **Prompts**: {s.get('prompts', '?')}")
        print(f"- **Files read**: {s.get('files_read', '?')}")
        print(f"- **Files written**: {s.get('files_written', '?')}")
        print(f"- **Total edits**: {s.get('total_edits', '?')}")
        tokens = s.get("tokens")
        if tokens:
            print(f"- **Model**: {tokens.get('model', '?')}")
            print(f"- **Tokens**: {_format_tokens(tokens.get('total_tokens', 0))} total ({_format_tokens(tokens.get('input_tokens', 0))} in, {_format_tokens(tokens.get('output_tokens', 0))} out)")
            if tokens.get("cache_read_tokens"):
                print(f"- **Cache**: {_format_tokens(tokens['cache_read_tokens'])} read, {_format_tokens(tokens.get('cache_write_tokens', 0))} written")

    # Load prompts, touches, and diffs
    prompts = _load_prompts(sdir)

    touches_by_prompt: dict[int | None, list[dict]] = {}
    if touches_file.exists():
        for line in touches_file.open():
            try:
                e = json.loads(line)
                touches_by_prompt.setdefault(e.get("prompt_idx"), []).append(e)
            except json.JSONDecodeError:
                pass

    diffs_by_prompt: dict[int | None, list[dict]] = {}
    if diffs_file.exists():
        for line in diffs_file.open():
            try:
                d = json.loads(line)
                diffs_by_prompt.setdefault(d.get("prompt_idx"), []).append(d)
            except json.JSONDecodeError:
                pass

    if prompts:
        # Group output by prompt
        for p in prompts:
            idx = p.get("idx", 0)
            print(f"\n## \"{p.get('prompt', '?')}\"\n")
            print(f"*{p.get('ts', '')}*\n")

            touches = touches_by_prompt.get(idx, [])
            if touches:
                print("| Action | File | Details |")
                print("|--------|------|---------|")
                for t in touches:
                    details = ""
                    if t.get("lines_changed"):
                        details = f"{t['lines_changed']} lines"
                    elif t.get("lines"):
                        details = f"{t['lines']} lines"
                    print(f"| {t.get('action', '')} | `{t.get('file', '')}` | {details} |")
                print()

            diffs = diffs_by_prompt.get(idx, [])
            for d in diffs:
                if d.get("old_string") is not None:
                    print(f"**`{d.get('file', '?')}`**")
                    print(f"```diff\n- {d['old_string']}\n+ {d.get('new_string', '')}\n```\n")
                elif d.get("is_new"):
                    print(f"**`{d.get('file', '?')}`** — new file ({d.get('lines', '?')} lines)\n")
    else:
        # No prompts — flat output
        if touches_by_prompt:
            print(f"\n## Activity\n")
            print("| Time | Action | File |")
            print("|------|--------|------|")
            for touches in touches_by_prompt.values():
                for e in touches:
                    print(f"| {e.get('ts', '')} | {e.get('action', '')} | `{e.get('file', '')}` |")

        if diffs_by_prompt:
            print(f"\n## Changes\n")
            for diffs in diffs_by_prompt.values():
                for d in diffs:
                    if d.get("old_string") is not None:
                        print(f"### `{d.get('file', '?')}` ({d.get('ts', '')})\n")
                        print(f"```diff\n- {d['old_string']}\n+ {d.get('new_string', '')}\n```\n")
                    elif d.get("is_new"):
                        print(f"New file `{d.get('file', '?')}` ({d.get('lines', '?')} lines)\n")
