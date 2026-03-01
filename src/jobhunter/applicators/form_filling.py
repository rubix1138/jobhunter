"""
Universal form-filling applicator — AX tree + Vision + LLM planning.

Replaces both WorkdayApplicator (4,089 lines) and GenericApplicator (298 lines)
with a single agent that handles any ATS platform by reading the accessibility
tree, planning field fills via Claude, and executing them with ARIA-based
locators.  LinkedIn Easy Apply stays separate (it's a contained modal).
"""

import asyncio
import json
import re
import secrets
import string
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from patchright.async_api import Page, TimeoutError as PlaywrightTimeout

from ..browser.accessibility import (
    find_by_aria_label,
    format_interactive_fields,
    get_ax_tree,
    search_ax_tree,
)
from ..browser.helpers import (
    fill_field,
    is_visible,
    scroll_to_bottom,
    select_option,
    wait_and_click,
    wait_for_navigation_settle,
)
from ..browser.stealth import micro_delay, random_delay
from ..browser.vision import VisionAnalyzer, image_to_base64, screenshot_page
from ..crypto.vault import CredentialVault
from ..db.models import Application, Credential, Job
from ..db.repository import CredentialRepo
from ..llm.client import ClaudeClient
from ..utils.logging import get_logger
from ..utils.profile_loader import UserProfile
from .base import BaseApplicator

logger = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_MAX_PAGES = 15

_SECTION_PLAN_SYSTEM = (
    "You fill job application form sections on behalf of a candidate.\n"
    "Return ONLY a JSON array — no prose, no markdown fences.\n"
    'Each element: {"label": "<exact field label>", "field_type": '
    '"text|radiogroup|select|checkbox|combobox", "value": "<answer>"}\n'
    "Include only fields that need a value. Skip already-answered optional fields.\n"
    "For radiogroup fields: label is the question text, value is the exact "
    "option to select (e.g. 'Yes' or 'No').\n"
    "For select/combobox, value must match one of the visible options exactly."
)

_CONFIRM_TEXTS = [
    "application submitted",
    "thank you for applying",
    "your application has been",
    "we've received your",
    "application received",
    "successfully submitted",
]

_AUTH_KEYWORDS = [
    "sign in", "log in", "login", "create account", "register",
    "sign up", "enter your email", "enter your password",
    "single sign-on", "single sign on", "sso", "okta", "microsoft",
    "azure ad", "identity provider",
]

_SSO_KEYWORDS = [
    "single sign-on",
    "single sign on",
    "sso",
    "okta",
    "azure ad",
    "microsoft sign in",
    "identity provider",
]

_AUTH_CONTROL_PATTERN = re.compile(
    r"sign in|log in|login|create account|register|sign up|single sign[- ]on|sso|continue with",
    re.IGNORECASE,
)

_GUEST_LABELS = (
    "Continue as Guest",
    "Apply Without Account",
    "Apply as Guest",
    "Continue Without Account",
    "Apply without signing in",
    "Apply Without Signing In",
    "Guest",
    "Continue Without Signing In",
)

_VERIFY_EMAIL_INDICATORS = [
    "verify your email",
    "check your email",
    "verification link",
    "confirm your email",
    "not been verified",
    "activate your account",
    "email activation",
    "please verify",
    "account activation",
    "email not verified",
    "confirm your account",
]

_ADVANCE_LABELS = (
    "Save and Continue",
    "Save & Continue",
    "Next",
    "Continue",
    "Next Step",
    "Proceed",
    "Save",
    "Finish",
    "Next Section",
)

_SUBMIT_LABELS = ("Submit", "Submit Application", "Apply Now", "Complete Application")

_FILE_SELECTORS = [
    "input[type='file']",
    "input[accept*='pdf']",
    "input[accept*='.pdf']",
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_domain(url: str) -> str:
    """Extract hostname from a URL for credential keying."""
    try:
        return urlparse(url).hostname or url
    except Exception:
        return url


def _parse_field_plan(response: str) -> list[dict]:
    """Parse LLM JSON response into a list of field-fill instructions.

    Returns list of dicts with keys ``label``, ``field_type``, ``value``.
    Returns [] on any parse or validation failure.
    """
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner)
    try:
        data = json.loads(text)
        if not isinstance(data, list):
            logger.debug("_parse_field_plan: response is not a JSON array")
            return []
        result = []
        for item in data:
            if (
                isinstance(item, dict)
                and "label" in item
                and "field_type" in item
                and "value" in item
            ):
                result.append(item)
        return result
    except Exception as exc:
        logger.debug(f"_parse_field_plan: JSON parse failed: {exc} — response: {text[:200]}")
        return []


def _generate_password(length: int = 20) -> str:
    """Generate a random password with letters, digits, and punctuation."""
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    # Ensure at least one of each category
    pw = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%&*"),
    ]
    pw += [secrets.choice(alphabet) for _ in range(length - 4)]
    secrets.SystemRandom().shuffle(pw)
    return "".join(pw)


# ── FormFillingAgent ───────────────────────────────────────────────────────────

class FormFillingAgent(BaseApplicator):
    """
    Universal applicator for any ATS platform (Workday, Greenhouse, Lever, etc.).

    Strategy:
    1. Navigate to the external apply URL.
    2. Detect auth walls and handle guest/login/account-creation flows.
    3. Loop through form pages:
       a. Snapshot AX tree → format_interactive_fields()
       b. LLM planner generates fill plan from fields + profile
       c. Execute plan items via ARIA locators with multi-strategy fallbacks
       d. Upload resume if file input detected
       e. Click Next/Continue/Submit
    4. Confirm submission via text matching + Vision fallback.
    """

    def __init__(
        self,
        page: Page,
        llm: ClaudeClient,
        profile: UserProfile,
        resume_path: Path,
        vault: Optional[CredentialVault] = None,
        cred_repo: Optional[CredentialRepo] = None,
        vision: Optional[VisionAnalyzer] = None,
        review_mode: bool = False,
        qa_cache=None,
        gmail=None,
    ) -> None:
        super().__init__(page, llm, profile, vision, review_mode, qa_cache)
        self._resume_path = resume_path
        self._vault = vault
        self._cred_repo = cred_repo
        self._gmail = gmail

    # ── Main entry point ──────────────────────────────────────────────────────

    async def apply(self, job: Job, application: Application) -> bool:
        """Navigate to the application page and attempt to fill + submit."""
        self._job = job
        self.failure_reason = None
        self.logger.info(f"FormFillingAgent: {job.title} @ {job.company}")

        url = job.external_url
        if not url:
            return self._fail("No external URL for application", level="error")

        try:
            await self._page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeout:
            return self._fail(f"Timeout loading application page: {url}", level="error")

        await random_delay(2.0, 4.0)
        context = f"{job.title} at {job.company}"

        # Auth detection & handling
        if await self._looks_like_auth_page():
            if not await self._handle_auth_if_needed(url):
                if self.failure_reason:
                    return False
                return self._fail("Auth failed — cannot proceed")
            await random_delay(1.5, 3.0)

        # Ensure we are actually on an application form, not a listing/search page.
        if not await self._ensure_on_application_form(context):
            return self._fail("Not on application form (listing/search page)")

        # Main form-filling loop
        prev_url = self._page.url
        prev_heading = await self._get_page_heading()
        stuck_count = 0

        for page_num in range(1, _MAX_PAGES + 1):
            self.logger.info(f"  Page {page_num}")
            await wait_for_navigation_settle(self._page)

            # Check for submission confirmation
            if await self._confirm_submission():
                self.logger.info("Application confirmed submitted")
                return True

            # Check for auth wall mid-flow
            if page_num > 1 and await self._looks_like_auth_page():
                if not await self._handle_auth_if_needed(url):
                    if self.failure_reason:
                        return False
                    return self._fail("Auth wall encountered mid-flow")
                await random_delay(1.0, 2.0)

            # Check for email verification wall
            if await self._is_email_verification_wall():
                return self._fail("Email verification wall — needs_review")

            # Check for CAPTCHA
            state = await self._assess_current_state(context)
            if state == "captcha":
                return self._fail("CAPTCHA detected — needs_review")
            if state == "error":
                return self._fail("Error page detected")

            # Dismiss any modal dialogs
            await self._dismiss_modal()

            # Upload resume if file input is visible
            await self._upload_resume_if_needed()

            # Fill the current page
            filled = await self._fill_current_page(context)
            self.logger.info(f"  Filled {filled} fields")

            # Try to advance or submit
            submitted = await self._advance_or_submit(context)
            if submitted == "submitted":
                await random_delay(2.0, 4.0)
                if await self._confirm_submission():
                    self.logger.info("Application confirmed submitted")
                    return True
                # Avoid false positives: if form is still present, require review.
                if await self._has_form_signals():
                    return self._fail("Submit clicked but confirmation unclear — needs_review")
                self.logger.info("Submit clicked and form disappeared (implicit success)")
                return True

            # Stuck detection
            advanced, _ = await self._detect_page_change(prev_url, prev_heading)
            if not advanced:
                stuck_count += 1
                self.logger.warning(f"Page did not change (stuck_count={stuck_count})")
                if stuck_count >= 3:
                    return self._fail("Stuck for 3 consecutive pages — giving up", level="error")
                # Try scrolling and filling any remaining fields
                await scroll_to_bottom(self._page, pause_s=0.5, max_scrolls=3)
                await self._fill_current_page(context)
                await self._advance_or_submit(context)
            else:
                stuck_count = 0

            prev_url = self._page.url
            prev_heading = await self._get_page_heading()
            await random_delay(1.0, 2.5)

        return self._fail(f"Exceeded {_MAX_PAGES} pages — giving up")

    def _fail(self, reason: str, level: str = "warning") -> bool:
        """Record a machine-readable failure reason and emit a single log line."""
        self.failure_reason = reason
        if level == "error":
            self.logger.error(reason)
        else:
            self.logger.warning(reason)
        return False

    # ── Page state assessment ─────────────────────────────────────────────────

    async def _assess_current_state(self, context: str) -> str:
        """Classify the current page as form/captcha/error/submitted."""
        # Quick text checks first
        try:
            content = (await self._page.content()).lower()
            if any(phrase in content for phrase in _CONFIRM_TEXTS):
                return "submitted"
            if await self._has_captcha_markers(content):
                return "captcha"
        except Exception:
            pass

        # Vision fallback for ambiguous pages
        if self._vision:
            try:
                screenshot = await screenshot_page(self._page)
                b64 = image_to_base64(screenshot)
                text, _ = await self._llm.vision_message(
                    image_b64=b64,
                    prompt=(
                        f"Context: {context}\n"
                        "What state is this page in? Reply with ONE word: "
                        "form, captcha, error, submitted, or auth"
                    ),
                    purpose="form_page_assessment",
                )
                state = text.strip().lower().split()[0] if text else "form"
                if state == "captcha":
                    # Vision-only captcha guesses are noisy; require concrete page markers.
                    if await self._has_captcha_markers():
                        return "captcha"
                    return "form"
                if state in ("form", "error", "submitted", "auth"):
                    return state
            except Exception:
                pass

        return "form"

    async def _has_captcha_markers(self, content: Optional[str] = None) -> bool:
        """Detect captcha using concrete DOM/text markers."""
        lowered = content
        if lowered is None:
            try:
                lowered = (await self._page.content()).lower()
            except Exception:
                lowered = ""
        if any(
            token in lowered
            for token in (
                "g-recaptcha",
                "hcaptcha",
                "captcha challenge",
                "i'm not a robot",
                "i am not a robot",
                "cf-turnstile",
            )
        ):
            return True
        selectors = (
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            "iframe[src*='turnstile']",
            "[class*='captcha']",
            "[id*='captcha']",
        )
        for sel in selectors:
            try:
                loc = self._page.locator(sel)
                count = await loc.count()
                for i in range(min(count, 3)):
                    try:
                        if await loc.nth(i).is_visible(timeout=250):
                            return True
                    except Exception:
                        continue
            except Exception:
                pass
        return False

    # ── Form filling ──────────────────────────────────────────────────────────

    async def _fill_current_page(self, context: str) -> int:
        """AX tree → LLM plan → execute. Returns count of fields filled."""
        tree = await get_ax_tree(self._page)
        field_summary = format_interactive_fields(tree) if tree else ""
        self.logger.info(
            f"AX fields ({len(field_summary.splitlines()) if field_summary else 0}): "
            f"tree={'ok' if tree else 'None'} | {field_summary[:200]!r}"
        )

        if not field_summary:
            # Vision fallback
            if self._vision:
                try:
                    vis_desc = await self._vision.analyze_page(
                        self._page,
                        "List every form field, radio button group, and dropdown "
                        "on this page. For each radio group, include the question "
                        "text and available options. Format: "
                        "'radiogroup: <question> | options: <opt1>, <opt2>'",
                        context,
                    )
                    if vis_desc:
                        self.logger.info(f"Using Vision field description: {vis_desc[:150]!r}")
                        field_summary = vis_desc
                    else:
                        self.logger.debug("Vision found no fields — page may be read-only")
                        return 0
                except Exception as e:
                    self.logger.debug(f"Vision fallback failed: {e}")
                    return 0
            else:
                return 0

        plan = await self._plan_section(field_summary, context)
        if not plan:
            # Even without a plan, try scanning radiogroups directly
            return await self._scan_radiogroups(context)

        filled = 0
        for item in plan:
            label = item.get("label", "")
            field_type = item.get("field_type", "text")
            value = item.get("value", "")
            if not label or not value:
                continue
            try:
                filled += await self._fill_field(label, field_type, value)
            except Exception as e:
                self.logger.debug(f"Plan item failed (label={label!r}): {e}")

        # Also scan radiogroups that the planner may have missed
        filled += await self._scan_radiogroups(context)
        self.logger.info(f"Page fill: {filled} fields filled")
        return filled

    async def _plan_section(self, field_summary: str, context: str) -> list[dict]:
        """LLM call with field list + profile → structured fill plan."""
        profile_summary = self._build_profile_summary()
        prompt = (
            f"Job: {context}\n\n"
            f"Form fields:\n{field_summary}\n\n"
            f"Candidate profile:\n{profile_summary}\n\n"
            f"Fill the form fields with the best values for this candidate and role."
        )
        try:
            text, _usage = await self._llm.message(
                prompt,
                system=_SECTION_PLAN_SYSTEM,
                purpose="form_section_plan",
            )
            return _parse_field_plan(text)
        except Exception as e:
            self.logger.debug(f"Section planner LLM call failed: {e}")
            return []

    def _build_profile_summary(self) -> str:
        """Build a compact profile summary for the section planner."""
        p = self._profile.personal
        aa = self._profile.application_answers
        skills = self._profile.skills

        name = f"{p.first_name} {p.last_name}"
        location = p.location or ""
        work_auth = getattr(p, "work_authorization", "US Citizen")
        years = getattr(aa, "years_of_experience", "")
        salary = getattr(aa, "desired_salary", "")
        relocate = "Yes" if getattr(p, "willing_to_relocate", False) else "No"
        sponsor = "Yes" if getattr(aa, "sponsorship_required", False) else "No"

        certs = getattr(skills, "certifications", [])
        cert_str = ", ".join(c.name for c in certs[:5]) if certs else "None"

        domains = getattr(skills, "domains", [])
        top_domains = ", ".join(
            f"{d.name} ({d.years}yr)" for d in (domains[:5] if domains else [])
        )

        return (
            f"Name: {name} | Location: {location} | Auth: {work_auth}\n"
            f"Years exp: {years} | Salary: {salary} | Relocate: {relocate} | Sponsor: {sponsor}\n"
            f"Certs: {cert_str}\n"
            f"Skills: {top_domains}"
        )

    # ── Field filling ─────────────────────────────────────────────────────────

    async def _fill_field(self, label: str, field_type: str, value: str) -> int:
        """Multi-strategy field locator + filler. Returns 1 on success, 0 on failure."""
        label_norm = re.sub(r"\[[^\]]+\]", "", label).replace("*", "").strip()
        if not label_norm:
            label_norm = label
        pattern = re.compile(re.escape(label_norm), re.IGNORECASE)

        # Strategy 1: find_by_aria_label
        locator = await find_by_aria_label(
            self._page,
            pattern,
            roles=("textbox", "combobox", "listbox", "checkbox", "radio", "radiogroup", "group"),
        )

        # Strategy 2: get_by_label
        if locator is None:
            try:
                locator = self._page.get_by_label(label_norm, exact=False)
                await locator.wait_for(state="visible", timeout=2_000)
            except Exception:
                locator = None
                if field_type not in ("select", "combobox", "dropdown", "radio", "radiogroup"):
                    self.logger.debug(f"Could not locate field {label!r}")
                    return 0

        try:
            if field_type == "text":
                await locator.fill(value)
                await micro_delay()
                return 1

            elif field_type in ("select", "combobox", "dropdown"):
                return await self._fill_select_field(locator, label, label_norm, field_type, value)

            elif field_type in ("radio", "radiogroup"):
                return await self._fill_radio_field(locator, label, label_norm, value)

            elif field_type == "checkbox":
                if value.lower() in ("yes", "true", "1"):
                    await locator.check()
                    await micro_delay()
                    return 1

        except Exception as e:
            self.logger.debug(f"_fill_field({label!r}, {field_type!r}): {e}")
        return 0

    async def _fill_select_field(
        self, locator, label: str, label_norm: str, field_type: str, value: str
    ) -> int:
        """Handle select/combobox/dropdown fields with multiple fallback strategies."""
        # Approach 1: native <select>
        if locator is not None:
            try:
                tag = await locator.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    await locator.select_option(label=value)
                    await micro_delay()
                    return 1
            except Exception:
                pass

            # Approach 2: click to open → click role='option'
            try:
                await locator.click()
                await micro_delay()
                opt = self._page.get_by_role("option", name=value, exact=False)
                if await opt.count() > 0:
                    await opt.first.click()
                    await micro_delay()
                    return 1
            except Exception:
                pass

            # Approach 3: typeahead for combobox
            if field_type == "combobox":
                try:
                    el = await locator.element_handle()
                    if el and await self._fill_typeahead(el, value):
                        return 1
                except Exception:
                    pass

            # Approach 4: ancestor container walk → click option
            try:
                el = await locator.element_handle()
                if el:
                    node = el
                    for _ in range(3):
                        node = await node.query_selector("xpath=..")
                        if node:
                            await node.click()
                            await micro_delay()
                            opt = self._page.get_by_role("option", name=value, exact=False)
                            if await opt.count() > 0:
                                await opt.first.click()
                                await micro_delay()
                                return 1
            except Exception:
                pass

            # Approach 5: native select_option via aria-label
            if await select_option(
                self._page,
                [f"[aria-label='{label}']", f"[aria-label='{label_norm}']"],
                value,
            ):
                return 1

            # Approach 6: typeahead as last resort
            try:
                el = await locator.element_handle()
                if el and await self._fill_typeahead(el, value):
                    return 1
            except Exception:
                pass

        # Approach 7: text-proximity — find label text, walk to <select> ancestor
        try:
            q_els = self._page.get_by_text(label_norm[:60], exact=False)
            q_count = await q_els.count()
            for i in range(min(q_count, 3)):
                try:
                    q_el = q_els.nth(i)
                    anc = q_el.locator("xpath=ancestor::*[.//select][1]")
                    if await anc.count() > 0:
                        sel = anc.first.locator("select").first
                        await sel.select_option(label=value)
                        await micro_delay()
                        return 1
                except Exception:
                    continue
        except Exception:
            pass

        # Approach 8: text-proximity — click button near label, click option
        try:
            q_els = self._page.get_by_text(label_norm[:60], exact=False)
            q_count = await q_els.count()
            for i in range(min(q_count, 3)):
                try:
                    q_el = q_els.nth(i)
                    anc = q_el.locator("xpath=ancestor::*[.//button][1]")
                    if await anc.count() > 0:
                        btn = anc.first.locator("button").first
                        await btn.click()
                        await micro_delay()
                        opt = self._page.get_by_role("option", name=value, exact=False)
                        if await opt.count() > 0:
                            await opt.first.click()
                            await micro_delay()
                            return 1
                except Exception:
                    continue
        except Exception:
            pass

        # Approach 9: text-proximity — walk to combobox/listbox ancestor
        try:
            q_els = self._page.get_by_text(label_norm[:60], exact=False)
            q_count = await q_els.count()
            for i in range(min(q_count, 3)):
                try:
                    q_el = q_els.nth(i)
                    anc = q_el.locator(
                        "xpath=ancestor::*[.//*[@role='combobox' or @role='listbox']][1]"
                    )
                    if await anc.count() > 0:
                        combo = anc.first.locator(
                            "[role='combobox'], [role='listbox']"
                        ).first
                        await combo.click()
                        await micro_delay()
                        opt = self._page.get_by_role("option", name=value, exact=False)
                        if await opt.count() > 0:
                            await opt.first.click()
                            await micro_delay()
                            return 1
                except Exception:
                    continue
        except Exception:
            pass

        return 0

    async def _fill_radio_field(self, locator, label: str, label_norm: str, value: str) -> int:
        """Handle radio/radiogroup fields with multiple fallback strategies."""
        # Approach 1: named radiogroup
        try:
            group_loc = self._page.get_by_role("radiogroup", name=label, exact=False).first
            await group_loc.wait_for(state="visible", timeout=1_500)
            opt = group_loc.get_by_role("radio", name=value, exact=False).first
            await opt.click()
            await micro_delay()
            return 1
        except Exception:
            pass

        # Approach 2: filter radiogroup/group containing question text
        label_snippet = label[:60]
        for rg_role in ("[role='radiogroup']", "[role='group']"):
            try:
                container = self._page.locator(rg_role).filter(has_text=label_snippet)
                if await container.count() > 0:
                    opt = container.first.get_by_role("radio", name=value, exact=False)
                    if await opt.count() > 0:
                        await opt.first.click()
                        await micro_delay()
                        return 1
            except Exception:
                pass

        # Approach 3: XPath ancestor — find text node, walk up to radiogroup
        try:
            text_loc = self._page.get_by_text(label[:50], exact=False).first
            rg = text_loc.locator("xpath=ancestor::*[@role='radiogroup'][1]")
            if await rg.count() > 0:
                opt = rg.get_by_role("radio", name=value, exact=False)
                await opt.first.click()
                await micro_delay()
                return 1
        except Exception:
            pass

        # Approach 4: broad search by value name only (last resort)
        try:
            opt = self._page.get_by_role("radio", name=value, exact=False)
            await opt.first.click()
            await micro_delay()
            return 1
        except Exception:
            pass

        return 0

    async def _fill_typeahead(self, input_el, answer: str) -> bool:
        """Type text into a typeahead input and click the first matching suggestion."""
        try:
            await input_el.triple_click()
            await input_el.type(answer[:80], delay=40)
            await random_delay(0.5, 1.0)

            suggestion_sel = "[role='option'], li[role='option']"
            try:
                await self._page.wait_for_selector(
                    suggestion_sel, state="visible", timeout=3_000
                )
            except Exception:
                self.logger.debug(f"No typeahead suggestions for {answer!r}")
                return True

            suggestions = await self._page.query_selector_all(suggestion_sel)
            answer_lower = answer.lower()

            # Exact match first
            for sug in suggestions:
                text = (await sug.inner_text()).strip().lower()
                if text == answer_lower:
                    await sug.click()
                    await micro_delay()
                    return True

            # Partial match
            for sug in suggestions:
                text = (await sug.inner_text()).strip().lower()
                if answer_lower in text or text in answer_lower:
                    await sug.click()
                    await micro_delay()
                    return True

            # No match — click first suggestion
            if suggestions:
                self.logger.debug(f"No exact typeahead match for {answer!r} — using first")
                await suggestions[0].click()
                await micro_delay()
                return True

            return False
        except Exception as e:
            self.logger.debug(f"Typeahead fill failed: {e}")
            return False

    async def _scan_radiogroups(self, context: str) -> int:
        """Broad scan for radiogroups on the page, answer via profile/Claude pipeline."""
        radiogroups = await self._page.query_selector_all(
            "[role='radiogroup']"
        )
        if not radiogroups:
            return 0

        self.logger.info(f"Radiogroup scan: {len(radiogroups)} groups found")
        filled = 0
        for rg in radiogroups:
            try:
                radios = await rg.query_selector_all("[role='radio'], input[type='radio']")
                if not radios:
                    continue

                # Determine question label
                question = ""
                aria_lb = await rg.get_attribute("aria-labelledby") or ""
                if aria_lb:
                    try:
                        lbl_el = await self._page.query_selector(f"#{aria_lb.split()[0]}")
                        if lbl_el:
                            question = (await lbl_el.inner_text()).strip()
                    except Exception:
                        pass
                if not question:
                    question = (await rg.get_attribute("aria-label") or "").strip()
                if not question:
                    try:
                        parent_text = await rg.evaluate(
                            """rg => {
                                const parent = rg.parentElement;
                                if (!parent) return '';
                                let prev = rg.previousElementSibling;
                                while (prev) {
                                    const t = prev.textContent.trim();
                                    if (t && t.length < 300) return t;
                                    prev = prev.previousElementSibling;
                                }
                                for (const child of parent.children) {
                                    if (child === rg) break;
                                    const t = child.textContent.trim();
                                    if (t && t.length < 300) return t;
                                }
                                return '';
                            }"""
                        )
                        question = (parent_text or "").strip()
                    except Exception:
                        pass
                if not question:
                    continue

                # Collect option labels
                options = []
                for r in radios:
                    aria_lbl = (await r.get_attribute("aria-label") or "").strip()
                    if aria_lbl:
                        options.append(aria_lbl)
                    else:
                        try:
                            inner = (await r.inner_text()).strip()
                            if inner:
                                options.append(inner)
                        except Exception:
                            pass
                if not options:
                    continue

                answer = await self.answer_question(question, "radio", options, context)
                self.record_qa(question, answer)
                self._write_qa_cache(question, options, "radio", answer)
                if answer.answer:
                    for r in radios:
                        aria_lbl = (await r.get_attribute("aria-label") or "").strip()
                        try:
                            inner_text = (await r.inner_text()).strip()
                        except Exception:
                            inner_text = ""
                        candidate = aria_lbl or inner_text
                        if candidate.lower() == answer.answer.lower():
                            await r.click()
                            await micro_delay()
                            break
                    filled += 1
            except Exception as e:
                self.logger.debug(f"Radiogroup scan error: {e}")

        return filled

    # ── Navigation ────────────────────────────────────────────────────────────

    async def _advance_or_submit(self, context: str) -> Optional[str]:
        """Click Next/Continue/Submit. Returns 'submitted' if submit was clicked, else None."""
        # Check for Submit button first
        for text in _SUBMIT_LABELS:
            try:
                btn = self._page.get_by_role("button", name=re.compile(text, re.IGNORECASE)).first
                if await btn.is_visible(timeout=1_000):
                    job = getattr(self, "_job", None)
                    title = job.title if job else "Unknown"
                    company = job.company if job else "Unknown"
                    if not await self._pause_for_review(title, company):
                        return None
                    await btn.click()
                    await random_delay(2.0, 4.0)
                    return "submitted"
            except Exception:
                pass

        # Try Next/Continue buttons
        for text in _ADVANCE_LABELS:
            try:
                btn = self._page.get_by_role("button", name=re.compile(text, re.IGNORECASE)).first
                if await btn.is_visible(timeout=1_000):
                    await btn.click()
                    await random_delay(1.0, 2.0)
                    return "advanced"
            except Exception:
                pass

        # Try link-based navigation (some ATS use <a> instead of <button>)
        for text in _ADVANCE_LABELS:
            try:
                link = self._page.get_by_role("link", name=re.compile(text, re.IGNORECASE)).first
                if await link.is_visible(timeout=1_000):
                    await link.click()
                    await random_delay(1.0, 2.0)
                    return "advanced"
            except Exception:
                pass

        return None

    async def _detect_page_change(
        self, prev_url: str, prev_heading: str, timeout: float = 4.0
    ) -> tuple[bool, str]:
        """Poll for URL or heading change to detect page advancement."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            current_url = self._page.url
            current_heading = await self._get_page_heading()
            if current_url != prev_url or current_heading != prev_heading:
                return True, current_heading
            await asyncio.sleep(0.4)
        return False, prev_heading

    async def _get_page_heading(self) -> str:
        """Get the first visible heading on the page."""
        for level in ("h1", "h2", "h3"):
            try:
                heading = self._page.locator(level).first
                if await heading.is_visible(timeout=500):
                    return (await heading.inner_text()).strip()
            except Exception:
                pass
        return ""

    # ── Auth handling ─────────────────────────────────────────────────────────

    async def _looks_like_auth_page(self) -> bool:
        """Check if the current page is an auth wall."""
        try:
            content = (await self._page.content()).lower()
            has_auth_text = any(kw in content for kw in _AUTH_KEYWORDS)
            if not has_auth_text:
                return False

            has_password_field = False
            try:
                has_password_field = await self._page.locator("input[type='password']").count() > 0
            except Exception:
                pass

            has_auth_controls = await self._has_auth_controls()

            # If page looks like an application form and has no password/auth controls,
            # avoid false auth classification (common on Lever/Oracle pages).
            has_form_signals = await self._has_form_signals()
            if has_form_signals and not has_password_field and not has_auth_controls:
                return False

            return has_password_field or has_auth_controls
        except Exception:
            return False

    async def _has_auth_controls(self) -> bool:
        """Detect visible auth-specific buttons/links."""
        for role in ("button", "link"):
            try:
                el = self._page.get_by_role(role, name=_AUTH_CONTROL_PATTERN).first
                if await el.is_visible(timeout=400):
                    return True
            except Exception:
                pass
        return False

    async def _handle_auth_if_needed(self, url: str) -> bool:
        """Try guest flow → stored login → account creation."""
        domain = _extract_domain(url)

        if await self._looks_like_sso_only_page():
            return self._fail("SSO-only auth wall — needs_review")

        # Try guest flow first
        for guest_label in _GUEST_LABELS:
            try:
                btn = self._page.get_by_role(
                    "button", name=re.compile(re.escape(guest_label), re.IGNORECASE)
                ).first
                if await btn.is_visible(timeout=1_500):
                    self.logger.info(f"Guest flow: clicking '{guest_label}'")
                    await btn.click()
                    await random_delay(1.5, 3.0)
                    if await self._wait_for_post_auth_transition():
                        return True
                    self.logger.info("Guest click did not clear auth wall")
            except Exception:
                pass
            # Also try links
            try:
                link = self._page.get_by_role(
                    "link", name=re.compile(re.escape(guest_label), re.IGNORECASE)
                ).first
                if await link.is_visible(timeout=1_000):
                    self.logger.info(f"Guest flow (link): clicking '{guest_label}'")
                    await link.click()
                    await random_delay(1.5, 3.0)
                    if await self._wait_for_post_auth_transition():
                        return True
                    self.logger.info("Guest link click did not clear auth wall")
            except Exception:
                pass

        # Try stored credentials
        email = self._profile.personal.email
        if self._vault and self._cred_repo:
            cred = self._cred_repo.get(domain, email)
            if cred:
                self.logger.info(f"Found stored credentials for {domain}")
                password = self._vault.decrypt(cred.password)
                if await self._try_login(email, password) and await self._wait_for_post_auth_transition():
                    return True

        # Try account creation
        if self._vault and self._cred_repo:
            if await self._try_create_account(domain, email) and await self._wait_for_post_auth_transition():
                return True

        return False

    async def _looks_like_sso_only_page(self) -> bool:
        """Detect auth pages that likely require enterprise SSO and cannot be automated."""
        try:
            content = (await self._page.content()).lower()
        except Exception:
            return False
        has_sso = any(kw in content for kw in _SSO_KEYWORDS)
        if not has_sso:
            return False
        has_password_field = False
        try:
            has_password_field = await self._page.locator("input[type='password']").count() > 0
        except Exception:
            pass
        has_auth_controls = await self._has_auth_controls()
        if not has_auth_controls:
            return False
        has_form_signals = await self._has_form_signals()
        if has_form_signals and not has_password_field:
            return False
        # SSO wording with no native password field is usually non-automatable here.
        return has_sso and not has_password_field

    async def _wait_for_post_auth_transition(self, timeout_s: float = 8.0) -> bool:
        """Return True only after leaving auth wall and reaching possible form state."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if not await self._looks_like_auth_page():
                return True
            await asyncio.sleep(0.4)
        return False

    async def _has_form_signals(self) -> bool:
        """Heuristic: page appears to be an application form, not a listing shell."""
        try:
            if await self._page.locator("input[type='file']").count() > 0:
                return True
        except Exception:
            pass

        selectors = [
            "input:not([type='hidden'])",
            "textarea",
            "select",
            "button[type='submit']",
        ]
        for sel in selectors:
            try:
                loc = self._page.locator(sel)
                count = await loc.count()
                for i in range(min(count, 4)):
                    if await loc.nth(i).is_visible(timeout=250):
                        return True
            except Exception:
                pass

        for label in (*_ADVANCE_LABELS, *_SUBMIT_LABELS, "Apply", "Apply Now", "Start Application"):
            try:
                btn = self._page.get_by_role("button", name=re.compile(re.escape(label), re.IGNORECASE)).first
                if await btn.is_visible(timeout=250):
                    return True
            except Exception:
                pass
        return False

    async def _ensure_on_application_form(self, context: str) -> bool:
        """Try to enter form flow once if currently on a listing/detail page."""
        if await self._has_form_signals():
            return True

        apply_labels = (
            "Apply Now",
            "Apply for this job",
            "Start Application",
            "Apply",
        )
        for label in apply_labels:
            pattern = re.compile(re.escape(label), re.IGNORECASE)
            for role in ("button", "link"):
                try:
                    el = self._page.get_by_role(role, name=pattern).first
                    if await el.is_visible(timeout=800):
                        self.logger.info(f"Preflight: clicking {role} '{label}' to enter form")
                        await el.click()
                        await random_delay(1.5, 3.0)
                        if await self._looks_like_auth_page():
                            if not await self._handle_auth_if_needed(self._page.url):
                                return False
                        if await self._has_form_signals():
                            return True
                except Exception:
                    pass

        # Non-ARIA fallback: some ATS render CTA as plain div/span.
        for sel in (
            "text=/apply now/i",
            "text=/submit your application/i",
            "text=/start application/i",
            "button:has-text('APPLY NOW')",
            "button:has-text('Apply Now')",
        ):
            try:
                el = self._page.locator(sel).first
                if await el.is_visible(timeout=800):
                    self.logger.info(f"Preflight fallback: clicking selector {sel!r}")
                    await el.click()
                    await random_delay(1.5, 3.0)
                    if await self._looks_like_auth_page():
                        if not await self._handle_auth_if_needed(self._page.url):
                            return False
                    if await self._has_form_signals():
                        return True
            except Exception:
                pass

        # Vision fallback: detect obvious listing/search pages and fail fast.
        state = await self._assess_current_state(context)
        if state == "form" and await self._has_form_signals():
            return True
        return False

    async def _try_login(self, email: str, password: str) -> bool:
        """Fill email + password by label, click submit."""
        try:
            # Click Sign In link/button first if visible
            for sign_in_text in ("Sign In", "Log In", "Login", "Continue with Email"):
                try:
                    btn = self._page.get_by_role(
                        "button", name=sign_in_text, exact=False
                    ).first
                    if await btn.is_visible(timeout=1_000):
                        await btn.click()
                        await random_delay(1.0, 2.0)
                        break
                except Exception:
                    pass
                try:
                    link = self._page.get_by_role(
                        "link", name=sign_in_text, exact=False
                    ).first
                    if await link.is_visible(timeout=1_000):
                        await link.click()
                        await random_delay(1.0, 2.0)
                        break
                except Exception:
                    pass

            # Fill email
            email_filled = await self._fill_login_email(email)
            if not email_filled:
                return False

            # Email-first flows often require an intermediate Continue/Next click
            await self._click_login_continue_if_present()

            # Fill password
            password_filled = await self._fill_login_password(password)
            if not password_filled:
                return False

            await micro_delay()

            # Click submit
            for submit_text in ("Sign In", "Log In", "Login", "Submit"):
                try:
                    btn = self._page.get_by_role("button", name=submit_text, exact=False).first
                    if await btn.is_visible(timeout=1_000):
                        await btn.click()
                        await random_delay(2.0, 4.0)
                        return True
                except Exception:
                    pass

            # Fallback: button[type='submit']
            try:
                btn = self._page.locator("button[type='submit']").first
                if await btn.is_visible(timeout=1_000):
                    await btn.click()
                    await random_delay(2.0, 4.0)
                    return True
            except Exception:
                pass

            return False
        except Exception as e:
            self.logger.debug(f"Login attempt failed: {e}")
            return False

    async def _fill_login_email(self, email: str) -> bool:
        """Fill login email/username field using label + selector fallbacks."""
        for label in (
            "Email",
            "Email Address",
            "Work Email",
            "Username",
            "Email or Username",
            "User ID",
            "Login ID",
        ):
            try:
                field = self._page.get_by_label(label, exact=False).first
                if await field.is_visible(timeout=700):
                    await field.fill(email)
                    return True
            except Exception:
                pass

        selector_candidates = [
            "input[type='email']",
            "input[name*='email' i]",
            "input[id*='email' i]",
            "input[name*='user' i]",
            "input[id*='user' i]",
            "input[name*='login' i]",
            "input[id*='login' i]",
            "input[name*='identifier' i]",
        ]
        for sel in selector_candidates:
            try:
                inp = self._page.locator(sel).first
                if await inp.is_visible(timeout=700):
                    await inp.fill(email)
                    return True
            except Exception:
                pass
        return False

    async def _fill_login_password(self, password: str) -> bool:
        """Fill login password field using label + selector fallbacks."""
        for label in ("Password", "Current Password", "Passcode"):
            try:
                field = self._page.get_by_label(label, exact=False).first
                if await field.is_visible(timeout=700):
                    await field.fill(password)
                    return True
            except Exception:
                pass
        try:
            inp = self._page.locator("input[type='password']").first
            if await inp.is_visible(timeout=700):
                await inp.fill(password)
                return True
        except Exception:
            pass
        return False

    async def _click_login_continue_if_present(self) -> None:
        """Click intermediate continue buttons used in email-first auth flows."""
        for label in ("Continue", "Next", "Proceed", "Verify", "Continue with Email"):
            try:
                btn = self._page.get_by_role("button", name=label, exact=False).first
                if await btn.is_visible(timeout=700):
                    await btn.click()
                    await random_delay(0.8, 1.6)
                    return
            except Exception:
                pass
        try:
            btn = self._page.locator("button[type='submit']").first
            if await btn.is_visible(timeout=500):
                txt = (await btn.inner_text()).strip().lower()
                if txt in ("continue", "next"):
                    await btn.click()
                    await random_delay(0.8, 1.6)
        except Exception:
            pass

    async def _try_create_account(self, domain: str, email: str) -> bool:
        """Create account with email subaddressing, store encrypted credentials."""
        # Click Create Account button
        create_clicked = False
        for create_text in ("Create Account", "Create an Account", "Register", "Sign Up"):
            try:
                btn = self._page.get_by_role("button", name=create_text, exact=False).first
                if await btn.is_visible(timeout=1_500):
                    await btn.click()
                    await random_delay(1.5, 3.0)
                    create_clicked = True
                    break
            except Exception:
                pass
            try:
                link = self._page.get_by_role("link", name=create_text, exact=False).first
                if await link.is_visible(timeout=1_000):
                    await link.click()
                    await random_delay(1.5, 3.0)
                    create_clicked = True
                    break
            except Exception:
                pass

        if not create_clicked:
            return False

        # Generate subaddressed email and password
        tag = domain.split(".")[0][:12]
        local, at_domain = email.split("@", 1)
        sub_email = f"{local}+{tag}@{at_domain}"
        password = _generate_password(20)

        # Fill email
        for label in ("Email", "Email Address"):
            try:
                field = self._page.get_by_label(label, exact=False)
                if await field.is_visible(timeout=1_000):
                    await field.fill(sub_email)
                    break
            except Exception:
                pass

        # Fill password + verify password
        pw_fields = await self._page.locator("input[type='password']").all()
        for pw_field in pw_fields:
            try:
                if await pw_field.is_visible(timeout=500):
                    await pw_field.fill(password)
            except Exception:
                pass

        await micro_delay()

        # Click submit
        for submit_text in ("Create Account", "Register", "Sign Up", "Submit"):
            try:
                btn = self._page.get_by_role("button", name=submit_text, exact=False).first
                if await btn.is_visible(timeout=1_000):
                    await btn.click()
                    await random_delay(2.0, 4.0)
                    break
            except Exception:
                pass

        # Store credentials
        try:
            encrypted = self._vault.encrypt(password)
            cred = Credential(domain=domain, username=sub_email, password=encrypted)
            self._cred_repo.upsert(cred)
            self.logger.info(f"Stored new credentials for {domain} ({sub_email})")
        except Exception as e:
            self.logger.warning(f"Failed to store credentials: {e}")

        return True

    # ── Resume upload ─────────────────────────────────────────────────────────

    async def _upload_resume_if_needed(self) -> None:
        """Find file input and upload resume."""
        for sel in _FILE_SELECTORS:
            try:
                el = self._page.locator(sel).first
                if await el.is_visible(timeout=1_000):
                    await el.set_input_files(str(self._resume_path))
                    await random_delay(2.0, 3.0)
                    self.logger.info("Resume uploaded")
                    return
            except Exception:
                pass

        # Try clicking an Upload button to reveal a file input
        try:
            upload_btn = self._page.get_by_role(
                "button", name=re.compile("upload|attach|resume", re.IGNORECASE)
            ).first
            if await upload_btn.is_visible(timeout=1_000):
                await upload_btn.click()
                await random_delay(1.0, 2.0)
                # Check for file input again
                for sel in _FILE_SELECTORS:
                    try:
                        el = self._page.locator(sel).first
                        await el.set_input_files(str(self._resume_path))
                        await random_delay(2.0, 3.0)
                        self.logger.info("Resume uploaded after clicking upload button")
                        return
                    except Exception:
                        pass
        except Exception:
            pass

    # ── Modal dismissal ───────────────────────────────────────────────────────

    async def _dismiss_modal(self) -> bool:
        """Detect role='dialog' and dismiss it."""
        try:
            dialog = self._page.locator("[role='dialog']").first
            if not await dialog.is_visible(timeout=1_000):
                return False
        except Exception:
            return False

        self.logger.info("Modal dialog detected — dismissing")
        for btn_text in ("OK", "Close", "Got it", "Dismiss", "Continue"):
            try:
                btn = self._page.get_by_role("button", name=btn_text, exact=False).first
                if await btn.is_visible(timeout=1_000):
                    await btn.click()
                    await random_delay(0.5, 1.0)
                    return True
            except Exception:
                pass

        # Last resort: Escape key
        await self._page.keyboard.press("Escape")
        await micro_delay()
        return True

    # ── Submission confirmation ───────────────────────────────────────────────

    async def _confirm_submission(self) -> bool:
        """Return True if the page shows a submission confirmation."""
        try:
            page_text = (await self._page.content()).lower()
            if any(phrase in page_text for phrase in _CONFIRM_TEXTS):
                return True
        except Exception:
            pass

        if self._vision:
            try:
                answer = await self._vision.analyze_page(
                    self._page,
                    "Was the job application successfully submitted? Reply with just 'yes' or 'no'.",
                )
                return answer.strip().lower().startswith("y")
            except Exception:
                pass

        return False

    async def _is_email_verification_wall(self) -> bool:
        """Return True if the page is asking for email verification."""
        try:
            content = (await self._page.content()).lower()
            return any(phrase in content for phrase in _VERIFY_EMAIL_INDICATORS)
        except Exception:
            return False
