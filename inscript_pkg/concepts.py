"""Cross-session concept detection.

Identifies recurring clusters of files that get worked on together
across multiple sessions. Concepts emerge from behavioral co-occurrence,
not text matching — files that repeatedly appear in the same prompts
across different sessions form a concept.
"""
from __future__ import annotations

import json
from collections import defaultdict

from . import (
    _load_jsonl,
    _load_prompts,
    _rel_path,
    list_sessions,
    session_dir,
)

# Paths containing these fragments are infrastructure, not work concepts
_INFRA_FRAGMENTS = {"/.claude/", "/.inscript/", "__pycache__", ".egg-info"}


def _is_infra(path: str) -> bool:
    return any(frag in path for frag in _INFRA_FRAGMENTS)


def _file_label(path: str) -> str:
    """Short display name for a file path."""
    return path.split("/")[-1]


def detect_concepts(min_sessions: int = 2) -> list[dict]:
    """Scan all sessions and detect file clusters that recur together.

    Algorithm:
    1. For each session, collect files that were edited/written per prompt
    2. Build prompt-level co-occurrence: files touched in the same prompt
    3. Filter to pairs co-occurring across min_sessions+ sessions
    4. Connected components of those pairs = concepts

    Returns a list of concept dicts, sorted by session count descending.
    """
    all_sessions = list_sessions()
    if not all_sessions:
        return []

    # Gather per-prompt file sets across all sessions
    # file_pair_sessions tracks which sessions each pair co-occurs in
    file_pair_sessions: dict[tuple[str, str], set[str]] = defaultdict(set)
    # Track per-file stats
    file_stats: dict[str, dict] = defaultdict(lambda: {
        "sessions": set(), "prompts": 0, "solo_prompts": 0,
        "first_seen": "", "last_seen": "",
    })

    for s in all_sessions:
        sid = s["session_id"]
        sdir = session_dir(sid)
        touches = _load_jsonl(sdir / "touches.jsonl")
        session_time = s.get("start_time", "")

        # Group edit/write touches by prompt
        prompt_files: dict[int, set[str]] = defaultdict(set)
        for t in touches:
            if t.get("action") not in ("edit", "write"):
                continue
            f = t.get("file", "")
            if not f or _is_infra(f):
                continue
            pidx = t.get("prompt_idx")
            if pidx is not None:
                prompt_files[pidx].add(f)

        # Update file stats (needs full prompt_files to count solos)
        for pidx, files in prompt_files.items():
            is_solo = len(files) == 1
            for f in files:
                fs = file_stats[f]
                fs["sessions"].add(sid)
                fs["prompts"] += 1
                if is_solo:
                    fs["solo_prompts"] += 1
                if not fs["first_seen"] or session_time < fs["first_seen"]:
                    fs["first_seen"] = session_time
                if session_time > fs["last_seen"]:
                    fs["last_seen"] = session_time

        # Build co-occurrence pairs for this session
        session_pairs: set[tuple[str, str]] = set()
        for files in prompt_files.values():
            sorted_files = sorted(files)
            for i, f1 in enumerate(sorted_files):
                for f2 in sorted_files[i + 1:]:
                    session_pairs.add((f1, f2))

        # Record which session this pair appeared in
        for pair in session_pairs:
            file_pair_sessions[pair].add(sid)

    # Support files: zero solo prompts — they never drive work independently.
    # Filter them out before clustering.
    support_files = {f for f, fs in file_stats.items() if fs["solo_prompts"] == 0}

    # Filter to pairs that co-occur in min_sessions+ sessions
    strong_pairs = {
        pair: sids
        for pair, sids in file_pair_sessions.items()
        if len(sids) >= min_sessions
        and pair[0] not in support_files
        and pair[1] not in support_files
    }

    if not strong_pairs:
        # Fall back: find files that individually appear in 2+ sessions
        # even if they don't have strong co-occurrence pairs
        solo_concepts = []
        for f, stats in file_stats.items():
            if len(stats["sessions"]) >= min_sessions and f not in support_files:
                solo_concepts.append({
                    "files": [f],
                    "sessions": sorted(stats["sessions"]),
                    "session_count": len(stats["sessions"]),
                    "total_prompts": stats["prompts"],
                    "first_seen": stats["first_seen"],
                    "last_seen": stats["last_seen"],
                })
        solo_concepts.sort(key=lambda c: -c["session_count"])
        return solo_concepts

    # Union-Find for connected components
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for (f1, f2) in strong_pairs:
        parent.setdefault(f1, f1)
        parent.setdefault(f2, f2)
        union(f1, f2)

    # Collect components
    components: dict[str, list[str]] = defaultdict(list)
    for f in parent:
        components[find(f)].append(f)

    # Also include files that appear in 2+ sessions but weren't in any pair
    # (they form single-file concepts)
    for f, stats in file_stats.items():
        if len(stats["sessions"]) >= min_sessions and f not in parent and f not in support_files:
            components[f] = [f]

    # Build concept dicts
    concepts = []
    for root, files in components.items():
        all_sids: set[str] = set()
        total_prompts = 0
        first_seen = ""
        last_seen = ""

        for f in files:
            fs = file_stats.get(f)
            if fs:
                all_sids.update(fs["sessions"])
                total_prompts += fs["prompts"]
                if not first_seen or (fs["first_seen"] and fs["first_seen"] < first_seen):
                    first_seen = fs["first_seen"]
                if fs["last_seen"] > last_seen:
                    last_seen = fs["last_seen"]

        # Sort files by prompt count (most active first)
        files_sorted = sorted(files, key=lambda f: -file_stats.get(f, {}).get("prompts", 0))

        concepts.append({
            "files": files_sorted,
            "sessions": sorted(all_sids),
            "session_count": len(all_sids),
            "total_prompts": total_prompts,
            "first_seen": first_seen,
            "last_seen": last_seen,
        })

    concepts.sort(key=lambda c: (-c["session_count"], -c["total_prompts"]))
    return concepts


def format_concepts(concepts: list[dict], project: str | None = None) -> str:
    """Format concepts for display."""
    if not concepts:
        return "No cross-session concepts detected yet."

    lines = [f"{len(concepts)} concept(s) detected\n"]

    for i, c in enumerate(concepts):
        file_labels = [_rel_path(f, project) for f in c["files"][:5]]
        remainder = len(c["files"]) - 5

        lines.append(f"  concept-{i + 1}: {', '.join(file_labels)}" +
                     (f" +{remainder} more" if remainder > 0 else ""))
        lines.append(f"    {c['session_count']} sessions, {c['total_prompts']} prompts, "
                     f"first: {c['first_seen'][:10]}, last: {c['last_seen'][:10]}")

        # Show session IDs
        sid_labels = [s[:8] for s in c["sessions"][:5]]
        lines.append(f"    sessions: {', '.join(sid_labels)}" +
                     (f" +{len(c['sessions']) - 5} more" if len(c["sessions"]) > 5 else ""))
        lines.append("")

    return "\n".join(lines)


def concept_for_file(file_path: str, concepts: list[dict] | None = None) -> dict | None:
    """Find the concept containing a given file."""
    if concepts is None:
        concepts = detect_concepts()
    for c in concepts:
        if file_path in c["files"]:
            return c
        # Also match by filename
        label = _file_label(file_path)
        if any(_file_label(f) == label for f in c["files"]):
            return c
    return None


def concept_history(concept: dict) -> str:
    """Show the cross-session activity timeline for a concept."""
    lines = []
    file_set = set(concept["files"])
    project = None

    for sid in concept["sessions"]:
        sdir = session_dir(sid)
        meta_file = sdir / "meta.json"
        if not meta_file.exists():
            continue

        try:
            meta = json.loads(meta_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        if project is None:
            project = meta.get("project")

        prompts = _load_prompts(sdir)
        touches = _load_jsonl(sdir / "touches.jsonl")

        # Find prompts that touched concept files
        relevant_prompts: dict[int, list[str]] = defaultdict(list)
        for t in touches:
            f = t.get("file", "")
            if f in file_set and t.get("action") in ("edit", "write"):
                pidx = t.get("prompt_idx")
                if pidx is not None:
                    relevant_prompts[pidx].append(f)

        if not relevant_prompts:
            continue

        start = meta.get("start_time", "?")
        lines.append(f"  {sid[:8]}  {start}")

        prompt_map = {p.get("idx", 0): p for p in prompts}
        for pidx in sorted(relevant_prompts):
            p = prompt_map.get(pidx)
            text = p.get("prompt", "")[:60] if p else "?"
            touched = sorted(set(_file_label(f) for f in relevant_prompts[pidx]))
            lines.append(f"    {pidx + 1}. \"{text}\" -> {', '.join(touched)}")

        lines.append("")

    if not lines:
        return "No activity found for this concept."

    header = f"Concept: {', '.join(_file_label(f) for f in concept['files'][:4])}\n"
    header += f"{concept['session_count']} sessions, {concept['total_prompts']} prompts\n"
    return header + "\n".join(lines)
