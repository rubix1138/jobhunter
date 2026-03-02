"""Tests for main.py CLI commands — init, status, daily-summary, build_parser."""

import os
from types import SimpleNamespace
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from jobhunter.db.engine import init_db
from jobhunter.main import (
    build_parser,
    cmd_init,
    cmd_prepare_referral,
    cmd_review_packet,
    cmd_status,
    cmd_daily_summary,
    _load_settings,
    _load_queries,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = init_db(path)
    conn.close()
    return path


@pytest.fixture(autouse=True)
def set_db_env(db_path, monkeypatch):
    """Point DB_PATH to the temp database for all tests."""
    monkeypatch.setenv("DB_PATH", db_path)


class _FakeArgs:
    """Minimal args namespace substitute."""
    db = None
    log_level = "INFO"


# ── build_parser ──────────────────────────────────────────────────────────────

class TestBuildParser:
    def test_parser_has_all_subcommands(self):
        parser = build_parser()
        subparsers_action = None
        for action in parser._subparsers._actions:
            if hasattr(action, "_name_parser_map"):
                subparsers_action = action
                break
        assert subparsers_action is not None
        commands = set(subparsers_action._name_parser_map.keys())
        expected = {"init", "status", "run", "search-now", "apply-now", "check-email", "daily-summary", "review-queue", "review-packet", "qa-log", "platform-stats", "prepare-referral"}
        assert expected == commands

    def test_parser_accepts_log_level(self):
        parser = build_parser()
        args = parser.parse_args(["--log-level", "DEBUG", "status"])
        assert args.log_level == "DEBUG"

    def test_parser_accepts_db_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--db", "/tmp/custom.db", "status"])
        assert args.db == "/tmp/custom.db"

    def test_apply_now_accepts_reprobe_blocked_workday_flag(self):
        parser = build_parser()
        args = parser.parse_args(["apply-now", "--reprobe-blocked-workday"])
        assert args.reprobe_blocked_workday is True

    def test_review_queue_accepts_limit(self):
        parser = build_parser()
        args = parser.parse_args(["review-queue", "--limit", "5"])
        assert args.limit == 5

    def test_review_packet_accepts_output(self):
        parser = build_parser()
        args = parser.parse_args(["review-packet", "--limit", "10", "--output", "/tmp/review.md"])
        assert args.limit == 10
        assert args.output == "/tmp/review.md"
        assert args.csv is False

    def test_review_packet_accepts_csv_flag(self):
        parser = build_parser()
        args = parser.parse_args(["review-packet", "--csv"])
        assert args.csv is True
        assert args.open_only is False

    def test_review_packet_accepts_open_flag(self):
        parser = build_parser()
        args = parser.parse_args(["review-packet", "--open"])
        assert args.open_only is True


# ── _load_settings ────────────────────────────────────────────────────────────

class TestLoadSettings:
    def test_returns_empty_dict_when_file_missing(self, tmp_path):
        result = _load_settings(str(tmp_path / "nonexistent.yaml"))
        assert result == {}

    def test_loads_valid_yaml(self, tmp_path):
        f = tmp_path / "settings.yaml"
        f.write_text("budget:\n  daily_limit_usd: 10.0\n")
        result = _load_settings(str(f))
        assert result["budget"]["daily_limit_usd"] == 10.0


# ── _load_queries ─────────────────────────────────────────────────────────────

class TestLoadQueries:
    def test_returns_empty_list_when_file_missing(self, tmp_path):
        result = _load_queries(str(tmp_path / "nonexistent.yaml"))
        assert result == []

    def test_loads_queries_from_yaml(self, tmp_path):
        f = tmp_path / "queries.yaml"
        f.write_text("queries:\n  - keywords: python engineer\n    location: Remote\n")
        result = _load_queries(str(f))
        assert len(result) == 1
        assert result[0]["keywords"] == "python engineer"

    def test_returns_empty_list_when_no_queries_key(self, tmp_path):
        f = tmp_path / "queries.yaml"
        f.write_text("other_key: value\n")
        result = _load_queries(str(f))
        assert result == []


# ── cmd_init ──────────────────────────────────────────────────────────────────

class TestCmdInit:
    def test_returns_zero_on_success(self, db_path, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        monkeypatch.setenv("FERNET_KEY", "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdGtleXQ=")
        with patch("jobhunter.main.Path") as MockPath:
            # Make profile path not exist so we skip profile loading
            mock_path_inst = MagicMock()
            mock_path_inst.exists.return_value = False
            MockPath.return_value = mock_path_inst
            result = cmd_init(_FakeArgs())
        assert result == 0

    def test_prints_db_initialized(self, db_path, monkeypatch, capsys):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("FERNET_KEY", "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdGtleXQ=")
        with patch("jobhunter.main.Path") as MockPath:
            mock_path_inst = MagicMock()
            mock_path_inst.exists.return_value = False
            MockPath.return_value = mock_path_inst
            cmd_init(_FakeArgs())
        out = capsys.readouterr().out
        assert "Database initialized" in out

    def test_warns_when_anthropic_key_missing(self, monkeypatch, capsys):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("FERNET_KEY", "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdGtleXQ=")
        with patch("jobhunter.main.Path") as MockPath:
            mock_path_inst = MagicMock()
            mock_path_inst.exists.return_value = False
            MockPath.return_value = mock_path_inst
            cmd_init(_FakeArgs())
        out = capsys.readouterr().out
        assert "ANTHROPIC_API_KEY" in out
        assert "WARNING" in out

    def test_generates_fernet_key_when_missing(self, monkeypatch, capsys):
        monkeypatch.delenv("FERNET_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        with patch("jobhunter.main.Path") as MockPath:
            mock_path_inst = MagicMock()
            mock_path_inst.exists.return_value = False
            MockPath.return_value = mock_path_inst
            cmd_init(_FakeArgs())
        out = capsys.readouterr().out
        assert "FERNET_KEY=" in out


# ── cmd_status ────────────────────────────────────────────────────────────────

class TestCmdStatus:
    def test_returns_zero(self, capsys):
        result = cmd_status(_FakeArgs())
        assert result == 0

    def test_shows_jobs_section(self, capsys):
        cmd_status(_FakeArgs())
        out = capsys.readouterr().out
        assert "Jobs by status" in out

    def test_shows_applications_section(self, capsys):
        cmd_status(_FakeArgs())
        out = capsys.readouterr().out
        assert "Applications by status" in out

    def test_shows_llm_spend(self, capsys):
        cmd_status(_FakeArgs())
        out = capsys.readouterr().out
        assert "LLM spend" in out

    def test_shows_recent_runs(self, capsys):
        cmd_status(_FakeArgs())
        out = capsys.readouterr().out
        assert "Recent agent runs" in out

    def test_budget_alert_at_80_percent(self, db_path, capsys):
        conn = init_db(db_path)
        # Insert LLM usage = 13.00 USD (86% of 15 USD limit)
        conn.execute(
            "INSERT INTO llm_usage "
            "(agent_name, model, purpose, input_tokens, output_tokens, cost_usd, created_at) "
            "VALUES ('search_agent', 'claude-sonnet-4-6', 'scoring', 1000, 200, 13.0, date('now'))"
        )
        conn.commit()
        conn.close()

        with patch("jobhunter.main._load_settings") as mock_settings:
            mock_settings.return_value = {
                "budget": {"daily_limit_usd": 15.0, "alert_threshold_pct": 0.80}
            }
            cmd_status(_FakeArgs())
        out = capsys.readouterr().out
        assert "WARNING" in out or "80%" in out

    def test_budget_exceeded_message(self, db_path, capsys):
        conn = init_db(db_path)
        conn.execute(
            "INSERT INTO llm_usage "
            "(agent_name, model, purpose, input_tokens, output_tokens, cost_usd, created_at) "
            "VALUES ('search_agent', 'claude-sonnet-4-6', 'scoring', 1000, 200, 16.0, date('now'))"
        )
        conn.commit()
        conn.close()

        with patch("jobhunter.main._load_settings") as mock_settings:
            mock_settings.return_value = {
                "budget": {"daily_limit_usd": 15.0, "alert_threshold_pct": 0.80}
            }
            cmd_status(_FakeArgs())
        out = capsys.readouterr().out
        assert "BUDGET EXCEEDED" in out


# ── cmd_daily_summary ─────────────────────────────────────────────────────────

class TestCmdDailySummary:
    def test_returns_zero(self, capsys):
        result = cmd_daily_summary(_FakeArgs())
        assert result == 0

    def test_prints_summary_header(self, capsys):
        cmd_daily_summary(_FakeArgs())
        out = capsys.readouterr().out
        assert "JobHunter Daily Summary" in out

    def test_shows_zero_counts_on_empty_db(self, capsys):
        cmd_daily_summary(_FakeArgs())
        out = capsys.readouterr().out
        # All counts should be 0 on empty DB
        assert "0" in out


class TestCmdPrepareReferral:
    def test_calls_scheduler_with_defaults(self, capsys):
        args = SimpleNamespace(
            url="https://example.com/jobs/123",
            title=None,
            company=None,
            output_dir=None,
        )
        fake_profile = MagicMock()
        fake_settings = {"models": {"routine": "claude-sonnet-4-6", "writing": "claude-opus-4-6"}}

        with (
            patch("jobhunter.main._load_settings", return_value=fake_settings),
            patch("jobhunter.main._load_profile", return_value=fake_profile),
            patch(
                "jobhunter.scheduler.run_referral_once",
                new=AsyncMock(return_value=(Path("/tmp/resume.pdf"), Path("/tmp/cover.pdf"))),
            ) as mock_run,
        ):
            rc = cmd_prepare_referral(args)

        assert rc == 0
        kwargs = mock_run.await_args.kwargs
        assert kwargs["settings"] == fake_settings
        assert kwargs["profile"] is fake_profile
        assert kwargs["url"] == "https://example.com/jobs/123"
        assert kwargs["output_dir"] == Path("data/resumes")
        assert kwargs["title"] is None
        assert kwargs["company"] is None
        out = capsys.readouterr().out
        assert "Preparing referral materials for: https://example.com/jobs/123" in out
        assert "Resume:       /tmp/resume.pdf" in out
        assert "Cover letter: /tmp/cover.pdf" in out

    def test_passes_overrides_and_custom_output_dir(self):
        args = SimpleNamespace(
            url="https://linkedin.com/jobs/view/999",
            title="CISO",
            company="Acme",
            output_dir="/tmp/referral-out",
        )

        with (
            patch("jobhunter.main._load_settings", return_value={}),
            patch("jobhunter.main._load_profile", return_value=MagicMock()),
            patch(
                "jobhunter.scheduler.run_referral_once",
                new=AsyncMock(return_value=(Path("/tmp/a.pdf"), Path("/tmp/b.pdf"))),
            ) as mock_run,
        ):
            rc = cmd_prepare_referral(args)

        assert rc == 0
        kwargs = mock_run.await_args.kwargs
        assert kwargs["url"] == "https://linkedin.com/jobs/view/999"
        assert kwargs["title"] == "CISO"
        assert kwargs["company"] == "Acme"
        assert kwargs["output_dir"] == Path("/tmp/referral-out")


class TestCmdReviewPacket:
    def test_writes_packet_file_when_queue_empty(self, tmp_path):
        out_path = tmp_path / "packet.md"
        args = SimpleNamespace(limit=5, output=str(out_path))
        rc = cmd_review_packet(args)
        assert rc == 0
        assert out_path.exists()
        text = out_path.read_text()
        assert "Manual Review Packet" in text
        assert "No `needs_review` applications in queue." in text

    def test_writes_csv_when_requested(self, tmp_path):
        out_path = tmp_path / "packet.csv"
        args = SimpleNamespace(limit=5, output=str(out_path), csv=True)
        rc = cmd_review_packet(args)
        assert rc == 0
        assert out_path.exists()
        text = out_path.read_text()
        assert "app_id,job_id,apply_type" in text

    def test_writes_open_url_list_when_requested(self, tmp_path):
        out_path = tmp_path / "packet.txt"
        args = SimpleNamespace(limit=5, output=str(out_path), csv=False, open_only=True)
        rc = cmd_review_packet(args)
        assert rc == 0
        assert out_path.exists()
        # Empty queue in test DB => empty file
        assert out_path.read_text() == ""

    def test_rejects_csv_and_open_together(self, tmp_path):
        out_path = tmp_path / "packet.txt"
        args = SimpleNamespace(limit=5, output=str(out_path), csv=True, open_only=True)
        rc = cmd_review_packet(args)
        assert rc == 2
