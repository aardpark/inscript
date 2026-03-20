"""Prompt and workflow category detection.

Classifies prompts by their structural category — the pattern of tool use,
not the content. Categories compose into workflows (sequences of categories)
and session shapes.

Prompt categories are derived from the action sequence in touches.jsonl:
  idle         — no file activity (discussion/thinking)
  investigate  — reads only
  search       — mostly grep/glob
  read-edit    — balanced read-then-edit
  deep-edit    — heavy reading, targeted edit
  explore-edit — more exploration than editing
  direct       — edit without reading (confident change)
  direct-multi — multiple confident edits
  iteration    — heavy editing cycle (5+ mutations)
  create       — write new files only
  study-create — read/search then write new files
"""
from __future__ import annotations

from collections import defaultdict

from . import (
    _load_jsonl,
    _load_prompts,
    list_sessions,
    session_dir,
)


def classify_prompt(touches: list[dict]) -> str:
    """Classify a prompt by its structural category."""
    if not touches:
        return "idle"

    actions = [t.get("action", "") for t in touches]
    has_read = "read" in actions
    has_edit = "edit" in actions
    has_write = "write" in actions
    has_search = "glob" in actions or "grep" in actions

    edits = actions.count("edit")
    writes = actions.count("write")
    reads = actions.count("read")
    searches = actions.count("glob") + actions.count("grep")

    # Position of first mutation
    first_mutation = None
    for i, a in enumerate(actions):
        if a in ("edit", "write"):
            first_mutation = i
            break

    reads_before = (
        sum(1 for a in actions[:first_mutation] if a in ("read", "glob", "grep"))
        if first_mutation is not None
        else 0
    )

    # No mutations — pure exploration
    if not has_edit and not has_write:
        return "search" if searches > reads else "investigate"

    # Writes only, no edits
    if has_write and not has_edit:
        return "study-create" if (has_read or has_search) else "create"

    # Edits only, no reads/searches
    if not has_read and not has_search:
        return "direct" if edits == 1 else "direct-multi"

    # Mixed: reads/searches and edits
    total_mutations = edits + writes
    total_exploration = reads + searches

    if reads_before >= 2 and total_mutations <= 2:
        return "deep-edit"

    if total_mutations > 5:
        return "iteration"

    if total_exploration > total_mutations:
        return "explore-edit"

    return "read-edit"


def session_categories(session_id: str) -> list[dict]:
    """Compute the category for every prompt in a session.

    Returns a list of dicts: {idx, category, prompt, touches}
    """
    sdir = session_dir(session_id)
    prompts = _load_prompts(sdir)
    touches = _load_jsonl(sdir / "touches.jsonl")

    by_prompt: dict[int, list[dict]] = defaultdict(list)
    for t in touches:
        pidx = t.get("prompt_idx")
        if pidx is not None:
            by_prompt[pidx].append(t)

    result = []
    for p in prompts:
        idx = p.get("idx", 0)
        pts = by_prompt.get(idx, [])
        result.append({
            "idx": idx,
            "category": classify_prompt(pts),
            "prompt": p.get("prompt", ""),
            "touches": len(pts),
        })
    return result


def session_shape(session_id: str) -> dict:
    """Characterize a session's overall shape from its category distribution.

    Returns: {categories, distribution, dominant, work_density, transitions}
    """
    categories = session_categories(session_id)
    if not categories:
        return {"categories": [], "distribution": {}, "dominant": "idle",
                "work_density": 0.0, "transitions": {}}

    # Distribution
    dist: dict[str, int] = defaultdict(int)
    for c in categories:
        dist[c["category"]] += 1

    # Work density: fraction of non-idle prompts
    total = len(categories)
    idle = dist.get("idle", 0)
    work_density = (total - idle) / total if total else 0.0

    # Dominant category (excluding idle)
    work_cats = {k: v for k, v in dist.items() if k != "idle"}
    dominant = max(work_cats, key=work_cats.get) if work_cats else "idle"

    # Transitions between non-idle categories
    transitions: dict[tuple[str, str], int] = defaultdict(int)
    work_seq = [c["category"] for c in categories if c["category"] != "idle"]
    for i in range(len(work_seq) - 1):
        transitions[(work_seq[i], work_seq[i + 1])] += 1

    return {
        "categories": categories,
        "distribution": dict(dist),
        "dominant": dominant,
        "work_density": work_density,
        "transitions": dict(transitions),
    }


def format_session_categories(session_id: str) -> str:
    """Format a session's prompt categories for display."""
    shape = session_shape(session_id)
    categories = shape["categories"]
    if not categories:
        return "No prompts in session."

    # Short labels for compact display
    labels = {
        "idle": "·", "investigate": "I", "search": "S",
        "read-edit": "e", "deep-edit": "E", "explore-edit": "x",
        "direct": "d", "direct-multi": "D",
        "iteration": "⚡", "create": "C", "study-create": "W",
    }

    seq = "".join(labels.get(c["category"], "?") for c in categories)

    lines = [f"Session: {session_id[:8]} ({len(categories)} prompts)\n"]
    lines.append(f"  {seq}\n")

    # Distribution
    dist = shape["distribution"]
    work_cats = {k: v for k, v in dist.items() if k != "idle"}
    if work_cats:
        lines.append(f"  work density: {shape['work_density']:.0%}, dominant: {shape['dominant']}")
        breakdown = ", ".join(f"{k}: {v}" for k, v in
                             sorted(work_cats.items(), key=lambda x: -x[1]))
        lines.append(f"  {breakdown}")

    # Top transitions
    transitions = shape["transitions"]
    if transitions:
        lines.append("")
        top = sorted(transitions.items(), key=lambda x: -x[1])[:5]
        lines.append("  transitions:")
        for (a, b), count in top:
            lines.append(f"    {a} → {b}  (x{count})")

    return "\n".join(lines)


def workflow_patterns(min_sessions: int = 2) -> list[dict]:
    """Find recurring workflow patterns across all sessions.

    A workflow pattern is a transition (pair of consecutive work categories)
    that appears in multiple sessions.

    Returns list of {transition, sessions, count}.
    """
    all_sessions = list_sessions()
    transition_sessions: dict[tuple[str, str], set[str]] = defaultdict(set)
    transition_counts: dict[tuple[str, str], int] = defaultdict(int)

    for s in all_sessions:
        sid = s["session_id"]
        categories = session_categories(sid)
        work_seq = [c["category"] for c in categories if c["category"] != "idle"]
        for i in range(len(work_seq) - 1):
            pair = (work_seq[i], work_seq[i + 1])
            transition_sessions[pair].add(sid)
            transition_counts[pair] += 1

    patterns = []
    for pair, sids in transition_sessions.items():
        if len(sids) >= min_sessions:
            patterns.append({
                "transition": pair,
                "sessions": sorted(sids),
                "session_count": len(sids),
                "total_count": transition_counts[pair],
            })

    patterns.sort(key=lambda p: (-p["session_count"], -p["total_count"]))
    return patterns


def format_workflow_patterns() -> str:
    """Format recurring workflow patterns for display."""
    patterns = workflow_patterns()
    if not patterns:
        return "No recurring workflow patterns detected yet."

    lines = [f"{len(patterns)} recurring workflow pattern(s)\n"]
    for p in patterns:
        a, b = p["transition"]
        lines.append(f"  {a} → {b}")
        lines.append(f"    {p['total_count']}x across {p['session_count']} sessions")
        sids = ", ".join(s[:8] for s in p["sessions"][:5])
        lines.append(f"    sessions: {sids}")
        lines.append("")

    return "\n".join(lines)
