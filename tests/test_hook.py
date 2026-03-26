"""Tests for the hook module — session lifecycle, prompt recording, file touches."""
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import inscript_pkg
from inscript_pkg.hook import (
    handle_session_start,
    handle_prompt_submit,
    handle_post_tool_use,
    handle_stop,
    _extract_path,
    main,
)


class TestExtractPath:
    def test_file_path(self):
        assert _extract_path({"tool_input": {"file_path": "/foo/bar.py"}}) == "/foo/bar.py"

    def test_path_field(self):
        assert _extract_path({"tool_input": {"path": "/foo/"}}) == "/foo/"

    def test_command_with_path(self):
        assert _extract_path({"tool_input": {"command": "cat /etc/hosts"}}) == "/etc/hosts"

    def test_no_path(self):
        assert _extract_path({"tool_input": {"command": "echo hello"}}) is None

    def test_empty(self):
        assert _extract_path({"tool_input": {}}) is None

    def test_relative_path_ignored(self):
        assert _extract_path({"tool_input": {"file_path": "relative/path.py"}}) is None


class TestSessionLifecycle:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmpdir / "sessions"
        self.sessions_dir.mkdir()
        self.active_sessions_dir = self.tmpdir / "active_sessions"
        self.active_sessions_dir.mkdir()

        self._patches = [
            patch.object(inscript_pkg.hook, "SESSIONS_DIR", self.sessions_dir),
            patch.object(inscript_pkg.hook, "INSCRIPT_DIR", self.tmpdir),
            patch.object(inscript_pkg.hook, "ACTIVE_PROJECT_FILE", self.tmpdir / "active_project"),
            patch.object(inscript_pkg, "SESSIONS_DIR", self.sessions_dir),
            patch.object(inscript_pkg, "ACTIVE_SESSION_FILE", self.tmpdir / "active_session"),
            patch.object(inscript_pkg, "ACTIVE_SESSIONS_DIR", self.active_sessions_dir),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir)

    def test_session_start_creates_directory(self):
        data = {"session_id": "test-sess-001", "cwd": "/project"}
        handle_session_start(data)
        sdir = self.sessions_dir / "test-sess-001"
        assert sdir.exists()
        assert (sdir / "meta.json").exists()
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["status"] == "active"

    def test_prompt_submit_records_prompt(self):
        sid = "test-sess-002"
        data = {"session_id": sid, "cwd": "/project"}
        handle_session_start(data)

        with patch.object(inscript_pkg.hook, "active_session_for_hook", return_value=sid):
            handle_prompt_submit({"prompt": "fix the bug"})

        prompts_file = self.sessions_dir / sid / "prompts.jsonl"
        assert prompts_file.exists()
        prompts = [json.loads(line) for line in prompts_file.open()]
        assert len(prompts) == 1
        assert prompts[0]["prompt"] == "fix the bug"
        assert prompts[0]["idx"] == 0

    def test_multiple_prompts_increment_index(self):
        sid = "test-sess-003"
        handle_session_start({"session_id": sid, "cwd": "/project"})

        with patch.object(inscript_pkg.hook, "active_session_for_hook", return_value=sid):
            handle_prompt_submit({"prompt": "first"})
            handle_prompt_submit({"prompt": "second"})
            handle_prompt_submit({"prompt": "third"})

        prompts = [json.loads(line) for line in
                   (self.sessions_dir / sid / "prompts.jsonl").open()]
        assert len(prompts) == 3
        assert [p["idx"] for p in prompts] == [0, 1, 2]

    def test_post_tool_use_records_touch(self):
        sid = "test-sess-004"
        handle_session_start({"session_id": sid, "cwd": "/project"})

        with patch.object(inscript_pkg.hook, "active_session_for_hook", return_value=sid):
            handle_prompt_submit({"prompt": "read the file"})
            handle_post_tool_use({
                "tool_name": "Read",
                "tool_input": {"file_path": "/project/main.py"},
                "cwd": "/project",
            })

        touches_file = self.sessions_dir / sid / "touches.jsonl"
        assert touches_file.exists()
        touches = [json.loads(line) for line in touches_file.open()]
        assert len(touches) >= 1
        assert touches[0]["file"] == "/project/main.py"
        assert touches[0]["action"] == "read"

    def test_stop_writes_summary(self):
        sid = "test-sess-005"
        handle_session_start({"session_id": sid, "cwd": "/project"})

        with patch.object(inscript_pkg.hook, "active_session_for_hook", return_value=sid):
            handle_prompt_submit({"prompt": "do work"})
            handle_stop({"cwd": "/project"})

        summary_file = self.sessions_dir / sid / "summary.json"
        assert summary_file.exists()
        summary = json.loads(summary_file.read_text())
        assert summary["status"] == "snapshot"
        assert "prompts" in summary


class TestMainDispatch:
    """Test that main() dispatches correctly and never crashes."""

    def test_invalid_json_doesnt_crash(self):
        import io
        with patch("sys.stdin", io.StringIO("not json")):
            main()  # Should return silently

    def test_empty_json_doesnt_crash(self):
        import io
        with patch("sys.stdin", io.StringIO("{}")):
            # Treated as PostToolUse with no data — should not crash
            main()
