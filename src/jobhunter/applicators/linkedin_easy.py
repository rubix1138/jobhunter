"""LinkedIn Easy Apply modal handler — multi-step form automation."""

import asyncio
from pathlib import Path
from typing import Optional
import re

from patchright.async_api import Page, TimeoutError as PlaywrightTimeout

from ..browser.accessibility import find_by_aria_label, get_ax_tree, search_ax_tree
from ..browser.helpers import (
    fill_field,
    is_visible,
    select_option,
    wait_and_click,
)
from ..browser.stealth import micro_delay, random_delay
from ..browser.vision import VisionAnalyzer
from ..db.models import Application, Job
from ..llm.client import ClaudeClient
from ..utils.logging import get_logger
from ..utils.profile_loader import UserProfile
from .base import BaseApplicator, QuestionAnswer

logger = get_logger(__name__)

# Easy Apply modal selectors
_MODAL = "div.jobs-easy-apply-modal"
_EASY_APPLY_BTN = [
    "button.jobs-apply-button",
    "button[aria-label*='Easy Apply']",
    "button[aria-label*='easy apply']",
]
_NEXT_BTN = [
    "button[aria-label='Continue to next step']",
    "button[aria-label='Review your application']",
    "footer button.artdeco-button--primary",
    f"{_MODAL} footer button.artdeco-button--primary",
]
_SUBMIT_BTN = [
    "button[aria-label='Submit application']",
    "button[aria-label*='Submit']",
    f"{_MODAL} footer button.artdeco-button--primary[aria-label*='Submit']",
]
_CLOSE_BTN = [
    "button[aria-label='Dismiss']",
    f"{_MODAL} button[aria-label='Dismiss']",
]
_STEP_INDICATOR = [
    f"{_MODAL} span.t-14.t-black--light",
    f"{_MODAL} div.ph5 span",
]
_FILE_INPUT = [
    "input[type='file'][name*='resume']",
    "input[type='file']",
]
_FORM_FIELDS = f"{_MODAL} .jobs-easy-apply-form-section__grouping"

# Step content selectors for recognizing step type
_UPLOAD_INDICATORS = ["resume", "cv", "upload", "attach"]
_REVIEW_INDICATORS = ["review", "preview", "confirm"]

_MAX_STEPS = 20        # safety cap on modal steps
_FIELD_TIMEOUT = 8_000
_VALIDATION_ERR_SEL = (
    ".artdeco-inline-feedback--error, "
    ".fb-form-element__error-text, "
    "[class*='error-message'], "
    ".jobs-easy-apply-form-element__error"
)


class LinkedInEasyApplicator(BaseApplicator):
    """
    Handles LinkedIn's Easy Apply multi-step modal.

    Flow:
    1. Click the Easy Apply button on the job page
    2. Iterate modal steps: fill fields, answer questions, upload resume
    3. Detect Review step and submit
    4. Return True on successful submission
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
        # Set to True by apply() when page shows "I'm interested" instead of
        # Easy Apply — signals apply_agent to correct the job's stored type.
        self.detected_interest_only: bool = False
        # Set to True when page shows "No longer accepting applications".
        self.detected_expired: bool = False
        # Set to True when Easy Apply is via SDUI link (navigates to /apply/ page)
        # instead of opening the traditional div.jobs-easy-apply-modal.
        self._sdui_flow: bool = False

    async def apply(self, job: Job, application: Application) -> bool:
        """Open Easy Apply modal and complete all steps."""
        self._job = job
        self.logger.info(f"Starting Easy Apply: {job.title} @ {job.company}")

        # Always navigate to the job page before opening the modal.
        # LLM calls take 40+ seconds — during which LinkedIn's SPA may have
        # navigated away or the session may have changed state. Re-navigating
        # ensures we get a fresh Easy Apply button render.
        if job.job_url:
            self.logger.debug(f"Navigating to job page: {job.job_url[:80]}")
            try:
                await self._page.goto(job.job_url, wait_until="load", timeout=25_000)
                await random_delay(2.5, 4.0)
            except Exception as e:
                self.logger.warning(f"Could not navigate to job page: {e}")

        # Check for closed listing BEFORE trying to open the modal — saves time
        # and prevents confusing "could not find Easy Apply button" errors.
        if await self._is_expired():
            self.logger.warning(
                f"Job listing closed: {job.title} @ {job.company} "
                "— 'No longer accepting applications'"
            )
            self.detected_expired = True
            return False

        # Open the modal
        if not await self._open_modal():
            # Check if this is actually a recruiter "I'm interested" listing —
            # stored as easy_apply during search but actually has no apply button.
            import re as _re
            try:
                interest_btn = self._page.get_by_role(
                    "button", name=_re.compile(r"i.?m\s+interested", _re.IGNORECASE)
                ).first
                if await interest_btn.is_visible(timeout=1_500):
                    self.logger.warning(
                        "Page shows 'I'm interested' — job is recruiter-sourced, "
                        "not Easy Apply. Signalling apply_agent to correct stored type."
                    )
                    self.detected_interest_only = True
            except Exception:
                pass
            self.logger.error("Could not open Easy Apply modal")
            return False

        await random_delay(1.5, 3.0)

        # Track consecutive identical steps to detect infinite loops.
        # We fingerprint each step by reading the first visible label text in the modal.
        _last_step_fingerprint: str = ""
        _stuck_count: int = 0
        _STUCK_LIMIT = 3  # abort after 3 consecutive steps with identical content
        _last_validation_errors: list[str] = []

        async def _step_fingerprint() -> str:
            """Return a short string identifying the current modal step content."""
            for sel in [
                f"{_MODAL} label",
                f"{_MODAL} legend",
                f"{_MODAL} h3",
                f"{_MODAL} h2",
            ]:
                try:
                    el = self._page.locator(sel).first
                    if await el.is_visible(timeout=500):
                        return (await el.inner_text()).strip()[:80]
                except Exception:
                    pass
            return ""

        # Iterate through steps
        for step_num in range(1, _MAX_STEPS + 1):
            self.logger.debug(f"Processing modal step {step_num}")

            step_type = await self._detect_step_type()
            self.logger.info(f"  Step {step_num}: {step_type}")

            if step_type == "submit":
                return await self._submit()

            if step_type == "upload":
                await self._handle_upload_step()
            elif step_type == "review":
                # On review page — click submit
                return await self._submit()
            else:
                # Generic question/form step
                await self._handle_form_step(job)

            # Detect no-progress: if the step content is identical to the previous
            # step, LinkedIn is showing a validation error and not advancing.
            fingerprint = await _step_fingerprint()
            if fingerprint and fingerprint == _last_step_fingerprint:
                _stuck_count += 1
                self.logger.warning(
                    f"Step content unchanged after Next click "
                    f"({_stuck_count}/{_STUCK_LIMIT}): {fingerprint!r}"
                )
                if _stuck_count >= 2 and _last_validation_errors:
                    remediated = await self._remediate_validation_groups(job)
                    if remediated:
                        self.logger.warning(
                            f"Applied required-field remediation to {remediated} "
                            "error group(s) before retrying Next"
                        )
                if _stuck_count >= _STUCK_LIMIT:
                    self.logger.error(
                        "Form not advancing — likely a required field LinkedIn "
                        "won't accept our answer. Aborting this application."
                    )
                    return False
            else:
                _stuck_count = 0
            _last_step_fingerprint = fingerprint

            # Advance to next step
            advanced = await self._click_next()
            if not advanced:
                # Try vision to find what's blocking us
                hint = await self.handle_stuck_page("next step button in Easy Apply modal")
                self.logger.warning(f"Could not advance modal — Vision hint: {hint}")
                return False

            await random_delay(1.0, 2.5)

            # Log any validation errors visible after clicking Next
            _last_validation_errors = await self._collect_validation_errors()
            for txt in _last_validation_errors[:5]:
                self.logger.warning(f"  Validation error: {txt!r}")

        self.logger.error("Exceeded max modal steps — aborting")
        return False

    # ── Modal lifecycle ───────────────────────────────────────────────────────

    async def _is_expired(self) -> bool:
        """Return True if the page shows 'No longer accepting applications'."""
        try:
            el = self._page.locator("text='No longer accepting applications'").first
            return await el.is_visible(timeout=2000)
        except Exception:
            return False

    async def _open_modal(self) -> bool:
        """Click the Easy Apply button and wait for modal to appear.

        Detection order (8 layers):
          1. AX tree — find_by_aria_label "easy apply" with job_id guard
          2. AX tree — interest_only check (early return False)
          3. Scope DOM to detail area (existing _DETAIL_SCOPES logic)
          4. get_by_role("button", "easy apply") → wait for modal
          5. SDUI link: get_by_role("link") + a[href*=openSDUIApplyFlow] with job_id validation
          6. CSS selector fallback via wait_and_click(_EASY_APPLY_BTN)
          7. Vision fallback: analyze_page → get_by_text(label_text)
          8. Diagnostic logging
        """
        import re as _re

        self.logger.info(f"_open_modal: url={self._page.url[:80]}")

        _job_id = getattr(self._job, "linkedin_job_id", None) if hasattr(self, "_job") else None
        _job_url = getattr(self._job, "job_url", None) if hasattr(self, "_job") else None
        # Set to True when an SDUI/AX click navigated away from the job page.
        # Layers 6+7 are skipped to avoid re-triggering the same broken redirect.
        _sdui_link_broken = False

        async def _back_to_job_if_drifted() -> bool:
            """Navigate back to the job page if a click took us to a different URL.
            Returns True if the link was broken (navigation needed), False if still on job page."""
            if not _job_url:
                return False
            current = self._page.url
            if _job_id and str(_job_id) in current:
                return False
            if "linkedin.com/jobs/view/" in current:
                return False
            self.logger.warning(
                f"Page drifted to {current[:80]} after click — navigating back to job URL"
            )
            try:
                await self._page.goto(_job_url, wait_until="load", timeout=20_000)
                await random_delay(2.0, 3.5)
            except Exception as nav_err:
                self.logger.debug(f"Navigate-back failed: {nav_err}")
            return True

        def _looks_like_easy_apply_href(href: str) -> bool:
            """Guard against sidebar/recruiter links mislabeled as Easy Apply."""
            h = (href or "").lower()
            if not h:
                return False
            if "/jobs/search/?" in h:
                return False
            return (
                "opensduiapplyflow" in h
                or "/apply/" in h
                or "easyapply" in h
            )

        # ── Layer 1: AX tree Easy Apply ───────────────────────────────────────
        try:
            _easy_pattern = _re.compile(r"easy\s*apply", _re.IGNORECASE)
            ax_btn = await find_by_aria_label(
                self._page,
                _easy_pattern,
                roles=("button", "link"),
                job_id=str(_job_id) if _job_id else None,
                timeout_ms=3_000,
            )
            if ax_btn is not None:
                self.logger.info("Found Easy Apply via AX tree — clicking")
                # If it's a link element, navigate directly to avoid new-tab open
                try:
                    ax_href = await ax_btn.get_attribute("href")
                except Exception:
                    ax_href = None
                if ax_href:
                    full_ax_href = ax_href if ax_href.startswith("http") else f"https://www.linkedin.com{ax_href}"
                    await self._page.goto(full_ax_href, wait_until="load", timeout=15_000)
                else:
                    await ax_btn.click()
                try:
                    await self._page.wait_for_selector(_MODAL, timeout=8_000)
                    self.logger.debug("Modal appeared after AX tree button click")
                    return True
                except PlaywrightTimeout:
                    pass
                # Check if SDUI flow instead (navigated to /apply/ page)
                try:
                    await self._page.wait_for_url("**/apply/**", timeout=5_000)
                    self._sdui_flow = True
                    self.logger.info("AX click → SDUI flow detected")
                    return True
                except PlaywrightTimeout:
                    self.logger.debug("AX tree click did not open modal or SDUI — recovering")
                    if await _back_to_job_if_drifted():
                        _sdui_link_broken = True
        except Exception as e:
            self.logger.debug(f"AX tree Easy Apply attempt failed: {e}")

        # ── Layer 2: AX tree interest_only check ──────────────────────────────
        try:
            _interest_pattern = _re.compile(r"i.?m\s+interested", _re.IGNORECASE)
            ax_interest = await find_by_aria_label(
                self._page,
                _interest_pattern,
                roles=("button",),
                timeout_ms=1_500,
            )
            if ax_interest is not None:
                self.logger.warning(
                    "AX tree shows 'I'm interested' — job is recruiter-sourced, "
                    "not Easy Apply. Signalling apply_agent to correct stored type."
                )
                self.detected_interest_only = True
                return False
        except Exception as e:
            self.logger.debug(f"AX tree interest_only check failed: {e}")

        # ── Layer 3: Scope DOM to job detail area ─────────────────────────────
        _DETAIL_SCOPES = [
            ".jobs-search__job-details",
            ".scaffold-layout__detail",
            "div[class*='jobs-details']",
            "main",
        ]
        scope = self._page
        for sel in _DETAIL_SCOPES:
            try:
                loc = self._page.locator(sel).first
                if await loc.is_visible(timeout=1_000):
                    scope = loc
                    self.logger.debug(f"Scoped button search to: {sel}")
                    break
            except Exception:
                pass

        # ── Layer 4: get_by_role("button") DOM fallback ───────────────────────
        try:
            btn = scope.get_by_role(
                "button", name=_re.compile(r"easy\s*apply", _re.IGNORECASE)
            ).first
            if await btn.is_visible(timeout=3_000):
                self.logger.debug("Found Easy Apply button via get_by_role — clicking")
                await btn.click()
                try:
                    await self._page.wait_for_selector(_MODAL, timeout=8_000)
                    return True
                except PlaywrightTimeout:
                    self.logger.debug("Modal did not appear after get_by_role button click")
            else:
                self.logger.debug("get_by_role button: Easy Apply not visible")
        except Exception as e:
            self.logger.debug(f"get_by_role button attempt failed: {e}")

        # ── Layer 5: SDUI link (get_by_role link + openSDUIApplyFlow href) ────
        for sdui_attempt in ("link_by_role", "link_by_href"):
            try:
                if sdui_attempt == "link_by_role":
                    link = scope.get_by_role(
                        "link", name=_re.compile(r"easy\s*apply", _re.IGNORECASE)
                    ).first
                else:
                    link = scope.locator("a[href*='openSDUIApplyFlow']").first
                if not await link.is_visible(timeout=2_000):
                    continue
                href = (await link.get_attribute("href") or "")
                href_l = href.lower()
                if not href or ("linkedin.com" not in href_l and not href.startswith("/")):
                    continue
                if _job_id and str(_job_id) not in href:
                    self.logger.debug(
                        f"SDUI link ({sdui_attempt}) skipped — href does not contain "
                        f"current job ID {_job_id!r} (likely a sidebar card for another job)"
                    )
                    continue
                if not _looks_like_easy_apply_href(href):
                    self.logger.debug(
                        f"SDUI link ({sdui_attempt}) skipped — href is not a true apply flow: "
                        f"{href[:120]!r}"
                    )
                    continue
                # Use goto() instead of click() to force navigation in the current tab.
                # link.click() may open a new tab (target="_blank"), leaving self._page on
                # the job detail page and making drift detection impossible.
                full_href = href if href.startswith("http") else f"https://www.linkedin.com{href}"
                self.logger.info(f"Found Easy Apply SDUI link ({sdui_attempt}) — navigating")
                try:
                    await self._page.goto(full_href, wait_until="load", timeout=15_000)
                except PlaywrightTimeout:
                    pass
                try:
                    await self._page.wait_for_url("**/apply/**", timeout=5_000)
                    self._sdui_flow = True
                    self.logger.info("SDUI apply flow — now on /apply/ page")
                    return True
                except PlaywrightTimeout:
                    try:
                        await self._page.wait_for_selector(_MODAL, timeout=3_000)
                        return True
                    except PlaywrightTimeout:
                        self.logger.debug("No SDUI navigation or modal after goto")
                        if await _back_to_job_if_drifted():
                            _sdui_link_broken = True
                            break  # stop trying more SDUI variants
            except Exception as e:
                self.logger.debug(f"SDUI link attempt ({sdui_attempt}) failed: {e}")

        # ── Layer 6: CSS selector fallback ────────────────────────────────────
        # Skip if SDUI link already redirected away — CSS and Vision would just
        # re-trigger the same broken redirect and waste 40+ seconds.
        if not _sdui_link_broken:
            clicked = await wait_and_click(self._page, _EASY_APPLY_BTN)
            if clicked:
                try:
                    await self._page.wait_for_selector(_MODAL, timeout=8_000)
                    return True
                except PlaywrightTimeout:
                    pass
        else:
            self.logger.info(
                "SDUI link redirected to wrong page — skipping CSS/Vision layers"
            )

        # ── Layer 7: Vision fallback ──────────────────────────────────────────
        if self._vision is not None and not _sdui_link_broken:
            try:
                self.logger.info("Trying Vision fallback to find Easy Apply button")
                response = await self._vision.analyze_page(
                    self._page,
                    question=(
                        "Is there an Easy Apply or Apply button visible on this page? "
                        "If yes, respond with JSON: "
                        '{"found": true, "element_type": "button or link", "label_text": "exact button text"}. '
                        'If no, respond with {"found": false}.'
                    ),
                    context="Finding Easy Apply button — DOM selectors failed",
                )
                import json as _json
                try:
                    vision_data = _json.loads(response.strip())
                except Exception:
                    # Try to extract JSON from response
                    import re as _re2
                    m = _re2.search(r'\{.*\}', response, _re2.DOTALL)
                    vision_data = _json.loads(m.group()) if m else {}

                if vision_data.get("found") and vision_data.get("label_text"):
                    label = vision_data["label_text"]
                    self.logger.info(f"Vision found button: {label!r} — clicking via get_by_text")
                    try:
                        el = self._page.get_by_text(label, exact=True).first
                        if await el.is_visible(timeout=2_000):
                            await el.click()
                            try:
                                await self._page.wait_for_selector(_MODAL, timeout=8_000)
                                return True
                            except PlaywrightTimeout:
                                try:
                                    await self._page.wait_for_url("**/apply/**", timeout=5_000)
                                    self._sdui_flow = True
                                    return True
                                except PlaywrightTimeout:
                                    pass
                    except Exception as ve:
                        self.logger.debug(f"Vision-guided click failed: {ve}")
            except Exception as e:
                self.logger.debug(f"Vision fallback failed: {e}")

        # ── Layer 8: Diagnostic logging ───────────────────────────────────────
        try:
            btns = self._page.get_by_role("button")
            count = await btns.count()
            visible = []
            for i in range(min(count, 20)):
                try:
                    b = btns.nth(i)
                    if await b.is_visible(timeout=200):
                        txt = (await b.inner_text()).strip().replace("\n", " ")
                        if txt:
                            visible.append(txt[:30])
                except Exception:
                    pass
            vis_links = []
            try:
                all_links = self._page.get_by_role("link")
                lcount = await all_links.count()
                for i in range(min(lcount, 30)):
                    try:
                        lnk = all_links.nth(i)
                        if await lnk.is_visible(timeout=200):
                            txt = (await lnk.inner_text()).strip().replace("\n", " ")
                            if txt and any(
                                kw in txt.lower() for kw in ("apply", "easy", "interest")
                            ):
                                href = (await lnk.get_attribute("href") or "")[:60]
                                vis_links.append(f"{txt[:25]}[{href}]")
                    except Exception:
                        pass
            except Exception:
                pass
            self.logger.info(
                f"Easy Apply not found | url={self._page.url[:80]} | "
                f"buttons={visible} | apply_links={vis_links}"
            )
        except Exception:
            pass
        return False

    async def _click_next(self) -> bool:
        """Click the Next/Continue/Review button."""
        for sel in _NEXT_BTN:
            try:
                locator = self._page.locator(sel).first
                if await locator.is_visible(timeout=2000):
                    label = await locator.get_attribute("aria-label") or await locator.inner_text()
                    self.logger.info(f"  _click_next: clicking {sel!r} (label={label!r})")
                    await locator.click()
                    return True
            except Exception:
                pass
        self.logger.warning("  _click_next: no Next button found")
        return False

    async def _submit(self) -> bool:
        """Click the Submit Application button."""
        job = getattr(self, "_job", None)
        title = job.title if job else "Unknown"
        company = job.company if job else "Unknown"
        if not await self._pause_for_review(title, company):
            return False
        self.logger.info("Clicking Submit Application")
        clicked = await wait_and_click(self._page, _SUBMIT_BTN)
        if not clicked:
            return False
        await random_delay(2.0, 4.0)
        # Confirm the modal closed (application submitted)
        modal_gone = not await is_visible(self._page, _MODAL, timeout=5_000)
        if modal_gone:
            self.logger.info("Application submitted — modal closed")
            return True
        # Some modals show a "done" confirmation inside
        done_visible = await is_visible(
            self._page,
            "div.jobs-easy-apply-content h3",
            timeout=3_000,
        )
        return done_visible

    async def _close_modal(self) -> None:
        await wait_and_click(self._page, _CLOSE_BTN, delay_after=False)

    # ── Step type detection ───────────────────────────────────────────────────

    async def _detect_step_type(self) -> str:
        """
        Detect the current modal step.
        Returns: 'upload' | 'review' | 'submit' | 'form'
        """
        try:
            # Check for submit button visible
            if await is_visible(self._page, _SUBMIT_BTN[0], timeout=1_000):
                return "submit"

            # Get all visible text in the modal header area
            header_text = ""
            for sel in [f"{_MODAL} h3", f"{_MODAL} h2", f"{_MODAL} .t-20"]:
                el = self._page.locator(sel).first
                try:
                    if await el.is_visible(timeout=1_000):
                        header_text = (await el.inner_text()).lower()
                        break
                except Exception:
                    pass

            if any(w in header_text for w in _REVIEW_INDICATORS):
                return "review"
            if any(w in header_text for w in _UPLOAD_INDICATORS):
                return "upload"

            # Check for file input as upload indicator
            if await is_visible(self._page, "input[type='file']", timeout=1_000):
                return "upload"

        except Exception as e:
            self.logger.debug(f"Step detection error: {e}")

        return "form"

    # ── Step handlers ─────────────────────────────────────────────────────────

    async def _handle_upload_step(self) -> None:
        """Upload the resume PDF."""
        self.logger.info(f"Uploading resume: {self._resume_path.name}")
        for sel in _FILE_INPUT:
            try:
                el = self._page.locator(sel).first
                if await el.is_visible(timeout=2_000) or True:
                    await el.set_input_files(str(self._resume_path))
                    await random_delay(1.5, 3.0)
                    self.logger.info("Resume uploaded")
                    return
            except Exception:
                pass
        self.logger.warning("Could not find file input for resume upload")

    async def _handle_form_step(self, job: Job) -> None:
        """Find and fill all form fields on the current step."""
        context = f"{job.title} at {job.company}"

        # SDUI flow: form fields are not inside the modal container
        if self._sdui_flow:
            groups = await self._page.query_selector_all(
                ".jobs-easy-apply-form-section__grouping"
            )
            if not groups:
                groups = await self._page.query_selector_all(
                    "fieldset, .artdeco-text-input--container"
                )
        else:
            # Collect all form groups visible in the modal
            groups = await self._page.query_selector_all(_FORM_FIELDS)
            if not groups:
                # Fallback: try individual input types
                groups = await self._page.query_selector_all(
                    f"{_MODAL} fieldset, {_MODAL} .artdeco-text-input--container"
                )

        # Vision guard: when no DOM groups found, log what Vision sees for diagnostics.
        # Does not yet drive filling — informational only to help tune selectors.
        if not groups and self._vision is not None:
            try:
                fields = await self._vision.analyze_form_fields(self._page, context=context)
                if fields:
                    self.logger.info(
                        f"_handle_form_step: DOM found no groups but Vision sees "
                        f"{len(fields)} field(s): {fields}"
                    )
            except Exception as e:
                self.logger.debug(f"Vision form field analysis failed: {e}")

        for group in groups:
            try:
                await self._fill_field_group(group, context)
            except Exception as e:
                self.logger.debug(f"Field group error: {e}")

    async def _fill_field_group(
        self,
        group,
        context: str,
        prefer_safe_defaults: bool = False,
    ) -> None:
        """Fill a single form field group (label + input)."""
        # Extract label text
        label_el = await group.query_selector("label, legend, span.t-bold, [id*='label']")
        question = ""
        if label_el:
            question = (await label_el.inner_text()).strip()

        if not question:
            return

        # Determine field type
        radio_inputs = await group.query_selector_all("input[type='radio']")
        select_el = await group.query_selector("select")
        textarea_el = await group.query_selector("textarea")
        text_input = await group.query_selector("input[type='text'], input[type='number']")

        if radio_inputs:
            self.logger.info(f"  field[radio] q={question[:60]!r}")
            await self._handle_radio(
                group, question, radio_inputs, context, prefer_safe_defaults=prefer_safe_defaults
            )
        elif select_el:
            self.logger.info(f"  field[select] q={question[:60]!r}")
            await self._handle_select(
                group, question, select_el, context, prefer_safe_defaults=prefer_safe_defaults
            )
        elif textarea_el:
            self.logger.info(f"  field[textarea] q={question[:60]!r}")
            await self._handle_textarea(question, textarea_el, context)
        elif text_input:
            self.logger.info(f"  field[text] q={question[:60]!r}")
            await self._handle_text_input(
                question, text_input, context, prefer_safe_defaults=prefer_safe_defaults
            )
        else:
            self.logger.debug(f"  field[unknown] q={question[:60]!r} — no input found")

    async def _handle_radio(
        self,
        group,
        question: str,
        inputs,
        context: str,
        prefer_safe_defaults: bool = False,
    ) -> None:
        options = []
        labels = []
        for inp in inputs:
            label = await group.query_selector(
                f"label[for='{await inp.get_attribute('id')}']"
            )
            if label:
                label_text = (await label.inner_text()).strip()
                options.append(label_text)
                labels.append((label_text, label))

        self.logger.info(f"    radio options: {options}")
        target = ""
        if prefer_safe_defaults:
            target = self._preferred_option(options, question) or ""
            self.logger.info(f"    radio remediation target: {target!r}")
        else:
            answer = await self.answer_question(question, "radio", options, context)
            self.record_qa(question, answer)
            self.logger.info(
                f"    radio answer: {answer.answer!r} "
                f"(src={answer.source}, conf={answer.confidence:.2f})"
            )
            target = (answer.answer or "").strip()
            if not target:
                target = self._preferred_option(options, question) or ""

        # Click the matching radio label (exact first, then fuzzy)
        if target:
            target_norm = target.lower()
            for label_text, label in labels:
                if label_text.lower() == target_norm:
                    await label.click()
                    self.logger.info(f"    radio clicked: {label_text!r}")
                    await micro_delay()
                    return
            for label_text, label in labels:
                l = label_text.lower()
                if target_norm in l or l in target_norm:
                    await label.click()
                    self.logger.info(f"    radio clicked (fuzzy): {label_text!r}")
                    await micro_delay()
                    return

        # Fallback — click first label when present, else first input
        self.logger.warning(f"    radio: no label matched {target!r}, using fallback")
        if labels:
            await labels[0][1].click()
            await micro_delay()
            return
        if inputs:
            await inputs[0].click()
            await micro_delay()

    async def _handle_select(
        self,
        group,
        question: str,
        select_el,
        context: str,
        prefer_safe_defaults: bool = False,
    ) -> None:
        option_els = await select_el.query_selector_all("option")
        options = []
        for o in option_els:
            label = (await o.inner_text()).strip()
            value = (await o.get_attribute("value") or "").strip()
            if not label:
                continue
            if value == "":
                continue
            if "select" in label.lower() and "option" in label.lower():
                continue
            options.append((label, value))

        option_labels = [l for l, _ in options]
        self.logger.info(f"    select options: {option_labels}")
        target = ""
        if prefer_safe_defaults:
            target = self._preferred_option(option_labels, question) or ""
            self.logger.info(f"    select remediation target: {target!r}")
        else:
            answer = await self.answer_question(question, "select", option_labels, context)
            self.record_qa(question, answer)
            self.logger.info(
                f"    select answer: {answer.answer!r} "
                f"(src={answer.source}, conf={answer.confidence:.2f})"
            )
            target = (answer.answer or "").strip()
        chosen_label = None
        chosen_value = None

        if target:
            t = target.lower()
            for label, value in options:
                if label.lower() == t:
                    chosen_label, chosen_value = label, value
                    break
            if chosen_label is None:
                for label, value in options:
                    l = label.lower()
                    if t in l or l in t:
                        chosen_label, chosen_value = label, value
                        break

        if chosen_label is None:
            preferred = self._preferred_option(option_labels, question)
            if preferred:
                for label, value in options:
                    if label == preferred:
                        chosen_label, chosen_value = label, value
                        break
        if chosen_label is None and options:
            chosen_label, chosen_value = options[0]

        try:
            if chosen_label is not None:
                await select_el.select_option(label=chosen_label)
                self.logger.info(f"    select_option(label={chosen_label!r}) succeeded")
        except Exception:
            try:
                if chosen_value is not None:
                    await select_el.select_option(value=chosen_value)
                    self.logger.info(f"    select_option(value={chosen_value!r}) succeeded")
            except Exception:
                self.logger.warning(
                    f"Could not select option {chosen_label or target!r} for {question!r}"
                )

        await micro_delay()

    async def _handle_textarea(self, question: str, textarea_el, context: str) -> None:
        answer = await self.answer_question(question, "textarea", None, context)
        self.record_qa(question, answer)
        if answer.answer:
            await textarea_el.triple_click()
            await textarea_el.type(answer.answer, delay=30)
            await micro_delay()

    async def _handle_text_input(
        self,
        question: str,
        input_el,
        context: str,
        prefer_safe_defaults: bool = False,
    ) -> None:
        target = ""
        if prefer_safe_defaults:
            target = await self._safe_text_fallback(question, input_el)
            if not target:
                return
            self.logger.info(f"    text remediation target: {target!r}")
        else:
            answer = await self.answer_question(question, "text", None, context)
            self.record_qa(question, answer)
            self.logger.info(
                f"    text answer: {answer.answer!r} (src={answer.source}, conf={answer.confidence:.2f})"
            )
            target = answer.answer or ""

        if target:
            await input_el.click()
            await micro_delay()
            # Use fill() to properly update React's controlled component state.
            # type() fires keyboard events but doesn't reliably trigger React's
            # synthetic onChange for <input type="number"> elements.
            await input_el.fill(target)
            await micro_delay()

    async def _safe_text_fallback(self, question: str, input_el) -> str:
        """Return a conservative fallback value for stubborn required text inputs."""
        q = (question or "").lower()
        try:
            input_type = (await input_el.get_attribute("type") or "").lower()
        except Exception:
            input_type = ""

        if input_type == "number" or re.search(r"\byears?\b|\bmonths?\b", q):
            return "1"
        if "email" in q:
            return getattr(self._profile, "email", "") or ""
        if "phone" in q:
            return getattr(self._profile, "phone", "") or ""
        if "linkedin" in q:
            return getattr(self._profile, "linkedin_url", "") or ""
        return ""

    async def _collect_validation_errors(self) -> list[str]:
        """Collect visible LinkedIn validation messages from the current step."""
        try:
            err_els = await self._page.query_selector_all(_VALIDATION_ERR_SEL)
        except Exception:
            return []

        messages: list[str] = []
        for el in err_els:
            try:
                txt = (await el.inner_text()).strip()
            except Exception:
                continue
            if txt:
                messages.append(txt)
        return messages

    async def _remediate_validation_groups(self, job: Job) -> int:
        """
        Re-fill only field groups currently marked with validation errors,
        using safe defaults to break required-field loops.
        """
        context = f"{job.title} at {job.company}"
        if self._sdui_flow:
            groups = await self._page.query_selector_all(".jobs-easy-apply-form-section__grouping")
            if not groups:
                groups = await self._page.query_selector_all("fieldset, .artdeco-text-input--container")
        else:
            groups = await self._page.query_selector_all(_FORM_FIELDS)
            if not groups:
                groups = await self._page.query_selector_all(
                    f"{_MODAL} fieldset, {_MODAL} .artdeco-text-input--container"
                )

        remediated = 0
        for group in groups:
            try:
                has_error = await group.query_selector(_VALIDATION_ERR_SEL)
                if not has_error:
                    continue
                await self._fill_field_group(group, context, prefer_safe_defaults=True)
                remediated += 1
            except Exception as e:
                self.logger.debug(f"Validation remediation failed on a group: {e}")
        return remediated

    def _preferred_option(self, options: list[str], question: str) -> Optional[str]:
        """Pick a safe fallback option when the model answer is empty/non-matching."""
        if not options:
            return None
        opts = [o.strip() for o in options if o and o.strip()]
        if not opts:
            return None

        lowered = [o.lower() for o in opts]
        q = (question or "").lower()

        if "citizen" in q or "authorized" in q or "eligible to work" in q:
            for i, o in enumerate(lowered):
                if o in ("yes", "y"):
                    return opts[i]

        for token in ("prefer not", "choose not", "decline", "not disclose", "not to say"):
            for i, o in enumerate(lowered):
                if token in o:
                    return opts[i]

        if "refer" in q or "how did you hear" in q or "source" in q:
            referral_order = (
                "linkedin",
                "company website",
                "job board",
                "indeed",
                "glassdoor",
                "other",
                "employee referral",
            )
            for token in referral_order:
                for i, o in enumerate(lowered):
                    if token in o:
                        return opts[i]

        return opts[0]
