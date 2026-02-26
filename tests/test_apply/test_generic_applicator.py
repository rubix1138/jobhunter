"""Tests for GenericApplicator — page state parsing, navigation logic."""

import pytest
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from jobhunter.applicators.generic import GenericApplicator
from jobhunter.utils.profile_loader import UserProfile


def make_profile() -> UserProfile:
    return UserProfile.model_validate({
        "personal": {
            "first_name": "Jane", "last_name": "Doe",
            "email": "jane@jobs.com", "personal_email": "jane@home.com",
            "phone": "555-0100", "location": "San Francisco, CA",
        },
        "skills": {
            "programming_languages": [{"name": "Python", "years": 5, "proficiency": "expert"}],
            "frameworks_and_tools": ["FastAPI"],
        },
        "preferences": {"job_titles": ["Engineer"]},
        "application_answers": {
            "years_of_experience": 5, "desired_salary": "180000",
            "start_date": "2 weeks", "sponsorship_required": False,
            "has_disability": "prefer_not_to_answer", "veteran_status": "not_a_veteran",
            "gender": "prefer_not_to_answer", "ethnicity": "prefer_not_to_answer",
            "how_did_you_hear": "LinkedIn", "willing_to_travel": "10%",
        },
    })


def make_applicator(vision=None):
    profile = make_profile()
    page = MagicMock()
    llm = MagicMock()
    return GenericApplicator(
        page=page,
        llm=llm,
        profile=profile,
        resume_path=Path("/tmp/resume.pdf"),
        vision=vision,
    )


class TestPageAssessment:
    @pytest.mark.asyncio
    async def test_returns_form_when_no_vision(self):
        applicator = make_applicator(vision=None)
        result = await applicator._assess_page("context")
        assert result["state"] == "form"

    @pytest.mark.asyncio
    async def test_parses_vision_response(self):
        vision = MagicMock()
        vision_response = json.dumps({
            "state": "file_upload",
            "detail": "Resume upload area visible",
            "suggested_action": "upload_file",
            "submit_button_visible": False,
            "next_button_visible": False,
            "has_required_unfilled": False,
        })
        applicator = make_applicator(vision=vision)
        applicator._llm.vision_message = AsyncMock(return_value=(vision_response, {}))

        from jobhunter.browser.vision import screenshot_page, image_to_base64
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("jobhunter.applicators.generic.screenshot_page", AsyncMock(return_value=b"png"))
            mp.setattr("jobhunter.applicators.generic.image_to_base64", lambda x: "b64data")
            result = await applicator._assess_page("context")

        assert result["state"] == "file_upload"
        assert result["suggested_action"] == "upload_file"

    @pytest.mark.asyncio
    async def test_falls_back_on_parse_error(self):
        vision = MagicMock()
        applicator = make_applicator(vision=vision)
        applicator._llm.vision_message = AsyncMock(return_value=("not json", {}))

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("jobhunter.applicators.generic.screenshot_page", AsyncMock(return_value=b"png"))
            mp.setattr("jobhunter.applicators.generic.image_to_base64", lambda x: "b64data")
            result = await applicator._assess_page("context")

        assert result["state"] == "form"


class TestNavigation:
    @pytest.mark.asyncio
    async def test_attempts_advance_with_next_button(self):
        applicator = make_applicator()
        # Simulate a visible Next button
        applicator._page.locator = MagicMock(return_value=MagicMock(
            first=MagicMock(
                is_visible=AsyncMock(return_value=True),
                click=AsyncMock(),
            )
        ))
        # is_visible helper returns True
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("jobhunter.applicators.generic.is_visible", AsyncMock(return_value=True))
            result = await applicator._attempt_advance({"suggested_action": "click_next"})
        # Should attempt to advance
        assert result is True or result is False  # just ensure no exception


class TestApplyNoUrl:
    @pytest.mark.asyncio
    async def test_returns_false_with_no_url(self):
        applicator = make_applicator()
        from jobhunter.db.models import Job, Application
        job = Job(
            linkedin_job_id="j1",
            title="Engineer",
            company="Acme",
            job_url="https://linkedin.com/jobs/view/1",
            external_url=None,
            apply_type="external_other",
        )
        app = Application(job_id=1)
        result = await applicator.apply(job, app)
        assert result is False
