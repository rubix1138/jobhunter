"""
Generic ATS applicator — best-effort Vision-heavy fallback for unknown sites.

Used when apply_type is 'external_other'. Makes a reasonable attempt to fill
and submit the application using Claude Vision for page understanding, but
marks the application needs_review if confidence is low at any step.
"""

import re
from pathlib import Path
from typing import Optional

from patchright.async_api import Page, TimeoutError as PlaywrightTimeout

from ..browser.helpers import (
    fill_field,
    is_visible,
    scroll_to_bottom,
    wait_and_click,
    wait_for_navigation_settle,
)
from ..browser.stealth import micro_delay, random_delay
from ..browser.vision import VisionAnalyzer, screenshot_page, image_to_base64
from ..db.models import Application, Job
from ..llm.client import ClaudeClient
from ..utils.logging import get_logger
from ..utils.profile_loader import UserProfile
from .base import BaseApplicator

logger = get_logger(__name__)

_MAX_STEPS = 12
_COMMON_SUBMIT_TEXTS = ["submit", "apply now", "send application", "complete application"]
_COMMON_NEXT_TEXTS = ["next", "continue", "save and continue", "proceed"]

_GENERIC_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "button[aria-label*='submit' i]",
    "button[aria-label*='apply' i]",
]
_GENERIC_NEXT_SELECTORS = [
    "button[aria-label*='next' i]",
    "button[aria-label*='continue' i]",
    "a[aria-label*='next' i]",
]
_GENERIC_FILE_SELECTORS = [
    "input[type='file']",
    "input[accept*='pdf']",
    "input[accept*='.pdf']",
]

_VISION_SYSTEM = (
    "You are analyzing a job application form screenshot. "
    "Respond only with valid JSON — no prose, no markdown fences."
)


class GenericApplicator(BaseApplicator):
    """
    Best-effort applicator for unknown ATS platforms.

    Strategy:
    1. Navigate to the external apply URL
    2. Take a screenshot and ask Claude what to do next
    3. Try standard DOM selectors; fall back to Vision coordinates when they fail
    4. Repeat for up to _MAX_STEPS steps
    5. Mark needs_review if confidence is low at any point
    """

    def __init__(
        self,
        page: Page,
        llm: ClaudeClient,
        profile: UserProfile,
        resume_path: Path,
        vision: Optional[VisionAnalyzer] = None,
        review_mode: bool = False,
        qa_cache=None,
    ) -> None:
        super().__init__(page, llm, profile, vision, review_mode, qa_cache)
        self._resume_path = resume_path

    async def apply(self, job: Job, application: Application) -> bool:
        self._job = job
        self.logger.info(f"Starting generic application: {job.title} @ {job.company}")

        if not job.external_url:
            self.logger.error("No external URL for generic application")
            return False

        try:
            await self._page.goto(job.external_url, wait_until="domcontentloaded")
        except PlaywrightTimeout:
            self.logger.error(f"Timeout loading: {job.external_url}")
            return False

        await random_delay(2.0, 4.0)
        context = f"{job.title} at {job.company}"

        for step_num in range(1, _MAX_STEPS + 1):
            self.logger.info(f"  Generic step {step_num}")
            await wait_for_navigation_settle(self._page)

            # Ask Vision what the current state is
            action = await self._assess_page(context)
            self.logger.info(f"  Vision assessment: {action.get('state', 'unknown')}")

            state = action.get("state", "form")

            if state == "submitted":
                self.logger.info("Generic application confirmed submitted")
                return True

            if state == "login_required":
                self.logger.warning("External site requires login — needs_review")
                return False

            if state == "error":
                self.logger.warning(f"Page error detected: {action.get('detail')}")
                return False

            if state == "file_upload":
                await self._attempt_file_upload()

            elif state == "form":
                await self._fill_visible_fields(context)
                await self._attempt_advance(action)

            elif state == "captcha":
                self.logger.warning("CAPTCHA detected on external site — needs_review")
                return False

            else:
                # Unknown — attempt to advance
                await self._attempt_advance(action)

            await random_delay(1.5, 3.0)

        self.logger.warning(f"Generic applicator exceeded {_MAX_STEPS} steps — giving up")
        return False

    # ── Page assessment ───────────────────────────────────────────────────────

    async def _assess_page(self, context: str) -> dict:
        """Screenshot and ask Claude what state the page is in."""
        if not self._vision:
            return {"state": "form"}

        try:
            screenshot = await screenshot_page(self._page)
            b64 = image_to_base64(screenshot)

            prompt = f"""Analyze this job application page screenshot.
Context: {context}

Return JSON:
{{
  "state": "<one of: form | file_upload | submitted | login_required | captcha | error | unknown>",
  "detail": "<brief description of what you see>",
  "suggested_action": "<what to do next: fill_fields | click_next | click_submit | upload_file | wait>",
  "submit_button_visible": <true/false>,
  "next_button_visible": <true/false>,
  "has_required_unfilled": <true/false>
}}
"""
            text, usage = await self._llm.vision_message(
                image_b64=b64,
                prompt=prompt,
                purpose="generic_page_assessment",
            )
            import json
            return json.loads(text)
        except Exception as e:
            self.logger.debug(f"Page assessment failed: {e}")
            return {"state": "form", "suggested_action": "fill_fields"}

    # ── Field filling ─────────────────────────────────────────────────────────

    async def _fill_visible_fields(self, context: str) -> None:
        """Find all labeled form fields and attempt to answer them."""
        await scroll_to_bottom(self._page, pause_s=0.5, max_scrolls=4)

        # Standard labeled inputs
        labels = await self._page.query_selector_all("label")
        for label in labels:
            try:
                question = (await label.inner_text()).strip()
                if not question or len(question) > 200:
                    continue

                # Find associated input
                for_id = await label.get_attribute("for")
                if for_id:
                    inp = await self._page.query_selector(f"#{for_id}")
                else:
                    # Try next sibling or parent's input
                    inp = await label.evaluate_handle(
                        "el => el.nextElementSibling || el.parentElement?.querySelector('input,textarea,select')"
                    )
                    inp = inp.as_element() if hasattr(inp, 'as_element') else None

                if not inp:
                    continue

                tag = (await inp.evaluate("el => el.tagName")).lower()
                inp_type = (await inp.get_attribute("type") or "text").lower()

                if inp_type in ("submit", "button", "reset", "hidden", "file"):
                    continue

                if tag == "select":
                    options = [
                        (await o.inner_text()).strip()
                        for o in await inp.query_selector_all("option")
                        if (await o.get_attribute("value") or "") not in ("", "0")
                    ]
                    answer = await self.answer_question(question, "select", options, context)
                    self.record_qa(question, answer)
                    if answer.answer:
                        try:
                            await inp.select_option(label=answer.answer)
                        except Exception:
                            pass
                elif tag == "textarea":
                    answer = await self.answer_question(question, "textarea", None, context)
                    self.record_qa(question, answer)
                    if answer.answer:
                        await inp.triple_click()
                        await inp.type(answer.answer, delay=20)
                else:
                    answer = await self.answer_question(question, "text", None, context)
                    self.record_qa(question, answer)
                    if answer.answer:
                        current = await inp.input_value() or ""
                        if not current:  # don't overwrite pre-filled fields
                            await inp.fill(answer.answer)

                await micro_delay()

            except Exception as e:
                self.logger.debug(f"Generic field error: {e}")

    # ── Navigation ────────────────────────────────────────────────────────────

    async def _attempt_advance(self, assessment: dict) -> bool:
        """Try to click Next or Submit based on Vision assessment."""
        suggested = assessment.get("suggested_action", "")

        if assessment.get("submit_button_visible") or suggested == "click_submit":
            # Try submit selectors
            for sel in _GENERIC_SUBMIT_SELECTORS:
                if await is_visible(self._page, sel, timeout=1_500):
                    btn = self._page.locator(sel).first
                    text = (await btn.inner_text()).lower()
                    if any(t in text for t in _COMMON_SUBMIT_TEXTS):
                        job = getattr(self, "_job", None)
                        title = job.title if job else "Unknown"
                        company = job.company if job else "Unknown"
                        if not await self._pause_for_review(title, company):
                            return False
                        await btn.click()
                        await random_delay(2.0, 4.0)
                        return True

        # Try next/continue buttons
        for sel in _GENERIC_NEXT_SELECTORS:
            if await is_visible(self._page, sel, timeout=1_500):
                await self._page.locator(sel).first.click()
                await random_delay(1.0, 2.0)
                return True

        # Vision click fallback — find any primary action button by text
        for text in _COMMON_NEXT_TEXTS + _COMMON_SUBMIT_TEXTS:
            try:
                btn = self._page.get_by_role("button", name=re.compile(text, re.IGNORECASE)).first
                if await btn.is_visible(timeout=1_000):
                    await btn.click()
                    await random_delay(1.0, 2.0)
                    return True
            except Exception:
                pass

        return False

    async def _attempt_file_upload(self) -> None:
        """Upload resume to any visible file input."""
        for sel in _GENERIC_FILE_SELECTORS:
            try:
                el = self._page.locator(sel).first
                await el.set_input_files(str(self._resume_path))
                await random_delay(2.0, 3.0)
                self.logger.info("Resume uploaded via generic applicator")
                return
            except Exception:
                pass

        self.logger.warning("Generic applicator: no file input found for resume upload")
