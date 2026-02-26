"""
Workday ATS applicator — account creation, login, and form navigation.

Workday portals are heavy JavaScript SPAs that follow a predictable multi-step
structure. This applicator handles the common sections: My Information,
My Experience, Documents (resume upload), Application Questions, and
Self-Identification. It stores encrypted credentials per domain so repeat
applications reuse existing accounts.
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from patchright.async_api import Page, TimeoutError as PlaywrightTimeout

from ..browser.accessibility import find_by_aria_label, format_interactive_fields, get_ax_tree, search_ax_tree
from ..browser.helpers import (
    fill_field,
    is_visible,
    scroll_to_bottom,
    select_option,
    wait_and_click,
    wait_for_navigation_settle,
)
from ..browser.stealth import micro_delay, random_delay
from ..browser.vision import VisionAnalyzer
from ..crypto.vault import CredentialVault
from ..db.models import Application, Credential, Job
from ..db.repository import CredentialRepo
from ..llm.client import ClaudeClient
from ..utils.logging import get_logger
from ..utils.profile_loader import UserProfile
from .base import BaseApplicator

logger = get_logger(__name__)

# ── Workday selector constants ─────────────────────────────────────────────────

# Primary apply button on job listing page
_APPLY_BTN = [
    "a[data-automation-id='applyButton']",
    "button[data-automation-id='applyButton']",
    "a[data-automation-id='applyNowButton']",
    # Workday entry page when URL already ends in /apply:
    # shows "Autofill with Resume" + "Apply Manually" — click the manual path
    "button[data-automation-id='applyManually']",
    "button[data-automation-id='manual-apply']",
    "a.css-1q2dra3",      # common Workday class variant
]

# Text labels tried by get_by_role before Vision fires
_APPLY_BTN_TEXT = ["Apply", "Apply Manually", "Apply Now", "Apply for this job"]

# Navigation
_NEXT_BTN = [
    "button[data-automation-id='bottom-navigation-next-button']",
    "button[data-automation-id='bottom-navigation-next-btn']",
    "button[data-automation-id='nextButton']",
    "button[data-automation-id='bottom-navigation-continue-button']",
    "button[data-automation-id='bottom-navigation-finish-button']",
    "button[data-automation-id='continueButton']",
    "button[aria-label='Next']",
    "button[aria-label='Save and Continue']",
    "button[aria-label='Continue']",
    "button[aria-label='Save & Continue']",
]
# Text labels tried via get_by_role for the Next/Continue button
_NEXT_BTN_LABELS = (
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
_SAVE_BTN = [
    "button[data-automation-id='bottom-navigation-save-button']",
    "button[data-automation-id='bottom-navigation-save-btn']",
]
_SUBMIT_BTN = [
    "button[data-automation-id='bottom-navigation-next-button'][aria-label*='Submit']",
    "button[data-automation-id='submit']",
    "button[aria-label='Submit']",
]

# Auth
_CREATE_ACCOUNT_BTN = [
    "a[data-automation-id='createAccountLink']",
    "button[data-automation-id='createAccountLink']",
    "a[data-automation-id='createAccount']",
    "button[data-automation-id='createAccount']",
    "a[href*='createAccount']",
    "a[href*='create-account']",
]
_SIGN_IN_BTN = [
    "a[data-automation-id='signInLink']",
    "button[data-automation-id='signInLink']",
    "a[data-automation-id='signIn']",
    "button[data-automation-id='signIn']",
]
# Text/role labels for Create Account buttons — used as get_by_role fallback in _handle_auth()
_CREATE_ACCOUNT_LABELS = ("Create Account", "Create an Account", "New User", "Register", "Sign Up")
_SIGN_IN_LABELS = ("Sign In", "Log In", "Login")
# Guest flow labels — tried before account creation; avoids email verification entirely
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
_EMAIL_INPUT = [
    "input[data-automation-id='email']",
    "input[data-automation-id='username']",
    "input[data-automation-id='emailAddress']",
    "input[name='email']",
    "input[name='username']",
    "input[type='email']",
    "#email",
    "#username",
]
_PASSWORD_INPUT = [
    "input[data-automation-id='password']",
    "input[data-automation-id='currentPassword']",
    "input[data-automation-id='newPassword']",
    "input[name='password']",
    "input[type='password']",
    "#password",
]
_VERIFY_PASSWORD_INPUT = [
    "input[data-automation-id='verifyPassword']",
    "input[data-automation-id='password2']",
    "input[data-automation-id='confirmPassword']",
    "input[name='verifyPassword']",
    "input[name='confirmPassword']",
]
_LOGIN_BTN = [
    "button[data-automation-id='signInSubmitButton']",
    "button[type='submit']",
]
_CREATE_ACCOUNT_SUBMIT = [
    "button[data-automation-id='createAccountSubmitButton']",
    "button[type='submit']",
]

# Form fields
_TEXT_INPUT = "input[data-automation-id='textInputBox']"
_DATE_INPUT = "input[data-automation-id='dateSectionDay'], input[data-automation-id='dateInputBox']"
_TEXTAREA = "textarea[data-automation-id='textAreaBox'], textarea"
_FILE_UPLOAD = "input[type='file']"
_RESUME_UPLOAD_AREA = [
    "div[data-automation-id='file-upload-drop-zone']",
    "button[aria-label*='resume']",
    "button[aria-label*='Upload']",
]

# Section detection
_SECTION_HEADER = "h2[data-automation-id='sectionHeader'], h2.css-1q2dra3, div.sectionTitle"
_PROGRESS_STEPS = "li[data-automation-id='progressStep']"

# Active section container — scopes field queries to the current wizard step
_SECTION_CONTAINERS = [
    "[data-automation-id='WizardTask']",
    "[data-automation-id='appContainerPanel']",
    "div[data-automation-id*='taskContent']",
    "main",
]

# Modal/popup that can appear mid-application
_POPUP_BODY = "[data-automation-id='wd-Popup-body'], [data-automation-id='promptContainer']"
_POPUP_OK = [
    "button[data-automation-id='wd-CommandButton_uic_okButton']",
    "button[aria-label='OK']",
    "button[aria-label='Close']",
    "button:has-text('OK')",
]

# Verification email wall
_VERIFY_EMAIL_INDICATORS = [
    "verify your email",
    "check your email",
    "verification link",
    "confirm your email",
    "not been verified",
    "activate your account",
    "email activation",
    "email has not been",
    "haven't verified",
    "please verify",
    "account activation",
    "email address is not verified",
    "email not verified",
    "confirm your account",
    "activate your email",
]

_MAX_SECTIONS = 15
_NAV_TIMEOUT = 30_000

# ── Planner-Actor-Validator constants ─────────────────────────────────────────

_SECTION_PLAN_SYSTEM = (
    "You fill Workday job application form sections on behalf of a candidate.\n"
    "Return ONLY a JSON array — no prose, no markdown fences.\n"
    'Each element: {"label": "<exact field label>", "field_type": "text|radiogroup|select|checkbox|combobox", "value": "<answer>"}\n'
    "Include only fields that need a value. Skip already-answered optional fields.\n"
    "For radiogroup fields: label is the question text, value is the exact option to select (e.g. 'Yes' or 'No').\n"
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

# Month number → name for Workday date dropdowns that use text month names
_MONTH_NAMES = {
    "1": "January", "2": "February", "3": "March", "4": "April",
    "5": "May", "6": "June", "7": "July", "8": "August",
    "9": "September", "10": "October", "11": "November", "12": "December",
    "01": "January", "02": "February", "03": "March", "04": "April",
    "05": "May", "06": "June", "07": "July", "08": "August",
    "09": "September",
}


def _parse_field_plan(response: str) -> list[dict]:
    """Parse LLM JSON response into a list of field-fill instructions.

    Args:
        response: Raw LLM text, optionally wrapped in markdown fences.

    Returns:
        List of dicts with keys ``label``, ``field_type``, ``value``.
        Returns [] on any parse or validation failure.
    """
    text = response.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Drop first line (``` or ```json) and last line (```)
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


class WorkdayApplicator(BaseApplicator):
    """
    Applies to jobs on Workday portals.

    Account lifecycle:
    - First visit: create account with auto-generated password, encrypt + store
    - Repeat visits: look up stored credentials, log in directly
    - Email verification walls: mark application as needs_review and abort

    Form navigation:
    - Advances through each Workday section using the Next button
    - Detects section type by header text and handles fields accordingly
    - Falls back to Vision for unrecognised layouts
    """

    def __init__(
        self,
        page: Page,
        llm: ClaudeClient,
        profile: UserProfile,
        vault: CredentialVault,
        cred_repo: CredentialRepo,
        resume_path: Path,
        vision: Optional[VisionAnalyzer] = None,
        review_mode: bool = False,
        qa_cache=None,
        gmail=None,
    ) -> None:
        super().__init__(page, llm, profile, vision, review_mode, qa_cache)
        self._vault = vault
        self._cred_repo = cred_repo
        self._resume_path = resume_path
        self._gmail = gmail  # Optional GmailClient for email verification

    # ── Entry point ───────────────────────────────────────────────────────────

    async def apply(self, job: Job, application) -> bool:
        self._job = job
        self.logger.info(f"Starting Workday application: {job.title} @ {job.company}")

        if not job.external_url:
            self.logger.error("No external URL for Workday job")
            return False

        domain = _extract_domain(job.external_url)
        self.logger.info(f"Workday domain: {domain}")

        # Navigate to the job page
        try:
            await self._page.goto(job.external_url, wait_until="domcontentloaded")
        except PlaywrightTimeout:
            self.logger.error(f"Timeout loading Workday job page: {job.external_url}")
            return False

        await random_delay(2.0, 4.0)

        # Click the Apply button — CSS selectors first, then text/role fallback, then Vision
        if not await wait_and_click(self._page, _APPLY_BTN, timeout=2_000):
            clicked = False
            for label in _APPLY_BTN_TEXT:
                for role in ("button", "link"):
                    try:
                        el = self._page.get_by_role(role, name=label)
                        if await el.first.is_visible(timeout=1_500):
                            await el.first.click()
                            clicked = True
                            self.logger.info(f"Apply button found via get_by_role({role!r}, {label!r})")
                            break
                    except Exception:
                        pass
                if clicked:
                    break

            if not clicked:
                hint = await self.handle_stuck_page("Apply button on Workday job page")
                self.logger.error(f"Could not find Apply button — Vision: {hint}")
                return False

        await random_delay(2.0, 4.0)
        await wait_for_navigation_settle(self._page)

        # Some Workday portals show a "Start Your Application" modal after the Apply click
        # with choices: Autofill with Resume / Apply Manually / Use My Last Application.
        # Click "Apply Manually" to enter the standard form flow.
        await self._handle_start_application_modal()

        # Handle auth (store domain for potential mid-form re-auth)
        self._domain = domain
        authed = await self._handle_auth(domain, job.company)
        if not authed:
            return False

        await random_delay(2.0, 4.0)

        # Workday may show an email verification notice after account creation.
        if await self._is_email_verification_wall():
            self.logger.warning(
                "Workday shows email verification notice — attempting Gmail verification"
            )
            email = self._profile.personal.email
            verified = await self._verify_email_via_gmail(email, domain)
            if verified:
                self.logger.info("Email verified via Gmail — re-authenticating")
                await random_delay(2.0, 3.0)
                authed = await self._handle_auth(domain, job.company)
                if not authed:
                    return False
                await random_delay(2.0, 3.0)
            else:
                # Gmail verification failed — the stored account is unverified and unusable.
                # Delete all stale credentials for this domain and create a fresh account
                # by re-navigating to the apply URL.  Fresh account creation bypasses the
                # email verification gate (Workday lets you into the form immediately after
                # registration, before the verification email is processed).
                self.logger.warning(
                    "Gmail verification failed — deleting stale credential, creating fresh account"
                )
                stored_list = self._cred_repo.list_by_domain(domain)
                for s in stored_list:
                    try:
                        self._cred_repo.delete(domain, s.username)
                        self.logger.debug(f"Deleted stale credential: {s.username}")
                    except Exception:
                        pass
                # Navigate back to the apply URL for a clean auth slate
                apply_url = job.external_url or ""
                if apply_url:
                    self.logger.info(f"Re-navigating to apply URL for fresh account creation")
                    await self._page.goto(apply_url, wait_until="load", timeout=30_000)
                    await random_delay(3.0, 5.0)
                    await wait_for_navigation_settle(self._page)
                    await self._handle_start_application_modal()
                    authed = await self._handle_auth(domain, job.company)
                    if not authed:
                        self.logger.error("Fresh account creation after verification failure also failed")
                        return False
                    await random_delay(2.0, 4.0)
                    # Re-check for verification wall after fresh account creation
                    if await self._is_email_verification_wall():
                        self.logger.warning(
                            "Email verification wall persists after fresh account — proceeding anyway"
                        )
                else:
                    self.logger.warning(
                        "No apply URL to re-navigate — attempting form navigation with unverified account"
                    )

        # Navigate the application form
        return await self._navigate_form(job)

    # ── Authentication ────────────────────────────────────────────────────────

    async def _handle_auth(self, domain: str, company: str) -> bool:
        """Log in with stored credentials or create a new account.

        Handles three page states:
        1. Auth choice page: links to "Sign In" and "Create Account" are visible
        2. Already on sign-in form: email + password inputs visible, no verify field
        3. Already on create-account form: email + password + verify-password all visible
        """
        base_email = self._profile.personal.email

        # Look up any stored credential for this domain (username may be a subaddress)
        stored_list = self._cred_repo.list_by_domain(domain)
        if stored_list:
            stored = stored_list[0]
            email = stored.username  # may be base or subaddress
            password = self._vault.decrypt(stored.password)
            self.logger.info(f"Found stored credentials for {domain} (username={email}) — signing in")
            if await self._sign_in(email, password):
                return True
            # Stored credentials rejected — delete them and fall through to account creation
            self.logger.warning(
                f"Stored credentials for {domain} failed — deleting stale entry, will attempt account creation"
            )
            try:
                self._cred_repo.delete(domain, email)
            except Exception:
                pass

        # Determine the email to use for account creation.
        # Use a domain+timestamp subaddress to guarantee a fresh account each run.
        # Gmail routes base+tag@gmail.com to the same inbox as base@gmail.com.
        # The timestamp suffix changes with each invocation (mod 0xFFFF → 4-char hex),
        # so a new account is created even if a prior run left an unverified account
        # with the same domain slug.
        import time as _time
        domain_slug = domain.split(".")[0][:10]  # e.g. "relationin" from "relationinsurance..."
        ts_suffix = hex(int(_time.time()) % 0xFFFF)[2:].zfill(4)
        email = base_email.replace("@", f"+wd{domain_slug}{ts_suffix}@", 1)

        # Guest flow: try "Continue as Guest" / "Apply Without Account" before creating
        # a new account.  This avoids the email-verification gate entirely on tenants that
        # support guest applications.  Check via get_by_role (most reliable for text buttons).
        for label in _GUEST_LABELS:
            for role in ("button", "link"):
                try:
                    el = self._page.get_by_role(role, name=label, exact=False).first
                    if await el.is_visible(timeout=1_000):
                        self.logger.info(
                            f"Guest flow available via get_by_role({role!r}, {label!r}) — clicking"
                        )
                        await el.click()
                        await random_delay(1.0, 2.0)
                        await wait_for_navigation_settle(self._page)
                        return True
                except Exception:
                    continue

        # Auth choice page: links to sign-in / create-account are present.
        # Try CSS selectors first, then get_by_role fallback for non-standard Workday tenants.
        if await is_visible(self._page, _SIGN_IN_BTN[0], timeout=3_000):
            self.logger.info("Auth choice page — no stored credentials, creating account")
        create_acct_visible = await is_visible(self._page, _CREATE_ACCOUNT_BTN[0], timeout=3_000)
        if not create_acct_visible:
            # Try remaining CSS selectors
            for sel in _CREATE_ACCOUNT_BTN[1:]:
                if await is_visible(self._page, sel, timeout=1_000):
                    create_acct_visible = True
                    break
        clicked_create_acct = False
        if not create_acct_visible:
            # Try get_by_role / get_by_text fallback — and click the button here so
            # _create_account() can start with already_on_form=True (skips CSS click).
            for label in _CREATE_ACCOUNT_LABELS:
                try:
                    for role in ("button", "link"):
                        el = self._page.get_by_role(role, name=label, exact=False).first
                        if await el.is_visible(timeout=1_500):
                            self.logger.info(
                                f"Auth choice page via get_by_role({role!r}, {label!r}) — clicking and creating account"
                            )
                            await el.click()
                            await random_delay(1.0, 2.0)
                            create_acct_visible = True
                            clicked_create_acct = True
                            break
                    if create_acct_visible:
                        break
                except Exception:
                    continue
        if create_acct_visible:
            return await self._create_account(
                domain, company, email, already_on_form=clicked_create_acct
            )

        # Already on an auth form — detect by presence of email input (try all selectors,
        # use a longer timeout to allow the Workday SPA to finish rendering the form).
        email_sel_found = None
        for sel in _EMAIL_INPUT:
            if await is_visible(self._page, sel, timeout=4_000):
                email_sel_found = sel
                break

        if email_sel_found:
            has_verify = await is_visible(self._page, _VERIFY_PASSWORD_INPUT[0], timeout=1_000)
            if has_verify:
                self.logger.info("Already on create-account form — filling directly")
                return await self._create_account(domain, company, email, already_on_form=True)
            else:
                self.logger.info("Already on sign-in form — filling directly")
                # No stored credentials yet; try to navigate to create-account from this page
                return await self._create_account(domain, company, email)

        # AX tree fallback: CSS selectors failed, but maybe the form uses different IDs.
        # If ANY textbox/combobox is visible on the page (which should be the auth form),
        # treat this as an auth form and attempt account creation via ARIA labels.
        try:
            tree = await get_ax_tree(self._page)
            if tree:
                text_nodes = search_ax_tree(tree, role="textbox") + search_ax_tree(tree, role="combobox")
                auth_keywords = ("email", "user", "sign", "log", "name", "account")
                auth_fields = [
                    n for n in text_nodes
                    if any(kw in (n.get("name") or "").lower() for kw in auth_keywords)
                ]
                if auth_fields:
                    field_names = [n.get("name", "") for n in auth_fields]
                    self.logger.info(
                        f"AX tree detected auth form fields (CSS selectors missed): {field_names} — attempting auth"
                    )
                    return await self._create_account(domain, company, email, already_on_form=True)

                # Check for guest flow buttons in the AX tree before SSO detection —
                # some tenants mix guest+SSO options on the same page.
                all_buttons = search_ax_tree(tree, role="button") + search_ax_tree(tree, role="link")
                guest_keywords = {lbl.lower() for lbl in _GUEST_LABELS}
                for node in all_buttons:
                    name_lower = (node.get("name") or "").lower()
                    if any(kw in name_lower for kw in guest_keywords):
                        self.logger.info(
                            f"AX tree found guest button {node.get('name')!r} — clicking"
                        )
                        try:
                            role_str = node.get("role", "button")
                            el = self._page.get_by_role(role_str, name=node.get("name"), exact=False).first
                            if await el.is_visible(timeout=1_500):
                                await el.click()
                                await random_delay(1.0, 2.0)
                                await wait_for_navigation_settle(self._page)
                                return True
                        except Exception:
                            pass

                # Detect SSO-only sign-in — buttons like "Sign in with Okta" or any
                # non-standard button when there are no form inputs means we cannot
                # automate this tenant.
                sso_keywords = ("okta", "sso", "saml", "google", "microsoft", "azure", "adfs", "with ")
                sso_buttons = [
                    n for n in all_buttons
                    if any(kw in (n.get("name") or "").lower() for kw in sso_keywords)
                ]
                if sso_buttons:
                    sso_names = [n.get("name", "") for n in sso_buttons]
                    self.logger.warning(
                        f"SSO-only sign-in detected for {domain}: {sso_names} — cannot automate, skipping"
                    )
                    return False
                # No inputs and no recognized SSO buttons but some buttons exist — likely
                # a non-standard auth page we can't handle.  Skip rather than looping.
                nav_names = {"next", "back", "cancel", "close", "previous", "continue", "skip"}
                non_nav_buttons = [
                    n for n in all_buttons
                    if (n.get("name") or "").lower() not in nav_names and n.get("name")
                ]
                if non_nav_buttons:
                    btn_names = [n.get("name", "") for n in non_nav_buttons[:5]]
                    self.logger.warning(
                        f"Auth page for {domain} has buttons but no form inputs: {btn_names} — "
                        "likely SSO or unsupported flow, skipping"
                    )
                    return False
        except Exception as exc:
            self.logger.debug(f"AX tree auth detection failed: {exc}")

        # Already on the application form — guest flow or pre-authenticated
        self.logger.info("No auth wall detected — proceeding as guest or already authenticated")
        return True

    async def _fill_auth_field(self, *labels: str, value: str, selectors: list[str] | None = None) -> bool:
        """Fill an auth form field by ARIA label first, then CSS selector fallback.

        Workday's auth form inputs are reliably ARIA-labeled; get_by_label() works even
        when data-automation-id and type selectors are inaccessible (e.g. inside overlays).
        """
        for label in labels:
            try:
                loc = self._page.get_by_label(label, exact=False).first
                await loc.wait_for(state="visible", timeout=3_000)
                await loc.fill(value)
                self.logger.debug(f"Filled auth field via get_by_label({label!r})")
                return True
            except Exception:
                continue
        # Fallback: CSS selector chain (short timeout — get_by_label above handles real work)
        if selectors:
            return await fill_field(self._page, selectors, value, timeout=2_000)
        return False

    async def _sign_in(self, email: str, password: str) -> bool:
        """Fill and submit the Workday sign-in form."""
        if not await wait_and_click(self._page, _SIGN_IN_BTN, timeout=2_000, delay_after=True):
            # May already be on sign-in form
            pass

        await random_delay(1.0, 2.0)
        await self._fill_auth_field("Email", "User Name", "Email Address", value=email, selectors=_EMAIL_INPUT)
        await random_delay(0.3, 0.8)
        await self._fill_auth_field("Password", "Current Password", value=password, selectors=_PASSWORD_INPUT)
        await random_delay(0.5, 1.0)

        if not await wait_and_click(self._page, _LOGIN_BTN, timeout=2_000):
            # get_by_role fallback — some tenants use non-standard data-automation-id
            submitted_login = False
            for btn_name in ("Sign In", "Log In", "Login", "Submit", "Next"):
                try:
                    btn = self._page.get_by_role("button", name=btn_name, exact=False).first
                    if await btn.is_visible(timeout=2_000):
                        await btn.click()
                        submitted_login = True
                        self.logger.info(f"Clicked sign-in submit via get_by_role('button', {btn_name!r})")
                        break
                except Exception:
                    continue
            if not submitted_login:
                self.logger.error("Could not click login button")
                return False

        await random_delay(2.0, 4.0)
        await wait_for_navigation_settle(self._page)

        # Check for explicit login failure indicators
        error_visible = await is_visible(
            self._page,
            "p[data-automation-id='signInError'], div.error-message, [data-automation-id='errorMessage']",
            timeout=2_000,
        )
        if error_visible:
            # Check whether the error is "please verify your email" — not a credential failure.
            # Workday blocks sign-in until email is verified; return True so form navigation
            # can still be attempted (and the email-verification-wall handler will catch it).
            if await self._is_email_verification_wall():
                self.logger.warning(
                    "Sign-in error is email-verification gate — treating as signed-in, "
                    "form navigation will handle it"
                )
                return True
            try:
                err_text = (
                    await self._page.locator("p[data-automation-id='signInError']").first.inner_text()
                ).lower()
                verif_words = ("verify", "verif", "confirm your email", "activate", "check your email")
                if any(w in err_text for w in verif_words):
                    self.logger.warning(
                        f"Sign-in blocked by email verification ({err_text[:80]!r}) — proceeding"
                    )
                    return True
            except Exception:
                pass
            self.logger.warning("Sign-in failed — incorrect credentials or account issue")
            return False

        # Verify we actually left the sign-in page — if email input is still visible,
        # login silently failed (wrong credentials, no Workday error element shown).
        still_on_signin = await is_visible(self._page, _EMAIL_INPUT[0], timeout=2_000)
        if still_on_signin:
            # Workday sometimes shows the email-verification notice inline on the sign-in
            # page rather than redirecting.  Return True so the higher-level code can
            # detect the verification wall and continue (the form may still be accessible).
            if await self._is_email_verification_wall():
                self.logger.warning(
                    "Sign-in page shows email verification notice — "
                    "treating as signed-in, form navigation will handle it"
                )
                return True
            self.logger.warning("Sign-in failed — still on sign-in page after submit (bad credentials?)")
            return False

        self.logger.info("Workday sign-in successful")
        return True

    async def _create_account(
        self, domain: str, company: str, email: str, *, already_on_form: bool = False
    ) -> bool:
        """Create a new Workday account and store encrypted credentials.

        Args:
            already_on_form: True when _handle_auth() detected we are already on the
                create-account page — skips clicking _CREATE_ACCOUNT_BTN (which would
                waste 30 s on timeouts and may cause the SPA to navigate away).
        """
        password = CredentialVault.generate_password(24)

        if not already_on_form:
            await wait_and_click(self._page, _CREATE_ACCOUNT_BTN, timeout=2_000, delay_after=True)
            await random_delay(1.0, 2.0)

        await self._fill_auth_field("Email", "User Name", "Email Address", value=email, selectors=_EMAIL_INPUT)
        await random_delay(0.3, 0.8)

        # Some Workday tenants use a multi-step create-account form where email is on
        # step 1 and password is on step 2. If the password input isn't visible yet,
        # try clicking Next/Continue to advance to the password screen.
        pw_visible = await is_visible(self._page, _PASSWORD_INPUT[0], timeout=3_000)
        if not pw_visible:
            # Also try get_by_label to check visibility
            try:
                pw_visible = await self._page.get_by_label("Password", exact=False).first.is_visible(timeout=2_000)
            except Exception:
                pass
        if not pw_visible:
            self.logger.debug("Password field not visible after email — trying Next (multi-step form)")
            await wait_and_click(self._page, _NEXT_BTN + ["button[type='submit']"], timeout=2_000, delay_after=True)
            await random_delay(1.0, 2.0)

        await self._fill_auth_field("Password", "New Password", value=password, selectors=_PASSWORD_INPUT)
        await random_delay(0.3, 0.8)
        verify_filled = await self._fill_auth_field(
            "Verify Password", "Confirm Password", "Re-enter Password",
            "Retype Password", "Re-Type Password", "Confirm New Password",
            "Password Confirm", "Repeat Password",
            value=password, selectors=_VERIFY_PASSWORD_INPUT,
        )
        if not verify_filled:
            # Some Workday forms label both password fields "Password" — try the second occurrence
            try:
                loc = self._page.get_by_label("Password", exact=False).nth(1)
                await loc.wait_for(state="visible", timeout=3_000)
                await loc.fill(password)
                verify_filled = True
                self.logger.debug("Filled verify-password via get_by_label('Password').nth(1)")
            except Exception:
                pass
        if not verify_filled:
            # Fill all empty password inputs — catches any confirm-password field regardless of label
            try:
                all_pw = await self._page.query_selector_all("input[type='password']")
                for pw_inp in all_pw:
                    val = await pw_inp.input_value()
                    if not val:
                        await pw_inp.fill(password)
                        verify_filled = True
                        self.logger.debug("Filled verify-password via empty password input scan")
                        break
            except Exception:
                pass
        await random_delay(0.5, 1.0)

        # Agree to terms if checkbox present
        terms_cb = self._page.locator(
            "input[data-automation-id='agreed'], input[type='checkbox']"
        ).first
        try:
            if await terms_cb.is_visible(timeout=2_000):
                await terms_cb.check()
                await micro_delay()
        except Exception:
            pass

        submitted = await wait_and_click(self._page, _CREATE_ACCOUNT_SUBMIT, timeout=2_000)
        if not submitted:
            # Workday often uses type='button' (not type='submit') — try get_by_role
            for btn_name in (
                "Create Account", "Create an Account", "Create Workday Account",
                "Submit", "Next", "Continue", "Sign Up", "Register",
            ):
                try:
                    btn = self._page.get_by_role("button", name=btn_name, exact=False).first
                    if await btn.is_visible(timeout=2_000):
                        await btn.click()
                        submitted = True
                        self.logger.debug(f"Submitted create-account form via get_by_role button={btn_name!r}")
                        break
                except Exception:
                    continue
        if not submitted:
            # Broader regex filter: find any button with typical create/submit text
            import re as _re
            try:
                btn = self._page.locator("button").filter(
                    has_text=_re.compile(r"create|register|sign up|submit|next|continue", _re.I)
                ).first
                if await btn.is_visible(timeout=2_000):
                    await btn.click()
                    submitted = True
                    self.logger.debug("Submitted create-account form via regex button filter")
            except Exception:
                pass
        if not submitted:
            self.logger.error("Could not submit account creation form")
            return False

        await random_delay(3.0, 5.0)
        await wait_for_navigation_settle(self._page)

        # Check if Workday redirected to sign-in after account creation — this is normal.
        # If the verify-password field is still visible we're still on the create-account
        # form, meaning submission silently failed (e.g. button[type='submit'] was the
        # wrong form's submit button).
        still_on_create = await is_visible(self._page, _VERIFY_PASSWORD_INPUT[0], timeout=2_000)
        if not still_on_create:
            # Also check via nth-of-type since the selector might use a different ID
            try:
                still_on_create = await self._page.locator("input[type='password']").nth(1).is_visible(timeout=1_000)
            except Exception:
                pass
        if still_on_create:
            # Workday uses multi-step create-account forms.  The "submit" may have just
            # advanced from step 2 (password) to step 3 (verify + final submit).
            # Detect this: fill verify password and try submitting again.
            self.logger.info("Still on create-account form — checking for multi-step (verify password step)")
            verify_now_filled = False
            verify_now_filled = await self._fill_auth_field(
                "Verify Password", "Confirm Password", "Re-enter Password",
                "Retype Password", "Confirm New Password",
                value=password, selectors=_VERIFY_PASSWORD_INPUT,
            )
            if not verify_now_filled:
                # Fill all empty password inputs — catches any confirm-password field
                try:
                    all_pw = await self._page.query_selector_all("input[type='password']")
                    for pw_inp in all_pw:
                        val = await pw_inp.input_value()
                        if not val:
                            await pw_inp.fill(password)
                            verify_now_filled = True
                            self.logger.debug("Filled verify-password (step 3) via empty password input scan")
                            break
                except Exception:
                    pass
            await random_delay(0.5, 1.0)
            # Submit the final step
            step3_submitted = False
            for btn_name in ("Create Account", "Create an Account", "Submit", "Finish", "Register", "Sign Up"):
                try:
                    btn = self._page.get_by_role("button", name=btn_name, exact=False).first
                    if await btn.is_visible(timeout=2_000):
                        await btn.click()
                        step3_submitted = True
                        self.logger.info(f"Submitted create-account step 3 via get_by_role({btn_name!r})")
                        break
                except Exception:
                    continue
            if step3_submitted:
                await random_delay(3.0, 5.0)
                await wait_for_navigation_settle(self._page)
                still_on_create = await is_visible(self._page, _VERIFY_PASSWORD_INPUT[0], timeout=2_000)
                if not still_on_create:
                    try:
                        still_on_create = await self._page.locator("input[type='password']").nth(1).is_visible(timeout=1_000)
                    except Exception:
                        pass
            if still_on_create:
                self.logger.error("Account creation failed — still on create-account form after submit")
                return False

        # Store credentials only after we've confirmed we navigated away from the form.
        # (Storing on failure would cause future sign-in attempts with an invalid password.)
        encrypted_pw = self._vault.encrypt(password)
        cred = Credential(
            domain=domain,
            company=company,
            username=email,
            password=encrypted_pw,
        )
        self._cred_repo.upsert(cred)
        self.logger.info(f"Stored credentials for {domain}")

        # Workday often redirects to sign-in after account creation.
        # Auto-sign-in with the new credentials so form navigation can continue.
        redirected_to_signin = await is_visible(self._page, _EMAIL_INPUT[0], timeout=2_000)
        if redirected_to_signin:
            self.logger.info("Account created — Workday redirected to sign-in, signing in with new credentials")
            return await self._sign_in(email, password)

        return True

    # ── Form navigation ───────────────────────────────────────────────────────

    async def _navigate_form(self, job: Job) -> bool:
        """Step through all Workday form sections."""
        context = f"{job.title} at {job.company}"
        auth_attempts: int = 0
        section_name: str = "unknown"

        for step_num in range(1, _MAX_SECTIONS + 1):
            await wait_for_navigation_settle(self._page)

            # Dismiss any mid-form modal/popup before reading state
            await self._dismiss_popup()

            section_name = await self._get_section_name()

            # Mid-form auth wall (sign-in page appeared after the Apply click)
            if any(w in section_name for w in ["sign in", "log in", "login", "signin", "create account"]):
                auth_attempts += 1
                if auth_attempts > 1:
                    self.logger.error(
                        f"Auth wall {section_name!r} persists after {auth_attempts - 1} attempt(s) — aborting"
                    )
                    return False
                self.logger.info(f"Auth wall detected mid-form ({section_name!r}) — attempt {auth_attempts}")
                # Check if this is actually an email verification wall (not a sign-in wall)
                if await self._is_email_verification_wall():
                    email = self._profile.personal.email
                    domain_val = getattr(self, "_domain", _extract_domain(job.external_url or ""))
                    self.logger.warning(f"Email verification wall mid-form — attempting Gmail verification")
                    verified = await self._verify_email_via_gmail(email, domain_val)
                    if verified:
                        self.logger.info("Email verified — re-authenticating mid-form")
                        authed2 = await self._handle_auth(domain_val, job.company)
                        if authed2:
                            await random_delay(2.0, 3.0)
                            continue
                    # Cannot proceed without email verification
                    self.logger.error("Email verification required but could not be completed — aborting")
                    return False
                # Brief pause so the Workday SPA can finish rendering the auth form
                # before _handle_auth() checks for email/password input selectors.
                await random_delay(2.0, 3.0)
                domain = getattr(self, "_domain", _extract_domain(job.external_url or ""))
                authed = await self._handle_auth(domain, job.company)
                if not authed:
                    return False
                await random_delay(2.0, 3.0)
                # If still on an auth section after _handle_auth(), try advancing once —
                # Workday sometimes presents a "Sign In" wizard step that can be skipped.
                current_name = await self._get_section_name()
                if any(w in current_name for w in ["sign in", "log in", "login", "signin", "create account"]):
                    self.logger.info(f"Still on auth section {current_name!r} after _handle_auth — trying to advance")
                    await self._advance()
                    await random_delay(1.5, 2.5)
                continue
            self.logger.info(f"  Workday section {step_num}: {section_name!r}")

            if await self._is_submit_page():
                return await self._submit_application()

            old_section_name = section_name
            _llm_retry_done = False

            await self._handle_section(section_name, context)

            # Try to advance
            if not await self._advance():
                hint = await self.handle_stuck_page(f"Next button on Workday section: {section_name}")
                self.logger.warning(f"Could not advance past section {section_name!r} — Vision: {hint}")
                return False

            await random_delay(1.5, 2.5)

            # Check for validation errors after advancing
            errors = await self._page.query_selector_all(
                "[data-automation-id='field-error'], [data-automation-id='errorMessage'], "
                "p.error-msg, span.error-text"
            )
            if errors:
                msgs = []
                for e in errors[:3]:
                    try:
                        msgs.append((await e.inner_text()).strip())
                    except Exception:
                        pass
                self.logger.warning(f"Workday validation errors on {section_name!r}: {msgs}")
                # Try to fix errored fields and advance once more
                await self._retry_errored_fields(msgs, context)
                await self._advance()
                await random_delay(1.0, 1.5)

            # Validator: poll for section name change
            advanced, section_name = await self._validate_advance(old_section_name)
            if not advanced:
                # Vision diagnosis
                diagnosis = ""
                if self._vision:
                    try:
                        diagnosis = await self._vision.analyze_page(
                            self._page,
                            "What required fields are missing or have errors preventing the form from advancing?",
                        )
                    except Exception:
                        pass
                self.logger.warning(
                    f"Section {old_section_name!r} did not advance. Diagnosis: {diagnosis[:200]}"
                )
                # One LLM-guided retry
                if not _llm_retry_done:
                    _llm_retry_done = True
                    await self._llm_guided_section(context)
                    if await self._advance():
                        advanced, section_name = await self._validate_advance(old_section_name)
                if not advanced:
                    self.logger.error(
                        f"Still stuck on {old_section_name!r} after LLM retry — aborting"
                    )
                    return False

            await random_delay(0.5, 1.0)

        self.logger.error("Exceeded max Workday sections")
        return False

    async def _get_section_name(self) -> str:
        """Read the current section header text."""
        for sel in [_SECTION_HEADER, "h2", "h1"]:
            try:
                el = self._page.locator(sel).first
                if await el.is_visible(timeout=2_000):
                    return (await el.inner_text()).strip().lower()
            except Exception:
                pass
        return "unknown"

    async def _is_submit_page(self) -> bool:
        """Return True if the Submit button is visible."""
        for sel in _SUBMIT_BTN:
            if await is_visible(self._page, sel, timeout=1_500):
                return True
        # Also check for "Review" section with submit intent
        section = await self._get_section_name()
        return "review" in section or "submit" in section

    async def _advance(self) -> bool:
        """Click Next or Save to advance to the next section."""
        # Short timeout — modern Workday tenants rarely use these automation IDs;
        # get_by_role fallback below handles the actual click.
        if await wait_and_click(self._page, _NEXT_BTN, timeout=2_000, delay_after=False):
            return True
        if await wait_and_click(self._page, _SAVE_BTN, timeout=2_000, delay_after=False):
            return True
        # get_by_role fallback — Workday tenants vary in button text and automation IDs
        for label in _NEXT_BTN_LABELS:
            try:
                btn = self._page.get_by_role("button", name=label, exact=False).first
                if await btn.is_visible(timeout=2_000):
                    await btn.click()
                    logger.debug(f"Advanced via get_by_role button={label!r}")
                    return True
            except Exception:
                continue
        # Broad fallback: find any enabled button in the bottom navigation container
        # (Workday always has a sticky bottom bar with primary action buttons)
        try:
            # Any button inside common Workday bottom-nav containers
            for nav_sel in (
                "[data-automation-id='bottom-navigation'] button:not([aria-label*='Back']):not([aria-label*='back'])",
                "[class*='bottom-navigation'] button:not([aria-label*='Back'])",
                "[class*='footerContainer'] button",
                "[class*='bottom-bar'] button",
            ):
                candidates = await self._page.locator(nav_sel).all()
                for btn in candidates:
                    try:
                        if await btn.is_visible(timeout=500) and await btn.is_enabled():
                            label_text = await btn.inner_text()
                            if any(kw in label_text.lower() for kw in ("next", "save", "continue", "finish", "proceed", "submit")):
                                await btn.click()
                                logger.info(f"Advanced via bottom-nav broad scan: {label_text!r}")
                                return True
                    except Exception:
                        continue
        except Exception:
            pass
        return False

    async def _submit_application(self) -> bool:
        """Click the final Submit button and confirm submission."""
        job = getattr(self, "_job", None)
        title = job.title if job else "Unknown"
        company = job.company if job else "Unknown"
        if not await self._pause_for_review(title, company):
            return False
        self.logger.info("Submitting Workday application")
        if not await wait_and_click(self._page, _SUBMIT_BTN):
            return False
        await random_delay(3.0, 5.0)
        await wait_for_navigation_settle(self._page)

        confirmed = await self._confirm_submission()
        if confirmed:
            self.logger.info("Workday application confirmed submitted")
        else:
            self.logger.warning("Submit clicked but no confirmation detected — assuming success")
        return True

    # ── Section handlers ──────────────────────────────────────────────────────

    async def _handle_section(self, section_name: str, context: str) -> None:
        """Route to the right handler based on section name."""
        if any(w in section_name for w in ["information", "contact", "personal"]):
            await self._handle_personal_section(context)
        elif any(w in section_name for w in ["experience", "employment", "work history"]):
            await self._handle_experience_section(context)
        elif any(w in section_name for w in ["education"]):
            await self._handle_education_section(context)
        elif any(w in section_name for w in ["resume", "cv", "document", "upload"]):
            await self._handle_document_section()
        elif any(w in section_name for w in ["question", "screening", "additional"]):
            await self._handle_questions_section(context)
        elif any(w in section_name for w in ["voluntar", "self-id", "diversity", "eeo", "equal"]):
            await self._handle_eeo_section(context)
        else:
            # Generic: try to answer whatever fields are visible
            await self._handle_generic_section(context)

    async def _handle_personal_section(self, context: str) -> None:
        """Fill contact / personal information fields."""
        p = self._profile.personal

        # Use get_by_label() — the most reliable way to find Workday fields across tenants.
        # Workday's input ARIA labels are stable; data-automation-id values vary per tenant.
        # Each entry: (label_patterns, value)
        field_fills = [
            (("First Name", "Legal First Name", "Given Name"), p.first_name),
            (("Last Name", "Legal Last Name", "Family Name", "Surname"), p.last_name),
            (("Email", "Email Address", "Work Email"), p.email),
            (("Phone", "Phone Number", "Mobile", "Cell Phone", "Home Phone"), p.phone),
            (("City", "City of Residence"), p.location.split(",")[0].strip()),
            (("Address Line 1", "Street Address", "Address"), p.location),
            (("State", "Province", "State/Province"), p.location.split(",")[-1].strip() if "," in p.location else ""),
        ]
        for labels, value in field_fills:
            if not value:
                continue
            for label in labels:
                try:
                    loc = self._page.get_by_label(label, exact=False).first
                    if await loc.is_visible(timeout=1_500):
                        # Only fill if empty (avoid overwriting pre-filled Workday data)
                        current = await loc.input_value()
                        if not current:
                            await loc.fill(value)
                            await micro_delay()
                            self.logger.debug(f"Filled personal field {label!r}")
                        break
                except Exception:
                    continue

        # LinkedIn / website fields
        if p.linkedin_url:
            for sel in ["input[aria-label*='LinkedIn']", "input[placeholder*='linkedin']"]:
                if await is_visible(self._page, sel, timeout=1_000):
                    await fill_field(self._page, sel, p.linkedin_url)
                    break

        # Handle any remaining custom questions on the personal-info section
        # (e.g. "Have you previously been employed here?", "How did you hear about us?")
        await self._handle_generic_section(context)

    async def _handle_experience_section(self, context: str) -> None:
        """Fill work experience — Workday usually auto-parses from resume."""
        # Workday often pre-fills this from the uploaded resume.
        # We just answer any explicit questions that appear.
        await self._handle_generic_section(context)

    async def _handle_education_section(self, context: str) -> None:
        """Fill education fields."""
        await self._handle_generic_section(context)

    async def _handle_document_section(self) -> None:
        """Upload resume via file input or drag-drop area."""
        self.logger.info(f"Uploading resume to Workday: {self._resume_path.name}")

        # Try file input first (most reliable)
        file_input = self._page.locator(_FILE_UPLOAD).first
        try:
            await file_input.set_input_files(str(self._resume_path))
            await random_delay(2.0, 4.0)
            self.logger.info("Resume uploaded to Workday")
            return
        except Exception:
            pass

        # Vision fallback for non-standard upload areas
        if self._vision:
            hint = await self.handle_stuck_page("resume upload button or file input")
            self.logger.warning(f"Could not upload resume — Vision hint: {hint}")

    async def _handle_questions_section(self, context: str) -> None:
        """Answer screening / application questions."""
        await self._handle_generic_section(context)

    async def _handle_eeo_section(self, context: str) -> None:
        """Handle EEO / self-identification questions using profile answers."""
        await self._handle_generic_section(context)

    async def _handle_generic_section(self, context: str) -> None:
        """
        Generic field handler — scrolls the section, finds labeled inputs,
        and answers each using the profile/Claude pipeline.

        Field discovery is scoped to the active section container when possible,
        reducing false positives from off-screen sections.

        If DOM detection finds 0 fields, falls back to the LLM-guided planner
        which uses the AX tree for stable field detection.
        """
        await scroll_to_bottom(self._page, pause_s=0.5, max_scrolls=5)

        # Scope field discovery to active section container when available
        scope = await self._get_section_scope()

        # Find all visible form field containers within scope
        field_groups = await scope.query_selector_all(
            "[data-automation-id^='formField-'], "
            "[data-automation-id='questionContainer'], "
            "[data-automation-id='radioGroup'], "
            "[data-automation-id='multiSelectContainer'], "
            "div[data-automation-id*='formField'], "
            "div[data-automation-id*='applicationQuestion'], "
            "div[data-automation-id*='screeningQuestion'], "
            "div.WGCQ, div.WM8K"
        )
        self.logger.info(f"DOM field detection: {len(field_groups)} groups found")

        filled_count = 0

        for group in field_groups:
            try:
                # Extract label — Workday uses several label patterns
                label_el = await group.query_selector(
                    "label, "
                    "span[data-automation-id='formLabel'], "
                    "legend, "
                    "[data-automation-id='questionText'], "
                    "[data-automation-id='labelContent'], "
                    "[data-automation-id='wd-Text-body'], "
                    "[data-automation-id='questionTitle'], "
                    "h1, h2, h3, h4"
                )
                question = (await label_el.inner_text()).strip() if label_el else ""
                if not question:
                    continue

                # Determine input type and answer — detection order matters
                # 1. Multi-select checkbox group
                multi_container = await group.query_selector(
                    "[data-automation-id='multiSelectContainer']"
                ) or (group if await group.get_attribute("data-automation-id") == "multiSelectContainer" else None)

                # 2. Radio group (data-automation-id='radioGroup' or individual radio inputs)
                # Workday uses custom div[role='radio'] elements, not native input[type='radio']
                radio_group = await group.query_selector("[data-automation-id='radioGroup']")
                radios = await group.query_selector_all("input[type='radio']")
                if not radios:
                    radios = await group.query_selector_all("[role='radio']")

                # 3. Workday custom JS dropdown
                dropdown_btn = await group.query_selector(
                    "button[aria-haspopup='listbox'], button[aria-expanded]"
                )

                # 4. Typeahead / combobox
                typeahead = await group.query_selector(
                    "input[aria-autocomplete], input[role='combobox'], "
                    "[data-automation-id='searchBox'] input, "
                    "[data-automation-id='combobox'] input"
                )

                # 5. Split date fields (month/day/year)
                date_month = await group.query_selector(
                    "[data-automation-id='dateSectionMonth-display'], "
                    "[data-automation-id='dateSectionMonth']"
                )

                # 6. Native select
                sel_el = await group.query_selector("select")

                # 7. Textarea
                ta = await group.query_selector("textarea")

                # 8. Text / number input
                inp = await group.query_selector(
                    "input[type='text'], input[type='number'], "
                    "input[data-automation-id='numericInput']"
                )

                if multi_container and multi_container is not group:
                    # Multi-select: read checkbox options, ask LLM for all that apply
                    checkboxes = await group.query_selector_all("input[type='checkbox']")
                    options = []
                    for cb in checkboxes:
                        cid = await cb.get_attribute("id") or ""
                        lbl = await group.query_selector(f"label[for='{cid}']")
                        if lbl:
                            options.append((await lbl.inner_text()).strip())
                    if not options:
                        continue
                    answer = await self.answer_question(question, "checkbox", options, context)
                    self.record_qa(question, answer)
                    self._write_qa_cache(question, options, "checkbox", answer)
                    if answer.answer:
                        chosen = {a.strip().lower() for a in answer.answer.split(",")}
                        for cb in checkboxes:
                            cid = await cb.get_attribute("id") or ""
                            lbl = await group.query_selector(f"label[for='{cid}']")
                            if lbl and (await lbl.inner_text()).strip().lower() in chosen:
                                if not await cb.is_checked():
                                    await cb.click()
                                    await micro_delay()
                        filled_count += 1

                elif radios:
                    options = []
                    for r in radios:
                        # Try aria-label first (Workday custom radio), then label[for=id]
                        aria_lbl = (await r.get_attribute("aria-label") or "").strip()
                        if aria_lbl:
                            options.append(aria_lbl)
                        else:
                            # Try inner text of the radio element itself
                            try:
                                inner = (await r.inner_text()).strip()
                                if inner:
                                    options.append(inner)
                                    continue
                            except Exception:
                                pass
                            rid = await r.get_attribute("id") or ""
                            lbl = await group.query_selector(f"label[for='{rid}']")
                            if lbl:
                                options.append((await lbl.inner_text()).strip())
                    answer = await self.answer_question(question, "radio", options, context)
                    self.record_qa(question, answer)
                    self._write_qa_cache(question, options, "radio", answer)
                    if answer.answer:
                        for r in radios:
                            # Match by aria-label, inner text, or label[for=id]
                            aria_lbl = (await r.get_attribute("aria-label") or "").strip()
                            try:
                                inner_text = (await r.inner_text()).strip()
                            except Exception:
                                inner_text = ""
                            rid = await r.get_attribute("id") or ""
                            lbl_el = await group.query_selector(f"label[for='{rid}']")
                            lbl_text = (await lbl_el.inner_text()).strip() if lbl_el else ""
                            candidate = aria_lbl or inner_text or lbl_text
                            if candidate.lower() == answer.answer.lower():
                                await r.click()
                                await micro_delay()
                                break
                    filled_count += 1

                elif dropdown_btn:
                    # Workday custom JS dropdown (button + listbox) — native <select> won't work
                    opts = await self._get_workday_dropdown_options(group)
                    answer = await self.answer_question(question, "select", opts or None, context)
                    self.record_qa(question, answer)
                    self._write_qa_cache(question, opts if opts else None, "select", answer)
                    if answer.answer:
                        clicked = await self._click_workday_option(group, answer.answer)
                        if not clicked:
                            # Fallback: try typeahead on any input within the group
                            # (some Workday tenants combine button+listbox with a search input)
                            inp = await group.query_selector(
                                "input[type='text'], input[role='combobox'], input"
                            )
                            if inp:
                                await self._fill_typeahead(inp, answer.answer)
                    filled_count += 1

                elif typeahead:
                    # Typeahead / combobox — type text and click the matching suggestion
                    answer = await self.answer_question(question, "text", None, context)
                    self.record_qa(question, answer)
                    self._write_qa_cache(question, None, "text", answer)
                    if answer.answer:
                        await self._fill_typeahead(typeahead, answer.answer)
                    filled_count += 1

                elif date_month:
                    # Split date — fill month/day/year separately
                    await self._fill_split_date(group, question, context)
                    filled_count += 1

                elif sel_el:
                    # Native <select> (rare in Workday, kept as fallback)
                    opts = [
                        (await o.inner_text()).strip()
                        for o in await sel_el.query_selector_all("option")
                        if (await o.get_attribute("value") or "") not in ("", "0")
                    ]
                    answer = await self.answer_question(question, "select", opts, context)
                    self.record_qa(question, answer)
                    self._write_qa_cache(question, opts, "select", answer)
                    if answer.answer:
                        try:
                            await sel_el.select_option(label=answer.answer)
                        except Exception:
                            pass
                    filled_count += 1

                elif ta:
                    answer = await self.answer_question(question, "textarea", None, context)
                    self.record_qa(question, answer)
                    self._write_qa_cache(question, None, "textarea", answer)
                    if answer.answer:
                        await ta.triple_click()
                        await ta.type(answer.answer, delay=25)
                    filled_count += 1

                elif inp:
                    answer = await self.answer_question(question, "text", None, context)
                    self.record_qa(question, answer)
                    self._write_qa_cache(question, None, "text", answer)
                    if answer.answer:
                        await inp.triple_click()
                        await inp.fill(answer.answer)
                    filled_count += 1

            except Exception as e:
                self.logger.debug(f"Workday field error: {e}")

        if filled_count == 0:
            # Broad radiogroup scan: find [role='radiogroup'] elements directly, regardless
            # of outer container structure.  Workday wd5 uses custom div[role='radio'] elements
            # whose outer wrapper may not match the formField-* selector chain above.
            filled_count = await self._scan_radiogroups(scope, context)

        if filled_count == 0:
            self.logger.info("DOM field detection found 0 fields — trying LLM-guided section fill")
            filled_count = await self._llm_guided_section(context)

        if filled_count == 0:
            self.logger.debug("No fields filled in this section (may be read-only)")

    # ── Workday custom dropdown helpers ───────────────────────────────────────

    async def _get_workday_dropdown_options(self, container) -> list[str]:
        """Open a Workday JS listbox, read option texts, then close it with Escape."""
        btn = await container.query_selector(
            "button[aria-haspopup='listbox'], button[aria-haspopup='true'], "
            "button[aria-haspopup], button[aria-expanded]"
        )
        if not btn:
            return []
        await btn.click()
        await micro_delay()
        try:
            listbox = self._page.locator("[role='listbox']").first
            await listbox.wait_for(state="visible", timeout=2_000)
            opts = await listbox.query_selector_all("[role='option']")
            texts = [(await o.inner_text()).strip() for o in opts]
            await self._page.keyboard.press("Escape")
            return [t for t in texts if t]
        except Exception:
            await self._page.keyboard.press("Escape")
            return []

    async def _click_workday_option(self, container, answer: str) -> bool:
        """Click the matching option inside a Workday JS listbox dropdown."""
        btn = await container.query_selector(
            "button[aria-haspopup='listbox'], button[aria-haspopup='true'], "
            "button[aria-haspopup], button[aria-expanded]"
        )
        if not btn:
            return False
        await btn.click()
        await random_delay(0.3, 0.7)

        # Find the open dropdown — try standard listbox role first, then common Workday variants
        listbox = None
        for listbox_sel in ("[role='listbox']", "[data-automation-id='promptOption']",
                            "ul[role='listbox']", "[data-automation-id='selectWidget-popup']",
                            "[class*='dropdownList']", "[class*='listbox']"):
            try:
                loc = self._page.locator(listbox_sel).first
                await loc.wait_for(state="visible", timeout=2_000)
                listbox = loc
                break
            except Exception:
                continue
        if not listbox:
            # Close any accidental open overlay and give up
            await self._page.keyboard.press("Escape")
            return False

        # Collect all option elements — try role='option' then li elements
        options = await listbox.query_selector_all("[role='option'], li")
        answer_lower = answer.lower()
        # Exact match first
        for opt in options:
            if (await opt.inner_text()).strip().lower() == answer_lower:
                await opt.click()
                await micro_delay()
                return True
        # Partial match fallback (answer substring of option OR option substring of answer)
        for opt in options:
            text = (await opt.inner_text()).strip().lower()
            if text and (answer_lower in text or text in answer_lower):
                await opt.click()
                await micro_delay()
                return True
        # No match — pick first non-empty option as last resort (better than leaving empty)
        for opt in options:
            text = (await opt.inner_text()).strip()
            if text:
                self.logger.debug(f"No exact/partial match for {answer!r} — picking first option: {text!r}")
                await opt.click()
                await micro_delay()
                return True
        await self._page.keyboard.press("Escape")
        return False

    # ── Section scope helper ──────────────────────────────────────────────────

    async def _get_section_scope(self):
        """Return the active section container as an ElementHandle, or the Page as fallback.

        Uses query_selector (returns ElementHandle) not locator() (returns Locator) so that
        the result supports .query_selector_all() in _handle_generic_section().
        """
        for sel in _SECTION_CONTAINERS:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    return el
            except Exception:
                pass
        return self._page

    # ── Typeahead / combobox ──────────────────────────────────────────────────

    async def _fill_typeahead(self, input_el, answer: str) -> bool:
        """Type text into a typeahead input and click the first matching suggestion."""
        try:
            await input_el.triple_click()
            await input_el.type(answer[:80], delay=40)
            await random_delay(0.5, 1.0)

            # Wait for suggestion dropdown
            suggestion_sel = (
                "[role='option'], [data-automation-id='promptOption'], "
                "[data-automation-id='multiselectOption'], li[role='option']"
            )
            try:
                await self._page.wait_for_selector(suggestion_sel, state="visible", timeout=3_000)
            except Exception:
                # No suggestions appeared — accept whatever is typed (may be a free-text field)
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

            # Partial match fallback — click first suggestion that contains the answer
            for sug in suggestions:
                text = (await sug.inner_text()).strip().lower()
                if answer_lower in text or text in answer_lower:
                    await sug.click()
                    await micro_delay()
                    return True

            # No match — click the first available suggestion
            if suggestions:
                self.logger.debug(
                    f"No exact typeahead match for {answer!r} — using first suggestion"
                )
                await suggestions[0].click()
                await micro_delay()
                return True

            return False
        except Exception as e:
            self.logger.debug(f"Typeahead fill failed: {e}")
            return False

    # ── Split date fields ─────────────────────────────────────────────────────

    async def _fill_split_date(self, group, question: str, context: str) -> None:
        """Fill Workday's split month/day/year date field from profile dates."""
        import re as _re

        # Ask Claude for the date as a string, then parse it
        answer = await self.answer_question(question, "text", None, context)
        self.record_qa(question, answer)
        if not answer.answer:
            return

        # Try to parse YYYY-MM-DD, MM/YYYY, or "Month YYYY" formats
        date_str = answer.answer.strip()
        year = month = day = ""

        m = _re.match(r"(\d{4})-(\d{2})-?(\d{2})?", date_str)
        if m:
            year, month, day = m.group(1), m.group(2).lstrip("0") or "1", m.group(3) or "1"
        else:
            m = _re.match(r"(\d{1,2})/(\d{4})", date_str)
            if m:
                month, year = m.group(1), m.group(2)
                day = "1"

        if not year:
            self.logger.debug(f"Could not parse date from {date_str!r}")
            return

        # Fill month selector (Workday uses a custom dropdown for month)
        month_sel = await group.query_selector(
            "[data-automation-id='dateSectionMonth-display'], "
            "[data-automation-id='dateSectionMonth']"
        )
        if month_sel:
            # Month may be a custom dropdown or a number input
            tag = await month_sel.evaluate("el => el.tagName.toLowerCase()")
            if tag == "input":
                await month_sel.triple_click()
                await month_sel.fill(month)
            else:
                # Treat as a Workday custom dropdown
                await self._click_workday_option(group, _MONTH_NAMES.get(month, month))

        # Fill day
        day_sel = await group.query_selector(
            "[data-automation-id='dateSectionDay-display'], "
            "[data-automation-id='dateSectionDay']"
        )
        if day_sel and day:
            await day_sel.triple_click()
            await day_sel.fill(day.lstrip("0") or "1")

        # Fill year
        year_sel = await group.query_selector(
            "[data-automation-id='dateSectionYear-display'], "
            "[data-automation-id='dateSectionYear'], "
            "[data-automation-id='dateInputBox']"
        )
        if year_sel:
            await year_sel.triple_click()
            await year_sel.fill(year)

        await micro_delay()

    # ── Start Application modal ───────────────────────────────────────────────

    async def _handle_start_application_modal(self) -> None:
        """Handle the 'Start Your Application' modal that some Workday portals show
        after clicking Apply on the job listing page.

        The modal offers: Autofill with Resume / Apply Manually / Use My Last Application.
        We click 'Apply Manually' to enter the standard form flow.
        """
        _manual_selectors = [
            "button[data-automation-id='applyManuallyBtn']",
            "button[data-automation-id='applyManually']",
        ]
        for sel in _manual_selectors:
            try:
                el = self._page.locator(sel).first
                if await el.is_visible(timeout=2_000):
                    await el.click()
                    self.logger.info("Clicked 'Apply Manually' on Start Application modal")
                    await random_delay(1.5, 2.5)
                    await wait_for_navigation_settle(self._page)
                    return
            except Exception:
                pass

        # Text-based fallback
        for label in ["Apply Manually", "Start Application"]:
            try:
                el = self._page.get_by_role("button", name=label).first
                if await el.is_visible(timeout=1_000):
                    await el.click()
                    self.logger.info(f"Clicked {label!r} on start modal via get_by_role")
                    await random_delay(1.5, 2.5)
                    await wait_for_navigation_settle(self._page)
                    return
            except Exception:
                pass
        # Modal may not be present — that's fine

    # ── Popup / modal dismissal ───────────────────────────────────────────────

    async def _dismiss_popup(self) -> bool:
        """Dismiss any Workday modal/popup that may have appeared mid-flow."""
        try:
            popup = self._page.locator(_POPUP_BODY).first
            if not await popup.is_visible(timeout=1_000):
                return False
        except Exception:
            return False

        self.logger.info("Workday popup detected — dismissing")
        for sel in _POPUP_OK:
            try:
                btn = self._page.locator(sel).first
                if await btn.is_visible(timeout=1_000):
                    await btn.click()
                    await random_delay(0.5, 1.0)
                    return True
            except Exception:
                pass
        # Last resort: press Escape
        await self._page.keyboard.press("Escape")
        await micro_delay()
        return True

    # ── Validation error recovery ─────────────────────────────────────────────

    async def _retry_errored_fields(self, error_msgs: list[str], context: str) -> None:
        """
        After detecting validation errors, attempt to re-answer the fields that
        Workday flagged. We re-scan only fields adjacent to visible error elements.
        """
        errored_groups = await self._page.query_selector_all(
            "[data-automation-id='field-error']:visible, "
            "[data-automation-id='errorMessage']:visible"
        )
        if not errored_groups:
            return

        self.logger.info(f"Retrying {len(errored_groups)} errored field(s)")
        for err_el in errored_groups[:5]:
            try:
                # Walk up to find the containing field group
                group = await err_el.evaluate_handle(
                    "el => el.closest('[data-automation-id^=\"formField-\"]"
                    ", [data-automation-id=\"questionContainer\"]') || el.parentElement"
                )
                if not group:
                    continue
                label_el = await group.query_selector(
                    "label, span[data-automation-id='formLabel'], [data-automation-id='questionText']"
                )
                question = (await label_el.inner_text()).strip() if label_el else ""
                if not question:
                    continue
                # Force a fresh Claude answer (bypass cache by calling _answer_via_claude directly)
                inp = await group.query_selector(
                    "input[type='text'], input[type='number'], "
                    "input[data-automation-id='numericInput']"
                )
                ta = await group.query_selector("textarea")
                if inp:
                    answer = await self._answer_via_claude(question, "text", None, context)
                    if answer.answer:
                        await inp.triple_click()
                        await inp.fill(answer.answer)
                elif ta:
                    answer = await self._answer_via_claude(question, "textarea", None, context)
                    if answer.answer:
                        await ta.triple_click()
                        await ta.type(answer.answer, delay=25)
            except Exception as e:
                self.logger.debug(f"Error retry failed: {e}")

    # ── Planner-Actor-Validator helpers ───────────────────────────────────────

    def _build_profile_summary(self) -> str:
        """Build a compact (400–600 char) profile summary for the section planner."""
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

    async def _plan_section_llm(self, field_summary: str, context: str) -> list[dict]:
        """Call Claude once with AX tree field list + profile → structured fill plan."""
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
                purpose="workday_section_plan",
            )
            return _parse_field_plan(text)
        except Exception as e:
            self.logger.debug(f"Section planner LLM call failed: {e}")
            return []

    async def _scan_radiogroups(self, scope, context: str) -> int:
        """Broad radiogroup scan — fallback when DOM field_groups detection finds 0 fields.

        Queries [role='radiogroup'] and [role='group'] containing radio children directly,
        then answers and clicks each using the profile/Claude pipeline.

        Returns count of radiogroups successfully answered.
        """
        radiogroups = await scope.query_selector_all(
            "[role='radiogroup'], [data-automation-id='radioGroup']"
        )
        if not radiogroups:
            return 0

        self.logger.info(f"Broad radiogroup scan: {len(radiogroups)} groups found")
        filled = 0
        for rg in radiogroups:
            try:
                radios = await rg.query_selector_all("[role='radio'], input[type='radio']")
                if not radios:
                    continue

                # Determine question label: aria-labelledby → aria-label → preceding sibling text
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
                    # Walk up to parent and look for a text/heading sibling before this node
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
                                // Try text children of parent before this node
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
                    clicked = False
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
                            clicked = True
                            break
                    if not clicked:
                        self.logger.debug(
                            f"_scan_radiogroups: no matching radio for {answer.answer!r} "
                            f"in options {options}"
                        )
                filled += 1
            except Exception as e:
                self.logger.debug(f"_scan_radiogroups group error: {e}")

        return filled

    async def _execute_plan_item(self, label: str, field_type: str, value: str) -> int:
        """Locate an element by ARIA label and fill it per the plan.

        Returns 1 on success, 0 on failure.
        """
        pattern = re.compile(re.escape(label), re.IGNORECASE)
        locator = await find_by_aria_label(
            self._page,
            pattern,
            roles=("textbox", "combobox", "listbox", "checkbox", "radio", "radiogroup", "group"),
        )
        if locator is None:
            # Fallback: get_by_label
            try:
                locator = self._page.get_by_label(label, exact=False)
                await locator.wait_for(state="visible", timeout=2_000)
            except Exception:
                locator = None
                # select/dropdown/radio types have text-proximity fallbacks below — don't bail yet
                if field_type not in ("select", "combobox", "dropdown", "radio", "radiogroup"):
                    self.logger.debug(
                        f"Could not locate field {label!r} by aria-label or get_by_label"
                    )
                    return 0

        try:
            if field_type == "text":
                await locator.fill(value)
                await micro_delay()
                return 1
            elif field_type in ("select", "combobox", "dropdown"):
                # Approach 1: native <select> element — call select_option directly
                try:
                    tag = await locator.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        await locator.select_option(label=value)
                        await micro_delay()
                        return 1
                except Exception:
                    pass

                # Approach 2: click the element to open, then click the matching [role='option']
                # Works for div[role='combobox'], button+listbox, and custom dropdowns alike.
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

                # Approach 3: typeahead for combobox (type text, click suggestion)
                if field_type == "combobox":
                    try:
                        el = await locator.element_handle()
                        if el and await self._fill_typeahead(el, value):
                            return 1
                    except Exception:
                        pass

                # Approach 4: Workday custom dropdown via ancestor container walk
                try:
                    el = await locator.element_handle()
                    if el:
                        node = el
                        for _ in range(3):
                            node = await node.query_selector("xpath=..")
                            if node and await self._click_workday_option(node, value):
                                return 1
                except Exception:
                    pass

                # Approach 5: native select_option via aria-label
                if await select_option(self._page, [f"[aria-label='{label}']"], value):
                    return 1

                # Approach 6: typeahead as last resort (requires locator)
                if locator is not None:
                    try:
                        el = await locator.element_handle()
                        if el and await self._fill_typeahead(el, value):
                            return 1
                    except Exception:
                        pass

                # Approach 7: text-proximity — find label text on page, walk up to <select> ancestor
                try:
                    q_els = self._page.get_by_text(label[:60], exact=False)
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

                # Approach 8: text-proximity — click button near label text, then click option
                try:
                    q_els = self._page.get_by_text(label[:60], exact=False)
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

                # Approach 9: text-proximity — walk up to combobox/listbox ancestor
                try:
                    q_els = self._page.get_by_text(label[:60], exact=False)
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
            elif field_type in ("radio", "radiogroup"):
                # field_type "radiogroup": label is the question, value is the option to select.
                # Scope the radio click to the specific group to avoid clicking the wrong Yes/No.

                # Approach 1: named radiogroup (Workday sometimes sets aria-label on the group)
                try:
                    group_loc = self._page.get_by_role("radiogroup", name=label, exact=False).first
                    await group_loc.wait_for(state="visible", timeout=1_500)
                    opt = group_loc.get_by_role("radio", name=value, exact=False).first
                    await opt.click()
                    await micro_delay()
                    return 1
                except Exception:
                    pass

                # Approach 2: filter any radiogroup/group that contains the question text
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

                # Approach 3: XPath ancestor — find text node with question, walk up to radiogroup
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
            elif field_type == "checkbox":
                if value.lower() in ("yes", "true", "1"):
                    await locator.check()
                    await micro_delay()
                    return 1
        except Exception as e:
            self.logger.debug(f"_execute_plan_item({label!r}, {field_type!r}): {e}")
        return 0

    async def _llm_guided_section(self, context: str) -> int:
        """
        AX tree snapshot → LLM fill plan → execute via find_by_aria_label.

        Called as fallback when DOM detection finds 0 fields.
        Returns count of fields successfully filled.
        """
        tree = await get_ax_tree(self._page)
        # Compute field_summary even if tree is None (will be empty string)
        field_summary = format_interactive_fields(tree) if tree else ""
        self.logger.info(
            f"LLM-guided AX fields ({len(field_summary.splitlines()) if field_summary else 0}): "
            f"tree={'ok' if tree else 'None'} | {field_summary[:200]!r}"
        )
        if not field_summary:
            # Vision fallback: describe the page (also runs when AX tree is unavailable)
            if self._vision:
                try:
                    vis_desc = await self._vision.analyze_page(
                        self._page,
                        "List every form field, radio button group, and dropdown on this page. "
                        "For each radio group, include the question text and available options. "
                        "Format: 'radiogroup: <question> | options: <opt1>, <opt2>'",
                        context,
                    )
                    if vis_desc:
                        self.logger.info(f"LLM-guided using Vision field description: {vis_desc[:150]!r}")
                        field_summary = vis_desc
                    else:
                        self.logger.debug("Vision also found no fields — section may be read-only")
                        return 0
                except Exception as e:
                    self.logger.debug(f"Vision fallback failed: {e}")
                    return 0
            else:
                self.logger.debug("No fillable fields in AX tree — section may be read-only")
                return 0

        plan = await self._plan_section_llm(field_summary, context)
        if not plan:
            return 0

        filled = 0
        for item in plan:
            label = item.get("label", "")
            field_type = item.get("field_type", "text")
            value = item.get("value", "")
            if not label or not value:
                continue
            try:
                filled += await self._execute_plan_item(label, field_type, value)
            except Exception as e:
                self.logger.debug(f"Plan item failed (label={label!r}): {e}")
        self.logger.info(f"LLM-guided section: {filled}/{len(plan)} fields filled")
        return filled

    async def _validate_advance(self, old_section: str, timeout: float = 4.0) -> tuple[bool, str]:
        """Poll for section name change after _advance().

        Args:
            old_section: Section name before the advance attempt.
            timeout: Max seconds to poll.

        Returns:
            (advanced, current_section_name) — advanced is True when the name changed.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            new_section = await self._get_section_name()
            if new_section != old_section:
                return True, new_section
            await asyncio.sleep(0.4)
        return False, old_section

    async def _confirm_submission(self) -> bool:
        """Return True if the page shows a submission confirmation."""
        try:
            page_text = (await self._page.content()).lower()
            if any(phrase in page_text for phrase in _CONFIRM_TEXTS):
                return True
        except Exception:
            pass

        confirm_selectors = [
            "[data-automation-id='confirmationText']",
            "[data-automation-id='successMessage']",
            "div.confirmation",
            "h1.confirmation-title",
        ]
        for sel in confirm_selectors:
            if await is_visible(self._page, sel, timeout=2_000):
                return True

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

    # ── Utility ───────────────────────────────────────────────────────────────

    async def _is_email_verification_wall(self) -> bool:
        """Return True if the page is asking for email verification."""
        try:
            content = (await self._page.content()).lower()
            return any(phrase in content for phrase in _VERIFY_EMAIL_INDICATORS)
        except Exception:
            return False

    async def _verify_email_via_gmail(self, email: str, domain: str) -> bool:
        """Check Gmail for a Workday verification email and click the link in the browser.

        Returns True if a verification link was found and navigated to.
        Waits up to 90 seconds polling for the email (Workday sends almost instantly).
        """
        if not self._gmail:
            self.logger.debug("No Gmail client configured — skipping email verification")
            return False

        import re as _re
        import asyncio

        # Search for Workday verification emails.  Include sender domain so we don't
        # accidentally match unrelated confirmation emails (e.g. LinkedIn, GitHub).
        # Workday uses workday.com and myworkdayjobs.com as sending domains.
        query = (
            "(from:workday.com OR from:myworkdayjobs.com OR from:workdayemails.com "
            " OR from:noreply@workday OR from:no-reply@workday) "
            "subject:(verify OR verification OR activation OR confirm OR activate) "
            "newer_than:15m"
        )
        # Poll for up to 90 seconds (email arrives within ~10–30 seconds usually)
        for attempt in range(9):
            self.logger.debug(f"Gmail verification check attempt {attempt + 1}/9")
            try:
                loop = asyncio.get_event_loop()
                msg_ids = await loop.run_in_executor(
                    None, lambda: self._gmail.search_messages(query, max_results=5)
                )
                for mid in msg_ids:
                    msg = await loop.run_in_executor(None, lambda: self._gmail.get_message(mid))
                    if not msg:
                        continue
                    # Extract verification URLs from the email body
                    body = (msg.body_text or "") + " " + (msg.body_preview or "")
                    urls = _re.findall(
                        r"https?://[^\s<>\"]+(?:verify|activation|confirm)[^\s<>\"]*",
                        body,
                        _re.IGNORECASE,
                    )
                    if not urls:
                        # Fallback: any URL containing the workday domain
                        urls = _re.findall(
                            r"https?://[^\s<>\"]+workday[^\s<>\"]*",
                            body,
                            _re.IGNORECASE,
                        )
                    # Only navigate to URLs from Workday's own domains to avoid
                    # false positives (e.g. LinkedIn/GitHub confirm emails).
                    _WORKDAY_URL_DOMAINS = (
                        "workday.com", "myworkdayjobs.com", "workdayemails.com",
                        "wd1.myworkdayjobs", "wd2.myworkdayjobs", "wd3.myworkdayjobs",
                        "wd5.myworkdayjobs",
                    )
                    for url in urls[:3]:
                        # Clean up URL (sometimes has trailing punctuation)
                        url = url.rstrip(".,;)")
                        # Skip non-Workday URLs
                        if not any(d in url.lower() for d in _WORKDAY_URL_DOMAINS):
                            self.logger.debug(f"Skipping non-Workday URL: {url[:80]}")
                            continue
                        self.logger.info(f"Found Workday verification URL — navigating: {url[:80]}")
                        try:
                            await self._page.goto(url, wait_until="load", timeout=30_000)
                            await random_delay(2.0, 3.0)
                            await loop.run_in_executor(None, lambda: self._gmail.mark_read(mid))
                            return True
                        except Exception as nav_err:
                            self.logger.debug(f"Verification URL navigation failed: {nav_err}")
                            continue
            except Exception as exc:
                self.logger.debug(f"Gmail verification poll failed: {exc}")
            # Wait 10 seconds before next poll
            if attempt < 8:
                await asyncio.sleep(10)

        self.logger.warning(f"No Workday verification email found in Gmail for {email} within 90s")
        return False


def _extract_domain(url: str) -> str:
    """Extract the tenant-specific hostname from a URL for credential keying.

    Workday (and other ATS platforms) host every company on the same root domain
    but under unique subdomains — e.g. acme.myworkdayjobs.com.  Using only the
    root domain (myworkdayjobs.com) would incorrectly share credentials across
    completely separate Workday tenants. We use the full hostname so that
    "acme.myworkdayjobs.com" and "other.myworkdayjobs.com" have distinct vault
    entries.
    """
    try:
        host = urlparse(url).hostname or ""
        return host or url[:80]
    except Exception:
        return url[:80]
