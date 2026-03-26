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
    SESSIONS_DIR,
    OVERLAP_DIR,
    _append_jsonl,
    active_session_for_hook,
    set_active_session_for_hook,
    active_sessions,
    project_hash,
    session_dir,
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
    if path and Path(path).is_absolute():
        return path

    command = inp.get("command", "")
    if command:
        # Unix absolute paths
        matches = re.findall(r"(/[^\s;|&\"']+)", command)
        if matches:
            return matches[0]
        # Windows absolute paths (C:\...)
        matches = re.findall(r"([A-Za-z]:\\[^\s;|&\"']+)", command)
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
    # Finalize previous session for THIS Claude Code instance (per-PPID)
    prev_session = active_session_for_hook()
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

    # Mark as active session (per-PPID + global)
    set_active_session_for_hook(session_id)

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
    lines.append("Use inscript MCP tools (replay, log, message, file_history, commits) for full context. `inscript explore` opens the interactive TUI.")

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

    # Cross-session notes: include recent notes from all sessions on the same project
    project = meta.get("project")
    if project:
        try:
            from . import list_sessions, _load_jsonl
            cross_notes = []
            for s in list_sessions():
                sid = s.get("session_id", "")
                if sid == prev_session:
                    continue
                if s.get("project") != project:
                    continue
                s_notes = _load_jsonl(SESSIONS_DIR / sid / "notes.jsonl")
                for n in s_notes[-5:]:  # Last 5 notes per session
                    cross_notes.append((sid[:8], s.get("start_time", "")[:10], n))

            if cross_notes:
                lines.append("")
                lines.append(f"Notes from other sessions on this project ({len(cross_notes)}):")
                for sid_short, date, n in cross_notes[-10:]:  # Cap at 10 total
                    text = n.get("text", "")
                    if len(text) > 100:
                        text = text[:97] + "..."
                    ref = n.get("ref")
                    line = f"  [{sid_short} {date}] {text}"
                    if ref:
                        line += f" -> {ref.split('/')[-1]}"
                    lines.append(line)
        except Exception:
            pass  # Don't block handoff if cross-notes fail

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# UserPromptSubmit handler
# ---------------------------------------------------------------------------

def handle_prompt_submit(data: dict) -> None:
    """Record a user prompt, starting a new work block."""
    session_id = active_session_for_hook()
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

    # Auto-capture /btw messages as notes
    # The agent's response is in the transcript at this prompt index —
    # future sessions can use message tool to expand: "session:prompt_idx"
    stripped = prompt.strip()
    if stripped.lower().startswith("/btw ") or stripped.lower().startswith("/btw\n"):
        note_text = stripped[4:].strip()
        if note_text:
            note_entry = {
                "ts": _now_time(),
                "text": note_text,
                "prompt_idx": prompt_idx,
                "source": "btw",
                "has_response": True,  # expand with message tool for agent's reply
            }
            if tag:
                note_entry["tag"] = tag
            _append_jsonl(sdir / "notes.jsonl", note_entry)


# ---------------------------------------------------------------------------
# PostToolUse handler
# ---------------------------------------------------------------------------

def handle_post_tool_use(data: dict) -> None:
    """Record a file touch, update active project, record diffs, detect git commits."""
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    tool_response = data.get("tool_response", {})

    # Git commit detection (Bash tool)
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if _is_git_commit(command):
            stdout = tool_response.get("stdout", "")
            if tool_response.get("success", False) or tool_response.get("exit_code") == 0:
                _handle_git_commit(data, command, stdout)
        return  # Bash touches are not file touches

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
    session_id = active_session_for_hook()
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
        _check_overlap(session_id, str(root), display_path, action=action)


# ---------------------------------------------------------------------------
# Git commit detection
# ---------------------------------------------------------------------------

def _is_git_commit(command: str) -> bool:
    """Check if a bash command is a git commit."""
    # Match: git commit, git -C ... commit, etc.
    # Exclude: git commit --amend (still a commit, still track it)
    # Exclude: git log, git status, git diff, etc.
    stripped = command.strip()
    # Handle chained commands: look for git commit in any segment
    for segment in re.split(r'[;&|]+', stripped):
        segment = segment.strip()
        if re.match(r'^git\b.*\bcommit\b', segment):
            return True
        # Handle: git -C /path commit ...
        if re.match(r'^git\s+-C\s+\S+\s+commit\b', segment):
            return True
    return False


def _parse_commit_hash(stdout: str) -> str | None:
    """Extract commit hash from git commit output.

    Git commit output looks like:
      [main abc1234] commit message
    or:
      [main (root-commit) abc1234] commit message
    """
    match = re.search(r'\[[\w/.-]+\s+(?:\([\w-]+\)\s+)?([0-9a-f]{7,40})\]', stdout)
    if match:
        return match.group(1)
    return None


def _parse_commit_message(stdout: str) -> str:
    """Extract commit message from git commit output."""
    match = re.search(r'\[[^\]]+\]\s+(.+?)(?:\n|$)', stdout)
    if match:
        return match.group(1).strip()
    return ""


def _handle_git_commit(data: dict, command: str, stdout: str) -> None:
    """Record a git commit and link it to the prompts that produced it."""
    commit_hash = _parse_commit_hash(stdout)
    if not commit_hash:
        return

    session_id = active_session_for_hook()
    if not session_id:
        return

    sdir = SESSIONS_DIR / session_id
    prompt_idx = _current_prompt_idx(sdir)
    commit_message = _parse_commit_message(stdout)

    # Find the project (git root) from the cwd or active project
    project = None
    cwd = data.get("cwd", "")
    if cwd:
        root = _find_git_root(cwd)
        if root:
            project = str(root)
    if not project:
        try:
            project = ACTIVE_PROJECT_FILE.read_text().strip()
        except OSError:
            pass

    # Walk backwards through recent touches to find which prompts edited
    # files that are likely in this commit
    touches = []
    touches_file = sdir / "touches.jsonl"
    if touches_file.exists():
        for line in touches_file.open():
            try:
                touches.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    # Find prompts that edited files (these are the prompts that contributed)
    contributing_prompts: dict[int, set[str]] = {}  # prompt_idx -> set of files edited
    for t in touches:
        if t.get("action") in ("edit", "write"):
            pidx = t.get("prompt_idx")
            if pidx is not None:
                contributing_prompts.setdefault(pidx, set()).add(t.get("file", ""))

    # The commit prompt is the one that ran git commit
    # Contributing prompts are all prompts that edited files before this commit
    # (since the last commit, if we have that info)
    last_commit_prompt = None
    commits = []
    commits_file = sdir / "commits.jsonl"
    if commits_file.exists():
        for line in commits_file.open():
            try:
                commits.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if commits:
        last_commit_prompt = commits[-1].get("prompt_idx")

    # Only include prompts after the last commit
    relevant_prompts = sorted(
        pidx for pidx in contributing_prompts
        if last_commit_prompt is None or pidx > last_commit_prompt
    )

    # Collect the files those prompts edited
    committed_files = set()
    for pidx in relevant_prompts:
        committed_files.update(contributing_prompts[pidx])

    entry = {
        "ts": _now_time(),
        "hash": commit_hash,
        "message": commit_message,
        "prompt_idx": prompt_idx,
        "contributing_prompts": relevant_prompts,
        "files": sorted(committed_files),
    }
    if project:
        entry["project"] = project

    tag = _current_tag(sdir)
    if tag:
        entry["tag"] = tag

    _append_jsonl(sdir / "commits.jsonl", entry)


def _check_overlap(current_session: str, project: str, file_path: str,
                    action: str = "touch") -> None:
    """Check if other active sessions are touching the same project.

    Detects write-write conflicts: when this session edits/writes a file
    that another active session has also edited/written.
    """
    others = active_sessions()
    overlapping = [
        s["session_id"] for s in others
        if s["session_id"] != current_session and s.get("project") == project
    ]

    if not overlapping:
        return

    # Check for write-write conflict: did another session also EDIT this file?
    conflict = False
    conflict_sessions = []
    if action in ("edit", "write"):
        for other_sid in overlapping:
            other_sdir = session_dir(other_sid)
            other_touches = other_sdir / "touches.jsonl"
            if not other_touches.exists():
                continue
            try:
                for line in other_touches.open():
                    try:
                        t = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (t.get("file") == file_path
                            and t.get("action") in ("edit", "write")):
                        conflict = True
                        conflict_sessions.append(other_sid)
                        break
            except OSError:
                pass

    OVERLAP_DIR.mkdir(parents=True, exist_ok=True)
    ph = project_hash(project)
    entry = {
        "ts": _now_time(),
        "file": file_path,
        "action": action,
        "project": project,
        "sessions": [current_session] + overlapping,
    }
    if conflict:
        entry["conflict"] = True
        entry["conflict_sessions"] = [current_session] + conflict_sessions
    _append_jsonl(OVERLAP_DIR / f"{ph}.jsonl", entry)


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
    except Exception:
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
    session_id = active_session_for_hook()
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

    try:
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
    except Exception:
        pass  # Hook must never crash — Claude Code waits for sync hooks


if __name__ == "__main__":
    main()
