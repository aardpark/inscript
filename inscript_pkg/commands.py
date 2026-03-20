"""CLI commands: tag, time, branch, resume, branches, overlap, cleanup, export, note."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import (
    INSCRIPT_DIR,
    CONFIG_FILE,
    OVERLAP_DIR,
    SESSIONS_DIR,
    _format_tokens,
    _load_config,
    _load_jsonl,
    _load_prompts,
    active_session,
    list_sessions,
    session_dir,
)
from .replay import _format_duration


def _append_jsonl(path: Path, data: dict) -> None:
    """Append a JSON line to a file."""
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
            diff += 86400
        return diff
    except (ValueError, IndexError):
        return 0


def compute_prompt_durations(
    prompts: list[dict],
    touches_by_prompt: dict[int | None, list[dict]],
) -> list[dict]:
    """Compute temporal breakdown for each prompt."""
    results = []
    for i, p in enumerate(prompts):
        idx = p.get("idx", 0)
        ts = p.get("ts", "")
        pts = touches_by_prompt.get(idx, [])

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


def cmd_note(text: str, ref: str | None = None):
    """Add a note to the current session."""
    import sys as _sys
    from . import active_project

    if not text:
        print("Usage: inscript note \"your thought\" [--ref file.md]", file=_sys.stderr)
        _sys.exit(1)

    sess = active_session()
    if not sess:
        print("No active session", file=_sys.stderr)
        _sys.exit(1)

    sdir = session_dir(sess)
    prompts = _load_prompts(sdir)
    prompt_idx = prompts[-1].get("idx") if prompts else None

    entry: dict = {
        "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "text": text,
    }
    if prompt_idx is not None:
        entry["prompt_idx"] = prompt_idx
    if ref:
        # Resolve ref to absolute path if it's a relative path
        ref_path = Path(ref)
        if not ref_path.is_absolute():
            proj = active_project()
            if proj:
                ref_path = proj / ref
        entry["ref"] = str(ref_path)

    tag_file = sdir / "active_tag"
    try:
        tag = tag_file.read_text().strip()
        if tag:
            entry["tag"] = tag
    except OSError:
        pass

    _append_jsonl(sdir / "notes.jsonl", entry)
    display = f"Note: {text}"
    if ref:
        display += f" -> {ref}"
    print(display)


def _format_notes(sess: str, notes: list[dict], prompts: list[dict], show_session: bool = False) -> list[str]:
    """Format notes for display. Returns lines."""
    lines = []
    prompt_map = {p.get("idx", 0): p for p in prompts}
    prefix = f"{sess[:8]} " if show_session else ""

    for n in notes:
        ts = n.get("ts", "?")
        text = n.get("text", "")
        ref = n.get("ref")
        pidx = n.get("prompt_idx")
        tag = n.get("tag")

        prompt_hint = ""
        if pidx is not None:
            p = prompt_map.get(pidx)
            if p:
                pt = p.get("prompt", "")[:40]
                prompt_hint = f" (after: \"{pt}\")"

        line = f"  {prefix}[{ts}] {text}"
        if ref:
            line += f"\n         {' ' * len(prefix)}-> {ref}"
        if tag:
            line += f"  #{tag}"
        if prompt_hint:
            line += f"\n        {' ' * len(prefix)}{prompt_hint}"
        lines.append(line)
        lines.append("")

    return lines


def cmd_notes(session_id: str | None = None, page: int = 0, page_size: int = 10):
    """List notes for a session, or all sessions if 'all'.

    When showing all sessions, results are paged (most recent first).
    Use --older to see the next batch.
    """
    if session_id == "all":
        all_sessions = list_sessions()
        all_notes = []
        all_refs = []
        for s in all_sessions:
            sid = s["session_id"]
            sdir = session_dir(sid)
            notes = _load_jsonl(sdir / "notes.jsonl")
            if notes:
                prompts = _load_prompts(sdir)
                all_notes.append((sid, notes, prompts))
                all_refs.extend(n.get("ref") for n in notes if n.get("ref"))

        if not all_notes:
            print("No notes across any session")
            return

        total = sum(len(notes) for _, notes, _ in all_notes)
        total_sessions = len(all_notes)

        # Page through sessions (most recent first, already sorted by list_sessions)
        start = page * page_size
        end = start + page_size
        batch = all_notes[start:end]

        if not batch:
            print("No more notes.")
            return

        showing = f"showing {start + 1}-{min(end, total_sessions)}" if total_sessions > page_size else ""
        print(f"{total} note(s) across {total_sessions} session(s)" +
              (f" ({showing})" if showing else "") + "\n")

        batch_refs = []
        for sid, notes, prompts in batch:
            meta_file = session_dir(sid) / "meta.json"
            start_time = ""
            if meta_file.exists():
                try:
                    start_time = json.loads(meta_file.read_text()).get("start_time", "")[:10]
                except (json.JSONDecodeError, OSError):
                    pass
            refs = sum(1 for n in notes if n.get("ref"))
            ref_hint = f", {refs} ref(s)" if refs else ""
            print(f"  {sid[:8]}  {start_time}  {len(notes)} note(s){ref_hint}")
            batch_refs.extend(n.get("ref") for n in notes if n.get("ref"))

        if batch_refs:
            print(f"\nReferenced files:")
            for r in sorted(set(batch_refs)):
                print(f"  {r}")

        if end < total_sessions:
            remaining = total_sessions - end
            print(f"\n  {remaining} more session(s) — inscript notes all --older")

        return

    if session_id:
        sess = session_id
    else:
        sess = active_session()
    if not sess:
        sessions = list_sessions()
        if sessions:
            sess = sessions[0]["session_id"]
        else:
            print("No sessions found")
            return

    sdir = session_dir(sess)
    notes = _load_jsonl(sdir / "notes.jsonl")
    if not notes:
        print(f"No notes in session {sess[:8]}")
        return

    prompts = _load_prompts(sdir)

    print(f"Session {sess[:8]} — {len(notes)} note(s)\n")
    for line in _format_notes(sess, notes, prompts):
        print(line)

    refs = [n.get("ref") for n in notes if n.get("ref")]
    if refs:
        print("Referenced files:")
        for r in refs:
            print(f"  {r}")


def cmd_tag(tag_name: str | None):
    sess = active_session()
    if not sess:
        print("No active session", file=__import__("sys").stderr)
        __import__("sys").exit(1)
    sdir = session_dir(sess)
    tag_file = sdir / "active_tag"
    if tag_name is None:
        try:
            tag_file.unlink()
        except OSError:
            pass
        print("Tag cleared")
    else:
        tag_file.write_text(tag_name + "\n")
        print(f"Tagged: {tag_name}")


def cmd_time(tag_filter: str | None):
    all_sessions = list_sessions()
    if not all_sessions:
        print("No sessions found")
        return

    tag_data: dict[str | None, dict] = {}

    for s in all_sessions:
        sid = s["session_id"]
        sdir = session_dir(sid)
        prompts = _load_prompts(sdir)
        touches: list[dict] = []
        touches_file = sdir / "touches.jsonl"
        if touches_file.exists():
            for line in touches_file.open():
                try:
                    touches.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        if not prompts:
            continue

        for i, p in enumerate(prompts):
            tag = p.get("tag")
            if tag_filter and tag != tag_filter:
                continue
            prompt_ts = p.get("ts", "")
            if i + 1 < len(prompts):
                next_ts = prompts[i + 1].get("ts", "")
            else:
                summary_file = sdir / "summary.json"
                if summary_file.exists():
                    try:
                        summary = json.loads(summary_file.read_text())
                        end = summary.get("end_time", "")
                        next_ts = end.split("T")[-1] if "T" in end else ""
                    except (json.JSONDecodeError, OSError):
                        next_ts = ""
                else:
                    block_touches = [t for t in touches if t.get("prompt_idx") == p.get("idx")]
                    next_ts = block_touches[-1].get("ts", "") if block_touches else ""

            block_seconds = _ts_diff(prompt_ts, next_ts)
            block_touches = [t for t in touches if t.get("prompt_idx") == p.get("idx")]
            block_files = {t.get("file") for t in block_touches}
            block_edits = sum(1 for t in block_touches if t.get("action") in ("edit", "write"))

            if tag not in tag_data:
                tag_data[tag] = {
                    "sessions": set(), "prompts": 0, "active_seconds": 0,
                    "first_ts": s.get("start_time", ""), "last_ts": s.get("start_time", ""),
                    "files": set(), "edits": 0, "prompt_texts": [],
                }
            td = tag_data[tag]
            td["sessions"].add(sid)
            td["prompts"] += 1
            td["active_seconds"] += block_seconds
            td["files"].update(block_files)
            td["edits"] += block_edits
            td["prompt_texts"].append(p.get("prompt", ""))
            session_time = s.get("start_time", "")
            if session_time < td["first_ts"] or not td["first_ts"]:
                td["first_ts"] = session_time
            if session_time > td["last_ts"]:
                td["last_ts"] = session_time

    if not tag_data:
        print(f"No data for tag: {tag_filter}" if tag_filter else "No prompt data found")
        return

    if tag_filter:
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
        print("Time by tag:\n")
        sorted_tags = sorted(tag_data.keys(), key=lambda t: (t is None, t or ""))
        for tag in sorted_tags:
            td = tag_data[tag]
            label = tag or "(untagged)"
            print(f"  {label}")
            print(f"    {_format_duration(td['active_seconds'])} active, {td['prompts']} prompts, {td['edits']} edits, {len(td['sessions'])} sessions")
            print()


def cmd_branch(reason: str | None):
    import sys as _sys
    if not reason:
        print("Usage: inscript branch \"reason for detour\"", file=_sys.stderr)
        _sys.exit(1)
    sess = active_session()
    if not sess:
        print("No active session", file=_sys.stderr)
        _sys.exit(1)
    sdir = session_dir(sess)
    parent = None
    active_branch_file = sdir / "active_branch"
    try:
        text = active_branch_file.read_text().strip()
        if text:
            parent = int(text)
    except (OSError, ValueError):
        pass
    branches = _load_jsonl(sdir / "branches.jsonl")
    next_id = max((b.get("id", -1) for b in branches), default=-1) + 1
    prompts = _load_prompts(sdir)
    prompt_idx = prompts[-1].get("idx") if prompts else None
    entry = {"type": "open", "id": next_id, "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"), "reason": reason}
    if prompt_idx is not None:
        entry["prompt_idx"] = prompt_idx
    if parent is not None:
        entry["parent"] = parent
    _append_jsonl(sdir / "branches.jsonl", entry)
    active_branch_file.write_text(str(next_id) + "\n")
    if parent is not None:
        print(f"Branch #{next_id}: {reason} (nested in #{parent})")
    else:
        print(f"Branch #{next_id}: {reason}")


def cmd_resume():
    import sys as _sys
    sess = active_session()
    if not sess:
        print("No active session", file=_sys.stderr)
        _sys.exit(1)
    sdir = session_dir(sess)
    active_branch_file = sdir / "active_branch"
    try:
        text = active_branch_file.read_text().strip()
        branch_id = int(text) if text else None
    except (OSError, ValueError):
        branch_id = None
    if branch_id is None:
        print("No active branch to resume from")
        return
    branches = _load_jsonl(sdir / "branches.jsonl")
    parent = None
    reason = "?"
    for b in branches:
        if b.get("id") == branch_id and b.get("type") == "open":
            parent = b.get("parent")
            reason = b.get("reason", "?")
            break
    prompts = _load_prompts(sdir)
    prompt_idx = prompts[-1].get("idx") if prompts else None
    entry = {"type": "close", "id": branch_id, "ts": datetime.now(timezone.utc).strftime("%H:%M:%S")}
    if prompt_idx is not None:
        entry["prompt_idx"] = prompt_idx
    _append_jsonl(sdir / "branches.jsonl", entry)
    if parent is not None:
        active_branch_file.write_text(str(parent) + "\n")
        print(f"Closed branch #{branch_id} ({reason}), back to branch #{parent}")
    else:
        try:
            active_branch_file.unlink()
        except OSError:
            pass
        print(f"Closed branch #{branch_id} ({reason}), back to trunk")


def cmd_branches():
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
    branch_info: dict[int, dict] = {}
    for b in branches:
        bid = b.get("id")
        if bid is None:
            continue
        if b.get("type") == "open":
            branch_info[bid] = {"reason": b.get("reason", "?"), "opened": b.get("ts", "?"), "prompt_idx": b.get("prompt_idx"), "parent": b.get("parent"), "closed": None}
        elif b.get("type") == "close" and bid in branch_info:
            branch_info[bid]["closed"] = b.get("ts", "?")
    active_bid = None
    try:
        text = (sdir / "active_branch").read_text().strip()
        active_bid = int(text) if text else None
    except (OSError, ValueError):
        pass
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
        indent = "  " if info["parent"] is not None else ""
        status = "open" if info["closed"] is None else f"closed {info['closed']}"
        if bid == active_bid:
            status = "active"
        tc = branch_touches.get(bid, 0)
        ec = branch_edits.get(bid, 0)
        print(f"{indent}#{bid} {info['reason']}")
        print(f"{indent}  {info['opened']} → {status}, {tc} touches, {ec} edits")
    print()


def cmd_overlap():
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


def cmd_cleanup():
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


def cmd_export(session_id: str):
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
