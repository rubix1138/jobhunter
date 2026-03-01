"""Apply Agent — generates materials and submits applications."""

import asyncio
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from ..applicators.form_filling import FormFillingAgent
from ..applicators.linkedin_easy import LinkedInEasyApplicator
from ..browser.context import BrowserSession
from ..browser.stealth import application_delay
from ..browser.vision import VisionAnalyzer
from ..crypto.vault import CredentialVault
from ..db.models import Application, Job, WorkdayTenant
from ..db.repository import (
    ApplicationRepo,
    CredentialRepo,
    JobRepo,
    QACacheRepo,
    WorkdayTenantRepo,
)
from ..llm.client import ClaudeClient
from ..llm.cover_letter import (
    generate_cover_letter,
    render_cover_letter_html,
    save_cover_letter_pdf,
)
from ..llm.resume import (
    generate_tailored_resume,
    render_resume_html,
    save_resume_pdf,
)
from ..utils.logging import get_logger
from ..utils.profile_loader import UserProfile
from .base import AgentError, AgentResult, BaseAgent, RetryableError

logger = get_logger(__name__)

_ERROR_MSG_MAX_LEN = 1000
_SSO_COOLDOWN_DAYS = 14
_CHALLENGE_COOLDOWN_DAYS = 3
_MAX_FAILED_ATTEMPTS_PER_JOB = 3
_MAX_CONSECUTIVE_SAME_FAILURES = 2


def _format_applicator_failure(applicator: object, job: Job, page_url: Optional[str]) -> str:
    """Build a concise DB-friendly failure message with context."""
    reason = getattr(applicator, "failure_reason", None) or "Applicator returned False"
    parts = [str(reason).strip()]
    if job.apply_type:
        parts.append(f"apply_type={job.apply_type}")
    if page_url:
        parts.append(f"url={page_url}")
    return " | ".join(parts)[:_ERROR_MSG_MAX_LEN]


def _extract_domain(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        return urlparse(url).hostname
    except Exception:
        return None


def _failure_reason_prefix(error_message: Optional[str]) -> str:
    """Normalize stored error into a stable reason prefix (before metadata pipe)."""
    if not error_message:
        return ""
    return error_message.split(" | ", 1)[0].strip()


class ApplyAgent(BaseAgent):
    """
    Selects qualified jobs without applications, generates tailored resume and
    cover letter for each, then dispatches to the correct applicator.

    Per-run limit is enforced and daily application count is checked against
    the configured maximum before each submission.
    """

    name = "apply_agent"

    def __init__(
        self,
        session: BrowserSession,
        llm: ClaudeClient,
        profile: UserProfile,
        settings: dict,
        db_path: Optional[str] = None,
        resume_dir: str = "data/resumes",
        template_dir: str = "templates",
        dry_run: bool = False,
        review_mode: bool = False,
        apply_type_filter: Optional[list[str]] = None,
        reprobe_blocked_workday: bool = False,
    ) -> None:
        super().__init__(
            db_path=db_path,
            daily_budget_usd=settings.get("budget", {}).get("daily_limit_usd", 15.0),
        )
        self._session = session
        self._llm = llm
        self._profile = profile
        self._settings = settings
        self._resume_dir = Path(resume_dir)
        self._template_dir = template_dir

        rl = settings.get("rate_limits", {})
        self._max_per_run = rl.get("applications_per_run", 10)
        self._max_per_day = rl.get("applications_per_day", 25)
        thresholds = settings.get("thresholds", {})
        self._min_confidence = thresholds.get("min_question_confidence", 0.5)

        self._dry_run = dry_run
        self._review_mode = review_mode
        self._apply_type_filter: Optional[list[str]] = apply_type_filter
        self._reprobe_blocked_workday = reprobe_blocked_workday
        self._last_apply_failure_message: Optional[str] = None
        self._blocked_domains_this_run: set[str] = set()
        self._vision = VisionAnalyzer(llm)
        self._vault = CredentialVault()  # reads FERNET_KEY from env
        # Try to initialise Gmail client for email verification flows (optional)
        self._gmail_client = None
        try:
            from ..gmail.auth import get_gmail_service
            from ..gmail.client import GmailClient
            svc = get_gmail_service()
            self._gmail_client = GmailClient(svc)
        except Exception:
            pass  # Gmail not configured — verification will be skipped

    async def run_once(self) -> AgentResult:
        job_repo = JobRepo(self._conn)
        app_repo = ApplicationRepo(self._conn)

        if not await self._session.ensure_linkedin_session():
            raise AgentError("Could not establish LinkedIn session")

        # Check daily cap
        submitted_today = app_repo.count_submitted_today()
        if submitted_today >= self._max_per_day:
            self.logger.info(
                f"Daily application limit reached ({submitted_today}/{self._max_per_day})"
            )
            return AgentResult(success=True, details={"skipped": "daily_limit"})

        remaining_today = self._max_per_day - submitted_today
        limit = min(self._max_per_run, remaining_today)

        # Select qualified jobs with no application yet
        candidates = job_repo.list_qualified_without_application()
        if self._apply_type_filter:
            candidates = [j for j in candidates if j.apply_type in self._apply_type_filter]
            self.logger.info(
                f"apply_type filter {self._apply_type_filter}: "
                f"{len(candidates)} matching jobs"
            )
        self.logger.info(
            f"Found {len(candidates)} qualified jobs without application, "
            f"will attempt up to {limit}"
        )

        if not candidates:
            self.logger.warning(
                "Apply queue is empty — no qualified easy_apply jobs without an "
                "existing application. Run 'jobhunter search-now' to find new jobs."
            )

        submitted = 0
        skipped = 0

        if self._dry_run:
            self.logger.info("DRY RUN — resumes and cover letters will be generated but not submitted")

        for job in candidates[:limit]:
            if self.is_over_budget():
                self.logger.warning("Daily LLM budget reached — stopping apply run")
                break

            self.logger.info(f"Processing: {job.title} @ {job.company} (score={job.match_score:.2f})")

            try:
                success = await self._apply_to_job(job, job_repo, app_repo)
                if success:
                    submitted += 1
                    if not self._dry_run:
                        await application_delay(
                            self._settings.get("rate_limits", {}).get("min_delay_between_applications", 30),
                            self._settings.get("rate_limits", {}).get("max_delay_between_applications", 90),
                        )
                else:
                    skipped += 1
            except Exception as e:
                self.logger.error(f"Unexpected error applying to {job.title} @ {job.company}: {e}")
                skipped += 1

        attempted = submitted + skipped
        if attempted > 0:
            if submitted > 0:
                self.logger.info(
                    f"Run summary: {submitted} submitted, {skipped} failed/skipped "
                    f"out of {attempted} attempted"
                )
            else:
                self.logger.warning(
                    f"Run summary: 0 submitted — {skipped} jobs attempted but none "
                    "completed successfully. Check logs above for per-job errors."
                )

        return AgentResult(
            success=True,
            apps_submitted=submitted,
            details={
                "submitted": submitted,
                "skipped": skipped,
                "candidates": len(candidates),
                "dry_run": self._dry_run,
            },
        )

    async def _apply_to_job(
        self, job: Job, job_repo: JobRepo, app_repo: ApplicationRepo
    ) -> bool:
        """Generate materials and apply. Returns True on submission."""
        self._last_apply_failure_message = None
        domain = _extract_domain(job.external_url)

        if domain and domain in self._blocked_domains_this_run:
            self.logger.info(
                f"Skipping {job.title} @ {job.company} — domain {domain} was "
                "blocked earlier in this run"
            )
            job_repo.update_status(job.id, "skipped")
            return False
        if domain and self._is_domain_in_sso_cooldown(domain):
            self.logger.info(
                f"Skipping {job.title} @ {job.company} — domain {domain} is in "
                f"SSO cooldown ({_SSO_COOLDOWN_DAYS}d)"
            )
            job_repo.update_status(job.id, "skipped")
            return False
        if domain and self._is_domain_in_challenge_cooldown(domain):
            self.logger.info(
                f"Skipping {job.title} @ {job.company} — domain {domain} is in "
                f"challenge cooldown ({_CHALLENGE_COOLDOWN_DAYS}d)"
            )
            job_repo.update_status(job.id, "skipped")
            return False
        retry_cap_reason = self._retry_cap_reason(job.id)
        if retry_cap_reason:
            self.logger.info(
                f"Skipping {job.title} @ {job.company} — retry cap reached: {retry_cap_reason}"
            )
            job_repo.update_status(job.id, "skipped")
            return False

        # Skip known blocked Workday tenants unless explicitly reprobed.
        if job.apply_type == "external_workday" and job.external_url:
            if domain:
                tenant_repo = WorkdayTenantRepo(self._conn)
                tenant = tenant_repo.get(domain)
                if tenant and tenant.status == "blocked" and not self._reprobe_blocked_workday:
                    self.logger.info(
                        f"Skipping {job.title} @ {job.company} — Workday tenant "
                        f"{domain} is blocked (use --reprobe-blocked-workday to retry)"
                    )
                    job_repo.update_status(job.id, "skipped")
                    return False
                if tenant and tenant.status == "blocked" and self._reprobe_blocked_workday:
                    self.logger.info(f"Reprobe enabled: retrying blocked Workday tenant {domain}")

        # Resolve apply type BEFORE spending LLM tokens on resume/cover letter.
        apply_type = job.apply_type or "unknown"

        # Hard bail for types we can never apply to — no materials, no waste.
        if apply_type == "interest_only":
            self.logger.info(
                f"Skipping {job.title} @ {job.company} — apply_type={apply_type!r} "
                f"(marking skipped to prevent future re-queue)"
            )
            job_repo.update_status(job.id, "skipped")
            return False

        # Jobs with unknown or easy_apply type get a live re-detection pass so we
        # avoid generating materials for recruiter-sourced "I'm interested" cards.
        # External jobs also re-detect when external_url is missing.
        needs_redetect = apply_type in ("unknown", "easy_apply") or (
            apply_type.startswith("external") and not job.external_url
        )
        if needs_redetect and not self._dry_run:
            self.logger.info(
                f"apply_type={apply_type!r} — checking job page before generating materials"
            )
            apply_type, external_url = await self._redetect_apply_type(job)
            if apply_type != "unknown":
                self._conn.execute(
                    "UPDATE jobs SET apply_type=?, external_url=? WHERE id=?",
                    (apply_type, external_url, job.id),
                )
                self._conn.commit()
                job.apply_type = apply_type
                job.external_url = external_url
            else:
                self.logger.warning(
                    f"Cannot determine apply method for {job.title} @ {job.company} — skipping"
                )
                job_repo.update_status(job.id, "skipped")
                return False

            # After re-detection, bail if still unapplyable
            if apply_type in ("interest_only", "unknown", "expired"):
                self.logger.info(
                    f"Skipping {job.title} @ {job.company} — "
                    f"re-detection returned apply_type={apply_type!r}"
                )
                job_repo.update_status(job.id, "skipped")
                return False

        # Create pending application record
        app = Application(job_id=job.id)
        app_id = app_repo.insert(app)
        app_repo.increment_attempt(app_id)
        app = app_repo.get_by_id(app_id)

        try:
            # ── Resume ────────────────────────────────────────────────────────
            resume_path = self._find_existing_resume(job)
            resume_html = None
            if resume_path:
                self.logger.info(f"Reusing existing resume: {resume_path.name}")
                tailored_summary = self._profile.summary  # fallback for cover letter
            else:
                self.logger.info("Generating tailored resume...")
                resume_data, resume_usage = await generate_tailored_resume(
                    self._llm, self._profile,
                    job.title, job.company, job.description or "",
                )
                self.log_llm_usage(**resume_usage, job_id=job.id)
                resume_html = render_resume_html(self._profile, resume_data, self._template_dir)
                resume_path = self._resume_path(job)
                save_resume_pdf(resume_html, resume_path)
                tailored_summary = resume_data.get("tailored_summary", self._profile.summary)

            # ── Cover letter ──────────────────────────────────────────────────
            cl_path = self._find_existing_cover_letter(job)
            cl_html = None
            if cl_path:
                self.logger.info(f"Reusing existing cover letter: {cl_path.name}")
            else:
                self.logger.info("Generating cover letter...")
                letter_data, letter_usage = await generate_cover_letter(
                    self._llm, self._profile,
                    job.title, job.company, job.description or "",
                    tailored_summary=tailored_summary,
                )
                self.log_llm_usage(**letter_usage, job_id=job.id)
                cl_html = render_cover_letter_html(
                    self._profile, letter_data, job.title, job.company, self._template_dir
                )
                cl_path = self._cover_letter_path(job)
                save_cover_letter_pdf(cl_html, cl_path)

            # Update application with material paths (HTML text only if freshly generated)
            self._conn.execute(
                """UPDATE applications SET
                    resume_path = ?, cover_letter_path = ?,
                    resume_text = ?, cover_letter_text = ?,
                    updated_at = datetime('now')
                WHERE id = ?""",
                (
                    str(resume_path), str(cl_path),
                    resume_html, cl_html,
                    app_id,
                ),
            )
            self._conn.commit()

            if self._dry_run:
                # Dry run — materials generated and saved, but do not submit
                app_repo.update_status(app_id, "dry_run", "Dry run — not submitted")
                self.logger.info(
                    f"[DRY RUN] Materials saved — resume: {resume_path}, "
                    f"cover letter: {cl_path}"
                )
                return True

            # Dispatch to the right applicator
            success = await self._dispatch(job, app_repo.get_by_id(app_id), resume_path)

            if success:
                app_repo.mark_submitted(app_id)
                job_repo.update_status(job.id, "applied")
                self._update_workday_tenant_state(job, success=True, failure_message=None)
                self.logger.info(f"Application submitted: {job.title} @ {job.company}")
            else:
                failure_msg = self._last_apply_failure_message or (
                    f"Applicator returned False | apply_type={job.apply_type}"
                )
                if domain and (
                    "Auth failed — cannot proceed" in failure_msg
                    or "SSO-only auth wall" in failure_msg
                    or "CAPTCHA detected — needs_review" in failure_msg
                    or "Email verification wall — needs_review" in failure_msg
                ):
                    self._blocked_domains_this_run.add(domain)
                self._update_workday_tenant_state(job, success=False, failure_message=failure_msg)
                await self._capture_apply_failure_artifacts(job, "apply_failed", failure_msg)
                app_repo.update_status(app_id, "failed", failure_msg)
                self.logger.warning(f"Application failed: {job.title} @ {job.company}")

            return success

        except Exception as e:
            await self._capture_apply_failure_artifacts(job, "apply_exception", e)
            app_repo.update_status(app_id, "failed", str(e))
            raise

    def _is_domain_in_sso_cooldown(self, domain: str) -> bool:
        """Return True if domain recently failed with SSO-only auth wall."""
        try:
            row = self._conn.execute(
                """
                SELECT 1
                FROM applications a
                JOIN jobs j ON j.id = a.job_id
                WHERE j.external_url LIKE ?
                  AND a.status = 'failed'
                  AND a.error_message LIKE 'SSO-only auth wall%'
                  AND a.updated_at >= datetime('now', ?)
                LIMIT 1
                """,
                (f"%{domain}%", f"-{_SSO_COOLDOWN_DAYS} days"),
            ).fetchone()
            return row is not None
        except Exception:
            return False

    def _is_domain_in_challenge_cooldown(self, domain: str) -> bool:
        """Return True if domain recently failed on captcha/email verification challenges."""
        try:
            row = self._conn.execute(
                """
                SELECT 1
                FROM applications a
                JOIN jobs j ON j.id = a.job_id
                WHERE j.external_url LIKE ?
                  AND a.status = 'failed'
                  AND (
                    a.error_message LIKE 'CAPTCHA detected — needs_review%'
                    OR a.error_message LIKE 'Email verification wall — needs_review%'
                  )
                  AND a.updated_at >= datetime('now', ?)
                LIMIT 1
                """,
                (f"%{domain}%", f"-{_CHALLENGE_COOLDOWN_DAYS} days"),
            ).fetchone()
            return row is not None
        except Exception:
            return False

    def _retry_cap_reason(self, job_id: int) -> Optional[str]:
        """Return retry-cap reason if this job should stop auto-retrying."""
        try:
            rows = self._conn.execute(
                """
                SELECT error_message
                FROM applications
                WHERE job_id = ? AND status = 'failed'
                ORDER BY id DESC
                """,
                (job_id,),
            ).fetchall()
        except Exception:
            return None

        if not rows:
            return None

        failed_count = len(rows)
        if failed_count >= _MAX_FAILED_ATTEMPTS_PER_JOB:
            return f"{failed_count} failed attempts (cap={_MAX_FAILED_ATTEMPTS_PER_JOB})"

        latest_reason = _failure_reason_prefix(rows[0]["error_message"])
        if not latest_reason:
            return None

        streak = 0
        for r in rows:
            if _failure_reason_prefix(r["error_message"]) == latest_reason:
                streak += 1
            else:
                break
        if streak >= _MAX_CONSECUTIVE_SAME_FAILURES:
            return (
                f"{streak} consecutive failures with same reason "
                f"({latest_reason!r}, cap={_MAX_CONSECUTIVE_SAME_FAILURES})"
            )
        return None

    def _update_workday_tenant_state(
        self,
        job: Job,
        success: bool,
        failure_message: Optional[str],
    ) -> None:
        """Persist Workday tenant capabilities based on latest outcome."""
        if job.apply_type != "external_workday":
            return
        domain = _extract_domain(job.external_url)
        if not domain:
            return
        repo = WorkdayTenantRepo(self._conn)
        existing = repo.get(domain)
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        if success:
            repo.upsert(
                WorkdayTenant(
                    domain=domain,
                    auth_mode="auto",
                    status="active",
                    notes=f"last_success_utc={now}",
                    id=existing.id if existing else None,
                )
            )
            return

        if failure_message and (
            "Auth failed — cannot proceed" in failure_message
            or "SSO-only auth wall" in failure_message
        ):
            repo.upsert(
                WorkdayTenant(
                    domain=domain,
                    auth_mode="sso_only" if "SSO-only auth wall" in failure_message else "signin_only",
                    status="blocked",
                    notes=f"blocked_auth_failure_utc={now}",
                    id=existing.id if existing else None,
                )
            )

    async def _capture_apply_failure_artifacts(
        self,
        job: Job,
        reason: str,
        error: Exception | str,
    ) -> None:
        """Capture screenshot + page context for unexpected apply-time failures."""
        try:
            out_dir = Path("data/logs/failures")
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            safe_reason = "".join(c if c.isalnum() or c in "._-" else "_" for c in reason)
            base = f"job{job.id}_{safe_reason}_{ts}"
            png_path = out_dir / f"{base}.png"
            txt_path = out_dir / f"{base}.txt"

            page = self._session.page
            url = ""
            body_text = ""
            try:
                url = page.url
                await page.screenshot(path=str(png_path), full_page=True)
            except Exception:
                pass
            try:
                body_text = await page.locator("body").inner_text()
            except Exception:
                body_text = "<could not read body text>"

            txt_path.write_text(
                "\n".join(
                    [
                        f"timestamp_utc={ts}",
                        f"job_id={job.id}",
                        f"title={job.title}",
                        f"company={job.company}",
                        f"reason={reason}",
                        f"error={error}",
                        f"url={url}",
                        "",
                        "body_text:",
                        body_text[:10000],
                    ]
                ),
                encoding="utf-8",
            )
            self.logger.error(
                f"Apply failure artifacts saved: screenshot={png_path} context={txt_path}"
            )
        except Exception as capture_err:
            self.logger.warning(f"Could not capture apply failure artifacts: {capture_err}")

    async def _dispatch(self, job: Job, application: Application, resume_path: Path) -> bool:
        """Route to the appropriate applicator based on apply_type."""
        apply_type = job.apply_type or "unknown"

        if apply_type in ("unknown", "interest_only"):
            self.logger.warning(
                f"Cannot apply to {job.title} @ {job.company} — apply_type={apply_type!r}"
            )
            return False

        qa_cache = QACacheRepo(self._conn)

        if apply_type == "easy_apply":
            applicator = LinkedInEasyApplicator(
                page=self._session.page,
                llm=self._llm,
                profile=self._profile,
                resume_path=resume_path,
                vision=self._vision,
                review_mode=self._review_mode,
                qa_cache=qa_cache,
            )

        else:
            # All external types (workday, greenhouse, lever, etc.) → FormFillingAgent
            cred_repo = CredentialRepo(self._conn)
            self.logger.info(f"Using FormFillingAgent for {apply_type} @ {job.company}")
            applicator = FormFillingAgent(
                page=self._session.page,
                llm=self._llm,
                profile=self._profile,
                resume_path=resume_path,
                vault=self._vault,
                cred_repo=cred_repo,
                vision=self._vision,
                review_mode=self._review_mode,
                qa_cache=qa_cache,
                gmail=self._gmail_client,
            )

        success = False

        try:
            success = await applicator.apply(job, application)
        finally:
            # NOTE: Playwright tracing with Patchright persistent contexts causes
            # Workday navigations to fail with net::ERR_NAME_NOT_RESOLVED in this
            # environment. Keep diagnostic screenshots/context artifacts instead.
            pass

        # If Easy Apply failed because the job is actually recruiter-sourced,
        # correct the stored type so it never re-queues.
        if (
            not success
            and apply_type == "easy_apply"
            and getattr(applicator, "detected_interest_only", False)
        ):
            self.logger.info(
                f"Correcting {job.title} @ {job.company}: "
                "easy_apply → interest_only (marking skipped)"
            )
            self._conn.execute(
                "UPDATE jobs SET apply_type='interest_only', status='skipped', "
                "updated_at=datetime('now') WHERE id=?",
                (job.id,),
            )
            self._conn.commit()

        # If apply failed because the listing is expired/closed, mark accordingly.
        if not success and getattr(applicator, "detected_expired", False):
            self.logger.info(
                f"Marking {job.title} @ {job.company} as expired "
                "— 'No longer accepting applications'"
            )
            self._conn.execute(
                "UPDATE jobs SET apply_type='expired', status='skipped', "
                "updated_at=datetime('now') WHERE id=?",
                (job.id,),
            )
            self._conn.commit()

        # Persist Q&A log for all applicator types
        if applicator._qa_log:
            self._conn.execute(
                "UPDATE applications SET questions_json = ? WHERE id = ?",
                (applicator.qa_log_json(), application.id),
            )
            self._conn.commit()

        # Mark needs_review if any low-confidence answers
        if applicator.has_low_confidence_answers():
            self._conn.execute(
                "UPDATE applications SET status = 'needs_review' "
                "WHERE id = ? AND status != 'submitted'",
                (application.id,),
            )
            self._conn.commit()

        if not success:
            page_url = None
            try:
                page_url = self._session.page.url
            except Exception:
                page_url = None
            self._last_apply_failure_message = _format_applicator_failure(
                applicator, job, page_url
            )
        else:
            self._last_apply_failure_message = None

        return success

    async def _redetect_apply_type(self, job: Job) -> tuple[str, Optional[str]]:
        """
        Navigate to the job's LinkedIn page and re-run apply type detection.
        Used when a job was stored with apply_type='unknown' or missing external_url.
        Falls back to Vision analysis when DOM/AX tree detection returns 'unknown'.
        """
        from ..agents.search_agent import detect_apply_type, vision_detect_apply_type
        from ..browser.stealth import random_delay
        try:
            # Use 'load' (not just domcontentloaded) — LinkedIn is a heavy SPA
            await self._session.page.goto(
                job.job_url, wait_until="load", timeout=20_000
            )
            # Extra wait for JS-rendered apply button
            await random_delay(2.5, 4.0)
            result = await detect_apply_type(self._session.page)
            self.logger.debug(f"Re-detection result: {result}")

            # Vision fallback when DOM/AX tree detection could not classify
            if result[0] == "unknown" and self._vision is not None:
                self.logger.info(
                    "Re-detection returned unknown — trying Vision fallback"
                )
                result = await vision_detect_apply_type(self._session.page, self._vision)
                self.logger.debug(f"Vision re-detection result: {result}")

            return result
        except Exception as e:
            self.logger.warning(f"apply_type re-detection failed: {e}")
            return "unknown", None

    def _resume_path(self, job: Job) -> Path:
        slug = _slug(f"{job.company}_{job.title}")
        return self._resume_dir / f"resume_{slug}_{date.today().isoformat()}.pdf"

    def _cover_letter_path(self, job: Job) -> Path:
        slug = _slug(f"{job.company}_{job.title}")
        return self._resume_dir / f"cover_{slug}_{date.today().isoformat()}.pdf"

    def _find_existing_resume(self, job: Job) -> Optional[Path]:
        """Return the most recent resume PDF for this job if one exists on disk."""
        slug = _slug(f"{job.company}_{job.title}")
        matches = sorted(self._resume_dir.glob(f"resume_{slug}_*.pdf"))
        return matches[-1] if matches else None

    def _find_existing_cover_letter(self, job: Job) -> Optional[Path]:
        """Return the most recent cover letter PDF for this job if one exists on disk."""
        slug = _slug(f"{job.company}_{job.title}")
        matches = sorted(self._resume_dir.glob(f"cover_{slug}_*.pdf"))
        return matches[-1] if matches else None


def _slug(text: str) -> str:
    """Convert text to a safe filename slug."""
    import re
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:50]
