"""Tests for behavioral branch inference.

Every test uses only behavioral signals (file paths, actions, project fields).
No prompt text is examined by the inference — these tests verify that.
"""
from inscript_pkg import infer_branches


def _t(file: str, action: str = "read", project: str | None = None) -> dict:
    """Shorthand for a touch entry."""
    entry = {"file": file, "action": action}
    if project:
        entry["project"] = project
    return entry


def _prompts(n: int) -> list[dict]:
    """Generate n empty prompts (text doesn't matter)."""
    return [{"idx": i} for i in range(n)]


# ---------------------------------------------------------------------------
# Core: trunk establishment from edits
# ---------------------------------------------------------------------------


class TestTrunkEstablishment:

    def test_trunk_from_edits_not_reads(self):
        """Only edits establish the trunk. Reads in a new area shouldn't
        contaminate the trunk and cause missed detections."""
        prompts = _prompts(6)
        tbp = {
            0: [_t("a.py", "edit", "/main")],
            1: [_t("a.py", "edit", "/main")],
            2: [_t("x.py", "read", "/other"), _t("y.py", "read", "/other")],
            3: [_t("x.py", "edit", "/other")],
            4: [_t("a.py", "edit", "/main")],
            5: [_t("a.py", "edit", "/main")],
        }
        branches = infer_branches(prompts, tbp)
        assert len(branches) >= 1
        assert any(b["start_idx"] <= 3 and b["end_idx"] >= 3 for b in branches)

    def test_trunk_fallback_to_reads_when_no_edits(self):
        """If no edits exist, trunk should be built from early reads."""
        prompts = _prompts(5)
        tbp = {
            0: [_t("a.py", "read", "/main")],
            1: [_t("a.py", "read", "/main")],
            2: [_t("x.py", "read", "/other")],
            3: [_t("x.py", "read", "/other")],
            4: [_t("a.py", "read", "/main")],
        }
        branches = infer_branches(prompts, tbp)
        assert len(branches) == 1
        assert branches[0]["start_idx"] <= 3


# ---------------------------------------------------------------------------
# Core: detour detection requires return
# ---------------------------------------------------------------------------


class TestDetourRequiresReturn:

    def test_permanent_shift_not_a_detour(self):
        """Moving to a new area and staying there = task change, not detour."""
        prompts = _prompts(4)
        tbp = {
            0: [_t("a.py", "edit", "/repo-a")],
            1: [_t("a.py", "edit", "/repo-a")],
            2: [_t("b.py", "edit", "/repo-b")],
            3: [_t("b.py", "edit", "/repo-b")],
        }
        assert infer_branches(prompts, tbp) == []

    def test_shift_and_return_is_detour(self):
        """Leave trunk, come back = detour."""
        prompts = _prompts(5)
        tbp = {
            0: [_t("a.py", "edit", "/main")],
            1: [_t("a.py", "edit", "/main")],
            2: [_t("x.py", "edit", "/other")],
            3: [_t("a.py", "edit", "/main")],
            4: [_t("a.py", "edit", "/main")],
        }
        branches = infer_branches(prompts, tbp)
        assert len(branches) == 1
        assert branches[0]["start_idx"] == 2
        assert branches[0]["end_idx"] == 2

    def test_multi_prompt_detour(self):
        """Detour spanning multiple prompts before return."""
        prompts = _prompts(6)
        tbp = {
            0: [_t("a.py", "edit", "/main")],
            1: [_t("a.py", "edit", "/main")],
            2: [_t("x.py", "read", "/other")],
            3: [_t("x.py", "edit", "/other")],
            4: [_t("a.py", "edit", "/main")],
            5: [_t("a.py", "edit", "/main")],
        }
        branches = infer_branches(prompts, tbp)
        assert len(branches) == 1
        assert branches[0]["start_idx"] <= 3
        assert branches[0]["end_idx"] >= 3


# ---------------------------------------------------------------------------
# Same-area activity is never a detour
# ---------------------------------------------------------------------------


class TestSameAreaNotDetour:

    def test_reads_in_trunk_area(self):
        """Reading files in the trunk area isn't a detour."""
        prompts = _prompts(4)
        tbp = {
            0: [_t("a.py", "edit", "/repo")],
            1: [_t("b.py", "read", "/repo")],
            2: [_t("c.py", "read", "/repo")],
            3: [_t("a.py", "edit", "/repo")],
        }
        assert infer_branches(prompts, tbp) == []

    def test_editing_different_files_same_project(self):
        """Editing different files in the same project isn't a detour."""
        prompts = _prompts(4)
        tbp = {
            0: [_t("src/a.py", "edit", "/repo")],
            1: [_t("src/b.py", "edit", "/repo")],
            2: [_t("tests/test_a.py", "edit", "/repo")],
            3: [_t("src/a.py", "edit", "/repo")],
        }
        assert infer_branches(prompts, tbp) == []


# ---------------------------------------------------------------------------
# Idle prompts (no file activity)
# ---------------------------------------------------------------------------


class TestIdlePrompts:

    def test_idle_prompts_dont_trigger_detour(self):
        """Prompts with no file activity are invisible to inference."""
        prompts = _prompts(5)
        tbp = {
            0: [_t("a.py", "edit", "/main")],
            1: [],  # idle
            2: [],  # idle
            3: [_t("a.py", "edit", "/main")],
            4: [_t("a.py", "edit", "/main")],
        }
        assert infer_branches(prompts, tbp) == []

    def test_idle_prompts_inside_detour(self):
        """Idle prompts sandwiched in a shift run don't break the detour."""
        prompts = _prompts(6)
        tbp = {
            0: [_t("a.py", "edit", "/main")],
            1: [_t("x.py", "edit", "/other")],
            2: [],  # idle mid-detour
            3: [_t("x.py", "edit", "/other")],
            4: [_t("a.py", "edit", "/main")],
            5: [_t("a.py", "edit", "/main")],
        }
        branches = infer_branches(prompts, tbp)
        assert len(branches) == 1
        assert branches[0]["start_idx"] == 1
        assert branches[0]["end_idx"] == 3


# ---------------------------------------------------------------------------
# Multiple detours in one session
# ---------------------------------------------------------------------------


class TestMultipleDetours:

    def test_two_separate_detours(self):
        """Two distinct detours with trunk work between them."""
        prompts = _prompts(8)
        tbp = {
            0: [_t("a.py", "edit", "/main")],
            1: [_t("x.py", "edit", "/other")],     # detour 1
            2: [_t("a.py", "edit", "/main")],       # return
            3: [_t("a.py", "edit", "/main")],
            4: [_t("y.py", "edit", "/infra")],      # detour 2
            5: [_t("y.py", "edit", "/infra")],
            6: [_t("a.py", "edit", "/main")],       # return
            7: [_t("a.py", "edit", "/main")],
        }
        branches = infer_branches(prompts, tbp)
        assert len(branches) == 2

    def test_consecutive_shifts_to_different_areas(self):
        """Shift to A then to B before returning = one combined detour."""
        prompts = _prompts(6)
        tbp = {
            0: [_t("a.py", "edit", "/main")],
            1: [_t("a.py", "edit", "/main")],
            2: [_t("x.py", "edit", "/tests")],
            3: [_t("y.py", "edit", "/infra")],
            4: [_t("a.py", "edit", "/main")],
            5: [_t("a.py", "edit", "/main")],
        }
        branches = infer_branches(prompts, tbp)
        assert len(branches) == 1


# ---------------------------------------------------------------------------
# Reason generation from focus data
# ---------------------------------------------------------------------------


class TestReasonGeneration:

    def test_reason_names_new_area(self):
        """Reason should describe the area that was shifted to."""
        prompts = _prompts(5)
        tbp = {
            0: [_t("a.py", "edit", "/main")],
            1: [_t("a.py", "edit", "/main")],
            2: [_t("x.py", "edit", "/Users/dev/config")],
            3: [_t("a.py", "edit", "/main")],
            4: [_t("a.py", "edit", "/main")],
        }
        branches = infer_branches(prompts, tbp)
        assert len(branches) == 1
        assert "config" in branches[0]["reason"]

    def test_reason_does_not_use_prompt_text(self):
        """Even with descriptive prompts, reason comes from file data only."""
        prompts = [
            {"idx": 0, "prompt": "build the main feature"},
            {"idx": 1, "prompt": "build the main feature"},
            {"idx": 2, "prompt": "OH NO everything is broken fix the deploy"},
            {"idx": 3, "prompt": "continue the main feature"},
            {"idx": 4, "prompt": "continue the main feature"},
        ]
        tbp = {
            0: [_t("a.py", "edit", "/main")],
            1: [_t("a.py", "edit", "/main")],
            2: [_t("deploy.py", "edit", "/ops")],
            3: [_t("a.py", "edit", "/main")],
            4: [_t("a.py", "edit", "/main")],
        }
        branches = infer_branches(prompts, tbp)
        assert len(branches) == 1
        # Reason should reference the directory, not the prompt text
        reason = branches[0]["reason"]
        assert "ops" in reason
        assert "broken" not in reason
        assert "OH NO" not in reason


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:

    def test_too_few_prompts(self):
        """Less than 3 prompts = no inference possible."""
        assert infer_branches(_prompts(2), {0: [_t("a.py", "edit")], 1: [_t("b.py", "edit")]}) == []

    def test_all_idle(self):
        """Session with no file activity at all."""
        assert infer_branches(_prompts(5), {}) == []

    def test_single_prompt_with_activity(self):
        """Only one prompt has touches."""
        prompts = _prompts(4)
        tbp = {0: [], 1: [_t("a.py", "edit", "/main")], 2: [], 3: []}
        assert infer_branches(prompts, tbp) == []

    def test_mixed_focus_prompt(self):
        """A prompt touching both trunk and new area has mixed focus.
        Should not trigger a detour by itself since it overlaps trunk."""
        prompts = _prompts(5)
        tbp = {
            0: [_t("a.py", "edit", "/main")],
            1: [_t("a.py", "edit", "/main")],
            2: [_t("a.py", "read", "/main"), _t("x.py", "read", "/other")],
            3: [_t("a.py", "edit", "/main")],
            4: [_t("a.py", "edit", "/main")],
        }
        assert infer_branches(prompts, tbp) == []

    def test_focus_from_file_paths_without_project_field(self):
        """Inference works for older touches that lack the project field."""
        prompts = _prompts(5)
        tbp = {
            0: [_t("/Users/dev/repo/src/a.py", "edit")],
            1: [_t("/Users/dev/repo/src/a.py", "edit")],
            2: [_t("/Users/dev/config/ci.yaml", "edit")],
            3: [_t("/Users/dev/repo/src/a.py", "edit")],
            4: [_t("/Users/dev/repo/src/a.py", "edit")],
        }
        branches = infer_branches(prompts, tbp)
        assert len(branches) == 1
        assert "config" in branches[0]["reason"]

    def test_trunk_evolves_after_return(self):
        """After a detour returns, trunk should absorb the return point.
        This prevents legitimate expansion of work from being flagged."""
        prompts = _prompts(8)
        tbp = {
            0: [_t("a.py", "edit", "/main")],
            1: [_t("a.py", "edit", "/main")],
            2: [_t("x.py", "edit", "/detour")],       # detour
            3: [_t("a.py", "edit", "/main")],          # return
            4: [_t("b.py", "edit", "/main"),
                _t("c.py", "edit", "/expansion")],     # main work expands
            5: [_t("c.py", "edit", "/expansion")],     # continuing in expanded area
            6: [_t("c.py", "edit", "/expansion")],     # still there
            7: [_t("a.py", "edit", "/main")],
        }
        branches = infer_branches(prompts, tbp)
        # Should only detect the /detour branch, not flag /expansion
        assert len(branches) == 1
        assert branches[0]["start_idx"] == 2
