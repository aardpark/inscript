"""Replay and log generation — the core data access layer."""
from __future__ import annotations

import json
from pathlib import Path

from . import (
    _format_tokens,
    _load_jsonl,
    _load_prompts,
    _rel_path,
    active_session,
    list_sessions,
    session_dir,
)
from .inference import infer_branches


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
    short_id = session_id[:8]
    if prompts:
        lines.append(f"### Prompts (expand any with message tool: \"{short_id}:<number>\")")
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

    # Notes
    notes = _load_jsonl(sdir / "notes.jsonl")
    if notes:
        lines.append("### Notes")
        for n in notes:
            text = n.get("text", "")
            ref = n.get("ref")
            pidx = n.get("prompt_idx")
            prompt_ref = f" (prompt {pidx + 1})" if pidx is not None else ""
            line = f"- {text}{prompt_ref}"
            if ref:
                line += f" -> {_rel_path(ref, project)}"
            lines.append(line)
        lines.append("")

    tokens = summary.get("tokens")
    if tokens:
        total = tokens.get("total_tokens", 0)
        model = tokens.get("model", "?")
        lines.append(f"*{_format_tokens(total)} tokens [{model}]*")

    return "\n".join(lines)


def generate_file_history(session_id: str, file_query: str) -> str | None:
    """Generate the full diff history for a file across all prompts in a session.

    Returns every change made to the file, in prompt order, with context.
    Accepts partial file names (substring match).
    """
    sdir = session_dir(session_id)
    if not sdir.exists():
        return None

    project = None
    meta_file = sdir / "meta.json"
    if meta_file.exists():
        try:
            project = json.loads(meta_file.read_text()).get("project")
        except (json.JSONDecodeError, OSError):
            pass

    prompts = _load_prompts(sdir)
    touches = _load_jsonl(sdir / "touches.jsonl")
    diffs = _load_jsonl(sdir / "diffs.jsonl")

    # Find matching file (substring match)
    matching = set()
    for t in touches:
        f = t.get("file", "")
        if file_query in f or file_query in f.split("/")[-1]:
            matching.add(f)

    if not matching:
        return f"No file matching \"{file_query}\" in session {session_id[:8]}."
    if len(matching) > 1:
        lines = [f"Multiple files match \"{file_query}\":"]
        for f in sorted(matching):
            lines.append(f"  {_rel_path(f, project)}")
        return "\n".join(lines)

    target = matching.pop()
    display_path = _rel_path(target, project)

    # Collect touches for this file by prompt
    prompt_touches: dict[int, list[dict]] = {}
    for t in touches:
        if t.get("file") == target:
            pidx = t.get("prompt_idx")
            if pidx is not None:
                prompt_touches.setdefault(pidx, []).append(t)

    # Collect diffs for this file by prompt
    prompt_diffs: dict[int, list[dict]] = {}
    for d in diffs:
        if d.get("file") == target:
            pidx = d.get("prompt_idx")
            if pidx is not None:
                prompt_diffs.setdefault(pidx, []).append(d)

    total_reads = sum(1 for t in touches if t.get("file") == target and t.get("action") == "read")
    total_edits = sum(1 for t in touches if t.get("file") == target and t.get("action") in ("edit", "write"))

    short_id = session_id[:8]
    lines = [
        f"File: {display_path}",
        f"Session: {short_id} | {total_reads} reads, {total_edits} edits across {len(prompt_touches)} prompts",
        f"(expand any prompt with message tool: \"{short_id}:<number>\")",
        "",
    ]

    # Walk through prompts in order
    for p in prompts:
        idx = p.get("idx", 0)
        if idx not in prompt_touches and idx not in prompt_diffs:
            continue

        prompt_text = p.get("prompt", "")
        if len(prompt_text) > 70:
            prompt_text = prompt_text[:67] + "..."

        pts = prompt_touches.get(idx, [])
        reads = sum(1 for t in pts if t.get("action") == "read")
        edits = sum(1 for t in pts if t.get("action") in ("edit", "write"))
        line_changes = sum(t.get("lines_changed", t.get("lines", 0)) for t in pts)

        parts = []
        if reads:
            parts.append(f"{reads} read")
        if edits:
            parts.append(f"{edits} edit")
        if line_changes:
            parts.append(f"+{line_changes} lines")

        lines.append(f"--- Prompt {idx + 1}: \"{prompt_text}\" ({', '.join(parts)}) ---")

        # Show diffs
        pdiffs = prompt_diffs.get(idx, [])
        if pdiffs:
            for d in pdiffs:
                if d.get("tool") == "Edit":
                    old = d.get("old_string", "")
                    new = d.get("new_string", "")
                    lines.append(f"  - {old}")
                    lines.append(f"  + {new}")
                elif d.get("tool") == "Write":
                    new_marker = " (new file)" if d.get("is_new") else ""
                    lines.append(f"  write {d.get('lines', '?')} lines{new_marker}")
                    if d.get("content"):
                        content_lines = d["content"].split("\n")
                        for cl in content_lines[:10]:
                            lines.append(f"    {cl}")
                        if len(content_lines) > 10:
                            lines.append(f"    ... ({len(content_lines) - 10} more lines)")
        elif edits:
            lines.append("  (diffs not stored)")

        lines.append("")

    if not any(idx in prompt_touches or idx in prompt_diffs for p in prompts for idx in [p.get("idx", 0)]):
        lines.append("(no activity found for this file)")

    return "\n".join(lines)
