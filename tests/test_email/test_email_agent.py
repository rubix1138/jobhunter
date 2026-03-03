"""Tests for EmailAgent — action routing, job linking, label management."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from jobhunter.agents.email_agent import EmailAgent
from jobhunter.gmail.classifier import ClassificationResult
from jobhunter.gmail.client import GmailMessage
from jobhunter.db.engine import init_db
from jobhunter.db.models import Job
from jobhunter.db.repository import JobRepo
from jobhunter.utils.profile_loader import (
    UserProfile,
    PersonalInfo,
    Preferences,
    Skills,
    ApplicationAnswers,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = init_db(path)
    conn.close()
    return path


@pytest.fixture
def profile() -> UserProfile:
    return UserProfile(
        personal=PersonalInfo(
            first_name="Jane",
            last_name="Doe",
            email="jane.job@gmail.com",
            personal_email="jane.personal@gmail.com",
            phone="555-1234",
            location="Remote",
        ),
        preferences=Preferences(job_titles=["Software Engineer"]),
        application_answers=ApplicationAnswers(),
    )


@pytest.fixture
def settings() -> dict:
    return {
        "budget": {"daily_limit_usd": 10.0},
        "thresholds": {
            "recruiter_reply_min_score": 0.7,
            "recruiter_reply_min_classification_confidence": 0.75,
        },
    }


def _make_gmail() -> MagicMock:
    gmail = MagicMock()
    gmail.list_unread_inbox.return_value = []
    gmail.get_message.return_value = None
    gmail.mark_read.return_value = True
    gmail.archive.return_value = True
    gmail.apply_label.return_value = True
    gmail.forward_message.return_value = True
    gmail.send_message.return_value = True
    gmail.get_or_create_label.return_value = "Label_123"
    return gmail


def _make_llm(reply_text: str = "Auto-reply text") -> MagicMock:
    llm = MagicMock()
    usage = {
        "model": "claude-sonnet-4-5",
        "purpose": "test",
        "input_tokens": 50,
        "output_tokens": 20,
        "cost_usd": 0.0005,
    }
    llm.message = AsyncMock(return_value=(reply_text, usage))
    llm.sonnet_model = "claude-sonnet-4-5"
    return llm


def _make_agent(gmail, llm, profile, settings, db_path) -> EmailAgent:
    agent = EmailAgent(gmail=gmail, llm=llm, profile=profile, settings=settings, db_path=db_path)
    # Pre-populate label cache so label operations work without API calls in tests
    agent._label_ids = {
        "JobHunter/Rejected": "Label_rejected",
        "JobHunter/Processed": "Label_processed",
        "JobHunter/Interview": "Label_interview",
        "JobHunter/Offer": "Label_offer",
    }
    return agent


def _make_message(
    msg_id: str = "msg001",
    subject: str = "Test Subject",
    from_addr: str = "sender@example.com",
    body: str = "Email body text.",
) -> GmailMessage:
    return GmailMessage(
        message_id=msg_id,
        thread_id="thread001",
        from_address=from_addr,
        to_address="jane.job@gmail.com",
        subject=subject,
        body_text=body,
        body_preview=body[:500],
        received_at="Mon, 1 Jan 2024 10:00:00 +0000",
        labels=["INBOX", "UNREAD"],
    )


def _make_classification(
    cls: str = "rejection",
    confidence: float = 0.95,
    company: str = "Acme Corp",
    reasoning: str = "Rejection wording",
    should_forward: bool = False,
    new_job_status: str | None = "rejected",
) -> ClassificationResult:
    return ClassificationResult(
        classification=cls,
        confidence=confidence,
        company_name=company,
        reasoning=reasoning,
        should_forward=should_forward,
        new_job_status=new_job_status,
    )


def _insert_job(db_path: str, company: str, title: str, status: str = "applied", score: float = 0.85) -> Job:
    conn = init_db(db_path)
    repo = JobRepo(conn)
    job = Job(
        linkedin_job_id=f"job_{company.lower().replace(' ', '_')}",
        title=title,
        company=company,
        job_url=f"https://linkedin.com/jobs/{company.lower().replace(' ', '-')}",
        location="Remote",
        description="Job description",
        apply_type="external_other",
        match_score=score,
        status=status,
    )
    job_id = repo.insert(job)
    conn.close()
    job.id = job_id
    return job


# ── _act: rejection ───────────────────────────────────────────────────────────

class TestActRejection:
    @pytest.mark.asyncio
    async def test_rejection_applies_label(self, db_path, profile, settings):
        gmail = _make_gmail()
        llm = _make_llm()
        agent = _make_agent(gmail, llm, profile, settings, db_path)

        msg = _make_message()
        result = _make_classification("rejection", should_forward=False, new_job_status="rejected")

        action, details = await agent._act(msg, result, None, MagicMock())

        assert action == "labeled_rejected"
        gmail.apply_label.assert_called_once_with("msg001", "Label_rejected")

    @pytest.mark.asyncio
    async def test_rejection_details_include_company(self, db_path, profile, settings):
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)

        msg = _make_message()
        result = _make_classification("rejection", company="TechCorp", should_forward=False)

        action, details = await agent._act(msg, result, None, MagicMock())

        assert "TechCorp" in details

    @pytest.mark.asyncio
    async def test_rejection_no_label_id_no_api_call(self, db_path, profile, settings):
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)
        agent._label_ids = {}  # Clear label cache

        msg = _make_message()
        result = _make_classification("rejection", should_forward=False)

        action, _ = await agent._act(msg, result, None, MagicMock())

        assert action == "labeled_rejected"
        gmail.apply_label.assert_not_called()


# ── _act: forward ─────────────────────────────────────────────────────────────

class TestActForward:
    @pytest.mark.asyncio
    async def test_interview_invite_is_forwarded(self, db_path, profile, settings):
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)

        msg = _make_message()
        result = _make_classification(
            "interview_invite", should_forward=True, new_job_status="interviewing"
        )

        action, details = await agent._act(msg, result, None, MagicMock())

        assert action == "forwarded"
        assert "jane.personal@gmail.com" in details
        gmail.forward_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_offer_is_forwarded_with_label(self, db_path, profile, settings):
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)

        msg = _make_message()
        result = _make_classification("offer", should_forward=True, new_job_status="offer")

        action, details = await agent._act(msg, result, None, MagicMock())

        assert action == "forwarded"
        gmail.apply_label.assert_called_once_with("msg001", "Label_offer")

    @pytest.mark.asyncio
    async def test_interview_invite_applies_interview_label(self, db_path, profile, settings):
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)

        msg = _make_message()
        result = _make_classification("interview_invite", should_forward=True)

        action, details = await agent._act(msg, result, None, MagicMock())

        gmail.apply_label.assert_called_once_with("msg001", "Label_interview")

    @pytest.mark.asyncio
    async def test_unknown_forwarded_without_label(self, db_path, profile, settings):
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)

        msg = _make_message()
        result = _make_classification(
            "unknown", should_forward=True, new_job_status=None
        )

        action, details = await agent._act(msg, result, None, MagicMock())

        assert action == "forwarded"
        # "unknown" has no entry in _CLASSIFICATION_LABELS, so no label applied
        gmail.apply_label.assert_not_called()


# ── _act: spam ────────────────────────────────────────────────────────────────

class TestActSpam:
    @pytest.mark.asyncio
    async def test_spam_is_archived(self, db_path, profile, settings):
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)

        msg = _make_message()
        result = _make_classification("spam", should_forward=False, new_job_status=None)

        action, details = await agent._act(msg, result, None, MagicMock())

        assert action == "archived"
        gmail.archive.assert_called_once_with("msg001")


# ── _act: recruiter_outreach ─────────────────────────────────────────────────

class TestActRecruiter:
    @pytest.mark.asyncio
    async def test_recruiter_above_threshold_auto_replies(self, db_path, profile, settings):
        gmail = _make_gmail()
        llm = _make_llm("Dear Recruiter, I am interested...")
        agent = _make_agent(gmail, llm, profile, settings, db_path)

        msg = _make_message(from_addr="recruiter@bigcorp.com", subject="Exciting Role")
        result = _make_classification("recruiter_outreach", company="BigCorp", should_forward=False)

        # Linked job with high score
        linked_job = MagicMock()
        linked_job.match_score = 0.85
        linked_job.title = "Senior Engineer"
        linked_job.company = "BigCorp"
        linked_job.company_domain = None
        linked_job.external_url = None
        linked_job.job_url = "https://www.linkedin.com/jobs/view/123"

        action, details = await agent._act(msg, result, linked_job, MagicMock())

        assert action == "auto_replied"
        assert "recruiter@bigcorp.com" in details
        gmail.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_recruiter_below_threshold_is_ignored(self, db_path, profile, settings):
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)

        msg = _make_message()
        result = _make_classification("recruiter_outreach", should_forward=False)

        linked_job = MagicMock()
        linked_job.match_score = 0.40  # below 0.7 threshold
        linked_job.company = "Acme"
        linked_job.job_url = "https://www.linkedin.com/jobs/view/1"
        linked_job.external_url = None
        linked_job.company_domain = None

        action, details = await agent._act(msg, result, linked_job, MagicMock())

        assert action == "ignored"
        assert "below threshold" in details
        gmail.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_recruiter_low_classification_confidence_is_ignored(self, db_path, profile, settings):
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)

        msg = _make_message(from_addr="recruiter@bigcorp.com")
        result = _make_classification(
            "recruiter_outreach",
            confidence=0.40,
            company="BigCorp",
            should_forward=False,
        )

        linked_job = MagicMock()
        linked_job.match_score = 0.95
        linked_job.title = "Senior Engineer"
        linked_job.company = "BigCorp"
        linked_job.company_domain = None
        linked_job.external_url = None
        linked_job.job_url = "https://www.linkedin.com/jobs/view/123"

        action, details = await agent._act(msg, result, linked_job, MagicMock())

        assert action == "ignored"
        assert "classification confidence" in details
        gmail.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_recruiter_no_linked_job_is_ignored(self, db_path, profile, settings):
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)

        msg = _make_message()
        result = _make_classification("recruiter_outreach", should_forward=False)

        action, details = await agent._act(msg, result, None, MagicMock())

        # No linked job → score = 0.0 → below threshold
        assert action == "ignored"

    @pytest.mark.asyncio
    async def test_recruiter_auto_reply_score_in_details(self, db_path, profile, settings):
        gmail = _make_gmail()
        llm = _make_llm("Reply text")
        agent = _make_agent(gmail, llm, profile, settings, db_path)

        msg = _make_message(from_addr="recruiter@acmesecurity.com")
        result = _make_classification("recruiter_outreach", should_forward=False)

        linked_job = MagicMock()
        linked_job.match_score = 0.92
        linked_job.title = "Staff Security Engineer"
        linked_job.company = "Acme Security"
        linked_job.company_domain = None
        linked_job.external_url = None
        linked_job.job_url = "https://www.linkedin.com/jobs/view/123"

        action, details = await agent._act(msg, result, linked_job, MagicMock())

        assert "0.92" in details

    @pytest.mark.asyncio
    async def test_recruiter_sender_domain_mismatch_is_ignored(self, db_path, profile, settings):
        gmail = _make_gmail()
        llm = _make_llm("Reply text")
        agent = _make_agent(gmail, llm, profile, settings, db_path)

        msg = _make_message(from_addr="recruiter@evil-domain.com")
        result = _make_classification("recruiter_outreach", should_forward=False, company="BigCorp")

        linked_job = MagicMock()
        linked_job.match_score = 0.93
        linked_job.title = "Senior Engineer"
        linked_job.company = "BigCorp"
        linked_job.company_domain = "bigcorp.com"
        linked_job.external_url = None
        linked_job.job_url = "https://www.linkedin.com/jobs/view/123"

        action, details = await agent._act(msg, result, linked_job, MagicMock())
        assert action == "ignored"
        assert "does not match linked job" in details
        gmail.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_recruiter_free_email_sender_is_ignored(self, db_path, profile, settings):
        gmail = _make_gmail()
        llm = _make_llm("Reply text")
        agent = _make_agent(gmail, llm, profile, settings, db_path)

        msg = _make_message(from_addr="Recruiter Name <someone@gmail.com>")
        result = _make_classification("recruiter_outreach", should_forward=False, company="BigCorp")

        linked_job = MagicMock()
        linked_job.match_score = 0.94
        linked_job.title = "Senior Engineer"
        linked_job.company = "BigCorp"
        linked_job.company_domain = "bigcorp.com"
        linked_job.external_url = None
        linked_job.job_url = "https://www.linkedin.com/jobs/view/123"

        action, details = await agent._act(msg, result, linked_job, MagicMock())
        assert action == "ignored"
        assert "free-email" in details
        gmail.send_message.assert_not_called()


# ── _find_linked_job ──────────────────────────────────────────────────────────

class TestFindLinkedJob:
    def test_finds_exact_company_match(self, db_path, profile, settings):
        _insert_job(db_path, "Acme Corp", "Engineer", status="applied")
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)
        agent._open_db()

        job_repo = JobRepo(agent._conn)
        result = agent._find_linked_job("Acme Corp", job_repo)

        agent._conn.close()
        assert result is not None
        assert result.company == "Acme Corp"

    def test_finds_partial_company_match(self, db_path, profile, settings):
        _insert_job(db_path, "Acme Corporation", "Engineer", status="applied")
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)
        agent._open_db()

        job_repo = JobRepo(agent._conn)
        result = agent._find_linked_job("Acme", job_repo)

        agent._conn.close()
        assert result is not None

    def test_finds_reverse_partial_match(self, db_path, profile, settings):
        _insert_job(db_path, "Acme", "Engineer", status="applied")
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)
        agent._open_db()

        job_repo = JobRepo(agent._conn)
        result = agent._find_linked_job("Acme Corporation", job_repo)

        agent._conn.close()
        assert result is not None

    def test_returns_none_for_no_match(self, db_path, profile, settings):
        _insert_job(db_path, "TechCo", "Engineer", status="applied")
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)
        agent._open_db()

        job_repo = JobRepo(agent._conn)
        result = agent._find_linked_job("Completely Different Corp", job_repo)

        agent._conn.close()
        assert result is None

    def test_returns_none_when_company_name_is_none(self, db_path, profile, settings):
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)
        agent._open_db()

        job_repo = JobRepo(agent._conn)
        result = agent._find_linked_job(None, job_repo)

        agent._conn.close()
        assert result is None

    def test_case_insensitive_match(self, db_path, profile, settings):
        _insert_job(db_path, "BIGTECH INC", "Engineer", status="applied")
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)
        agent._open_db()

        job_repo = JobRepo(agent._conn)
        result = agent._find_linked_job("bigtech inc", job_repo)

        agent._conn.close()
        assert result is not None

    def test_searches_across_multiple_statuses(self, db_path, profile, settings):
        _insert_job(db_path, "InterviewCorp", "Engineer", status="interviewing")
        gmail = _make_gmail()
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)
        agent._open_db()

        job_repo = JobRepo(agent._conn)
        result = agent._find_linked_job("InterviewCorp", job_repo)

        agent._conn.close()
        assert result is not None


# ── _forward_note ─────────────────────────────────────────────────────────────

class TestForwardNote:
    def test_includes_classification_and_confidence(self, db_path, profile, settings):
        agent = _make_agent(_make_gmail(), _make_llm(), profile, settings, db_path)
        result = _make_classification("interview_invite", confidence=0.92)

        note = agent._forward_note(result, None)

        assert "INTERVIEW_INVITE" in note
        assert "92%" in note

    def test_includes_linked_job_info(self, db_path, profile, settings):
        agent = _make_agent(_make_gmail(), _make_llm(), profile, settings, db_path)
        result = _make_classification("offer")

        job = MagicMock()
        job.title = "Senior Engineer"
        job.company = "MegaCorp"
        job.match_score = 0.88

        note = agent._forward_note(result, job)

        assert "Senior Engineer" in note
        assert "MegaCorp" in note
        assert "0.88" in note

    def test_includes_reasoning(self, db_path, profile, settings):
        agent = _make_agent(_make_gmail(), _make_llm(), profile, settings, db_path)
        result = _make_classification(reasoning="Clearly an offer letter")

        note = agent._forward_note(result, None)

        assert "Clearly an offer letter" in note

    def test_no_linked_job_omits_job_line(self, db_path, profile, settings):
        agent = _make_agent(_make_gmail(), _make_llm(), profile, settings, db_path)
        result = _make_classification()

        note = agent._forward_note(result, None)

        assert "Linked job" not in note


# ── run_once: full flow ───────────────────────────────────────────────────────

def _usage_dict() -> dict:
    return {
        "model": "claude-sonnet-4-5",
        "purpose": "email_classification",
        "input_tokens": 80,
        "output_tokens": 30,
        "cost_usd": 0.001,
    }


class TestRunOnce:
    """
    run_once() requires an open DB connection (set by _open_db()).
    Each test calls _open_db() before run_once() and _close_db() after.
    """

    @pytest.mark.asyncio
    async def test_processes_no_messages(self, db_path, profile, settings):
        gmail = _make_gmail()
        gmail.list_unread_inbox.return_value = []
        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)
        agent._open_db()
        try:
            result = await agent.run_once()
        finally:
            agent._close_db()

        assert result.success is True
        assert result.emails_processed == 0

    @pytest.mark.asyncio
    async def test_skips_already_processed_message(self, db_path, profile, settings):
        gmail = _make_gmail()
        gmail.list_unread_inbox.return_value = ["msg001"]
        gmail.get_message.return_value = _make_message("msg001")

        # Pre-insert into email_log to simulate already-processed
        from jobhunter.db.models import EmailLog
        from jobhunter.db.repository import EmailRepo

        conn = init_db(db_path)
        email_repo = EmailRepo(conn)
        email_repo.insert(EmailLog(
            gmail_message_id="msg001",
            thread_id="t1",
            from_address="a@b.com",
            to_address="me@me.com",
            subject="Old",
            body_preview="...",
            received_at="Mon, 1 Jan 2024 10:00:00 +0000",
            classification="rejection",
            confidence=0.9,
            linked_job_id=None,
            action_taken="labeled_rejected",
            action_details="company=X",
        ))
        conn.close()

        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)
        agent._open_db()
        try:
            result = await agent.run_once()
        finally:
            agent._close_db()

        assert result.emails_processed == 0
        gmail.get_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejection_updates_job_status(self, db_path, profile, settings):
        job = _insert_job(db_path, "Acme Corp", "Engineer", status="applied")

        gmail = _make_gmail()
        gmail.list_unread_inbox.return_value = ["msg001"]
        gmail.get_message.return_value = _make_message(
            "msg001", from_addr="hr@acme.com", subject="Application update"
        )

        classification_result = ClassificationResult(
            classification="rejection",
            confidence=0.96,
            company_name="Acme Corp",
            reasoning="We regret to inform you",
            should_forward=False,
            new_job_status="rejected",
        )

        with patch(
            "jobhunter.agents.email_agent.classify_email",
            new=AsyncMock(return_value=(classification_result, _usage_dict())),
        ):
            agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)
            agent._open_db()
            try:
                result = await agent.run_once()
            finally:
                agent._close_db()

        assert result.emails_processed == 1

        conn = init_db(db_path)
        job_repo = JobRepo(conn)
        updated_job = job_repo.get_by_id(job.id)
        conn.close()
        assert updated_job.status == "rejected"

    @pytest.mark.asyncio
    async def test_message_marked_read_after_processing(self, db_path, profile, settings):
        gmail = _make_gmail()
        gmail.list_unread_inbox.return_value = ["msg002"]
        gmail.get_message.return_value = _make_message("msg002")

        classification_result = ClassificationResult(
            classification="spam",
            confidence=0.9,
            company_name=None,
            reasoning="spam",
            should_forward=False,
            new_job_status=None,
        )

        with patch(
            "jobhunter.agents.email_agent.classify_email",
            new=AsyncMock(return_value=(classification_result, _usage_dict())),
        ):
            agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)
            agent._open_db()
            try:
                await agent.run_once()
            finally:
                agent._close_db()

        gmail.mark_read.assert_called_with("msg002")

    @pytest.mark.asyncio
    async def test_email_log_stored_after_processing(self, db_path, profile, settings):
        gmail = _make_gmail()
        gmail.list_unread_inbox.return_value = ["msg003"]
        gmail.get_message.return_value = _make_message("msg003", subject="Rejection email")

        classification_result = ClassificationResult(
            classification="rejection",
            confidence=0.95,
            company_name="CorpX",
            reasoning="Rejected",
            should_forward=False,
            new_job_status="rejected",
        )

        with patch(
            "jobhunter.agents.email_agent.classify_email",
            new=AsyncMock(return_value=(classification_result, _usage_dict())),
        ):
            agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)
            agent._open_db()
            try:
                await agent.run_once()
            finally:
                agent._close_db()

        from jobhunter.db.repository import EmailRepo as ER
        conn = init_db(db_path)
        repo = ER(conn)
        assert repo.exists("msg003") is True
        conn.close()

    @pytest.mark.asyncio
    async def test_skips_message_when_get_returns_none(self, db_path, profile, settings):
        gmail = _make_gmail()
        gmail.list_unread_inbox.return_value = ["msg_bad"]
        gmail.get_message.return_value = None

        agent = _make_agent(gmail, _make_llm(), profile, settings, db_path)
        agent._open_db()
        try:
            result = await agent.run_once()
        finally:
            agent._close_db()

        assert result.emails_processed == 0


# ── _ensure_labels ────────────────────────────────────────────────────────────

class TestEnsureLabels:
    def test_populate_label_ids_from_api(self, db_path, profile, settings):
        gmail = _make_gmail()
        gmail.get_or_create_label.side_effect = lambda name: f"ID_{name.replace('/', '_')}"

        agent = EmailAgent(gmail=gmail, llm=_make_llm(), profile=profile, settings=settings, db_path=db_path)
        agent._ensure_labels()

        assert "JobHunter/Rejected" in agent._label_ids
        assert "JobHunter/Processed" in agent._label_ids
        assert "JobHunter/Interview" in agent._label_ids
        assert "JobHunter/Offer" in agent._label_ids

    def test_skips_label_when_api_returns_none(self, db_path, profile, settings):
        gmail = _make_gmail()
        gmail.get_or_create_label.return_value = None

        agent = EmailAgent(gmail=gmail, llm=_make_llm(), profile=profile, settings=settings, db_path=db_path)
        agent._ensure_labels()

        assert len(agent._label_ids) == 0
