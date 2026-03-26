"""Tests for __init__.py — config loading, session resolution, path safety."""
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

import inscript_pkg


class TestConfig:
    def test_default_config_when_no_file(self):
        with patch.object(inscript_pkg, "CONFIG_FILE", Path("/nonexistent/config.toml")):
            config = inscript_pkg._load_config()
            assert config == inscript_pkg.DEFAULT_CONFIG

    def test_store_diffs_default(self):
        with patch.object(inscript_pkg, "CONFIG_FILE", Path("/nonexistent/config.toml")):
            assert inscript_pkg.store_diffs() is True

    def test_config_loads_real_toml(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            config_file = tmpdir / "config.toml"
            config_file.write_text('[retention]\nstore_diffs = false\n')
            with patch.object(inscript_pkg, "CONFIG_FILE", config_file):
                config = inscript_pkg._load_config()
                assert config["retention"]["store_diffs"] is False
        finally:
            shutil.rmtree(tmpdir)


class TestSessionDir:
    def test_normal_session_id(self):
        result = inscript_pkg.session_dir("abc-123-def")
        assert result.name == "abc-123-def"

    def test_path_traversal_blocked(self):
        result = inscript_pkg.session_dir("../../etc/passwd")
        assert "invalid" in str(result)
        assert "etc" not in str(result)

    def test_dot_dot_in_middle(self):
        result = inscript_pkg.session_dir("foo/../../../etc")
        assert "invalid" in str(result)


class TestFormatTokens:
    def test_small(self):
        assert inscript_pkg._format_tokens(500) == "500"

    def test_thousands(self):
        assert inscript_pkg._format_tokens(1500) == "1.5k"

    def test_millions(self):
        assert inscript_pkg._format_tokens(2_500_000) == "2.5M"


class TestRelPath:
    def test_within_project(self):
        assert inscript_pkg._rel_path("/project/src/main.py", "/project") == "src/main.py"

    def test_outside_project(self):
        assert inscript_pkg._rel_path("/other/file.py", "/project") == "/other/file.py"

    def test_no_project(self):
        assert inscript_pkg._rel_path("/some/file.py", None) == "/some/file.py"


class TestAppendJsonl:
    def test_creates_and_appends(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            f = tmpdir / "sub" / "test.jsonl"
            inscript_pkg._append_jsonl(f, {"key": "val1"})
            inscript_pkg._append_jsonl(f, {"key": "val2"})
            lines = [json.loads(line) for line in f.open()]
            assert len(lines) == 2
            assert lines[0]["key"] == "val1"
            assert lines[1]["key"] == "val2"
        finally:
            shutil.rmtree(tmpdir)


class TestLoadJsonl:
    def test_missing_file(self):
        assert inscript_pkg._load_jsonl(Path("/nonexistent.jsonl")) == []

    def test_loads_entries(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            f = tmpdir / "test.jsonl"
            f.write_text('{"a":1}\n{"a":2}\n')
            entries = inscript_pkg._load_jsonl(f)
            assert len(entries) == 2
            assert entries[0]["a"] == 1
        finally:
            shutil.rmtree(tmpdir)

    def test_skips_bad_lines(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            f = tmpdir / "test.jsonl"
            f.write_text('{"a":1}\nnot json\n{"a":3}\n')
            entries = inscript_pkg._load_jsonl(f)
            assert len(entries) == 2
        finally:
            shutil.rmtree(tmpdir)
