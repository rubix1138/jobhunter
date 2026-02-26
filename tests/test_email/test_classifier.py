"""Tests for gmail/classifier.py — email classification via Claude."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from jobhunter.gmail.classifier import (
    classify_email,
    ClassificationResult,
    CLASSIFICATIONS,
    FORWARD_CLASSIFICATIONS,
    STATUS_MAP,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_llm(response_text: str) -> MagicMock:
    """Build a mock ClaudeClient that returns a given text."""
    llm = MagicMock()
    usage = {
        "model": "claude-sonnet-4-5",
        "purpose": "email_classification",
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_usd": 0.001,
    }
    llm.message = AsyncMock(return_value=(response_text, usage))
    llm.sonnet_model = "claude-sonnet-4-5"
    return llm


def _valid_response(
    classification: str = "rejection",
    confidence: float = 0.95,
    company_name: str = "Acme Corp",
    reasoning: str = "Standard rejection wording",
) -> str:
    return json.dumps(
        {
            "classification": classification,
            "confidence": confidence,
            "company_name": company_name,
            "reasoning": reasoning,
        }
    )


# ── CLASSIFICATIONS constant ──────────────────────────────────────────────────

class TestConstants:
    def test_all_expected_classifications_present(self):
        expected = {
            "interview_invite",
            "rejection",
            "follow_up",
            "assessment",
            "offer",
            "recruiter_outreach",
            "spam",
            "unknown",
        }
        assert expected == CLASSIFICATIONS

    def test_forward_classifications_subset_of_classifications(self):
        assert FORWARD_CLASSIFICATIONS.issubset(CLASSIFICATIONS)

    def test_rejection_not_in_forward(self):
        assert "rejection" not in FORWARD_CLASSIFICATIONS

    def test_spam_not_in_forward(self):
        assert "spam" not in FORWARD_CLASSIFICATIONS

    def test_recruiter_outreach_not_in_forward(self):
        assert "recruiter_outreach" not in FORWARD_CLASSIFICATIONS

    def test_status_map_values_are_valid_statuses(self):
        valid_statuses = {"applied", "interviewing", "offer", "rejected", "qualified"}
        for status in STATUS_MAP.values():
            assert status in valid_statuses

    def test_rejection_maps_to_rejected(self):
        assert STATUS_MAP["rejection"] == "rejected"

    def test_offer_maps_to_offer(self):
        assert STATUS_MAP["offer"] == "offer"

    def test_interview_invite_maps_to_interviewing(self):
        assert STATUS_MAP["interview_invite"] == "interviewing"


# ── classify_email happy paths ────────────────────────────────────────────────

class TestClassifyEmailHappyPath:
    @pytest.mark.asyncio
    async def test_rejection_classification(self):
        llm = _make_llm(_valid_response("rejection", 0.95, "Acme Corp"))
        result, usage = await classify_email(llm, "hr@acme.com", "Update on your application", "We regret...")
        assert result.classification == "rejection"
        assert result.confidence == pytest.approx(0.95)
        assert result.company_name == "Acme Corp"
        assert result.should_forward is False
        assert result.new_job_status == "rejected"

    @pytest.mark.asyncio
    async def test_interview_invite_classification(self):
        llm = _make_llm(_valid_response("interview_invite", 0.98, "TechCo"))
        result, usage = await classify_email(llm, "hr@techco.com", "Interview invitation", "We'd love to meet...")
        assert result.classification == "interview_invite"
        assert result.should_forward is True
        assert result.new_job_status == "interviewing"

    @pytest.mark.asyncio
    async def test_offer_classification(self):
        llm = _make_llm(_valid_response("offer", 0.99, "StartupXYZ"))
        result, usage = await classify_email(llm, "hr@startup.com", "Offer letter", "We are pleased to offer...")
        assert result.classification == "offer"
        assert result.should_forward is True
        assert result.new_job_status == "offer"

    @pytest.mark.asyncio
    async def test_spam_classification(self):
        llm = _make_llm(_valid_response("spam", 0.80, None))
        result, usage = await classify_email(llm, "noreply@spam.com", "Win a prize!", "Click here...")
        assert result.classification == "spam"
        assert result.should_forward is False
        assert result.new_job_status is None

    @pytest.mark.asyncio
    async def test_recruiter_outreach_classification(self):
        llm = _make_llm(_valid_response("recruiter_outreach", 0.85, "BigCorp"))
        result, usage = await classify_email(llm, "recruiter@bigcorp.com", "Exciting opportunity", "I came across...")
        assert result.classification == "recruiter_outreach"
        assert result.should_forward is False

    @pytest.mark.asyncio
    async def test_assessment_classification(self):
        llm = _make_llm(_valid_response("assessment", 0.90, "DevCo"))
        result, usage = await classify_email(llm, "hr@devco.com", "Technical assessment", "Please complete...")
        assert result.classification == "assessment"
        assert result.should_forward is True
        assert result.new_job_status == "interviewing"

    @pytest.mark.asyncio
    async def test_follow_up_classification(self):
        llm = _make_llm(_valid_response("follow_up", 0.75, "CompanyA"))
        result, usage = await classify_email(llm, "hr@a.com", "Following up", "Just checking in...")
        assert result.classification == "follow_up"
        assert result.should_forward is True

    @pytest.mark.asyncio
    async def test_unknown_classification(self):
        llm = _make_llm(_valid_response("unknown", 0.50, None))
        result, usage = await classify_email(llm, "noreply@x.com", "Misc", "Something else")
        assert result.classification == "unknown"
        assert result.should_forward is True
        assert result.new_job_status is None

    @pytest.mark.asyncio
    async def test_company_name_none_when_null(self):
        response = json.dumps({
            "classification": "spam",
            "confidence": 0.8,
            "company_name": None,
            "reasoning": "No company",
        })
        llm = _make_llm(response)
        result, _ = await classify_email(llm, "x@x.com", "Spam", "")
        assert result.company_name is None

    @pytest.mark.asyncio
    async def test_usage_info_returned(self):
        llm = _make_llm(_valid_response())
        _, usage = await classify_email(llm, "a@b.com", "Sub", "Body")
        assert "input_tokens" in usage
        assert "output_tokens" in usage

    @pytest.mark.asyncio
    async def test_reasoning_captured(self):
        llm = _make_llm(_valid_response(reasoning="Clearly a rejection due to 'not moving forward'"))
        result, _ = await classify_email(llm, "a@b.com", "Sub", "Body")
        assert "not moving forward" in result.reasoning


# ── classify_email fallback / error paths ────────────────────────────────────

class TestClassifyEmailFallback:
    @pytest.mark.asyncio
    async def test_falls_back_on_invalid_json(self):
        llm = _make_llm("This is not JSON at all!")
        result, _ = await classify_email(llm, "a@b.com", "Sub", "Body")
        assert result.classification == "unknown"
        assert result.confidence == 0.0
        assert result.should_forward is True  # safe default: forward unknown
        assert result.reasoning == "Parse error"

    @pytest.mark.asyncio
    async def test_falls_back_on_unrecognised_classification(self):
        response = json.dumps({
            "classification": "totally_made_up_label",
            "confidence": 0.9,
            "company_name": "Corp",
            "reasoning": "Weird",
        })
        llm = _make_llm(response)
        result, _ = await classify_email(llm, "a@b.com", "Sub", "Body")
        assert result.classification == "unknown"

    @pytest.mark.asyncio
    async def test_falls_back_on_missing_classification_key(self):
        response = json.dumps({"confidence": 0.8})
        llm = _make_llm(response)
        result, _ = await classify_email(llm, "a@b.com", "Sub", "Body")
        # missing key → defaults to "unknown" via .get("classification", "unknown")
        assert result.classification == "unknown"

    @pytest.mark.asyncio
    async def test_confidence_defaults_to_0_5_when_missing(self):
        response = json.dumps({
            "classification": "rejection",
            "company_name": "Corp",
            "reasoning": "No confidence key",
        })
        llm = _make_llm(response)
        result, _ = await classify_email(llm, "a@b.com", "Sub", "Body")
        assert result.confidence == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_parse_error_new_job_status_is_none(self):
        llm = _make_llm("{bad json")
        result, _ = await classify_email(llm, "a@b.com", "Sub", "Body")
        assert result.new_job_status is None
