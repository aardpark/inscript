"""Tests for git commit detection and linking."""
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import inscript_pkg
from inscript_pkg.hook import (
    _is_git_commit,
    _parse_commit_hash,
    _parse_commit_message,
    _handle_git_commit,
)


class TestIsGitCommit:
    def test_simple_commit(self):
        assert _is_git_commit('git commit -m "test"')

    def test_commit_with_heredoc(self):
        assert _is_git_commit('git commit -m "$(cat <<\'EOF\'\nmessage\nEOF\n)"')

    def test_chained_commit(self):
        assert _is_git_commit('git add . && git commit -m "test"')

    def test_commit_with_path(self):
        assert _is_git_commit('git -C /path/to/repo commit -m "test"')

    def test_amend(self):
        assert _is_git_commit('git commit --amend')

    def test_not_commit_status(self):
        assert not _is_git_commit('git status')

    def test_not_commit_log(self):
        assert not _is_git_commit('git log --oneline')

    def test_not_commit_diff(self):
        assert not _is_git_commit('git diff HEAD')

    def test_not_commit_push(self):
        assert not _is_git_commit('git push origin main')

    def test_not_commit_in_echo(self):
        assert not _is_git_commit('echo "git commit"')

    def test_not_commit_in_grep(self):
        assert not _is_git_commit('grep "git commit" file.txt')


class TestParseCommitHash:
    def test_normal_commit(self):
        assert _parse_commit_hash('[main abc1234] test') == 'abc1234'

    def test_root_commit(self):
        assert _parse_commit_hash('[main (root-commit) abc1234] test') == 'abc1234'

    def test_feature_branch(self):
        assert _parse_commit_hash('[feature/auth fb43a29] add login') == 'fb43a29'

    def test_full_hash(self):
        h = 'abc1234567890abcdef1234567890abcdef12345678'[:40]  # 40 hex chars
        assert len(h) == 40
        assert _parse_commit_hash(f'[main {h}] test') == h

    def test_no_match(self):
        assert _parse_commit_hash('Everything up to date') is None

    def test_multiline_output(self):
        stdout = " 3 files changed, 50 insertions(+)\n[master fb43a29] inscript v0.8.0\ncreate mode 100644 file.py"
        assert _parse_commit_hash(stdout) == 'fb43a29'


class TestParseCommitMessage:
    def test_simple_message(self):
        assert _parse_commit_message('[main abc1234] fix the bug') == 'fix the bug'

    def test_multiline(self):
        assert _parse_commit_message('[main abc1234] first line\n\nsecond para') == 'first line'

    def test_no_match(self):
        assert _parse_commit_message('no commit here') == ''


class TestHandleGitCommit:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmpdir / "sessions"
        self.inscript_dir = self.tmpdir / "inscript"
        self.inscript_dir.mkdir()
        self.sid = "commit-test-001"
        self.sdir = self.sessions_dir / self.sid
        self.sdir.mkdir(parents=True)

        # Write active session
        self.active_file = self.inscript_dir / "active_session"
        self.active_file.write_text(self.sid + "\n")

        # Write prompts
        prompts = [
            {"idx": 0, "ts": "10:00:00", "prompt": "build feature"},
            {"idx": 1, "ts": "10:05:00", "prompt": "add tests"},
            {"idx": 2, "ts": "10:10:00", "prompt": "commit it"},
        ]
        (self.sdir / "prompts.jsonl").write_text(
            "\n".join(json.dumps(p) for p in prompts) + "\n"
        )

        # Write touches — prompts 0 and 1 edited files
        touches = [
            {"file": "/repo/a.py", "action": "edit", "prompt_idx": 0, "lines_changed": 10},
            {"file": "/repo/b.py", "action": "read", "prompt_idx": 0},
            {"file": "/repo/test_a.py", "action": "write", "prompt_idx": 1, "lines": 50},
        ]
        (self.sdir / "touches.jsonl").write_text(
            "\n".join(json.dumps(t) for t in touches) + "\n"
        )

        self._patches = [
            patch.object(inscript_pkg.hook, "SESSIONS_DIR", self.sessions_dir),
            patch.object(inscript_pkg.hook, "ACTIVE_PROJECT_FILE", self.inscript_dir / "active_project"),
            patch.object(inscript_pkg, "ACTIVE_SESSION_FILE", self.active_file),
            patch.object(inscript_pkg, "SESSIONS_DIR", self.sessions_dir),
            # active_session_for_hook reads per-PID files, patch to return our session
            patch.object(inscript_pkg.hook, "active_session_for_hook", return_value=self.sid),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir)

    def test_commit_creates_entry(self):
        data = {"cwd": "/repo"}
        _handle_git_commit(data, 'git commit -m "test"', '[main abc1234] test commit')

        commits_file = self.sdir / "commits.jsonl"
        assert commits_file.exists()
        commits = [json.loads(line) for line in commits_file.open()]
        assert len(commits) == 1
        assert commits[0]["hash"] == "abc1234"
        assert commits[0]["message"] == "test commit"

    def test_commit_links_to_contributing_prompts(self):
        data = {"cwd": "/repo"}
        _handle_git_commit(data, 'git commit -m "test"', '[main abc1234] test')

        commits = [json.loads(line) for line in (self.sdir / "commits.jsonl").open()]
        # Prompts 0 and 1 edited files, prompt 2 is the commit prompt
        contributing = commits[0]["contributing_prompts"]
        assert 0 in contributing  # edited a.py
        assert 1 in contributing  # wrote test_a.py

    def test_commit_lists_files(self):
        data = {"cwd": "/repo"}
        _handle_git_commit(data, 'git commit -m "test"', '[main abc1234] test')

        commits = [json.loads(line) for line in (self.sdir / "commits.jsonl").open()]
        files = commits[0]["files"]
        assert "/repo/a.py" in files
        assert "/repo/test_a.py" in files
        # b.py was only read, not edited — should NOT be in committed files
        assert "/repo/b.py" not in files

    def test_second_commit_only_includes_new_prompts(self):
        data = {"cwd": "/repo"}
        # First commit
        _handle_git_commit(data, 'git commit -m "first"', '[main abc1234] first')

        # Add more touches for new prompts
        with (self.sdir / "touches.jsonl").open("a") as f:
            f.write(json.dumps({"file": "/repo/c.py", "action": "edit", "prompt_idx": 3, "lines_changed": 5}) + "\n")

        # Add a new prompt
        with (self.sdir / "prompts.jsonl").open("a") as f:
            f.write(json.dumps({"idx": 3, "ts": "10:15:00", "prompt": "more work"}) + "\n")

        # Second commit
        _handle_git_commit(data, 'git commit -m "second"', '[main def5678] second')

        commits = [json.loads(line) for line in (self.sdir / "commits.jsonl").open()]
        assert len(commits) == 2
        # Second commit should only reference prompt 3, not 0 or 1
        assert 3 in commits[1]["contributing_prompts"]
        assert 0 not in commits[1]["contributing_prompts"]

    def test_no_commit_entry_on_bad_hash(self):
        data = {"cwd": "/repo"}
        _handle_git_commit(data, 'git commit -m "test"', 'Everything up to date')

        commits_file = self.sdir / "commits.jsonl"
        assert not commits_file.exists()
