"""Branch inference and decision point detection — purely behavioral."""
from __future__ import annotations

from pathlib import Path


def _is_bookkeeping(f: str) -> bool:
    """Paths that are operational overhead, not real work focus."""
    home = str(Path.home())
    return f.startswith(f"{home}/.claude/") or f.startswith(f"{home}/.inscript/")


def _touch_focus(touches: list[dict]) -> set[str]:
    """Extract focus set (project roots or parent dirs) from touches."""
    focus = set()
    for t in touches:
        proj = t.get("project")
        if proj:
            focus.add(proj)
            continue
        f = t.get("file", "")
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
        focus = _touch_focus(pts)
        has_edits = any(t.get("action") in ("edit", "write") for t in pts)
        edit_focus = _touch_focus(
            [t for t in pts if t.get("action") in ("edit", "write")]
        )
        prompt_meta.append({
            "idx": idx,
            "focus": focus,
            "edit_focus": edit_focus,
            "has_edits": has_edits,
            "has_touches": len(pts) > 0,
        })

    # Step 2: build trunk from the first edit-bearing prompts
    # Only edits establish trunk — reads are investigation, not building
    trunk: set[str] = set()
    edit_prompts_seen = 0
    for pm in prompt_meta:
        if pm["edit_focus"]:
            trunk.update(pm["edit_focus"])
            edit_prompts_seen += 1
            if edit_prompts_seen >= 2:
                break
    # Fallback: if no edits at all, use early read focus
    if not trunk:
        for pm in prompt_meta[:3]:
            if pm["focus"]:
                trunk.update(pm["focus"])

    if not trunk:
        return []

    # Step 3: classify each prompt by behavior relative to trunk
    classifications: list[str] = []  # "trunk", "shift", or "idle"
    for pm in prompt_meta:
        if not pm["has_touches"]:
            classifications.append("idle")
            continue
        if pm["has_edits"]:
            sim = _jaccard(pm["edit_focus"], trunk)
            classifications.append("trunk" if sim >= 0.5 else "shift")
        else:
            sim = _jaccard(pm["focus"], trunk)
            classifications.append("trunk" if sim >= 0.5 else "shift")

    # Step 4: find shift runs that return to trunk (confirmed detours)
    # A permanent shift at the end of the session is NOT a detour.
    branches: list[dict] = []
    i = 0
    while i < len(classifications):
        if classifications[i] == "shift":
            start_i = i
            j = i + 1
            while j < len(classifications) and classifications[j] in ("shift", "idle"):
                j += 1
            end_i = j - 1

            # Only a detour if followed by a return to trunk
            if j < len(classifications) and classifications[j] == "trunk":
                while end_i > start_i and not prompt_meta[end_i]["has_touches"]:
                    end_i -= 1
                branches.append(
                    _make_inferred_branch(prompt_meta, trunk, start_i, end_i)
                )
                trunk.update(prompt_meta[j]["focus"])
            i = j
        else:
            if classifications[i] == "trunk" and prompt_meta[i]["has_edits"]:
                trunk.update(prompt_meta[i]["edit_focus"])
            i += 1

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
