"""Inscript MCP server — exposes session tools to Claude Code agents.

Run directly:
    inscript-mcp

Or via Claude Code:
    claude mcp add inscript -- inscript-mcp
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from . import (
    _load_jsonl,
    _load_prompts,
    _rel_path,
    active_session,
    list_sessions,
    session_dir,
)
from .replay import generate_log, generate_replay, generate_file_history

mcp = FastMCP("inscript")


def _resolve_session(session_id: str | None, skip_current: bool = False) -> str | None:
    """Resolve a session ID, defaulting to the most recent completed session."""
    if session_id:
        return session_id

    current = active_session()
    sessions = list_sessions()

    if skip_current:
        for s in sessions:
            sid = s["session_id"]
            if sid == current:
                continue
            sdir = session_dir(sid)
            if (sdir / "summary.json").exists():
                return sid

    return current


def _resolve_session_by_prefix(prefix: str) -> str | None:
    """Find a session ID matching a short prefix."""
    sessions = list_sessions()
    for s in sessions:
        if s["session_id"].startswith(prefix):
            return s["session_id"]
    return None


@mcp.tool()
def replay(session_id: str | None = None) -> str:
    """Get a compact context summary of a previous session.

    Use this to catch up on what happened in a prior session — files modified,
    prompts exchanged, detours detected, and where work left off.
    Defaults to the most recent completed session.
    """
    sid = _resolve_session(session_id, skip_current=True)
    if not sid:
        return "No sessions found."
    result = generate_replay(sid)
    return result or f"No replay data for session {sid}."


@mcp.tool()
def log(session_id: str | None = None) -> str:
    """Show the activity log for a session — every prompt with files touched and token usage.

    Defaults to the current active session.
    """
    sid = _resolve_session(session_id)
    if not sid:
        return "No sessions found."
    result = generate_log(sid)
    return result or f"No activity log for session {sid}."


@mcp.tool()
def sessions() -> str:
    """List all recorded sessions with their IDs, timestamps, and status."""
    all_sessions = list_sessions()
    if not all_sessions:
        return "No sessions found."

    current = active_session()
    lines = [f"{len(all_sessions)} sessions recorded\n"]
    for s in all_sessions[:20]:
        sid = s["session_id"]
        start = s.get("start_time", "?")
        status = s.get("status", "?")
        marker = " (active)" if sid == current else ""
        lines.append(f"  {sid[:8]}  {start}  {status}{marker}")

    if len(all_sessions) > 20:
        lines.append(f"  ... and {len(all_sessions) - 20} more")

    return "\n".join(lines)


@mcp.tool()
def status() -> str:
    """Show current inscript status — active project and session."""
    from . import active_project, active_sessions

    proj = active_project()
    sess = active_session()
    lines = [f"Project: {proj or 'none'}"]

    if sess:
        sdir = session_dir(sess)
        meta_file = sdir / "meta.json"
        if meta_file.exists():
            meta = json.loads(meta_file.read_text())
            touches_file = sdir / "touches.jsonl"
            touch_count = sum(1 for _ in touches_file.open()) if touches_file.exists() else 0
            lines.append(f"Session: {sess} (started {meta.get('start_time', '?')}, {touch_count} touches)")
        else:
            lines.append(f"Session: {sess}")
    else:
        lines.append("Session: none")

    others = [s for s in active_sessions() if s.get("session_id") != sess]
    if others:
        lines.append(f"\nOther active sessions: {len(others)}")
        for s in others[:5]:
            lines.append(f"  {s['session_id']} — {s.get('project', '?')}")

    return "\n".join(lines)


@mcp.tool()
def message(ref: str) -> str:
    """Look up a specific message by reference. Accepts 'session_id:prompt_idx'
    (e.g. '1bf733d6:5') or just a prompt index for the current session (e.g. '5').

    Returns the prompt text, all file touches with actions, and diffs for that message.
    """
    if ":" in ref:
        sid_part, idx_part = ref.split(":", 1)
        try:
            prompt_idx = int(idx_part) - 1
        except ValueError:
            return f"Invalid prompt index: {idx_part}"
        sid = _resolve_session_by_prefix(sid_part)
        if not sid:
            return f"No session matching '{sid_part}'."
    else:
        try:
            prompt_idx = int(ref) - 1
        except ValueError:
            return f"Invalid reference: {ref}. Use 'session_id:prompt_idx' or just a number."
        sid = active_session()
        if not sid:
            return "No active session."

    sdir = session_dir(sid)
    prompts = _load_prompts(sdir)

    if prompt_idx < 0 or prompt_idx >= len(prompts):
        return f"Prompt {prompt_idx + 1} not found (session has {len(prompts)} prompts)."

    p = prompts[prompt_idx]

    project = None
    meta_file = sdir / "meta.json"
    if meta_file.exists():
        try:
            project = json.loads(meta_file.read_text()).get("project")
        except (json.JSONDecodeError, OSError):
            pass

    lines = [f"Session: {sid[:8]} | Prompt {prompt_idx + 1}/{len(prompts)}"]
    if p.get("ts"):
        lines[0] += f" | {p['ts']}"
    if p.get("tag"):
        lines[0] += f" | tag: {p['tag']}"
    lines.append("")
    lines.append(f"> {p.get('prompt', '')}")
    lines.append("")

    touches = _load_jsonl(sdir / "touches.jsonl")
    prompt_touches = [t for t in touches if t.get("prompt_idx") == prompt_idx]

    if prompt_touches:
        lines.append("Files:")
        for t in prompt_touches:
            f = _rel_path(t.get("file", "?"), project)
            action = t.get("action", "?")
            extra = ""
            if t.get("lines_changed"):
                extra = f" (+{t['lines_changed']} lines)"
            elif t.get("lines"):
                extra = f" ({t['lines']} lines)"
            lines.append(f"  {action:6s} {f}{extra}")
        lines.append("")

    diffs = _load_jsonl(sdir / "diffs.jsonl")
    prompt_diffs = [d for d in diffs if d.get("prompt_idx") == prompt_idx]

    if prompt_diffs:
        lines.append("Diffs:")
        for d in prompt_diffs:
            f = _rel_path(d.get("file", "?"), project)
            if d.get("tool") == "Edit":
                lines.append(f"  {f}:")
                lines.append(f"    - {d.get('old_string', '')}")
                lines.append(f"    + {d.get('new_string', '')}")
            elif d.get("tool") == "Write":
                new_marker = " (new file)" if d.get("is_new") else ""
                lines.append(f"  {f}: write {d.get('lines', '?')} lines{new_marker}")
                if d.get("content"):
                    content_lines = d["content"].split("\n")
                    for cl in content_lines[:10]:
                        lines.append(f"    {cl}")
                    if len(content_lines) > 10:
                        lines.append(f"    ... ({len(content_lines) - 10} more lines)")
            lines.append("")

    if not prompt_touches and not prompt_diffs:
        lines.append("(no file activity for this prompt)")

    return "\n".join(lines)


@mcp.tool()
def file_history(session_id: str, file_name: str) -> str:
    """Show the complete diff history for a file across all prompts in a session.

    Use this to understand the arc of changes to a specific file without
    expanding individual messages. Accepts partial file names (substring match).
    Each change links back to its prompt number for further drill-down.
    """
    sid = _resolve_session_by_prefix(session_id)
    if not sid:
        return f"No session matching '{session_id}'."
    result = generate_file_history(sid, file_name)
    return result or f"No data for '{file_name}' in session {sid[:8]}."


@mcp.tool()
def commits(session_id: str | None = None) -> str:
    """Show git commits made during a session, linked to the prompts that produced them.

    Each commit shows: hash, message, which prompt ran the commit,
    which earlier prompts contributed edits, and which files were involved.
    """
    sid = _resolve_session(session_id)
    if not sid:
        return "No sessions found."

    sdir = session_dir(sid)
    commit_data = _load_jsonl(sdir / "commits.jsonl")

    if not commit_data:
        return f"No commits recorded in session {sid[:8]}."

    prompts = _load_prompts(sdir)
    prompt_map = {p.get("idx", 0): p for p in prompts}

    short_id = sid[:8]
    lines = [f"Session: {short_id} | {len(commit_data)} commit(s)\n"]

    for c in commit_data:
        hash_short = c.get("hash", "?")[:7]
        msg = c.get("message", "")
        commit_prompt = c.get("prompt_idx")
        contributing = c.get("contributing_prompts", [])
        files = c.get("files", [])

        lines.append(f"  {hash_short}  {msg}")

        if commit_prompt is not None:
            p = prompt_map.get(commit_prompt)
            prompt_text = p.get("prompt", "")[:60] if p else "?"
            lines.append(f"    committed at prompt {commit_prompt + 1}: \"{prompt_text}\"")

        if contributing:
            lines.append(f"    contributing prompts: {', '.join(str(p + 1) for p in contributing)}")
            # Show the first few contributing prompts with their text
            for pidx in contributing[:5]:
                p = prompt_map.get(pidx)
                if p:
                    pt = p.get("prompt", "")
                    if len(pt) > 50:
                        pt = pt[:47] + "..."
                    lines.append(f"      {pidx + 1}. \"{pt}\"")
            if len(contributing) > 5:
                lines.append(f"      ... and {len(contributing) - 5} more")

        if files:
            project = c.get("project")
            for f in files[:10]:
                lines.append(f"    {_rel_path(f, project)}")
            if len(files) > 10:
                lines.append(f"    ... and {len(files) - 10} more files")

        lines.append("")

    lines.append(f"(expand any prompt with message tool: \"{short_id}:<number>\")")

    return "\n".join(lines)


@mcp.tool()
def note(text: str, ref: str | None = None) -> str:
    """Save a note linked to the current session and prompt.

    Use this to capture thoughts, ideas, or decisions that aren't
    reflected in code changes. Notes surface in replay output so
    future sessions can see them.

    Optionally include a ref to point at a file (spec, doc, etc).
    """
    from .commands import cmd_note as _cmd_note
    import io
    import contextlib

    f = io.StringIO()
    try:
        with contextlib.redirect_stdout(f):
            _cmd_note(text, ref)
        return f.getvalue().strip() or f"Note saved: {text}"
    except SystemExit:
        return "No active session — note not saved."


@mcp.tool()
def notes(session_id: str | None = None) -> str:
    """List all notes for a session. Defaults to current session."""
    from .commands import cmd_notes as _cmd_notes
    import io
    import contextlib

    sid = _resolve_session(session_id)
    if not sid:
        return "No sessions found."

    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        _cmd_notes(sid)
    return f.getvalue().strip() or f"No notes in session {sid[:8]}."


@mcp.tool()
def categories(session_id: str | None = None) -> str:
    """Analyze prompt categories for a session — the structural shape of each prompt.

    Each prompt is classified by its tool-use pattern (not content):
    idle, investigate, search, read-edit, deep-edit, explore-edit,
    direct, direct-multi, iteration, create, study-create.

    Shows the category sequence, work density, and dominant transitions.
    Pass 'all' to see recurring workflow patterns across sessions.
    """
    from .categories import format_session_categories, format_workflow_patterns

    if session_id == "all":
        return format_workflow_patterns()

    sid = _resolve_session(session_id)
    if not sid:
        return "No sessions found."
    return format_session_categories(sid)


@mcp.tool()
def concepts(file_name: str | None = None) -> str:
    """Detect cross-session concepts — recurring clusters of files worked on together.

    Concepts emerge from behavioral co-occurrence: files that repeatedly appear
    in the same prompts across multiple sessions form a concept.

    Without arguments, lists all detected concepts.
    With a file_name, shows the cross-session history for that file's concept.
    """
    from .concepts import detect_concepts, format_concepts, concept_for_file, concept_history
    from . import active_project

    detected = detect_concepts()
    if file_name:
        c = concept_for_file(file_name, detected)
        if c:
            return concept_history(c)
        return f"No concept found for '{file_name}'."

    proj = active_project()
    return format_concepts(detected, str(proj) if proj else None)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
