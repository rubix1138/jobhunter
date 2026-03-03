"""Tests for CRUD repository classes."""

import json
import pytest

from jobhunter.db.engine import init_db
from jobhunter.db.models import (
    AgentRun, Application, Credential, EmailLog, Job, LlmUsage, QACache, WorkdayTenant
)
from jobhunter.db.repository import (
    AgentRunRepo, ApplicationRepo, CredentialRepo, EmailRepo, JobRepo, LlmUsageRepo,
    QACacheRepo, WorkdayTenantRepo,
)


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "test.db"))
    yield c
    c.close()


@pytest.fixture
def job_repo(conn):
    return JobRepo(conn)


@pytest.fixture
def app_repo(conn):
    return ApplicationRepo(conn)


@pytest.fixture
def cred_repo(conn):
    return CredentialRepo(conn)


@pytest.fixture
def email_repo(conn):
    return EmailRepo(conn)


@pytest.fixture
def run_repo(conn):
    return AgentRunRepo(conn)


@pytest.fixture
def llm_repo(conn):
    return LlmUsageRepo(conn)


@pytest.fixture
def tenant_repo(conn):
    return WorkdayTenantRepo(conn)


def make_job(**overrides) -> Job:
    defaults = {
        "linkedin_job_id": "jid-001",
        "title": "Senior Engineer",
        "company": "Acme Corp",
        "job_url": "https://linkedin.com/jobs/1",
    }
    defaults.update(overrides)
    return Job(**defaults)


# ── JobRepo ───────────────────────────────────────────────────────────────────

class TestJobRepo:
    def test_insert_and_get_by_id(self, job_repo):
        job = make_job()
        job_id = job_repo.insert(job)
        fetched = job_repo.get_by_id(job_id)
        assert fetched is not None
        assert fetched.title == "Senior Engineer"
        assert fetched.company == "Acme Corp"

    def test_get_by_linkedin_id(self, job_repo):
        job_repo.insert(make_job())
        fetched = job_repo.get_by_linkedin_id("jid-001")
        assert fetched is not None
        assert fetched.linkedin_job_id == "jid-001"

    def test_exists_true(self, job_repo):
        job_repo.insert(make_job())
        assert job_repo.exists("jid-001") is True

    def test_exists_false(self, job_repo):
        assert job_repo.exists("nonexistent") is False

    def test_get_by_id_missing(self, job_repo):
        assert job_repo.get_by_id(9999) is None

    def test_list_by_status(self, job_repo):
        job_repo.insert(make_job(linkedin_job_id="jid-001", status="qualified"))
        job_repo.insert(make_job(linkedin_job_id="jid-002", status="new"))
        results = job_repo.list_by_status("qualified")
        assert len(results) == 1
        assert results[0].status == "qualified"

    def test_update_status(self, job_repo):
        job_id = job_repo.insert(make_job())
        job_repo.update_status(job_id, "qualified")
        job = job_repo.get_by_id(job_id)
        assert job.status == "qualified"

    def test_update_score(self, job_repo):
        job_id = job_repo.insert(make_job())
        job_repo.update_score(job_id, 0.85, "Great match", "qualified")
        job = job_repo.get_by_id(job_id)
        assert job.match_score == pytest.approx(0.85)
        assert job.match_reasoning == "Great match"
        assert job.status == "qualified"

    def test_upsert_insert(self, job_repo):
        job = make_job()
        job_id = job_repo.upsert(job)
        assert job_id is not None
        assert job_repo.get_by_id(job_id).title == "Senior Engineer"

    def test_upsert_update(self, job_repo):
        job_repo.insert(make_job())
        updated = make_job(title="Staff Engineer")
        job_id = job_repo.upsert(updated)
        assert job_repo.get_by_id(job_id).title == "Staff Engineer"

    def test_duplicate_linkedin_id_raises(self, job_repo):
        job_repo.insert(make_job())
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            job_repo.insert(make_job())

    def test_list_qualified_without_application(self, conn, job_repo, app_repo):
        # easy_apply and unknown both appear (unknown gets live re-detection at apply time)
        jid1 = job_repo.insert(make_job(linkedin_job_id="j1", status="qualified", apply_type="easy_apply"))
        jid2 = job_repo.insert(make_job(linkedin_job_id="j2", status="qualified", apply_type="easy_apply"))
        jid4 = job_repo.insert(make_job(linkedin_job_id="j4", status="qualified", apply_type="unknown"))
        # interest_only and expired must always be excluded
        job_repo.insert(make_job(linkedin_job_id="j3", status="qualified", apply_type="interest_only"))
        job_repo.insert(make_job(linkedin_job_id="j5", status="qualified", apply_type="expired"))
        # Create application for jid1
        app_repo.insert(Application(job_id=jid1))
        results = job_repo.list_qualified_without_application()
        result_ids = {r.id for r in results}
        assert jid2 in result_ids
        assert jid4 in result_ids
        assert len(results) == 2


# ── ApplicationRepo ───────────────────────────────────────────────────────────

class TestApplicationRepo:
    def test_insert_and_get(self, job_repo, app_repo):
        job_id = job_repo.insert(make_job())
        app = Application(job_id=job_id, resume_text="My resume")
        app_id = app_repo.insert(app)
        fetched = app_repo.get_by_id(app_id)
        assert fetched is not None
        assert fetched.resume_text == "My resume"
        assert fetched.status == "pending"

    def test_get_latest_for_job(self, job_repo, app_repo):
        job_id = job_repo.insert(make_job())
        app_repo.insert(Application(job_id=job_id))
        app_id2 = app_repo.insert(Application(job_id=job_id, resume_text="v2"))
        latest = app_repo.get_latest_for_job(job_id)
        assert latest.id == app_id2

    def test_get_latest_none(self, job_repo, app_repo):
        job_id = job_repo.insert(make_job())
        assert app_repo.get_latest_for_job(job_id) is None

    def test_update_status(self, job_repo, app_repo):
        job_id = job_repo.insert(make_job())
        app_id = app_repo.insert(Application(job_id=job_id))
        app_repo.update_status(app_id, "failed", "Timeout")
        app = app_repo.get_by_id(app_id)
        assert app.status == "failed"
        assert app.error_message == "Timeout"

    def test_mark_submitted(self, job_repo, app_repo):
        job_id = job_repo.insert(make_job())
        app_id = app_repo.insert(Application(job_id=job_id))
        app_repo.mark_submitted(app_id)
        app = app_repo.get_by_id(app_id)
        assert app.status == "submitted"
        assert app.submitted_at is not None

    def test_increment_attempt(self, job_repo, app_repo):
        job_id = job_repo.insert(make_job())
        app_id = app_repo.insert(Application(job_id=job_id))
        app_repo.increment_attempt(app_id)
        app_repo.increment_attempt(app_id)
        app = app_repo.get_by_id(app_id)
        assert app.attempt_count == 2

    def test_count_submitted_today(self, job_repo, app_repo):
        job_id = job_repo.insert(make_job())
        app_id = app_repo.insert(Application(job_id=job_id))
        assert app_repo.count_submitted_today() == 0
        app_repo.mark_submitted(app_id)
        assert app_repo.count_submitted_today() == 1


# ── CredentialRepo ────────────────────────────────────────────────────────────

class TestCredentialRepo:
    def test_upsert_and_get(self, cred_repo):
        cred = Credential(domain="workday.com", username="user@test.com", password="enc_pass")
        cred_repo.upsert(cred)
        fetched = cred_repo.get("workday.com", "user@test.com")
        assert fetched is not None
        assert fetched.password == "enc_pass"

    def test_upsert_updates_password(self, cred_repo):
        cred = Credential(domain="workday.com", username="user@test.com", password="old")
        cred_repo.upsert(cred)
        cred.password = "new_encrypted"
        cred_repo.upsert(cred)
        fetched = cred_repo.get("workday.com", "user@test.com")
        assert fetched.password == "new_encrypted"

    def test_get_missing(self, cred_repo):
        assert cred_repo.get("nonexistent.com", "nobody") is None

    def test_list_by_domain(self, cred_repo):
        cred_repo.upsert(Credential(domain="site.com", username="a@a.com", password="p1"))
        cred_repo.upsert(Credential(domain="site.com", username="b@b.com", password="p2"))
        cred_repo.upsert(Credential(domain="other.com", username="c@c.com", password="p3"))
        results = cred_repo.list_by_domain("site.com")
        assert len(results) == 2

    def test_delete(self, cred_repo):
        cred_repo.upsert(Credential(domain="d.com", username="u@u.com", password="p"))
        cred_repo.delete("d.com", "u@u.com")
        assert cred_repo.get("d.com", "u@u.com") is None


class TestWorkdayTenantRepo:
    def test_upsert_and_get(self, tenant_repo):
        tenant_repo.upsert(
            WorkdayTenant(
                domain="acme.wd5.myworkdayjobs.com",
                auth_mode="signin_only",
                status="active",
                notes="detected",
            )
        )
        got = tenant_repo.get("acme.wd5.myworkdayjobs.com")
        assert got is not None
        assert got.auth_mode == "signin_only"
        assert got.status == "active"

    def test_upsert_updates(self, tenant_repo):
        tenant_repo.upsert(
            WorkdayTenant(domain="acme.wd5.myworkdayjobs.com", auth_mode="auto")
        )
        tenant_repo.upsert(
            WorkdayTenant(
                domain="acme.wd5.myworkdayjobs.com",
                auth_mode="sso_only",
                status="blocked",
                notes="recovery_failed",
            )
        )
        got = tenant_repo.get("acme.wd5.myworkdayjobs.com")
        assert got is not None
        assert got.auth_mode == "sso_only"
        assert got.status == "blocked"


# ── EmailRepo ─────────────────────────────────────────────────────────────────

class TestEmailRepo:
    def make_email(self, **overrides):
        defaults = {
            "gmail_message_id": "msg001",
            "from_address": "recruiter@company.com",
            "subject": "Interview Invitation",
            "received_at": "2024-01-15T10:00:00",
        }
        defaults.update(overrides)
        return EmailLog(**defaults)

    def test_insert_and_get(self, email_repo):
        email = self.make_email()
        email_id = email_repo.insert(email)
        fetched = email_repo.get_by_gmail_id("msg001")
        assert fetched is not None
        assert fetched.subject == "Interview Invitation"

    def test_exists(self, email_repo):
        email_repo.insert(self.make_email())
        assert email_repo.exists("msg001") is True
        assert email_repo.exists("nonexistent") is False

    def test_list_by_classification(self, email_repo):
        email_repo.insert(self.make_email(gmail_message_id="m1", classification="rejection"))
        email_repo.insert(self.make_email(gmail_message_id="m2", classification="rejection"))
        email_repo.insert(self.make_email(gmail_message_id="m3", classification="interview_invite"))
        rejections = email_repo.list_by_classification("rejection")
        assert len(rejections) == 2


# ── AgentRunRepo ──────────────────────────────────────────────────────────────

class TestAgentRunRepo:
    def test_start_and_finish(self, run_repo):
        run_id = run_repo.start("search_agent")
        run_repo.finish(run_id, "success", jobs_found=5)
        runs = run_repo.list_recent("search_agent")
        assert len(runs) == 1
        assert runs[0].status == "success"
        assert runs[0].jobs_found == 5

    def test_finish_with_error(self, run_repo):
        run_id = run_repo.start("apply_agent")
        run_repo.finish(run_id, "error", error_message="Timeout")
        runs = run_repo.list_recent("apply_agent")
        assert runs[0].error_message == "Timeout"

    def test_finish_with_details(self, run_repo):
        run_id = run_repo.start("email_agent")
        run_repo.finish(run_id, "success", details={"processed": 3, "skipped": 1})
        runs = run_repo.list_recent("email_agent")
        details = json.loads(runs[0].details_json)
        assert details["processed"] == 3

    def test_list_recent_limit(self, run_repo):
        for _ in range(5):
            rid = run_repo.start("test_agent")
            run_repo.finish(rid, "success")
        runs = run_repo.list_recent("test_agent", limit=3)
        assert len(runs) == 3

    def test_reconcile_stale_running_marks_error(self, conn, run_repo):
        run_id = run_repo.start("apply_agent")
        conn.execute(
            "UPDATE agent_runs SET started_at=datetime('now', '-4 hours') WHERE id=?",
            (run_id,),
        )
        conn.commit()

        fixed = run_repo.reconcile_stale_running(stale_after_minutes=60)
        assert fixed == 1

        row = conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        assert row["status"] == "error"
        assert row["finished_at"] is not None
        assert "Reconciled stale run" in (row["error_message"] or "")

    def test_reconcile_stale_running_keeps_fresh_rows(self, conn, run_repo):
        run_id = run_repo.start("search_agent")
        fixed = run_repo.reconcile_stale_running(stale_after_minutes=180)
        assert fixed == 0

        row = conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        assert row["status"] == "running"
        assert row["finished_at"] is None


# ── LlmUsageRepo ─────────────────────────────────────────────────────────────

class TestLlmUsageRepo:
    def test_insert_and_daily_cost(self, llm_repo):
        usage = LlmUsage(
            agent_name="search_agent",
            model="claude-sonnet-4-6",
            purpose="job_scoring",
            input_tokens=1000,
            output_tokens=200,
            cost_usd=0.015,
        )
        llm_repo.insert(usage)
        assert llm_repo.daily_cost() == pytest.approx(0.015)

    def test_daily_cost_accumulates(self, llm_repo):
        for i in range(3):
            llm_repo.insert(LlmUsage(
                agent_name="agent",
                model="claude-sonnet-4-6",
                purpose="test",
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.01,
            ))
        assert llm_repo.daily_cost() == pytest.approx(0.03)

    def test_cost_by_agent_today(self, llm_repo):
        llm_repo.insert(LlmUsage("search_agent", "claude-sonnet-4-6", "scoring", 100, 50, 0.01))
        llm_repo.insert(LlmUsage("apply_agent", "claude-opus-4-6", "resume", 500, 300, 0.15))
        costs = llm_repo.cost_by_agent_today()
        assert costs["search_agent"] == pytest.approx(0.01)
        assert costs["apply_agent"] == pytest.approx(0.15)


# ── QACacheRepo ───────────────────────────────────────────────────────────────

@pytest.fixture
def qa_repo(conn):
    return QACacheRepo(conn)


def make_qa_cache(**overrides) -> QACache:
    defaults = {
        "question_key": "years of infosec experience",
        "options_hash": "",
        "field_type": "text",
        "answer": "8",
        "confidence": 0.85,
        "source": "claude",
    }
    defaults.update(overrides)
    return QACache(**defaults)


class TestQACacheRepo:
    def test_qa_cache_upsert_and_get(self, qa_repo):
        entry = make_qa_cache()
        qa_repo.upsert(entry)
        fetched = qa_repo.get(entry.question_key, entry.options_hash)
        assert fetched is not None
        assert fetched.answer == "8"
        assert fetched.confidence == pytest.approx(0.85)
        assert fetched.source == "claude"
        assert fetched.times_used == 1

    def test_qa_cache_increments_times_used(self, qa_repo):
        entry = make_qa_cache()
        qa_repo.upsert(entry)
        qa_repo.upsert(entry)
        fetched = qa_repo.get(entry.question_key, entry.options_hash)
        assert fetched.times_used == 2

    def test_qa_cache_updates_answer(self, qa_repo):
        qa_repo.upsert(make_qa_cache(answer="8"))
        qa_repo.upsert(make_qa_cache(answer="10", confidence=0.9))
        fetched = qa_repo.get("years of infosec experience", "")
        assert fetched.answer == "10"
        assert fetched.confidence == pytest.approx(0.9)

    def test_qa_cache_miss_returns_none(self, qa_repo):
        result = qa_repo.get("nonexistent question", "")
        assert result is None
