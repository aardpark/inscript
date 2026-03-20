"""Tests for replay and log generation."""
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import inscript_pkg
from inscript_pkg.replay import generate_log, generate_replay, generate_file_history, _format_duration


class TestFormatDuration:
    def test_seconds(self):
        assert _format_duration(45) == "45s"

    def test_minutes(self):
        assert _format_duration(125) == "2m 5s"

    def test_hours(self):
        assert _format_duration(3725) == "1h 2m"


class TestGenerateLog:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmpdir / "sessions"
        self.sid = "test-session-001"
        self.sdir = self.sessions_dir / self.sid
        self.sdir.mkdir(parents=True)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def _patch_session_dir(self):
        return patch.object(inscript_pkg, "SESSIONS_DIR", self.sessions_dir)

    def test_no_touches_returns_none(self):
        with self._patch_session_dir():
            assert generate_log(self.sid) is None

    def test_basic_log(self):
        (self.sdir / "meta.json").write_text(json.dumps({"project": "/repo"}))
        (self.sdir / "prompts.jsonl").write_text(
            json.dumps({"idx": 0, "ts": "10:00:00", "prompt": "do something"}) + "\n"
        )
        (self.sdir / "touches.jsonl").write_text(
            json.dumps({"file": "/repo/a.py", "action": "edit", "prompt_idx": 0, "lines_changed": 5}) + "\n"
        )
        with self._patch_session_dir():
            result = generate_log(self.sid)
        assert result is not None
        assert "test-session-001" in result
        assert "do something" in result
        assert "a.py" in result
        assert "1 files, 1 edits, 1 prompts" in result

    def test_log_with_tokens(self):
        (self.sdir / "meta.json").write_text(json.dumps({"project": "/repo"}))
        (self.sdir / "prompts.jsonl").write_text(
            json.dumps({"idx": 0, "ts": "10:00:00", "prompt": "hi"}) + "\n"
        )
        (self.sdir / "touches.jsonl").write_text(
            json.dumps({"file": "/repo/a.py", "action": "read", "prompt_idx": 0}) + "\n"
        )
        (self.sdir / "summary.json").write_text(json.dumps({
            "tokens": {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500, "model": "test-model"}
        }))
        with self._patch_session_dir():
            result = generate_log(self.sid)
        assert "1.5k tokens" in result
        assert "test-model" in result


class TestGenerateReplay:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmpdir / "sessions"
        self.sid = "replay-test-002"
        self.sdir = self.sessions_dir / self.sid
        self.sdir.mkdir(parents=True)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def _patch_session_dir(self):
        return patch.object(inscript_pkg, "SESSIONS_DIR", self.sessions_dir)

    def test_no_meta_returns_none(self):
        with self._patch_session_dir():
            assert generate_replay(self.sid) is None

    def test_no_prompts_returns_none(self):
        (self.sdir / "meta.json").write_text(json.dumps({"project": "/repo"}))
        with self._patch_session_dir():
            assert generate_replay(self.sid) is None

    def test_basic_replay(self):
        (self.sdir / "meta.json").write_text(json.dumps({"project": "/repo"}))
        (self.sdir / "summary.json").write_text(json.dumps({
            "duration_seconds": 3600, "total_edits": 5,
        }))
        prompts = [
            {"idx": 0, "prompt": "start building"},
            {"idx": 1, "prompt": "add tests"},
            {"idx": 2, "prompt": "fix bug"},
        ]
        (self.sdir / "prompts.jsonl").write_text(
            "\n".join(json.dumps(p) for p in prompts) + "\n"
        )
        (self.sdir / "touches.jsonl").write_text(
            json.dumps({"file": "/repo/a.py", "action": "edit", "prompt_idx": 0, "lines_changed": 10}) + "\n" +
            json.dumps({"file": "/repo/test_a.py", "action": "write", "prompt_idx": 1, "lines": 50}) + "\n" +
            json.dumps({"file": "/repo/a.py", "action": "edit", "prompt_idx": 2, "lines_changed": 3}) + "\n"
        )
        with self._patch_session_dir():
            result = generate_replay(self.sid)
        assert result is not None
        assert "1h 0m" in result
        assert "5 edits" in result
        assert "3 prompts" in result
        assert "start building" in result
        assert "add tests" in result
        assert "a.py" in result
        # Check message drill-down hint
        assert "replay-t:" in result  # short session ID (8 chars)
        assert "<number>" in result

    def test_replay_detects_detours(self):
        (self.sdir / "meta.json").write_text(json.dumps({"project": "/main"}))
        prompts = [{"idx": i} for i in range(5)]
        (self.sdir / "prompts.jsonl").write_text(
            "\n".join(json.dumps(p) for p in prompts) + "\n"
        )
        touches = [
            {"file": "/main/a.py", "action": "edit", "prompt_idx": 0, "project": "/main"},
            {"file": "/main/a.py", "action": "edit", "prompt_idx": 1, "project": "/main"},
            {"file": "/other/x.py", "action": "edit", "prompt_idx": 2, "project": "/other"},
            {"file": "/main/a.py", "action": "edit", "prompt_idx": 3, "project": "/main"},
            {"file": "/main/a.py", "action": "edit", "prompt_idx": 4, "project": "/main"},
        ]
        (self.sdir / "touches.jsonl").write_text(
            "\n".join(json.dumps(t) for t in touches) + "\n"
        )
        with self._patch_session_dir():
            result = generate_replay(self.sid)
        assert "Detected detours" in result
        assert "other" in result

    def test_replay_shows_tags(self):
        (self.sdir / "meta.json").write_text(json.dumps({"project": "/repo"}))
        prompts = [
            {"idx": 0, "prompt": "start", "tag": "auth-feature"},
            {"idx": 1, "prompt": "continue", "tag": "auth-feature"},
        ]
        (self.sdir / "prompts.jsonl").write_text(
            "\n".join(json.dumps(p) for p in prompts) + "\n"
        )
        (self.sdir / "touches.jsonl").write_text("")
        with self._patch_session_dir():
            result = generate_replay(self.sid)
        assert "auth-feature" in result


class TestGenerateFileHistory:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmpdir / "sessions"
        self.sid = "file-hist-003"
        self.sdir = self.sessions_dir / self.sid
        self.sdir.mkdir(parents=True)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def _patch_session_dir(self):
        return patch.object(inscript_pkg, "SESSIONS_DIR", self.sessions_dir)

    def test_no_session_returns_none(self):
        with self._patch_session_dir():
            assert generate_file_history("nonexistent", "a.py") is None

    def test_no_matching_file(self):
        (self.sdir / "meta.json").write_text(json.dumps({"project": "/repo"}))
        (self.sdir / "prompts.jsonl").write_text(
            json.dumps({"idx": 0, "prompt": "hi"}) + "\n"
        )
        (self.sdir / "touches.jsonl").write_text(
            json.dumps({"file": "/repo/a.py", "action": "edit", "prompt_idx": 0}) + "\n"
        )
        (self.sdir / "diffs.jsonl").write_text("")
        with self._patch_session_dir():
            result = generate_file_history(self.sid, "nonexistent.py")
        assert "No file matching" in result

    def test_file_history_with_diffs(self):
        (self.sdir / "meta.json").write_text(json.dumps({"project": "/repo"}))
        prompts = [
            {"idx": 0, "prompt": "first change"},
            {"idx": 1, "prompt": "second change"},
        ]
        (self.sdir / "prompts.jsonl").write_text(
            "\n".join(json.dumps(p) for p in prompts) + "\n"
        )
        (self.sdir / "touches.jsonl").write_text(
            json.dumps({"file": "/repo/a.py", "action": "edit", "prompt_idx": 0, "lines_changed": 5}) + "\n" +
            json.dumps({"file": "/repo/a.py", "action": "edit", "prompt_idx": 1, "lines_changed": 3}) + "\n"
        )
        (self.sdir / "diffs.jsonl").write_text(
            json.dumps({"file": "/repo/a.py", "tool": "Edit", "prompt_idx": 0, "old_string": "old1", "new_string": "new1"}) + "\n" +
            json.dumps({"file": "/repo/a.py", "tool": "Edit", "prompt_idx": 1, "old_string": "old2", "new_string": "new2"}) + "\n"
        )
        with self._patch_session_dir():
            result = generate_file_history(self.sid, "a.py")
        assert "a.py" in result
        assert "2 edits" in result
        assert "Prompt 1" in result
        assert "Prompt 2" in result
        assert "old1" in result
        assert "new1" in result
        assert "old2" in result
        # Check message drill-down hint
        assert "file-his:" in result  # short session ID (8 chars)

    def test_ambiguous_file_match(self):
        (self.sdir / "meta.json").write_text(json.dumps({"project": "/repo"}))
        (self.sdir / "prompts.jsonl").write_text(
            json.dumps({"idx": 0, "prompt": "hi"}) + "\n"
        )
        (self.sdir / "touches.jsonl").write_text(
            json.dumps({"file": "/repo/src/a.py", "action": "edit", "prompt_idx": 0}) + "\n" +
            json.dumps({"file": "/repo/tests/a.py", "action": "edit", "prompt_idx": 0}) + "\n"
        )
        (self.sdir / "diffs.jsonl").write_text("")
        with self._patch_session_dir():
            result = generate_file_history(self.sid, "a.py")
        assert "Multiple files match" in result
