"""Tests for FormFillingAgent — page state assessment, navigation logic."""

import pytest
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from jobhunter.applicators.form_filling import FormFillingAgent
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
    return FormFillingAgent(
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
        applicator._page.content = AsyncMock(return_value="<html><body>Form here</body></html>")
        result = await applicator._assess_current_state("context")
        assert result == "form"

    @pytest.mark.asyncio
    async def test_detects_submitted_from_text(self):
        applicator = make_applicator(vision=None)
        applicator._page.content = AsyncMock(
            return_value="<html><body>Thank you for applying!</body></html>"
        )
        result = await applicator._assess_current_state("context")
        assert result == "submitted"

    @pytest.mark.asyncio
    async def test_detects_captcha_from_text(self):
        applicator = make_applicator(vision=None)
        applicator._page.content = AsyncMock(
            return_value="<html><body>Please complete the captcha</body></html>"
        )
        result = await applicator._assess_current_state("context")
        assert result == "captcha"


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


class TestFillCurrentPage:
    @pytest.mark.asyncio
    async def test_fill_current_page_executes_plan_and_radiogroup_scan(self):
        applicator = make_applicator()
        applicator._scan_radiogroups = AsyncMock(return_value=2)
        applicator._plan_section = AsyncMock(
            return_value=[
                {"label": "Full Name", "field_type": "text", "value": "Jane Doe"},
                {"label": "", "field_type": "text", "value": "ignored"},
                {"label": "Email", "field_type": "text", "value": ""},
            ]
        )
        applicator._fill_field = AsyncMock(return_value=1)

        with patch(
            "jobhunter.applicators.form_filling.get_ax_tree",
            new=AsyncMock(return_value={"role": "document"}),
        ), patch(
            "jobhunter.applicators.form_filling.format_interactive_fields",
            return_value="textbox: Full Name",
        ):
            result = await applicator._fill_current_page("ctx")

        assert result == 3
        applicator._plan_section.assert_awaited_once_with("textbox: Full Name", "ctx")
        applicator._fill_field.assert_awaited_once_with("Full Name", "text", "Jane Doe")
        applicator._scan_radiogroups.assert_awaited_once_with("ctx")

    @pytest.mark.asyncio
    async def test_fill_current_page_uses_vision_when_ax_is_empty(self):
        vision = MagicMock()
        vision.analyze_page = AsyncMock(return_value="radiogroup: Work auth | options: Yes, No")
        applicator = make_applicator(vision=vision)
        applicator._plan_section = AsyncMock(return_value=[])
        applicator._scan_radiogroups = AsyncMock(return_value=1)

        with patch(
            "jobhunter.applicators.form_filling.get_ax_tree",
            new=AsyncMock(return_value=None),
        ):
            result = await applicator._fill_current_page("ctx")

        assert result == 1
        vision.analyze_page.assert_awaited_once()
        applicator._plan_section.assert_awaited_once()
        applicator._scan_radiogroups.assert_awaited_once_with("ctx")


class TestFillField:
    @pytest.mark.asyncio
    async def test_fill_field_text_uses_aria_locator(self):
        applicator = make_applicator()
        locator = MagicMock()
        locator.fill = AsyncMock()

        with patch(
            "jobhunter.applicators.form_filling.find_by_aria_label",
            new=AsyncMock(return_value=locator),
        ), patch("jobhunter.applicators.form_filling.micro_delay", new=AsyncMock()):
            result = await applicator._fill_field("Full Name [required]*", "text", "Jane Doe")

        assert result == 1
        locator.fill.assert_awaited_once_with("Jane Doe")

    @pytest.mark.asyncio
    async def test_fill_field_select_dispatches_to_select_helper(self):
        applicator = make_applicator()
        locator = MagicMock()
        locator.wait_for = AsyncMock()
        applicator._page.get_by_label.return_value = locator
        applicator._fill_select_field = AsyncMock(return_value=1)

        with patch(
            "jobhunter.applicators.form_filling.find_by_aria_label",
            new=AsyncMock(return_value=None),
        ):
            result = await applicator._fill_field("Country", "select", "United States")

        assert result == 1
        applicator._fill_select_field.assert_awaited_once_with(
            locator, "Country", "Country", "select", "United States"
        )

    @pytest.mark.asyncio
    async def test_fill_field_checkbox_checks_for_truthy_value(self):
        applicator = make_applicator()
        locator = MagicMock()
        locator.check = AsyncMock()

        with patch(
            "jobhunter.applicators.form_filling.find_by_aria_label",
            new=AsyncMock(return_value=locator),
        ), patch("jobhunter.applicators.form_filling.micro_delay", new=AsyncMock()):
            result = await applicator._fill_field("I agree", "checkbox", "yes")

        assert result == 1
        locator.check.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fill_field_returns_zero_when_text_field_not_found(self):
        applicator = make_applicator()
        applicator._page.get_by_label.side_effect = Exception("not found")

        with patch(
            "jobhunter.applicators.form_filling.find_by_aria_label",
            new=AsyncMock(return_value=None),
        ):
            result = await applicator._fill_field("Missing", "text", "x")

        assert result == 0


class TestAdvanceOrSubmit:
    @pytest.mark.asyncio
    async def test_advance_or_submit_clicks_submit_when_review_allows(self):
        applicator = make_applicator()
        applicator._pause_for_review = AsyncMock(return_value=True)
        submit_btn = MagicMock()
        submit_btn.is_visible = AsyncMock(return_value=True)
        submit_btn.click = AsyncMock()
        applicator._page.get_by_role.return_value.first = submit_btn

        with patch("jobhunter.applicators.form_filling._SUBMIT_LABELS", ("Submit",)), patch(
            "jobhunter.applicators.form_filling._ADVANCE_LABELS", ("Next",)
        ), patch("jobhunter.applicators.form_filling.random_delay", new=AsyncMock()):
            result = await applicator._advance_or_submit("ctx")

        assert result == "submitted"
        submit_btn.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_advance_or_submit_uses_advance_button_when_no_submit(self):
        applicator = make_applicator()
        hidden_submit = MagicMock()
        hidden_submit.is_visible = AsyncMock(return_value=False)
        advance_btn = MagicMock()
        advance_btn.is_visible = AsyncMock(return_value=True)
        advance_btn.click = AsyncMock()
        applicator._page.get_by_role.side_effect = [
            MagicMock(first=hidden_submit),  # submit button lookup
            MagicMock(first=advance_btn),  # advance button lookup
        ]

        with patch("jobhunter.applicators.form_filling._SUBMIT_LABELS", ("Submit",)), patch(
            "jobhunter.applicators.form_filling._ADVANCE_LABELS", ("Next",)
        ), patch("jobhunter.applicators.form_filling.random_delay", new=AsyncMock()):
            result = await applicator._advance_or_submit("ctx")

        assert result == "advanced"
        advance_btn.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_advance_or_submit_uses_advance_link_when_button_missing(self):
        applicator = make_applicator()
        hidden_submit = MagicMock()
        hidden_submit.is_visible = AsyncMock(return_value=False)
        hidden_advance_btn = MagicMock()
        hidden_advance_btn.is_visible = AsyncMock(return_value=False)
        advance_link = MagicMock()
        advance_link.is_visible = AsyncMock(return_value=True)
        advance_link.click = AsyncMock()

        def get_by_role_side_effect(role, **_kwargs):
            if role == "button":
                if not hasattr(get_by_role_side_effect, "button_calls"):
                    get_by_role_side_effect.button_calls = 0
                get_by_role_side_effect.button_calls += 1
                if get_by_role_side_effect.button_calls == 1:
                    return MagicMock(first=hidden_submit)
                return MagicMock(first=hidden_advance_btn)
            return MagicMock(first=advance_link)

        applicator._page.get_by_role.side_effect = get_by_role_side_effect

        with patch("jobhunter.applicators.form_filling._SUBMIT_LABELS", ("Submit",)), patch(
            "jobhunter.applicators.form_filling._ADVANCE_LABELS", ("Next",)
        ), patch("jobhunter.applicators.form_filling.random_delay", new=AsyncMock()):
            result = await applicator._advance_or_submit("ctx")

        assert result == "advanced"
        advance_link.click.assert_awaited_once()


class TestDetectPageChange:
    @pytest.mark.asyncio
    async def test_detect_page_change_returns_true_when_heading_changes(self):
        applicator = make_applicator()
        applicator._page.url = "https://example.com/form"
        applicator._get_page_heading = AsyncMock(return_value="Step 2")

        changed, heading = await applicator._detect_page_change(
            prev_url="https://example.com/form", prev_heading="Step 1", timeout=0.5
        )

        assert changed is True
        assert heading == "Step 2"

    @pytest.mark.asyncio
    async def test_detect_page_change_times_out_when_no_change(self):
        applicator = make_applicator()
        applicator._page.url = "https://example.com/form"
        applicator._get_page_heading = AsyncMock(return_value="Step 1")

        changed, heading = await applicator._detect_page_change(
            prev_url="https://example.com/form", prev_heading="Step 1", timeout=0
        )

        assert changed is False
        assert heading == "Step 1"


class TestFillSelectField:
    @pytest.mark.asyncio
    async def test_fill_select_field_native_select(self):
        applicator = make_applicator()
        locator = MagicMock()
        locator.evaluate = AsyncMock(return_value="select")
        locator.select_option = AsyncMock()

        with patch("jobhunter.applicators.form_filling.micro_delay", new=AsyncMock()):
            result = await applicator._fill_select_field(
                locator, "Country", "Country", "select", "United States"
            )

        assert result == 1
        locator.select_option.assert_awaited_once_with(label="United States")

    @pytest.mark.asyncio
    async def test_fill_select_field_click_option_fallback(self):
        applicator = make_applicator()
        locator = MagicMock()
        locator.evaluate = AsyncMock(return_value="div")
        locator.click = AsyncMock()
        locator.element_handle = AsyncMock(return_value=None)

        option = MagicMock()
        option.count = AsyncMock(return_value=1)
        option.first.click = AsyncMock()
        applicator._page.get_by_role.return_value = option

        with patch("jobhunter.applicators.form_filling.micro_delay", new=AsyncMock()):
            result = await applicator._fill_select_field(
                locator, "Country", "Country", "select", "United States"
            )

        assert result == 1
        locator.click.assert_awaited_once()
        option.first.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fill_select_field_uses_select_option_helper_fallback(self):
        applicator = make_applicator()
        locator = MagicMock()
        locator.evaluate = AsyncMock(return_value="div")
        locator.click = AsyncMock(side_effect=Exception("not clickable"))
        locator.element_handle = AsyncMock(return_value=None)

        empty_text_matches = MagicMock()
        empty_text_matches.count = AsyncMock(return_value=0)
        applicator._page.get_by_text.return_value = empty_text_matches

        with patch(
            "jobhunter.applicators.form_filling.select_option",
            new=AsyncMock(return_value=True),
        ), patch("jobhunter.applicators.form_filling.micro_delay", new=AsyncMock()):
            result = await applicator._fill_select_field(
                locator, "Country", "Country", "select", "United States"
            )

        assert result == 1

    @pytest.mark.asyncio
    async def test_fill_select_field_combobox_uses_typeahead(self):
        applicator = make_applicator()
        locator = MagicMock()
        locator.evaluate = AsyncMock(return_value="div")
        locator.click = AsyncMock(side_effect=Exception("not clickable"))
        element = MagicMock()
        locator.element_handle = AsyncMock(return_value=element)
        applicator._fill_typeahead = AsyncMock(return_value=True)

        with patch(
            "jobhunter.applicators.form_filling.select_option",
            new=AsyncMock(return_value=False),
        ):
            result = await applicator._fill_select_field(
                locator, "City", "City", "combobox", "San Francisco"
            )

        assert result == 1
        applicator._fill_typeahead.assert_awaited()

    @pytest.mark.asyncio
    async def test_fill_select_field_text_proximity_select_ancestor(self):
        applicator = make_applicator()
        locator = MagicMock()
        locator.evaluate = AsyncMock(return_value="div")
        locator.click = AsyncMock(side_effect=Exception("no click"))
        locator.element_handle = AsyncMock(return_value=None)

        q_els = MagicMock()
        q_els.count = AsyncMock(return_value=1)
        q_el = MagicMock()
        q_els.nth.return_value = q_el

        anc = MagicMock()
        anc.count = AsyncMock(return_value=1)
        sel = MagicMock()
        sel.select_option = AsyncMock()
        anc.first.locator.return_value.first = sel
        q_el.locator.return_value = anc
        applicator._page.get_by_text.return_value = q_els

        with patch(
            "jobhunter.applicators.form_filling.select_option",
            new=AsyncMock(return_value=False),
        ), patch("jobhunter.applicators.form_filling.micro_delay", new=AsyncMock()):
            result = await applicator._fill_select_field(
                locator, "Country", "Country", "select", "United States"
            )

        assert result == 1
        sel.select_option.assert_awaited_once_with(label="United States")


class TestFillRadioField:
    @pytest.mark.asyncio
    async def test_fill_radio_field_approach1_named_radiogroup(self):
        applicator = make_applicator()
        group = MagicMock()
        group.wait_for = AsyncMock()
        radio = MagicMock()
        radio.click = AsyncMock()
        group.get_by_role.return_value.first = radio
        applicator._page.get_by_role.return_value.first = group

        with patch("jobhunter.applicators.form_filling.micro_delay", new=AsyncMock()):
            result = await applicator._fill_radio_field(
                locator=None, label="Work authorization", label_norm="Work authorization", value="Yes"
            )

        assert result == 1
        radio.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fill_radio_field_approach2_group_filter(self):
        applicator = make_applicator()
        applicator._page.get_by_role.side_effect = Exception("approach1 fails")

        container = MagicMock()
        container.count = AsyncMock(return_value=1)
        opt = MagicMock()
        opt.count = AsyncMock(return_value=1)
        opt.first.click = AsyncMock()
        container.first.get_by_role.return_value = opt
        applicator._page.locator.return_value.filter.return_value = container

        with patch("jobhunter.applicators.form_filling.micro_delay", new=AsyncMock()):
            result = await applicator._fill_radio_field(
                locator=None, label="Are you authorized?", label_norm="Are you authorized?", value="Yes"
            )

        assert result == 1
        opt.first.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fill_radio_field_approach3_xpath_ancestor(self):
        applicator = make_applicator()
        applicator._page.get_by_role.side_effect = Exception("approach1 fails")
        applicator._page.locator.return_value.filter.side_effect = Exception("approach2 fails")

        text_loc = MagicMock()
        rg = MagicMock()
        rg.count = AsyncMock(return_value=1)
        opt = MagicMock()
        opt.first.click = AsyncMock()
        rg.get_by_role.return_value = opt
        text_loc.locator.return_value = rg
        applicator._page.get_by_text.return_value.first = text_loc

        with patch("jobhunter.applicators.form_filling.micro_delay", new=AsyncMock()):
            result = await applicator._fill_radio_field(
                locator=None, label="Veteran status", label_norm="Veteran status", value="No"
            )

        assert result == 1
        opt.first.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fill_radio_field_approach4_broad_value_search(self):
        applicator = make_applicator()

        def get_by_role_side_effect(role, **kwargs):
            if role == "radiogroup":
                raise Exception("approach1 fails")
            if role == "radio":
                opt = MagicMock()
                opt.first.click = AsyncMock()
                return opt
            raise Exception("unexpected role")

        applicator._page.get_by_role.side_effect = get_by_role_side_effect
        applicator._page.locator.return_value.filter.side_effect = Exception("approach2 fails")
        applicator._page.get_by_text.return_value.first.locator.side_effect = Exception(
            "approach3 fails"
        )

        with patch("jobhunter.applicators.form_filling.micro_delay", new=AsyncMock()):
            result = await applicator._fill_radio_field(
                locator=None, label="Gender", label_norm="Gender", value="Prefer not to answer"
            )

        assert result == 1


class TestApplyMainLoop:
    def _make_job_and_app(self, idx: str):
        from jobhunter.db.models import Application, Job

        job = Job(
            linkedin_job_id=f"j{idx}",
            title="Engineer",
            company="Acme",
            job_url=f"https://linkedin.com/jobs/view/{idx}",
            external_url="https://example.com/apply",
            apply_type="external_other",
        )
        app = Application(job_id=1)
        return job, app

    @pytest.mark.asyncio
    async def test_apply_returns_true_after_submit_click_even_without_confirmation(self):
        applicator = make_applicator()
        job, app = self._make_job_and_app("2")

        applicator._page.goto = AsyncMock()
        applicator._page.url = "https://example.com/apply"
        applicator._looks_like_auth_page = AsyncMock(return_value=False)
        applicator._ensure_on_application_form = AsyncMock(return_value=True)
        applicator._get_page_heading = AsyncMock(return_value="Step 1")
        applicator._confirm_submission = AsyncMock(side_effect=[False, False])
        applicator._is_email_verification_wall = AsyncMock(return_value=False)
        applicator._assess_current_state = AsyncMock(return_value="form")
        applicator._dismiss_modal = AsyncMock()
        applicator._upload_resume_if_needed = AsyncMock()
        applicator._fill_current_page = AsyncMock(return_value=2)
        applicator._advance_or_submit = AsyncMock(return_value="submitted")

        with patch(
            "jobhunter.applicators.form_filling.wait_for_navigation_settle",
            new=AsyncMock(),
        ), patch("jobhunter.applicators.form_filling.random_delay", new=AsyncMock()):
            result = await applicator.apply(job, app)

        assert result is True
        applicator._advance_or_submit.assert_awaited()

    @pytest.mark.asyncio
    async def test_apply_marks_closed_listing_as_expired(self):
        applicator = make_applicator()
        job, app = self._make_job_and_app("closed")

        applicator._page.goto = AsyncMock()
        applicator._is_expired_listing = AsyncMock(return_value=True)

        with patch("jobhunter.applicators.form_filling.random_delay", new=AsyncMock()):
            result = await applicator.apply(job, app)

        assert result is False
        assert applicator.detected_expired is True
        assert applicator.failure_reason == "Job listing closed: no longer available"

    @pytest.mark.asyncio
    async def test_apply_returns_false_on_captcha_state(self):
        applicator = make_applicator()
        job, app = self._make_job_and_app("3")

        applicator._page.goto = AsyncMock()
        applicator._page.url = "https://example.com/apply"
        applicator._looks_like_auth_page = AsyncMock(return_value=False)
        applicator._ensure_on_application_form = AsyncMock(return_value=True)
        applicator._get_page_heading = AsyncMock(return_value="Step 1")
        applicator._confirm_submission = AsyncMock(return_value=False)
        applicator._is_email_verification_wall = AsyncMock(return_value=False)
        applicator._assess_current_state = AsyncMock(return_value="captcha")

        with patch(
            "jobhunter.applicators.form_filling.wait_for_navigation_settle",
            new=AsyncMock(),
        ), patch("jobhunter.applicators.form_filling.random_delay", new=AsyncMock()):
            result = await applicator.apply(job, app)

        assert result is False

    @pytest.mark.asyncio
    async def test_apply_returns_false_after_three_stuck_pages(self):
        applicator = make_applicator()
        job, app = self._make_job_and_app("4")

        applicator._page.goto = AsyncMock()
        applicator._page.url = "https://example.com/apply"
        applicator._looks_like_auth_page = AsyncMock(return_value=False)
        applicator._ensure_on_application_form = AsyncMock(return_value=True)
        applicator._get_page_heading = AsyncMock(return_value="Step 1")
        applicator._confirm_submission = AsyncMock(return_value=False)
        applicator._is_email_verification_wall = AsyncMock(return_value=False)
        applicator._assess_current_state = AsyncMock(return_value="form")
        applicator._dismiss_modal = AsyncMock()
        applicator._upload_resume_if_needed = AsyncMock()
        applicator._fill_current_page = AsyncMock(return_value=1)
        applicator._advance_or_submit = AsyncMock(return_value=None)
        applicator._detect_page_change = AsyncMock(return_value=(False, "Step 1"))

        with patch(
            "jobhunter.applicators.form_filling.wait_for_navigation_settle",
            new=AsyncMock(),
        ), patch(
            "jobhunter.applicators.form_filling.scroll_to_bottom",
            new=AsyncMock(),
        ), patch("jobhunter.applicators.form_filling.random_delay", new=AsyncMock()):
            result = await applicator.apply(job, app)

        assert result is False

    @pytest.mark.asyncio
    async def test_apply_rechecks_expired_when_preflight_cannot_enter_form(self):
        applicator = make_applicator()
        job, app = self._make_job_and_app("closed-after-preflight")

        applicator._page.goto = AsyncMock()
        applicator._looks_like_auth_page = AsyncMock(return_value=False)
        applicator._is_expired_listing = AsyncMock(side_effect=[False, True])
        applicator._ensure_on_application_form = AsyncMock(return_value=False)

        with patch("jobhunter.applicators.form_filling.random_delay", new=AsyncMock()):
            result = await applicator.apply(job, app)

        assert result is False
        assert applicator.detected_expired is True
        assert applicator.failure_reason == "Job listing closed: no longer available"

    @pytest.mark.asyncio
    async def test_apply_returns_false_on_email_verification_wall(self):
        applicator = make_applicator()
        job, app = self._make_job_and_app("5")

        applicator._page.goto = AsyncMock()
        applicator._page.url = "https://example.com/apply"
        applicator._looks_like_auth_page = AsyncMock(return_value=False)
        applicator._ensure_on_application_form = AsyncMock(return_value=True)
        applicator._get_page_heading = AsyncMock(return_value="Step 1")
        applicator._confirm_submission = AsyncMock(return_value=False)
        applicator._is_email_verification_wall = AsyncMock(return_value=True)
        applicator._assess_current_state = AsyncMock(return_value="form")
        applicator._dismiss_modal = AsyncMock()
        applicator._upload_resume_if_needed = AsyncMock()

        with patch(
            "jobhunter.applicators.form_filling.wait_for_navigation_settle",
            new=AsyncMock(),
        ), patch("jobhunter.applicators.form_filling.random_delay", new=AsyncMock()):
            result = await applicator.apply(job, app)

        assert result is False
        applicator._assess_current_state.assert_not_awaited()
        applicator._dismiss_modal.assert_not_awaited()
        applicator._upload_resume_if_needed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_handles_mid_flow_auth_wall_and_aborts_on_auth_failure(self):
        applicator = make_applicator()
        job, app = self._make_job_and_app("6")

        applicator._page.goto = AsyncMock()
        applicator._page.url = "https://example.com/apply"
        applicator._looks_like_auth_page = AsyncMock(side_effect=[False, True])
        applicator._ensure_on_application_form = AsyncMock(return_value=True)
        applicator._handle_auth_if_needed = AsyncMock(return_value=False)
        applicator._get_page_heading = AsyncMock(return_value="Step 1")
        applicator._confirm_submission = AsyncMock(return_value=False)
        applicator._is_email_verification_wall = AsyncMock(return_value=False)
        applicator._assess_current_state = AsyncMock(return_value="form")
        applicator._dismiss_modal = AsyncMock()
        applicator._upload_resume_if_needed = AsyncMock()
        applicator._fill_current_page = AsyncMock(return_value=1)
        applicator._advance_or_submit = AsyncMock(return_value=None)
        applicator._detect_page_change = AsyncMock(return_value=(True, "Step 2"))

        with patch(
            "jobhunter.applicators.form_filling.wait_for_navigation_settle",
            new=AsyncMock(),
        ), patch("jobhunter.applicators.form_filling.random_delay", new=AsyncMock()):
            result = await applicator.apply(job, app)

        assert result is False
        applicator._fill_current_page.assert_awaited_once()
        applicator._handle_auth_if_needed.assert_awaited_once_with("https://example.com/apply")

    @pytest.mark.asyncio
    async def test_apply_runs_modal_and_resume_hooks_before_fill(self):
        applicator = make_applicator()
        job, app = self._make_job_and_app("7")

        applicator._page.goto = AsyncMock()
        applicator._page.url = "https://example.com/apply"
        applicator._looks_like_auth_page = AsyncMock(return_value=False)
        applicator._ensure_on_application_form = AsyncMock(return_value=True)
        applicator._get_page_heading = AsyncMock(return_value="Step 1")
        applicator._confirm_submission = AsyncMock(side_effect=[False, False])
        applicator._is_email_verification_wall = AsyncMock(return_value=False)
        applicator._assess_current_state = AsyncMock(return_value="form")
        applicator._dismiss_modal = AsyncMock()
        applicator._upload_resume_if_needed = AsyncMock()
        applicator._fill_current_page = AsyncMock(return_value=2)
        applicator._advance_or_submit = AsyncMock(return_value="submitted")

        with patch(
            "jobhunter.applicators.form_filling.wait_for_navigation_settle",
            new=AsyncMock(),
        ), patch("jobhunter.applicators.form_filling.random_delay", new=AsyncMock()):
            result = await applicator.apply(job, app)

        assert result is True
        applicator._dismiss_modal.assert_awaited_once()
        applicator._upload_resume_if_needed.assert_awaited_once()
        applicator._fill_current_page.assert_awaited_once()
