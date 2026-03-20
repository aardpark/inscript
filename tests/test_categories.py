"""Tests for prompt and workflow category detection."""
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import inscript_pkg
from inscript_pkg.categories import (
    classify_prompt,
    session_categories,
    session_shape,
    workflow_patterns,
)


def _make_session(sessions_dir, sid, touches, prompts):
    sdir = sessions_dir / sid
    sdir.mkdir(parents=True)
    meta = {"start_time": "2026-03-20T10:00:00", "project": "/proj", "status": "completed"}
    (sdir / "meta.json").write_text(json.dumps(meta))
    with (sdir / "touches.jsonl").open("w") as f:
        for t in touches:
            f.write(json.dumps(t) + "\n")
    with (sdir / "prompts.jsonl").open("w") as f:
        for p in prompts:
            f.write(json.dumps(p) + "\n")


class TestClassifyPrompt:
    def test_idle(self):
        assert classify_prompt([]) == "idle"

    def test_investigate(self):
        assert classify_prompt([
            {"action": "read"}, {"action": "read"},
        ]) == "investigate"

    def test_search(self):
        assert classify_prompt([
            {"action": "grep"}, {"action": "glob"},
        ]) == "search"

    def test_direct_single(self):
        assert classify_prompt([{"action": "edit"}]) == "direct"

    def test_direct_multi(self):
        assert classify_prompt([
            {"action": "edit"}, {"action": "edit"}, {"action": "edit"},
        ]) == "direct-multi"

    def test_create(self):
        assert classify_prompt([{"action": "write"}]) == "create"

    def test_study_create(self):
        assert classify_prompt([
            {"action": "read"}, {"action": "read"}, {"action": "write"},
        ]) == "study-create"

    def test_read_edit(self):
        assert classify_prompt([
            {"action": "read"}, {"action": "edit"},
        ]) == "read-edit"

    def test_deep_edit(self):
        assert classify_prompt([
            {"action": "read"}, {"action": "read"}, {"action": "read"}, {"action": "edit"},
        ]) == "deep-edit"

    def test_iteration(self):
        assert classify_prompt([
            {"action": "read"},
            {"action": "edit"}, {"action": "edit"}, {"action": "edit"},
            {"action": "edit"}, {"action": "edit"}, {"action": "edit"},
        ]) == "iteration"

    def test_explore_edit(self):
        # 4 exploration, 3 mutations — more exploration than editing
        assert classify_prompt([
            {"action": "grep"}, {"action": "read"}, {"action": "grep"}, {"action": "read"},
            {"action": "edit"}, {"action": "edit"}, {"action": "edit"},
        ]) == "explore-edit"


class TestSessionCategories:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmpdir / "sessions"
        self.sessions_dir.mkdir()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def _patch(self):
        return patch.object(inscript_pkg, "SESSIONS_DIR", self.sessions_dir)

    def test_basic_session(self):
        _make_session(self.sessions_dir, "s1",
            touches=[
                {"action": "read", "prompt_idx": 0},
                {"action": "edit", "prompt_idx": 1},
            ],
            prompts=[
                {"idx": 0, "prompt": "look at it"},
                {"idx": 1, "prompt": "fix it"},
            ],
        )
        with self._patch():
            cats = session_categories("s1")
            assert len(cats) == 2
            assert cats[0]["category"] == "investigate"
            assert cats[1]["category"] == "direct"

    def test_session_shape(self):
        _make_session(self.sessions_dir, "s1",
            touches=[
                {"action": "read", "prompt_idx": 0},
                {"action": "edit", "prompt_idx": 2},
            ],
            prompts=[
                {"idx": 0, "prompt": "look"},
                {"idx": 1, "prompt": "think"},
                {"idx": 2, "prompt": "do"},
            ],
        )
        with self._patch():
            shape = session_shape("s1")
            assert shape["work_density"] == 2 / 3
            assert shape["distribution"]["idle"] == 1
            assert shape["distribution"]["investigate"] == 1


class TestWorkflowPatterns:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmpdir / "sessions"
        self.sessions_dir.mkdir()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def _patch(self):
        return patch.object(inscript_pkg, "SESSIONS_DIR", self.sessions_dir)

    def test_recurring_pattern(self):
        """Same transition in 2 sessions = recurring pattern."""
        # Both sessions: investigate → direct
        _make_session(self.sessions_dir, "s1",
            touches=[
                {"action": "read", "prompt_idx": 0},
                {"action": "edit", "prompt_idx": 1},
            ],
            prompts=[
                {"idx": 0, "prompt": "look"},
                {"idx": 1, "prompt": "fix"},
            ],
        )
        _make_session(self.sessions_dir, "s2",
            touches=[
                {"action": "read", "prompt_idx": 0},
                {"action": "edit", "prompt_idx": 1},
            ],
            prompts=[
                {"idx": 0, "prompt": "look again"},
                {"idx": 1, "prompt": "fix again"},
            ],
        )
        with self._patch():
            patterns = workflow_patterns()
            transitions = [p["transition"] for p in patterns]
            assert ("investigate", "direct") in transitions

    def test_no_patterns_single_session(self):
        _make_session(self.sessions_dir, "s1",
            touches=[{"action": "read", "prompt_idx": 0}],
            prompts=[{"idx": 0, "prompt": "look"}],
        )
        with self._patch():
            assert workflow_patterns() == []
