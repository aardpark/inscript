"""Inscript MCP server — exposes session tools to Claude Code agents.

Run directly:
    inscript-mcp

Or via Claude Code:
    claude mcp add inscript -- inscript-mcp
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import (
    active_session,
    generate_log,
    generate_replay,
    list_sessions,
    session_dir,
)

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
        import json
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


def main():
    mcp.run()


if __name__ == "__main__":
    main()
