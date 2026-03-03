"""Tests for scheduler.py — daily summary, email throttling, LLM factory."""

import sqlite3
from datetime import datetime, timedelta, date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jobhunter.db.engine import init_db
from jobhunter.scheduler import (
    JobHunterScheduler,
    build_daily_summary,
    print_daily_summary,
    _build_browser_session,
    _build_llm,
    run_referral_once,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = init_db(path)
    conn.close()
    return path


@pytest.fixture
def conn(db_path):
    c = init_db(db_path)
    yield c
    c.close()


@pytest.fixture
def settings() -> dict:
    return {
        "scheduler": {
            "search_interval_min": 240,
            "search_interval_max": 360,
            "apply_interval_min": 120,
            "apply_interval_max": 180,
            "email_interval_business": 5,
            "email_interval_offhours": 30,
            "business_hours_start": 8,
            "business_hours_end": 20,
        },
        "rate_limits": {
            "linkedin_page_loads_per_hour": 40,
            "applications_per_day": 25,
            "applications_per_run": 10,
        },
        "budget": {
            "daily_limit_usd": 15.0,
            "alert_threshold_pct": 0.80,
        },
        "models": {
            "routine": "claude-sonnet-4-6",
            "writing": "claude-opus-4-6",
        },
        "thresholds": {
            "recruiter_reply_min_score": 0.7,
        },
    }


@pytest.fixture
def profile():
    from jobhunter.utils.profile_loader import (
        UserProfile, PersonalInfo, Preferences, ApplicationAnswers
    )
    return UserProfile(
        personal=PersonalInfo(
            first_name="Jane",
            last_name="Doe",
            email="jane@job.com",
            personal_email="jane@personal.com",
            phone="555-0000",
            location="Remote",
        ),
        preferences=Preferences(job_titles=["Engineer"]),
        application_answers=ApplicationAnswers(),
    )


def _make_scheduler(settings, profile, db_path) -> JobHunterScheduler:
    return JobHunterScheduler(
        settings=settings,
        profile=profile,
        queries=[{"keywords": "python engineer", "location": "Remote"}],
        db_path=db_path,
    )


# ── build_daily_summary ───────────────────────────────────────────────────────

class TestBuildDailySummary:
    def test_empty_db_returns_zeros(self, conn):
        summary = build_daily_summary(conn)
        assert summary["jobs_found"] == 0
        assert summary["apps_submitted"] == 0
        assert summary["emails_processed"] == 0
        assert summary["rejections"] == 0
        assert summary["interviews"] == 0
        assert summary["llm_cost_usd"] == 0.0

    def test_date_is_today(self, conn):
        from datetime import date
        summary = build_daily_summary(conn)
        assert summary["date"] == date.today().isoformat()

    def test_counts_todays_jobs(self, conn):
        today = date.today().isoformat()
        conn.execute(
            "INSERT INTO jobs (linkedin_job_id, title, company, job_url, status, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("j1", "Engineer", "Acme", "https://li.com/j1", "qualified", today),
        )
        conn.execute(
            "INSERT INTO jobs (linkedin_job_id, title, company, job_url, status, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("j2", "Developer", "Corp", "https://li.com/j2", "qualified", today),
        )
        conn.commit()
        summary = build_daily_summary(conn)
        assert summary["jobs_found"] == 2

    def test_counts_submitted_applications(self, conn):
        today = date.today().isoformat()
        conn.execute(
            "INSERT INTO jobs (linkedin_job_id, title, company, job_url, status) "
            "VALUES ('j1', 'Eng', 'Co', 'https://li.com/j1', 'applied')"
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO applications (job_id, status, created_at) "
            "VALUES (?, 'submitted', ?)",
            (job_id, today),
        )
        conn.commit()
        summary = build_daily_summary(conn)
        assert summary["apps_submitted"] == 1

    def test_counts_todays_emails(self, conn):
        today = date.today().isoformat()
        conn.execute(
            "INSERT INTO email_log "
            "(gmail_message_id, thread_id, from_address, to_address, subject, "
            "body_preview, received_at, classification, confidence, action_taken, "
            "action_details, processed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("msg1", "t1", "a@b.com", "me@me.com", "Sub",
             "prev", "Mon, 1 Jan 2024 10:00:00", "rejection", 0.9,
             "labeled_rejected", "company=X", today),
        )
        conn.commit()
        summary = build_daily_summary(conn)
        assert summary["emails_processed"] == 1
        assert summary["rejections"] == 1

    def test_counts_interview_invites(self, conn):
        today = date.today().isoformat()
        conn.execute(
            "INSERT INTO email_log "
            "(gmail_message_id, thread_id, from_address, to_address, subject, "
            "body_preview, received_at, classification, confidence, action_taken, "
            "action_details, processed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("msg2", "t2", "a@b.com", "me@me.com", "Interview",
             "prev", "Mon, 1 Jan 2024 10:00:00", "interview_invite", 0.98,
             "forwarded", "→ personal@me.com", today),
        )
        conn.commit()
        summary = build_daily_summary(conn)
        assert summary["interviews"] == 1

    def test_sums_llm_cost(self, conn):
        today = date.today().isoformat()
        conn.execute(
            "INSERT INTO llm_usage "
            "(agent_name, model, purpose, input_tokens, output_tokens, cost_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("search_agent", "claude-sonnet-4-6", "scoring", 1000, 200, 0.0045, today),
        )
        conn.execute(
            "INSERT INTO llm_usage "
            "(agent_name, model, purpose, input_tokens, output_tokens, cost_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("email_agent", "claude-sonnet-4-6", "classification", 500, 100, 0.0018, today),
        )
        conn.commit()
        summary = build_daily_summary(conn)
        assert summary["llm_cost_usd"] == pytest.approx(0.0063, rel=1e-3)

    def test_does_not_count_old_records(self, conn):
        yesterday = "2020-01-01"
        conn.execute(
            "INSERT INTO jobs (linkedin_job_id, title, company, job_url, status, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("old1", "Old Job", "OldCo", "https://li.com/old", "qualified", yesterday),
        )
        conn.commit()
        summary = build_daily_summary(conn)
        assert summary["jobs_found"] == 0

    def test_includes_needs_review_queue_items(self, conn):
        today = date.today().isoformat()
        conn.execute(
            "INSERT INTO jobs (linkedin_job_id, title, company, job_url, external_url, apply_type, status, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "r1",
                "Director Security",
                "Acme",
                "https://linkedin.com/jobs/view/r1",
                "https://jobs.example.com/apply/1",
                "external_other",
                "qualified",
                today,
            ),
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO applications (job_id, status, error_message, updated_at, created_at) "
            "VALUES (?, 'needs_review', ?, ?, ?)",
            (
                job_id,
                "CAPTCHA detected — needs_review | apply_type=external_other",
                today,
                today,
            ),
        )
        conn.commit()
        summary = build_daily_summary(conn)
        assert len(summary["review_queue"]) == 1
        assert summary["review_queue"][0]["job_id"] == job_id


# ── print_daily_summary ───────────────────────────────────────────────────────

class TestPrintDailySummary:
    def test_outputs_date(self, capsys):
        summary = {
            "date": "2024-06-15",
            "jobs_found": 12,
            "apps_submitted": 3,
            "emails_processed": 7,
            "rejections": 2,
            "interviews": 1,
            "llm_cost_usd": 4.1234,
            "review_queue": [],
        }
        print_daily_summary(summary)
        out = capsys.readouterr().out
        assert "2024-06-15" in out

    def test_outputs_all_metrics(self, capsys):
        summary = {
            "date": "2024-06-15",
            "jobs_found": 12,
            "apps_submitted": 3,
            "emails_processed": 7,
            "rejections": 2,
            "interviews": 1,
            "llm_cost_usd": 4.1234,
            "review_queue": [],
        }
        print_daily_summary(summary)
        out = capsys.readouterr().out
        assert "12" in out
        assert "3" in out
        assert "7" in out
        assert "2" in out
        assert "1" in out
        assert "4.1234" in out

    def test_outputs_review_queue_section(self, capsys):
        summary = {
            "date": "2024-06-15",
            "jobs_found": 1,
            "apps_submitted": 0,
            "emails_processed": 0,
            "rejections": 0,
            "interviews": 0,
            "llm_cost_usd": 0.0,
            "review_queue": [
                {
                    "app_id": 77,
                    "apply_type": "external_other",
                    "title": "Director Security",
                    "company": "Acme",
                    "error_message": "CAPTCHA detected — needs_review",
                    "url": "https://jobs.example.com/apply/1",
                }
            ],
        }
        print_daily_summary(summary)
        out = capsys.readouterr().out
        assert "Needs-review queue" in out
        assert "App #77" in out


# ── _build_llm ────────────────────────────────────────────────────────────────

class TestBuildLlm:
    def test_uses_models_from_settings(self):
        settings = {
            "models": {
                "routine": "claude-sonnet-4-6",
                "writing": "claude-opus-4-6",
            }
        }
        with patch("jobhunter.scheduler.ClaudeClient") as MockClient:
            MockClient.return_value = MagicMock()
            _build_llm(settings)
            MockClient.assert_called_once_with(
                sonnet_model="claude-sonnet-4-6",
                opus_model="claude-opus-4-6",
            )


class TestBuildBrowserSession:
    def test_uses_minimize_setting_and_label(self):
        settings = {"browser": {"start_minimized": True}}
        with (
            patch("jobhunter.scheduler.os.getpid", return_value=4321),
            patch("jobhunter.scheduler.BrowserSession") as MockSession,
        ):
            _build_browser_session(settings, "search-now")
            MockSession.assert_called_once_with(
                start_minimized=True,
                window_label="search-now-pid4321",
            )

    def test_defaults_minimize_false(self):
        with (
            patch("jobhunter.scheduler.os.getpid", return_value=999),
            patch("jobhunter.scheduler.BrowserSession") as MockSession,
        ):
            _build_browser_session({}, "scheduler")
            MockSession.assert_called_once_with(
                start_minimized=False,
                window_label="scheduler-pid999",
            )


class TestRunReferralOnce:
    @pytest.mark.asyncio
    async def test_non_linkedin_url_skips_browser_session(self, settings, profile):
        from pathlib import Path

        fake_llm = MagicMock()
        resume_path = Path("/tmp/resume.pdf")
        cover_path = Path("/tmp/cover.pdf")

        with (
            patch("jobhunter.scheduler._build_llm", return_value=fake_llm),
            patch("jobhunter.scheduler.BrowserSession") as MockSession,
            patch(
                "jobhunter.agents.referral_agent.generate_referral_materials",
                new=AsyncMock(return_value=(resume_path, cover_path)),
            ) as mock_generate,
        ):
            out_resume, out_cover = await run_referral_once(
                settings=settings,
                profile=profile,
                url="https://example.com/jobs/123",
                output_dir="data/resumes",
            )

        assert (out_resume, out_cover) == (resume_path, cover_path)
        MockSession.assert_not_called()
        kwargs = mock_generate.await_args.kwargs
        assert kwargs["url"] == "https://example.com/jobs/123"
        assert kwargs["profile"] is profile
        assert kwargs["llm"] is fake_llm
        assert kwargs["output_dir"] == Path("data/resumes")
        assert kwargs["title_override"] is None
        assert kwargs["company_override"] is None
        assert kwargs["browser_session"] is None

    @pytest.mark.asyncio
    async def test_linkedin_url_uses_and_stops_browser_session(self, settings, profile):
        from pathlib import Path

        fake_llm = MagicMock()
        fake_session = MagicMock()
        fake_session.start = AsyncMock()
        fake_session.ensure_linkedin_session = AsyncMock()
        fake_session.stop = AsyncMock()

        with (
            patch("jobhunter.scheduler._build_llm", return_value=fake_llm),
            patch("jobhunter.scheduler.BrowserSession", return_value=fake_session),
            patch(
                "jobhunter.agents.referral_agent.generate_referral_materials",
                new=AsyncMock(return_value=(Path("/tmp/r.pdf"), Path("/tmp/c.pdf"))),
            ) as mock_generate,
        ):
            await run_referral_once(
                settings=settings,
                profile=profile,
                url="https://www.linkedin.com/jobs/view/123",
                output_dir=Path("data/resumes"),
                title="Security Engineer",
                company="Acme",
            )

        fake_session.start.assert_awaited_once()
        fake_session.ensure_linkedin_session.assert_awaited_once()
        fake_session.stop.assert_awaited_once()
        kwargs = mock_generate.await_args.kwargs
        assert kwargs["browser_session"] is fake_session
        assert kwargs["title_override"] == "Security Engineer"
        assert kwargs["company_override"] == "Acme"

    def test_uses_default_models_when_not_configured(self):
        with patch("jobhunter.scheduler.ClaudeClient") as MockClient:
            MockClient.return_value = MagicMock()
            _build_llm({})
            MockClient.assert_called_once_with(
                sonnet_model="claude-sonnet-4-6",
                opus_model="claude-opus-4-6",
            )


# ── Email throttling logic ─────────────────────────────────────────────────────

class TestEmailThrottling:
    """Test the business-hours email throttling inside _run_email."""

    def _make_scheduler_no_deps(self, settings, profile, db_path):
        """Build a scheduler without starting it (no browser/Gmail needed)."""
        s = _make_scheduler(settings, profile, db_path)
        # Provide a mock gmail and llm so _run_email can build the agent
        s._gmail = MagicMock()
        s._llm = MagicMock()
        return s

    @pytest.mark.asyncio
    async def test_skips_when_called_too_soon_in_business_hours(
        self, settings, profile, db_path
    ):
        scheduler = self._make_scheduler_no_deps(settings, profile, db_path)
        # Use a fixed anchor: noon today
        fake_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        # Last run was 2 minutes before fake_now (threshold = 5 min)
        scheduler._last_email_run = fake_now - timedelta(minutes=2)

        with patch("jobhunter.scheduler.EmailAgent") as MockAgent:
            with patch("jobhunter.scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = fake_now
                await scheduler._run_email()

        MockAgent.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_when_enough_time_has_passed(self, settings, profile, db_path):
        scheduler = self._make_scheduler_no_deps(settings, profile, db_path)
        # Fixed anchor: noon today
        fake_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        # Last run was 10 minutes before fake_now (threshold = 5 min business hours)
        scheduler._last_email_run = fake_now - timedelta(minutes=10)

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=MagicMock(emails_processed=2))

        with patch("jobhunter.scheduler.EmailAgent", return_value=mock_agent):
            with patch("jobhunter.scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = fake_now
                await scheduler._run_email()

        mock_agent.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_off_hours_uses_30_minute_threshold(self, settings, profile, db_path):
        scheduler = self._make_scheduler_no_deps(settings, profile, db_path)
        # Fixed anchor: 11 PM (off-hours)
        fake_now = datetime.now().replace(hour=23, minute=0, second=0, microsecond=0)
        # Last run was 10 minutes before fake_now — ok for business hours but not 30-min off-hours
        scheduler._last_email_run = fake_now - timedelta(minutes=10)

        with patch("jobhunter.scheduler.EmailAgent") as MockAgent:
            with patch("jobhunter.scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = fake_now
                await scheduler._run_email()

        MockAgent.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_first_time_with_no_previous_run(self, settings, profile, db_path):
        scheduler = self._make_scheduler_no_deps(settings, profile, db_path)
        assert scheduler._last_email_run is None

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=MagicMock(emails_processed=0))

        fake_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        with patch("jobhunter.scheduler.EmailAgent", return_value=mock_agent):
            with patch("jobhunter.scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = fake_now
                await scheduler._run_email()

        mock_agent.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_updates_last_email_run_after_success(self, settings, profile, db_path):
        scheduler = self._make_scheduler_no_deps(settings, profile, db_path)
        scheduler._last_email_run = None

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=MagicMock(emails_processed=3))

        fake_now = datetime.now().replace(hour=14, minute=0, second=0, microsecond=0)
        with patch("jobhunter.scheduler.EmailAgent", return_value=mock_agent):
            with patch("jobhunter.scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = fake_now
                await scheduler._run_email()

        assert scheduler._last_email_run is not None

    @pytest.mark.asyncio
    async def test_error_in_email_agent_does_not_raise(self, settings, profile, db_path):
        scheduler = self._make_scheduler_no_deps(settings, profile, db_path)

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(side_effect=Exception("Gmail quota exceeded"))

        fake_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        with patch("jobhunter.scheduler.EmailAgent", return_value=mock_agent):
            with patch("jobhunter.scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = fake_now
                # Should not raise
                await scheduler._run_email()


# ── Daily summary via scheduler ───────────────────────────────────────────────

class TestSchedulerDailySummary:
    @pytest.mark.asyncio
    async def test_daily_summary_does_not_raise_on_empty_db(
        self, settings, profile, db_path
    ):
        scheduler = _make_scheduler(settings, profile, db_path)
        # Should not raise even with an empty database
        await scheduler._daily_summary()

    @pytest.mark.asyncio
    async def test_daily_summary_prints_output(
        self, settings, profile, db_path, capsys
    ):
        scheduler = _make_scheduler(settings, profile, db_path)
        await scheduler._daily_summary()
        out = capsys.readouterr().out
        assert "JobHunter Daily Summary" in out
