"""Tests for CLI commands: formatting, time calculations."""
from inscript_pkg.commands import (
    _ts_to_secs,
    _ts_diff,
    _format_secs,
    compute_prompt_durations,
)


class TestTimeUtils:
    def test_ts_to_secs(self):
        assert _ts_to_secs("00:00:00") == 0
        assert _ts_to_secs("01:00:00") == 3600
        assert _ts_to_secs("10:30:45") == 10 * 3600 + 30 * 60 + 45

    def test_ts_diff_normal(self):
        assert _ts_diff("10:00:00", "10:05:00") == 300

    def test_ts_diff_midnight_crossing(self):
        assert _ts_diff("23:59:00", "00:01:00") == 120

    def test_ts_diff_invalid(self):
        assert _ts_diff("invalid", "10:00:00") == 0

    def test_format_secs(self):
        assert _format_secs(5) == "5s"
        assert _format_secs(60) == "1m"
        assert _format_secs(90) == "1m30s"


class TestPromptDurations:
    def test_basic_durations(self):
        prompts = [
            {"idx": 0, "ts": "10:00:00"},
            {"idx": 1, "ts": "10:05:00"},
            {"idx": 2, "ts": "10:08:00"},
        ]
        tbp = {
            0: [{"ts": "10:00:30"}, {"ts": "10:01:00"}],
            1: [{"ts": "10:05:10"}],
            2: [],
        }
        results = compute_prompt_durations(prompts, tbp)
        assert len(results) == 3
        assert results[0]["total"] == 300  # 5 minutes
        assert results[0]["think"] == 30   # prompt to first touch
        assert results[0]["work"] == 30    # first to last touch
        assert results[1]["total"] == 180  # 3 minutes

    def test_last_prompt_has_no_total(self):
        prompts = [{"idx": 0, "ts": "10:00:00"}]
        results = compute_prompt_durations(prompts, {})
        assert results[0]["total"] is None

    def test_empty_touches(self):
        prompts = [
            {"idx": 0, "ts": "10:00:00"},
            {"idx": 1, "ts": "10:01:00"},
        ]
        results = compute_prompt_durations(prompts, {0: [], 1: []})
        assert results[0]["think"] == 0
        assert results[0]["work"] == 0
