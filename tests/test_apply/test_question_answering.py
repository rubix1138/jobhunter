"""Tests for BaseApplicator question answering — profile matching and option selection."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from jobhunter.applicators.base import (
    BaseApplicator, QuestionAnswer, _match_option, _normalize_question, _options_hash,
)
from jobhunter.utils.profile_loader import UserProfile


def make_profile(**overrides) -> UserProfile:
    base = {
        "personal": {
            "first_name": "Jane",
            "last_name": "Doe",
            "email": "jane@jobs.com",
            "personal_email": "jane@home.com",
            "phone": "555-0100",
            "location": "San Francisco, CA",
            "linkedin_url": "https://linkedin.com/in/janedoe",
            "github_url": "https://github.com/janedoe",
            "willing_to_relocate": False,
            "work_authorization": "US Citizen",
        },
        "skills": {
            "programming_languages": [{"name": "Python", "years": 5, "proficiency": "expert"}],
            "frameworks_and_tools": ["FastAPI"],
        },
        "preferences": {"job_titles": ["Senior Engineer"]},
        "application_answers": {
            "years_of_experience": 7,
            "desired_salary": "180000",
            "start_date": "2 weeks",
            "sponsorship_required": False,
            "has_disability": "prefer_not_to_answer",
            "veteran_status": "not_a_veteran",
            "gender": "prefer_not_to_answer",
            "ethnicity": "prefer_not_to_answer",
            "how_did_you_hear": "LinkedIn",
            "willing_to_travel": "up to 10%",
            "custom_answers": {
                "Why do you want to work here": "I admire your engineering culture.",
            },
        },
    }
    base.update(overrides)
    return UserProfile.model_validate(base)


def make_applicator(profile=None, qa_cache=None):
    """Create a concrete BaseApplicator subclass for testing."""
    if profile is None:
        profile = make_profile()

    class ConcreteApplicator(BaseApplicator):
        async def apply(self, job, application):
            return True

    page = MagicMock()
    llm = MagicMock()
    return ConcreteApplicator(page=page, llm=llm, profile=profile, qa_cache=qa_cache)


class TestProfileAnswers:
    def test_years_of_experience(self):
        app = make_applicator()
        result = app._answer_from_profile("Years of experience", None)
        assert result == "7"

    def test_desired_salary(self):
        app = make_applicator()
        result = app._answer_from_profile("What is your desired salary?", None)
        assert result == "180000"

    def test_start_date(self):
        app = make_applicator()
        result = app._answer_from_profile("What is your available start date?", None)
        assert result == "2 weeks"

    def test_sponsorship_no(self):
        app = make_applicator()
        result = app._answer_from_profile("Do you require visa sponsorship?", None)
        assert result == "No"

    def test_first_name(self):
        app = make_applicator()
        result = app._answer_from_profile("First name", None)
        assert result == "Jane"

    def test_last_name(self):
        app = make_applicator()
        result = app._answer_from_profile("Last name", None)
        assert result == "Doe"

    def test_email(self):
        app = make_applicator()
        result = app._answer_from_profile("Email address", None)
        assert result == "jane@jobs.com"

    def test_phone(self):
        app = make_applicator()
        result = app._answer_from_profile("Phone number", None)
        assert result == "555-0100"

    def test_location(self):
        app = make_applicator()
        result = app._answer_from_profile("City / Location", None)
        assert result == "San Francisco, CA"

    def test_linkedin_url(self):
        app = make_applicator()
        result = app._answer_from_profile("LinkedIn profile URL", None)
        assert result == "https://linkedin.com/in/janedoe"

    def test_willing_to_relocate_no(self):
        app = make_applicator()
        result = app._answer_from_profile("Are you willing to relocate?", None)
        assert result == "No"

    def test_work_authorization(self):
        app = make_applicator()
        result = app._answer_from_profile("Are you authorized to work in the US?", None)
        assert result == "Yes"

    def test_unknown_question_returns_none(self):
        app = make_applicator()
        result = app._answer_from_profile("What is your spirit animal?", None)
        assert result is None

    def test_custom_answer_pattern(self):
        app = make_applicator()
        result = app._answer_from_profile("Why do you want to work here at our company?", None)
        assert result == "I admire your engineering culture."

    def test_radio_option_matching(self):
        app = make_applicator()
        options = ["Yes", "No", "Prefer not to answer"]
        result = app._answer_from_profile("Do you require visa sponsorship?", options)
        assert result == "No"

    def test_radio_option_matching_disability(self):
        app = make_applicator()
        options = ["Yes", "No", "I don't wish to answer"]
        result = app._answer_from_profile("Do you have a disability?", options)
        # prefer_not_to_answer should match "I don't wish to answer" (partial)
        assert result is not None


class TestOptionMatching:
    def test_exact_match(self):
        assert _match_option("Yes", ["Yes", "No"]) == "Yes"

    def test_case_insensitive_exact(self):
        assert _match_option("yes", ["Yes", "No"]) == "Yes"

    def test_substring_match(self):
        assert _match_option("prefer not", ["I prefer not to answer", "Yes", "No"]) == "I prefer not to answer"

    def test_no_match_returns_none(self):
        assert _match_option("maybe", ["Yes", "No"]) is None

    def test_exact_before_substring(self):
        result = _match_option("No", ["Not applicable", "No", "Yes"])
        assert result == "No"


class TestQuestionAnswerRecord:
    def test_record_qa_appends(self):
        app = make_applicator()
        qa = QuestionAnswer(answer="7", confidence=1.0, source="profile")
        app.record_qa("Years of experience?", qa)
        assert len(app._qa_log) == 1
        assert app._qa_log[0]["answer"] == "7"

    def test_has_low_confidence(self):
        app = make_applicator()
        app.record_qa("Q1", QuestionAnswer("a", 1.0, "profile"))
        assert app.has_low_confidence_answers() is False
        app.record_qa("Q2", QuestionAnswer("b", 0.3, "claude", needs_review=True))
        assert app.has_low_confidence_answers() is True

    def test_qa_log_json(self):
        import json
        app = make_applicator()
        app.record_qa("Q?", QuestionAnswer("A", 0.9, "claude"))
        data = json.loads(app.qa_log_json())
        assert len(data) == 1
        assert data[0]["question"] == "Q?"
        assert data[0]["answer"] == "A"


class TestLlmPrompts:
    def test_resume_tailor_prompt_contains_job(self):
        from jobhunter.llm.resume import _resume_tailor_prompt
        profile = make_profile()
        prompt = _resume_tailor_prompt(profile, "Staff Engineer", "Acme", "Python expert needed")
        assert "Staff Engineer" in prompt
        assert "Acme" in prompt
        assert "Python expert" in prompt

    def test_cover_letter_prompt_contains_candidate(self):
        from jobhunter.llm.cover_letter import _cover_letter_prompt
        profile = make_profile()
        prompt = _cover_letter_prompt(profile, "Engineer", "Corp", "JD here", "Great engineer")
        assert "Jane Doe" in prompt
        assert "Corp" in prompt

    def test_resume_fallback_uses_profile(self):
        from jobhunter.llm.resume import _fallback_resume_data
        profile = make_profile()
        data = _fallback_resume_data(profile)
        assert data["tailored_summary"] == profile.summary
        assert isinstance(data["experience"], list)
        assert isinstance(data["skills_emphasis"], list)

    def test_slug_generation(self):
        from jobhunter.agents.apply_agent import _slug
        assert _slug("Acme Corp_Senior Engineer") == "acme_corp_senior_engineer"
        assert _slug("  Special!!  Chars  ") == "special_chars"
        assert len(_slug("x" * 100)) <= 50


class TestVisionAnalyzerFormFields:
    """Tests for VisionAnalyzer.analyze_form_fields()."""

    @pytest.mark.asyncio
    async def test_returns_parsed_list_on_success(self):
        from jobhunter.browser.vision import VisionAnalyzer

        mock_llm = MagicMock()
        analyzer = VisionAnalyzer(mock_llm)

        page = MagicMock()
        fields_json = '[{"label": "Phone", "type": "text", "required": true}]'

        with patch.object(
            analyzer, "analyze_page", new=AsyncMock(return_value=fields_json)
        ):
            result = await analyzer.analyze_form_fields(page, context="Test job")

        assert result == [{"label": "Phone", "type": "text", "required": True}]

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_api_failure(self):
        from jobhunter.browser.vision import VisionAnalyzer

        mock_llm = MagicMock()
        analyzer = VisionAnalyzer(mock_llm)

        page = MagicMock()

        with patch.object(
            analyzer,
            "analyze_page",
            new=AsyncMock(side_effect=Exception("API error")),
        ):
            result = await analyzer.analyze_form_fields(page)

        assert result == []


class TestQACacheHelpers:
    def test_normalize_question(self):
        raw = "  How many years of InfoSec experience?  "
        result = _normalize_question(raw)
        assert result == "how many years of infosec experience"
        # Punctuation stripped
        assert "?" not in result

    def test_normalize_question_collapses_whitespace(self):
        result = _normalize_question("Do  you   have   a  CISM?")
        assert result == "do you have a cism"
        assert "  " not in result

    def test_options_hash_order_independent(self):
        opts_a = ["Yes", "No", "Prefer not to answer"]
        opts_b = ["No", "Prefer not to answer", "Yes"]
        assert _options_hash(opts_a) == _options_hash(opts_b)

    def test_options_hash_empty_returns_empty_string(self):
        assert _options_hash(None) == ""
        assert _options_hash([]) == ""

    def test_options_hash_is_8_chars(self):
        h = _options_hash(["Yes", "No"])
        assert len(h) == 8


class TestQACacheIntegration:
    @pytest.mark.asyncio
    async def test_answer_question_cache_hit(self):
        """Cache hit with conf>=0.7 returns source='cache' without calling LLM."""
        from jobhunter.db.models import QACache

        mock_cache = MagicMock()
        mock_cache.get.return_value = QACache(
            question_key="years of infosec experience",
            options_hash="",
            field_type="text",
            answer="8",
            confidence=0.8,
            source="claude",
            times_used=3,
        )
        app = make_applicator(qa_cache=mock_cache)
        result = await app.answer_question("Years of InfoSec experience?", "text")
        assert result.source == "cache"
        assert result.answer == "8"
        assert result.confidence == pytest.approx(0.8)
        # LLM should never have been called
        app._llm.message.assert_not_called()

    @pytest.mark.asyncio
    async def test_answer_question_cache_write_after_claude(self):
        """High-confidence Claude answer triggers a cache upsert."""
        mock_cache = MagicMock()
        mock_cache.get.return_value = None  # cache miss

        app = make_applicator(qa_cache=mock_cache)

        # Use a question that has no profile answer so Claude path is reached
        claude_response = QuestionAnswer(answer="Healthcare", confidence=0.85, source="claude")
        with patch.object(app, "_answer_via_claude", new=AsyncMock(return_value=claude_response)):
            result = await app.answer_question("What industry do you prefer?", "text")

        assert result.answer == "Healthcare"
        mock_cache.upsert.assert_called_once()
        call_arg = mock_cache.upsert.call_args[0][0]
        assert call_arg.answer == "Healthcare"
        assert call_arg.source == "claude"

    @pytest.mark.asyncio
    async def test_answer_question_cache_not_written_low_confidence(self):
        """Low-confidence Claude answer does NOT trigger a cache write."""
        mock_cache = MagicMock()
        mock_cache.get.return_value = None  # cache miss

        app = make_applicator(qa_cache=mock_cache)

        low_conf = QuestionAnswer(answer="maybe", confidence=0.4, source="claude")
        with patch.object(app, "_answer_via_claude", new=AsyncMock(return_value=low_conf)):
            with patch.object(app, "_answer_strategically", new=AsyncMock(return_value=None)):
                await app.answer_question("What is your spirit animal?", "text")

        mock_cache.upsert.assert_not_called()


class TestApplyFailureFormatting:
    def test_includes_reason_type_and_url(self):
        from jobhunter.agents.apply_agent import _format_applicator_failure
        from jobhunter.db.models import Job

        class DummyApplicator:
            failure_reason = "Auth failed — cannot proceed"

        job = Job(
            linkedin_job_id="1",
            title="Director",
            company="Acme",
            job_url="https://linkedin.com/jobs/view/1",
            apply_type="external_workday",
        )
        msg = _format_applicator_failure(
            DummyApplicator(),
            job,
            "https://wd5.myworkdayjobs.com/en-US/foo",
        )
        assert "Auth failed" in msg
        assert "apply_type=external_workday" in msg
        assert "url=https://wd5.myworkdayjobs.com/en-US/foo" in msg

    def test_falls_back_to_generic_reason(self):
        from jobhunter.agents.apply_agent import _format_applicator_failure
        from jobhunter.db.models import Job

        class DummyApplicator:
            pass

        job = Job(
            linkedin_job_id="2",
            title="Director",
            company="Acme",
            job_url="https://linkedin.com/jobs/view/2",
            apply_type="external_other",
        )
        msg = _format_applicator_failure(DummyApplicator(), job, None)
        assert msg.startswith("Applicator returned False")
        assert "apply_type=external_other" in msg


class TestFailureReasonHelpers:
    def test_failure_reason_prefix_extracts_before_metadata(self):
        from jobhunter.agents.apply_agent import _failure_reason_prefix
        msg = "Auth failed — cannot proceed | apply_type=external_other | url=https://x"
        assert _failure_reason_prefix(msg) == "Auth failed — cannot proceed"

    def test_failure_reason_prefix_handles_empty(self):
        from jobhunter.agents.apply_agent import _failure_reason_prefix
        assert _failure_reason_prefix(None) == ""

    def test_manual_review_failure_detection(self):
        from jobhunter.agents.apply_agent import _is_manual_review_failure
        assert _is_manual_review_failure(
            "CAPTCHA detected — needs_review | apply_type=external_lever"
        ) is True
        assert _is_manual_review_failure(
            "Auth failed — cannot proceed | apply_type=external_other"
        ) is False
