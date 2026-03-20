"""Tests for the MCP server tools."""
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import inscript_pkg
from inscript_pkg.mcp_server import (
    file_history,
    log,
    message,
    replay,
    sessions,
    status,
    _resolve_session_by_prefix,
)


def _make_session(sessions_dir: Path, sid: str, prompts: list[dict],
                   touches: list[dict], diffs: list[dict] | None = None,
                   project: str = "/repo", summary: dict | None = None) -> Path:
    """Create a test session with the given data."""
    sdir = sessions_dir / sid
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({
        "project": project, "start_time": "2026-01-01T10:00:00", "status": "completed",
    }))
    (sdir / "prompts.jsonl").write_text(
        "\n".join(json.dumps(p) for p in prompts) + "\n" if prompts else ""
    )
    (sdir / "touches.jsonl").write_text(
        "\n".join(json.dumps(t) for t in touches) + "\n" if touches else ""
    )
    if diffs is not None:
        (sdir / "diffs.jsonl").write_text(
            "\n".join(json.dumps(d) for d in diffs) + "\n" if diffs else ""
        )
    if summary:
        (sdir / "summary.json").write_text(json.dumps(summary))
    return sdir


class TestMessage:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmpdir / "sessions"
        self.sid = "abcdef12-3456-7890-abcd-ef1234567890"
        _make_session(
            self.sessions_dir, self.sid,
            prompts=[
                {"idx": 0, "ts": "10:00:00", "prompt": "build the feature"},
                {"idx": 1, "ts": "10:05:00", "prompt": "add tests", "tag": "testing"},
            ],
            touches=[
                {"file": "/repo/a.py", "action": "edit", "prompt_idx": 0, "lines_changed": 10},
                {"file": "/repo/b.py", "action": "read", "prompt_idx": 0},
                {"file": "/repo/test_a.py", "action": "write", "prompt_idx": 1, "lines": 50},
            ],
            diffs=[
                {"file": "/repo/a.py", "tool": "Edit", "prompt_idx": 0,
                 "old_string": "def foo():", "new_string": "def foo(x):"},
                {"file": "/repo/test_a.py", "tool": "Write", "prompt_idx": 1,
                 "is_new": True, "lines": 50, "content": "import pytest\n\ndef test_foo():\n    pass"},
            ],
        )
        self._patches = [
            patch.object(inscript_pkg, "SESSIONS_DIR", self.sessions_dir),
            patch.object(inscript_pkg, "ACTIVE_SESSION_FILE", self.tmpdir / "active_session"),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir)

    def test_message_by_session_and_idx(self):
        result = message("abcdef12:1")
        assert "build the feature" in result
        assert "a.py" in result
        assert "edit" in result
        assert "def foo(x):" in result

    def test_message_shows_tag(self):
        result = message("abcdef12:2")
        assert "tag: testing" in result
        assert "add tests" in result

    def test_message_shows_new_file(self):
        result = message("abcdef12:2")
        assert "test_a.py" in result
        assert "new file" in result

    def test_message_invalid_index(self):
        result = message("abcdef12:99")
        assert "not found" in result

    def test_message_no_session_match(self):
        result = message("zzzzz:1")
        assert "No session" in result

    def test_message_no_activity(self):
        # Prompt 1 has touches, but if we had a prompt 3 with no activity:
        _make_session(
            self.sessions_dir, "empty-sess-001",
            prompts=[{"idx": 0, "prompt": "just a question"}],
            touches=[], diffs=[],
        )
        result = message("empty-se:1")
        assert "no file activity" in result


class TestReplay:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmpdir / "sessions"
        self.sid = "replay-mcp-001"
        _make_session(
            self.sessions_dir, self.sid,
            prompts=[{"idx": 0, "prompt": "do work"}],
            touches=[{"file": "/repo/a.py", "action": "edit", "prompt_idx": 0, "project": "/repo"}],
            summary={"duration_seconds": 600, "total_edits": 1, "status": "completed"},
        )
        self._patches = [
            patch.object(inscript_pkg, "SESSIONS_DIR", self.sessions_dir),
            patch.object(inscript_pkg, "ACTIVE_SESSION_FILE", self.tmpdir / "active_session"),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir)

    def test_replay_returns_content(self):
        result = replay(self.sid)
        assert "Previous Session" in result
        assert "do work" in result

    def test_replay_no_sessions(self):
        shutil.rmtree(self.sessions_dir)
        result = replay()
        assert "No sessions" in result


class TestLog:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmpdir / "sessions"
        self.sid = "log-mcp-001"
        _make_session(
            self.sessions_dir, self.sid,
            prompts=[{"idx": 0, "ts": "10:00:00", "prompt": "hi"}],
            touches=[{"file": "/repo/a.py", "action": "read", "prompt_idx": 0}],
        )
        self._patches = [
            patch.object(inscript_pkg, "SESSIONS_DIR", self.sessions_dir),
            patch.object(inscript_pkg, "ACTIVE_SESSION_FILE", self.tmpdir / "active_session"),
        ]
        for p in self._patches:
            p.start()
        (self.tmpdir / "active_session").write_text(self.sid + "\n")

    def teardown_method(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir)

    def test_log_returns_content(self):
        result = log()
        assert "log-mcp-001" in result
        assert "a.py" in result


class TestSessions:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmpdir / "sessions"
        for i in range(3):
            _make_session(
                self.sessions_dir, f"sess-{i:03d}",
                prompts=[{"idx": 0, "prompt": f"session {i}"}],
                touches=[],
            )
        self._patches = [
            patch.object(inscript_pkg, "SESSIONS_DIR", self.sessions_dir),
            patch.object(inscript_pkg, "ACTIVE_SESSION_FILE", self.tmpdir / "active_session"),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir)

    def test_lists_sessions(self):
        result = sessions()
        assert "3 sessions" in result
        assert "sess-000" in result


class TestFileHistory:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmpdir / "sessions"
        self.sid = "fhist-mcp-001"
        _make_session(
            self.sessions_dir, self.sid,
            prompts=[
                {"idx": 0, "prompt": "first edit"},
                {"idx": 1, "prompt": "second edit"},
            ],
            touches=[
                {"file": "/repo/main.py", "action": "edit", "prompt_idx": 0, "lines_changed": 5},
                {"file": "/repo/main.py", "action": "edit", "prompt_idx": 1, "lines_changed": 3},
            ],
            diffs=[
                {"file": "/repo/main.py", "tool": "Edit", "prompt_idx": 0, "old_string": "a", "new_string": "b"},
                {"file": "/repo/main.py", "tool": "Edit", "prompt_idx": 1, "old_string": "c", "new_string": "d"},
            ],
        )
        self._patches = [
            patch.object(inscript_pkg, "SESSIONS_DIR", self.sessions_dir),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir)

    def test_file_history_shows_all_diffs(self):
        result = file_history(self.sid, "main.py")
        assert "main.py" in result
        assert "2 edits" in result
        assert "Prompt 1" in result
        assert "Prompt 2" in result

    def test_file_history_no_match(self):
        result = file_history(self.sid, "nonexistent.py")
        assert "No file matching" in result or "No data" in result


class TestResolveSessionByPrefix:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmpdir / "sessions"
        _make_session(self.sessions_dir, "abcdef12-full-id", prompts=[], touches=[])
        _make_session(self.sessions_dir, "xyz98765-full-id", prompts=[], touches=[])
        self._patch = patch.object(inscript_pkg, "SESSIONS_DIR", self.sessions_dir)
        self._patch.start()

    def teardown_method(self):
        self._patch.stop()
        shutil.rmtree(self.tmpdir)

    def test_prefix_match(self):
        assert _resolve_session_by_prefix("abcdef") == "abcdef12-full-id"

    def test_no_match(self):
        assert _resolve_session_by_prefix("zzzzz") is None
