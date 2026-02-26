"""Apply Agent — generates materials and submits applications."""

import asyncio
import json
import os
from datetime import date
from pathlib import Path
from typing import Optional

from ..applicators.generic import GenericApplicator
from ..applicators.linkedin_easy import LinkedInEasyApplicator
from ..applicators.workday import WorkdayApplicator
from ..browser.context import BrowserSession
from ..browser.stealth import application_delay
from ..browser.vision import VisionAnalyzer
from ..crypto.vault import CredentialVault
from ..db.models import Application, Job
from ..db.repository import ApplicationRepo, CredentialRepo, JobRepo, QACacheRepo
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

        # Jobs with unknown apply_type or missing external_url get a live re-detection pass.
        needs_redetect = apply_type == "unknown" or (
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
            if apply_type in ("interest_only", "unknown"):
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
                self.logger.info(f"Application submitted: {job.title} @ {job.company}")
            else:
                app_repo.update_status(app_id, "failed", "Applicator returned False")
                self.logger.warning(f"Application failed: {job.title} @ {job.company}")

            return success

        except Exception as e:
            app_repo.update_status(app_id, "failed", str(e))
            raise

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

        elif apply_type == "external_workday":
            cred_repo = CredentialRepo(self._conn)
            applicator = WorkdayApplicator(
                page=self._session.page,
                llm=self._llm,
                profile=self._profile,
                vault=self._vault,
                cred_repo=cred_repo,
                resume_path=resume_path,
                vision=self._vision,
                review_mode=self._review_mode,
                qa_cache=qa_cache,
                gmail=self._gmail_client,
            )

        else:
            # external_other or unknown — best-effort generic
            self.logger.info(f"Using generic applicator for {apply_type} @ {job.company}")
            applicator = GenericApplicator(
                page=self._session.page,
                llm=self._llm,
                profile=self._profile,
                resume_path=resume_path,
                vision=self._vision,
                review_mode=self._review_mode,
                qa_cache=qa_cache,
            )

        success = await applicator.apply(job, application)

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
