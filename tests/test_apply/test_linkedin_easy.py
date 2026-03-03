"""Targeted tests for LinkedIn Easy Apply validation fallback behavior."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from jobhunter.applicators.linkedin_easy import LinkedInEasyApplicator


@pytest.fixture
def applicator():
    return LinkedInEasyApplicator(
        page=MagicMock(),
        llm=MagicMock(),
        profile=MagicMock(
            email="jane@example.com",
            phone="5551234567",
            linkedin_url="https://linkedin.com/in/jane",
        ),
        resume_path=Path("/tmp/resume.pdf"),
    )


class TestPreferredOption:
    def test_referral_prefers_linkedin_source(self, applicator):
        options = ["Employee Referral", "LinkedIn", "Company Website"]
        picked = applicator._preferred_option(options, "How did you hear about this position?")
        assert picked == "LinkedIn"

    def test_work_authorization_prefers_yes(self, applicator):
        options = ["No", "Yes", "Prefer not to say"]
        picked = applicator._preferred_option(options, "Are you legally authorized to work in the US?")
        assert picked == "Yes"

    def test_demographic_prefers_prefer_not(self, applicator):
        options = ["Male", "Female", "Prefer not to answer"]
        picked = applicator._preferred_option(options, "Gender")
        assert picked == "Prefer not to answer"


class TestSafeTextFallback:
    @pytest.mark.asyncio
    async def test_numeric_question_uses_one(self, applicator):
        input_el = MagicMock()
        input_el.get_attribute = AsyncMock(return_value="number")
        picked = await applicator._safe_text_fallback("Years of experience", input_el)
        assert picked == "1"

    @pytest.mark.asyncio
    async def test_email_question_uses_profile_email(self, applicator):
        input_el = MagicMock()
        input_el.get_attribute = AsyncMock(return_value="text")
        picked = await applicator._safe_text_fallback("Email address", input_el)
        assert picked == "jane@example.com"
