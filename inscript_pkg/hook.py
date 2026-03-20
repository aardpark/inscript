"""Inscript hook — records agent activity to ~/.inscript/.

Handles four Claude Code hook events:
  SessionStart    → creates session directory + meta.json (finalizes previous session)
  UserPromptSubmit → records the user's prompt, starts a new work block
  PostToolUse     → appends to touches.jsonl + diffs.jsonl, tags with prompt + project
  Stop            → updates running summary snapshot (session stays active)

All entry points read JSON from stdin and write to the filesystem.
Designed to run async (non-blocking) so the agent never waits.

Install:
    pip install inscript
    # Then add hooks to ~/.claude/settings.json (see README)
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import (
    INSCRIPT_DIR,
    ACTIVE_PROJECT_FILE,
    ACTIVE_SESSION_FILE,
    SESSIONS_DIR,
    OVERLAP_DIR,
    active_session,
    active_sessions,
    project_hash,
    store_diffs,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _now_time() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _extract_path(data: dict) -> str | None:
    """Extract a file path from hook input JSON."""
    inp = data.get("tool_input", {})

    path = inp.get("file_path") or inp.get("path")
    if path and path.startswith("/"):
        return path

    command = inp.get("command", "")
    if command:
        matches = re.findall(r"(/[^\s;|&\"']+)", command)
        if matches:
            return matches[0]

    return None


def _find_git_root(path: str) -> Path | None:
    """Walk up from path to find the git root."""
    p = Path(path)
    if p.is_file():
        p = p.parent
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    return None


def _tool_action(tool_name: str) -> str:
    """Map tool name to action verb."""
    return {
        "Read": "read",
        "Edit": "edit",
        "Write": "write",
        "Glob": "glob",
        "Grep": "grep",
    }.get(tool_name, "touch")


def _append_jsonl(path: Path, data: dict) -> None:
    """Append a JSON line to a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(data, default=str) + "\n")


def _count_lines(path: Path) -> int:
    """Count lines in a JSONL file."""
    try:
        return sum(1 for _ in path.open())
    except OSError:
        return 0


def _current_prompt_idx(sdir: Path) -> int | None:
    """Get the index of the most recent prompt (0-based)."""
    n = _count_lines(sdir / "prompts.jsonl")
    return n - 1 if n > 0 else None


def _log_warning(sdir: Path, message: str) -> None:
    """Log a warning event to the session's warnings.jsonl."""
    _append_jsonl(sdir / "warnings.jsonl", {
        "ts": _now_time(),
        "message": message,
    })


def _current_tag(sdir: Path) -> str | None:
    """Read the active tag for this session."""
    tag_file = sdir / "active_tag"
    try:
        return tag_file.read_text().strip() or None
    except OSError:
        return None


def _current_branch(sdir: Path) -> int | None:
    """Read the active branch ID for this session."""
    branch_file = sdir / "active_branch"
    try:
        text = branch_file.read_text().strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# SessionStart handler
# ---------------------------------------------------------------------------

def handle_session_start(data: dict) -> dict | None:
    """Create a new session directory. Finalizes any previous active session.

    Returns a dict with hookSpecificOutput if there's a replay to inject,
    or None if no context to inject.
    """
    # Finalize previous session and capture its ID for replay
    prev_session = active_session()
    if prev_session:
        _finalize_session(prev_session)

    session_id = data.get("session_id", f"s-{int(time.time())}")

    sdir = SESSIONS_DIR / session_id
    sdir.mkdir(parents=True, exist_ok=True)

    # Read current project if available
    proj = None
    try:
        proj = ACTIVE_PROJECT_FILE.read_text().strip()
    except OSError:
        pass

    meta = {
        "start_time": _now_iso(),
        "project": proj,
        "status": "active",
    }

    # Save transcript path if provided (for token counting on Stop)
    transcript_path = data.get("transcript_path")
    if transcript_path:
        meta["transcript_path"] = transcript_path

    (sdir / "meta.json").write_text(json.dumps(meta, indent=2))

    # Mark as active session
    INSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_SESSION_FILE.write_text(session_id + "\n")

    # Generate compact handoff context for the new session
    if prev_session:
        try:
            context = _build_handoff_context(prev_session)
            if context:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": context,
                    }
                }
        except Exception:
            pass  # Don't block session start if handoff fails

    return None


def _build_handoff_context(prev_session: str) -> str | None:
    """Build a compact handoff: pointer + recent prompt sample.

    ~150 tokens instead of full replay. Agent can run `inscript replay`
    or `inscript viz <N>` to drill into details.
    """
    sdir = SESSIONS_DIR / prev_session

    # Load meta
    meta_file = sdir / "meta.json"
    if not meta_file.exists():
        return None
    try:
        meta = json.loads(meta_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    # Load summary
    summary = {}
    summary_file = sdir / "summary.json"
    if summary_file.exists():
        try:
            summary = json.loads(summary_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # Load prompts and touches
    prompts = []
    prompts_file = sdir / "prompts.jsonl"
    if prompts_file.exists():
        for line in prompts_file.open():
            try:
                prompts.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    if not prompts:
        return None

    touches = []
    touches_file = sdir / "touches.jsonl"
    if touches_file.exists():
        for line in touches_file.open():
            try:
                touches.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    # Top edited files
    file_edits: dict[str, int] = {}
    for t in touches:
        if t.get("action") in ("edit", "write"):
            f = t.get("file", "?").split("/")[-1]
            file_edits[f] = file_edits.get(f, 0) + 1
    top_files = sorted(file_edits.items(), key=lambda x: -x[1])[:3]

    # Duration
    duration = ""
    if summary.get("duration_seconds"):
        secs = summary["duration_seconds"]
        if secs >= 3600:
            h, m = divmod(secs, 3600)
            duration = f"{int(h)}h {int(m // 60)}m"
        elif secs >= 60:
            duration = f"{int(secs // 60)}m"
        else:
            duration = f"{secs}s"

    total_edits = summary.get("total_edits", len(file_edits))

    # Detour count (quick check via behavioral inference)
    try:
        from . import infer_branches
        tbp: dict[int | None, list[dict]] = {}
        for t in touches:
            tbp.setdefault(t.get("prompt_idx"), []).append(t)
        inferred = infer_branches(prompts, tbp)
        detour_count = len(inferred)
    except Exception:
        detour_count = 0

    # Build header
    lines = []
    files_str = ", ".join(f"{name} ({count} edits)" for name, count in top_files)
    lines.append(f"Previous session: {prev_session[:8]} ({duration}, {total_edits} edits, {len(prompts)} prompts)")
    if files_str:
        lines.append(f"Main files: {files_str}")
    if detour_count:
        lines.append(f"{detour_count} detour(s) detected.")
    lines.append("Run `inscript replay` or `inscript viz <N>` for full context.")

    # Last few prompts with activity summary
    tail_count = min(5, len(prompts))
    tail = prompts[-tail_count:]
    lines.append("")
    lines.append(f"Last {tail_count} prompts:")
    for p in tail:
        idx = p.get("idx", 0)
        text = p.get("prompt", "")
        if len(text) > 70:
            text = text[:67] + "..."

        # Summarize touches for this prompt
        prompt_touches = [t for t in touches if t.get("prompt_idx") == idx]
        edit_files = sorted({t.get("file", "?").split("/")[-1]
                           for t in prompt_touches
                           if t.get("action") in ("edit", "write")})
        touch_str = ""
        if edit_files:
            touch_str = " -> edited " + ", ".join(edit_files[:3])
        elif prompt_touches:
            read_files = sorted({t.get("file", "?").split("/")[-1]
                               for t in prompt_touches
                               if t.get("action") == "read"})
            if read_files:
                touch_str = " -> read " + ", ".join(read_files[:3])

        lines.append(f"  {idx + 1}. \"{text}\"{touch_str}")

    # Flag if last prompt may be unfinished
    last_touches = [t for t in touches if t.get("prompt_idx") == prompts[-1].get("idx")]
    has_edits = any(t.get("action") in ("edit", "write") for t in last_touches)
    if not has_edits and last_touches:
        lines.append("     ^ reads only -- may be unfinished")
    elif not last_touches:
        lines.append("     ^ no file activity -- may be unfinished")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# UserPromptSubmit handler
# ---------------------------------------------------------------------------

def handle_prompt_submit(data: dict) -> None:
    """Record a user prompt, starting a new work block."""
    session_id = active_session()
    if not session_id:
        session_id = f"s-{int(time.time())}"
        handle_session_start({"session_id": session_id})
        sdir = SESSIONS_DIR / session_id
        _log_warning(sdir, f"Implicit session created by UserPromptSubmit (no active session found)")
    else:
        sdir = SESSIONS_DIR / session_id

    # Extract prompt text from hook input
    # Claude Code sends tool_input with the user's message
    prompt = data.get("tool_input", {}).get("prompt", "")
    if not prompt:
        # Try alternate locations
        prompt = data.get("prompt", "") or data.get("message", "")
    if not prompt:
        return

    prompt_idx = _count_lines(sdir / "prompts.jsonl")
    tag = _current_tag(sdir)
    branch_id = _current_branch(sdir)

    entry = {
        "idx": prompt_idx,
        "ts": _now_time(),
        "prompt": prompt,
    }
    if tag:
        entry["tag"] = tag
    if branch_id is not None:
        entry["branch_id"] = branch_id

    _append_jsonl(sdir / "prompts.jsonl", entry)


# ---------------------------------------------------------------------------
# PostToolUse handler
# ---------------------------------------------------------------------------

def handle_post_tool_use(data: dict) -> None:
    """Record a file touch, update active project, record diffs."""
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    tool_response = data.get("tool_response", {})

    # Extract file path
    file_path = _extract_path(data)
    if not file_path:
        return

    # Update active project
    root = _find_git_root(file_path)
    if root:
        try:
            current = ACTIVE_PROJECT_FILE.read_text().strip()
            if current != str(root):
                INSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
                ACTIVE_PROJECT_FILE.write_text(str(root) + "\n")
        except OSError:
            INSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
            ACTIVE_PROJECT_FILE.write_text(str(root) + "\n")

    # Get or create session
    session_id = active_session()
    if not session_id:
        # No session yet — create one implicitly
        session_id = f"s-{int(time.time())}"
        handle_session_start({"session_id": session_id})
        sdir = SESSIONS_DIR / session_id
        _log_warning(sdir, f"Implicit session created by PostToolUse (no active session found)")
    else:
        sdir = SESSIONS_DIR / session_id

    # Update session meta with project + transcript_path if needed
    meta_file = sdir / "meta.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
            changed = False
            if root and meta.get("project") != str(root):
                meta["project"] = str(root)
                changed = True
            transcript_path = data.get("transcript_path")
            if transcript_path and not meta.get("transcript_path"):
                meta["transcript_path"] = transcript_path
                changed = True
            if changed:
                meta_file.write_text(json.dumps(meta, indent=2))
        except (json.JSONDecodeError, OSError):
            pass

    # Always store absolute paths — let CLI relativize for display
    display_path = file_path

    # Append to touches.jsonl
    action = _tool_action(tool_name)
    prompt_idx = _current_prompt_idx(sdir)
    tag = _current_tag(sdir)
    branch_id = _current_branch(sdir)
    touch = {
        "ts": _now_time(),
        "file": display_path,
        "action": action,
        "tool": tool_name,
    }
    if root:
        touch["project"] = str(root)
    if prompt_idx is not None:
        touch["prompt_idx"] = prompt_idx
    if tag:
        touch["tag"] = tag
    if branch_id is not None:
        touch["branch_id"] = branch_id

    # Add lines_changed for Edit
    if tool_name == "Edit" and tool_input.get("new_string") is not None:
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        touch["lines_changed"] = abs(new.count("\n") - old.count("\n")) + 1

    # Add line count for Write
    if tool_name == "Write" and tool_input.get("content"):
        touch["lines"] = tool_input["content"].count("\n") + 1

    _append_jsonl(sdir / "touches.jsonl", touch)

    # Append to diffs.jsonl for Edit/Write
    if store_diffs() and tool_name in ("Edit", "Write"):
        diff_entry: dict = {
            "ts": _now_time(),
            "file": display_path,
            "tool": tool_name,
        }
        if prompt_idx is not None:
            diff_entry["prompt_idx"] = prompt_idx

        if tool_name == "Edit":
            diff_entry["old_string"] = tool_input.get("old_string", "")
            diff_entry["new_string"] = tool_input.get("new_string", "")
            if tool_input.get("replace_all"):
                diff_entry["replace_all"] = True

        elif tool_name == "Write":
            content = tool_input.get("content", "")
            diff_entry["content_hash"] = hashlib.sha256(content.encode()).hexdigest()[:16]
            diff_entry["lines"] = content.count("\n") + 1
            diff_entry["is_new"] = not Path(file_path).exists()
            if diff_entry["is_new"]:
                diff_entry["content"] = content

        _append_jsonl(sdir / "diffs.jsonl", diff_entry)

    # Check for overlap with other active sessions
    if root:
        _check_overlap(session_id, str(root), display_path)


def _check_overlap(current_session: str, project: str, file_path: str) -> None:
    """Check if other active sessions are touching the same project."""
    others = active_sessions()
    overlapping = [
        s["session_id"] for s in others
        if s["session_id"] != current_session and s.get("project") == project
    ]

    if not overlapping:
        return

    OVERLAP_DIR.mkdir(parents=True, exist_ok=True)
    ph = project_hash(project)
    _append_jsonl(OVERLAP_DIR / f"{ph}.jsonl", {
        "ts": _now_time(),
        "file": file_path,
        "project": project,
        "sessions": [current_session] + overlapping,
    })


# ---------------------------------------------------------------------------
# Stop handler
# ---------------------------------------------------------------------------

def _compute_token_usage(transcript_path: str) -> dict | None:
    """Read a Claude Code transcript JSONL and sum token usage."""
    try:
        tp = Path(transcript_path)
        if not tp.exists():
            return None

        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_write = 0
        model = None

        for line in tp.open():
            try:
                d = json.loads(line)
                msg = d.get("message", {})
                usage = msg.get("usage")
                if not usage:
                    continue

                if model is None:
                    model = msg.get("model")

                total_input += usage.get("input_tokens", 0)
                total_output += usage.get("output_tokens", 0)
                total_cache_read += usage.get("cache_read_input_tokens", 0)
                total_cache_write += usage.get("cache_creation_input_tokens", 0)
            except json.JSONDecodeError:
                continue

        if total_input == 0 and total_output == 0:
            return None

        return {
            "model": model,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read_tokens": total_cache_read,
            "cache_write_tokens": total_cache_write,
            "total_tokens": total_input + total_output,
        }
    except (OSError, Exception):
        return None


def _build_summary(sdir: Path, data: dict) -> dict:
    """Build a summary dict from session data. Used by both Stop and finalize."""
    touches_file = sdir / "touches.jsonl"

    files_read = set()
    files_written = set()
    total_edits = 0
    total_lines = 0

    if touches_file.exists():
        for line in touches_file.open():
            try:
                e = json.loads(line)
                f = e.get("file", "")
                action = e.get("action", "")
                if action == "read":
                    files_read.add(f)
                elif action in ("edit", "write"):
                    files_written.add(f)
                    total_edits += 1
                    total_lines += e.get("lines_changed", e.get("lines", 0))
            except json.JSONDecodeError:
                pass

    # Read start time and transcript path from meta
    meta_file = sdir / "meta.json"
    start_time = None
    transcript_path = None
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
            start_time = meta.get("start_time")
            transcript_path = meta.get("transcript_path")
            if not transcript_path:
                transcript_path = data.get("transcript_path")
        except (json.JSONDecodeError, OSError):
            pass

    end_time = _now_iso()
    duration = None
    if start_time:
        try:
            start_dt = datetime.fromisoformat(start_time)
            end_dt = datetime.fromisoformat(end_time)
            duration = int((end_dt - start_dt).total_seconds())
        except ValueError:
            pass

    prompts_file = sdir / "prompts.jsonl"
    prompt_count = _count_lines(prompts_file)

    summary: dict = {
        "end_time": end_time,
        "duration_seconds": duration,
        "prompts": prompt_count,
        "files_read": len(files_read),
        "files_written": len(files_written),
        "files_read_list": sorted(files_read),
        "files_written_list": sorted(files_written),
        "total_edits": total_edits,
        "total_lines_changed": total_lines,
    }

    if transcript_path:
        token_usage = _compute_token_usage(transcript_path)
        if token_usage:
            summary["tokens"] = token_usage

    return summary


def handle_stop(data: dict) -> None:
    """Update running summary snapshot. Does NOT finalize the session —
    Claude Code fires Stop at the end of every turn, not just session end.
    Session finalization happens when a new SessionStart arrives."""
    session_id = active_session()
    if not session_id:
        return

    sdir = SESSIONS_DIR / session_id
    summary = _build_summary(sdir, data)
    summary["status"] = "snapshot"
    (sdir / "summary.json").write_text(json.dumps(summary, indent=2))
    # NOTE: Do NOT delete active_session — session continues across turns


def _finalize_session(session_id: str) -> None:
    """Mark a session as completed and write final summary."""
    sdir = SESSIONS_DIR / session_id
    if not sdir.exists():
        return

    summary = _build_summary(sdir, {})
    summary["status"] = "completed"
    (sdir / "summary.json").write_text(json.dumps(summary, indent=2))

    meta_file = sdir / "meta.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
            meta["status"] = "completed"
            meta_file.write_text(json.dumps(meta, indent=2))
        except (json.JSONDecodeError, OSError):
            pass


# ---------------------------------------------------------------------------
# Main dispatch — reads hook event from stdin JSON
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for all hook events. Dispatches based on hook_event field
    or falls back to PostToolUse behavior for backwards compatibility."""
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return

    # Claude Code sends "hook_event_name"; also accept "hook_event" for manual testing
    hook_event = data.get("hook_event_name") or data.get("hook_event", "")

    if hook_event == "SessionStart":
        result = handle_session_start(data)
        if result:
            print(json.dumps(result))
    elif hook_event == "UserPromptSubmit":
        handle_prompt_submit(data)
    elif hook_event == "Stop":
        handle_stop(data)
    else:
        # Default: PostToolUse (also handles backwards compat)
        handle_post_tool_use(data)


if __name__ == "__main__":
    main()
