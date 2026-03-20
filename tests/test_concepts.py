"""Tests for cross-session concept detection."""
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import inscript_pkg
from inscript_pkg.concepts import (
    detect_concepts,
    format_concepts,
    concept_for_file,
    concept_history,
    _is_infra,
)


def _make_session(sessions_dir, sid, project, touches, prompts=None):
    """Helper to create a test session with touches and optional prompts."""
    sdir = sessions_dir / sid
    sdir.mkdir(parents=True)

    meta = {"start_time": "2026-03-20T10:00:00", "project": project, "status": "completed"}
    (sdir / "meta.json").write_text(json.dumps(meta))

    with (sdir / "touches.jsonl").open("w") as f:
        for t in touches:
            f.write(json.dumps(t) + "\n")

    if prompts:
        with (sdir / "prompts.jsonl").open("w") as f:
            for p in prompts:
                f.write(json.dumps(p) + "\n")

    return sdir


class TestIsInfra:
    def test_claude_dir(self):
        assert _is_infra("/Users/x/.claude/settings.json")

    def test_inscript_dir(self):
        assert _is_infra("/Users/x/.inscript/config.toml")

    def test_pycache(self):
        assert _is_infra("/foo/__pycache__/bar.pyc")

    def test_normal_file(self):
        assert not _is_infra("/Users/x/project/main.py")


class TestDetectConcepts:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmpdir / "sessions"
        self.sessions_dir.mkdir()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def _patch(self):
        return patch.object(inscript_pkg, "SESSIONS_DIR", self.sessions_dir)

    def test_no_sessions(self):
        with self._patch():
            assert detect_concepts() == []

    def test_single_session_no_concepts(self):
        """A single session can't produce cross-session concepts."""
        _make_session(self.sessions_dir, "s1", "/proj", [
            {"file": "/proj/a.py", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/b.py", "action": "edit", "prompt_idx": 0},
        ])
        with self._patch():
            concepts = detect_concepts()
            assert len(concepts) == 0

    def test_two_sessions_shared_file_creates_concept(self):
        """A file appearing in 2 sessions forms a concept."""
        _make_session(self.sessions_dir, "s1", "/proj", [
            {"file": "/proj/a.py", "action": "edit", "prompt_idx": 0},
        ])
        _make_session(self.sessions_dir, "s2", "/proj", [
            {"file": "/proj/a.py", "action": "edit", "prompt_idx": 0},
        ])
        with self._patch():
            concepts = detect_concepts()
            assert len(concepts) == 1
            assert "/proj/a.py" in concepts[0]["files"]
            assert concepts[0]["session_count"] == 2

    def test_co_occurring_files_cluster(self):
        """Files edited in the same prompt across sessions cluster together."""
        _make_session(self.sessions_dir, "s1", "/proj", [
            {"file": "/proj/a.py", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/b.py", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/a.py", "action": "edit", "prompt_idx": 1},  # solo prompt for a
        ])
        _make_session(self.sessions_dir, "s2", "/proj", [
            {"file": "/proj/a.py", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/b.py", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/b.py", "action": "edit", "prompt_idx": 1},  # solo prompt for b
        ])
        with self._patch():
            concepts = detect_concepts()
            assert len(concepts) == 1
            assert set(concepts[0]["files"]) == {"/proj/a.py", "/proj/b.py"}

    def test_read_only_touches_excluded(self):
        """Read-only touches don't form concepts — only edits/writes."""
        _make_session(self.sessions_dir, "s1", "/proj", [
            {"file": "/proj/a.py", "action": "read", "prompt_idx": 0},
        ])
        _make_session(self.sessions_dir, "s2", "/proj", [
            {"file": "/proj/a.py", "action": "read", "prompt_idx": 0},
        ])
        with self._patch():
            assert detect_concepts() == []

    def test_infra_files_excluded(self):
        """Infrastructure files (.claude/, .inscript/) are excluded."""
        _make_session(self.sessions_dir, "s1", "/proj", [
            {"file": "/home/.claude/settings.json", "action": "edit", "prompt_idx": 0},
        ])
        _make_session(self.sessions_dir, "s2", "/proj", [
            {"file": "/home/.claude/settings.json", "action": "edit", "prompt_idx": 0},
        ])
        with self._patch():
            assert detect_concepts() == []

    def test_separate_clusters_stay_separate(self):
        """Files that never co-occur should be separate concepts."""
        _make_session(self.sessions_dir, "s1", "/proj", [
            {"file": "/proj/a.py", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/c.py", "action": "edit", "prompt_idx": 1},
        ])
        _make_session(self.sessions_dir, "s2", "/proj", [
            {"file": "/proj/a.py", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/c.py", "action": "edit", "prompt_idx": 1},
        ])
        with self._patch():
            concepts = detect_concepts()
            assert len(concepts) == 2

    def test_transitive_clustering(self):
        """If A co-occurs with B, and B with C, all three cluster."""
        _make_session(self.sessions_dir, "s1", "/proj", [
            {"file": "/proj/a.py", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/b.py", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/a.py", "action": "edit", "prompt_idx": 1},  # solo
        ])
        _make_session(self.sessions_dir, "s2", "/proj", [
            {"file": "/proj/a.py", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/b.py", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/c.py", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/b.py", "action": "edit", "prompt_idx": 1},  # solo
        ])
        _make_session(self.sessions_dir, "s3", "/proj", [
            {"file": "/proj/b.py", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/c.py", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/c.py", "action": "edit", "prompt_idx": 1},  # solo
        ])
        with self._patch():
            concepts = detect_concepts()
            assert len(concepts) == 1
            assert set(concepts[0]["files"]) == {"/proj/a.py", "/proj/b.py", "/proj/c.py"}

    def test_support_files_filtered(self):
        """Files with zero solo prompts (always co-edited) are excluded."""
        _make_session(self.sessions_dir, "s1", "/proj", [
            {"file": "/proj/main.py", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/config.toml", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/main.py", "action": "edit", "prompt_idx": 1},  # solo
        ])
        _make_session(self.sessions_dir, "s2", "/proj", [
            {"file": "/proj/main.py", "action": "edit", "prompt_idx": 0},
            {"file": "/proj/config.toml", "action": "edit", "prompt_idx": 0},
        ])
        with self._patch():
            concepts = detect_concepts()
            # config.toml has 0 solo prompts — filtered as support
            for c in concepts:
                assert "/proj/config.toml" not in c["files"]
            # main.py should still be a concept
            assert any("/proj/main.py" in c["files"] for c in concepts)

    def test_sorted_by_session_count(self):
        """Concepts with more sessions come first."""
        for sid in ["s1", "s2", "s3"]:
            _make_session(self.sessions_dir, sid, "/proj", [
                {"file": "/proj/hot.py", "action": "edit", "prompt_idx": 0},
            ])
        for sid in ["s1", "s2"]:
            touches_file = self.sessions_dir / sid / "touches.jsonl"
            with touches_file.open("a") as f:
                f.write(json.dumps({"file": "/proj/warm.py", "action": "edit", "prompt_idx": 1}) + "\n")

        with self._patch():
            concepts = detect_concepts()
            assert concepts[0]["session_count"] >= concepts[-1]["session_count"]


class TestConceptForFile:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmpdir / "sessions"
        self.sessions_dir.mkdir()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def _patch(self):
        return patch.object(inscript_pkg, "SESSIONS_DIR", self.sessions_dir)

    def test_finds_by_full_path(self):
        _make_session(self.sessions_dir, "s1", "/proj", [
            {"file": "/proj/a.py", "action": "edit", "prompt_idx": 0},
        ])
        _make_session(self.sessions_dir, "s2", "/proj", [
            {"file": "/proj/a.py", "action": "edit", "prompt_idx": 0},
        ])
        with self._patch():
            concepts = detect_concepts()
            c = concept_for_file("/proj/a.py", concepts)
            assert c is not None
            assert "/proj/a.py" in c["files"]

    def test_finds_by_filename(self):
        _make_session(self.sessions_dir, "s1", "/proj", [
            {"file": "/proj/a.py", "action": "edit", "prompt_idx": 0},
        ])
        _make_session(self.sessions_dir, "s2", "/proj", [
            {"file": "/proj/a.py", "action": "edit", "prompt_idx": 0},
        ])
        with self._patch():
            concepts = detect_concepts()
            c = concept_for_file("a.py", concepts)
            assert c is not None

    def test_returns_none_for_unknown(self):
        with self._patch():
            assert concept_for_file("nonexistent.py", []) is None


class TestFormatConcepts:
    def test_empty(self):
        assert "No cross-session concepts" in format_concepts([])

    def test_formats_concept(self):
        concepts = [{
            "files": ["/proj/a.py", "/proj/b.py"],
            "sessions": ["s1", "s2"],
            "session_count": 2,
            "total_prompts": 10,
            "first_seen": "2026-03-20T10:00:00",
            "last_seen": "2026-03-20T12:00:00",
        }]
        output = format_concepts(concepts, "/proj")
        assert "concept-1" in output
        assert "a.py" in output
        assert "2 sessions" in output


class TestConceptHistory:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmpdir / "sessions"
        self.sessions_dir.mkdir()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def _patch(self):
        return patch.object(inscript_pkg, "SESSIONS_DIR", self.sessions_dir)

    def test_shows_prompts_that_touched_concept(self):
        _make_session(self.sessions_dir, "s1", "/proj",
            touches=[
                {"file": "/proj/a.py", "action": "edit", "prompt_idx": 0},
                {"file": "/proj/a.py", "action": "edit", "prompt_idx": 2},
            ],
            prompts=[
                {"idx": 0, "prompt": "first change"},
                {"idx": 1, "prompt": "unrelated"},
                {"idx": 2, "prompt": "second change"},
            ],
        )
        _make_session(self.sessions_dir, "s2", "/proj",
            touches=[
                {"file": "/proj/a.py", "action": "edit", "prompt_idx": 0},
            ],
            prompts=[
                {"idx": 0, "prompt": "continued work"},
            ],
        )
        with self._patch():
            concepts = detect_concepts()
            assert len(concepts) >= 1
            history = concept_history(concepts[0])
            assert "first change" in history
            assert "second change" in history
            assert "continued work" in history
            # "unrelated" prompt didn't touch the concept files
            assert "unrelated" not in history
