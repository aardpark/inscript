"""Visualization: heatmap overview, prompt/file/detour drill-downs."""
from __future__ import annotations

import json
from pathlib import Path

from . import (
    _load_jsonl,
    _load_prompts,
    active_session,
    list_sessions,
    session_dir,
)
from .commands import compute_prompt_durations, _format_secs
from .inference import infer_branches, detect_decision_points
from .replay import _format_duration


def _load_transcript_responses(
    transcript_path: str, prompts: list[dict],
) -> dict[int, list[tuple[str, str]]]:
    """Extract assistant responses from transcript, matched to inscript prompts."""
    responses: dict[int, list[tuple[str, str]]] = {}
    try:
        tp = Path(transcript_path)
        if not tp.exists():
            return responses
    except (OSError, TypeError):
        return responses

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
                has_tool_result = any(b.get("type") == "tool_result" for b in content)
                if not has_tool_result:
                    for b in content:
                        if b.get("type") == "text" and b.get("text", "").strip():
                            prompt_text = b["text"].strip()
                            break
            if prompt_text:
                if current_idx is not None and current_parts:
                    responses[current_idx] = current_parts
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


def _diff_summary(old: str, new: str) -> str:
    """Produce a one-line summary of what changed between old and new text."""
    old_lines = old.split("\n")
    new_lines = new.split("\n")
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
    if len(old_lines) == 1 and len(new_lines) > 1:
        return f"expanded: {old_lines[0].strip()[:50]} (+{len(new_lines) - 1} lines)"
    if len(new_lines) == 1 and len(old_lines) > 1:
        return f"collapsed: {new_lines[0].strip()[:50]} (-{len(old_lines) - 1} lines)"
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
    if len(s) <= w:
        return s.ljust(w)
    if "/" in s:
        parts = s.split("/")
        filename = parts[-1]
        head = parts[0] + "/" if parts[0] else "/"
        candidate = head + "../" + filename
        if len(candidate) <= w:
            return candidate.ljust(w)
        candidate = "../" + filename
        if len(candidate) <= w:
            return candidate.ljust(w)
    return ".." + s[-(w - 2):]


def load_viz_context(session_id: str | None) -> dict | None:
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
    transcript_path = meta.get("transcript_path", "")
    responses = _load_transcript_responses(transcript_path, prompts)
    decisions = detect_decision_points(prompts, responses)
    decision_prompts: set[int] = set()
    for d in decisions:
        decision_prompts.add(d["idx"])
    return {
        "session_id": session_id, "sdir": sdir, "meta": meta,
        "prompts": prompts, "touches": touches, "diffs": diffs,
        "touches_by_prompt": touches_by_prompt,
        "inferred": inferred, "detour_prompts": detour_prompts,
        "decisions": decisions, "decision_prompts": decision_prompts,
        "responses": responses,
        "project_list": project_list, "project_letter": project_letter,
        "summary": summary, "home": home, "_infer_project": _infer_project,
    }


def dispatch_viz(args: list[str]):
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
    ctx = load_viz_context(session_id)
    if ctx is None:
        return
    if prompt_filter == "detours":
        viz_detours(ctx)
    elif isinstance(prompt_filter, int):
        viz_prompt(ctx, prompt_filter)
    elif file_filter:
        viz_file(ctx, file_filter)
    else:
        viz_overview(ctx)


def viz_overview(ctx: dict):
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
    display_cols: list[int | None] = []
    in_gap = False
    for idx in prompt_idxs:
        is_notable = bool(touches_by_prompt.get(idx)) or idx in decision_prompts
        if not is_notable:
            if not in_gap:
                display_cols.append(None)
                in_gap = True
        else:
            display_cols.append(idx)
            in_gap = False

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
            if is_edit and old_char.islower():
                new_char = letter.upper()
            grid[f][pidx] = (new_char, old_count + 1)
        else:
            char = letter.upper() if is_edit else letter.lower()
            grid[f][pidx] = (char, 1)

    file_order: list[str] = []
    file_set: set[str] = set()
    for t in touches:
        f = t.get("file", "?")
        if f not in file_set:
            file_order.append(f)
            file_set.add(f)

    def _cell(f: str, idx: int) -> str:
        entry = grid.get(f, {}).get(idx)
        if not entry:
            return "·"
        char, count = entry
        if count >= 4:
            return char.upper()
        return char

    total_edits = sum(1 for t in touches if t.get("action") in ("edit", "write"))
    sid_short = ctx["session_id"][:8] if len(ctx["session_id"]) > 16 else ctx["session_id"]
    duration_str = ""
    if summary.get("duration_seconds"):
        duration_str = f" ({_format_duration(summary['duration_seconds'])})"
    from . import _format_tokens
    tokens_str = ""
    tok = summary.get("tokens")
    if tok:
        tokens_str = f" | {_format_tokens(tok.get('total_tokens', 0))} tokens"

    print(f"{sid_short}{duration_str}")
    print(f"{len(file_order)} files, {total_edits} edits, {len(prompts)} prompts{tokens_str}")
    print()

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


def viz_prompt(ctx: dict, prompt_num: int):
    prompt_idx = prompt_num - 1
    prompts = ctx["prompts"]
    touches_by_prompt = ctx["touches_by_prompt"]
    diffs = ctx["diffs"]
    detour_prompts = ctx["detour_prompts"]
    home = ctx["home"]
    _infer_project = ctx["_infer_project"]
    project_letter = ctx["project_letter"]
    meta = ctx["meta"]
    decision_prompts = ctx.get("decision_prompts", set())
    decisions = ctx.get("decisions", [])

    prompt = None
    for p in prompts:
        if p.get("idx") == prompt_idx:
            prompt = p
            break
    if not prompt:
        print(f"Prompt {prompt_num} not found (session has {len(prompts)} prompts)")
        return

    pts = touches_by_prompt.get(prompt_idx, [])
    tags = []
    if prompt_idx in detour_prompts:
        tags.append("detour")
    if prompt_idx in decision_prompts:
        tags.append("decision point")
    tag_str = f"  [{', '.join(tags)}]" if tags else ""

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

    responses = _load_transcript_responses(meta.get("transcript_path", ""), prompts)
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


def viz_file(ctx: dict, file_query: str):
    prompts = ctx["prompts"]
    touches = ctx["touches"]
    diffs = ctx["diffs"]
    detour_prompts = ctx["detour_prompts"]
    home = ctx["home"]

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

    touches_by_prompt = ctx["touches_by_prompt"]
    durations = compute_prompt_durations(prompts, touches_by_prompt)
    dur_map = {d["idx"]: d for d in durations}

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


def viz_detours(ctx: dict):
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
