"""Email Agent — poll Gmail, classify, act, and update job status."""
from typing import Optional

from ..db.models import EmailLog, Job
from ..db.repository import EmailRepo, JobRepo
from ..gmail.classifier import classify_email, ClassificationResult
from ..gmail.client import GmailClient, GmailMessage
from ..llm.client import ClaudeClient
from ..llm.prompts import recruiter_reply_prompt
from ..utils.logging import get_logger
from ..utils.profile_loader import UserProfile
from .base import AgentError, AgentResult, BaseAgent

logger = get_logger(__name__)

# Gmail label names applied by this agent
_LABEL_REJECTION = "JobHunter/Rejected"
_LABEL_PROCESSED = "JobHunter/Processed"
_LABEL_INTERVIEW = "JobHunter/Interview"
_LABEL_OFFER = "JobHunter/Offer"

_CLASSIFICATION_LABELS = {
    "rejection": _LABEL_REJECTION,
    "interview_invite": _LABEL_INTERVIEW,
    "offer": _LABEL_OFFER,
}


class EmailAgent(BaseAgent):
    """
    Polls the Gmail inbox, classifies each unread message, and takes action:

    - interview_invite / assessment / offer / follow_up / unknown:
        Forward to personal email + apply Gmail label
    - rejection:
        Log, update job status → 'rejected', apply rejection label, mark read
    - recruiter_outreach:
        Auto-reply if the matched job score > threshold, else ignore
    - spam:
        Archive

    All processed emails are stored in email_log (deduped by gmail_message_id).
    """

    name = "email_agent"

    def __init__(
        self,
        gmail: GmailClient,
        llm: ClaudeClient,
        profile: UserProfile,
        settings: dict,
        db_path: Optional[str] = None,
    ) -> None:
        super().__init__(
            db_path=db_path,
            daily_budget_usd=settings.get("budget", {}).get("daily_limit_usd", 15.0),
        )
        self._gmail = gmail
        self._llm = llm
        self._profile = profile
        self._personal_email = profile.personal.personal_email
        self._recruiter_threshold = settings.get("thresholds", {}).get(
            "recruiter_reply_min_score", 0.7
        )

        # Cache label IDs (populated on first run)
        self._label_ids: dict[str, str] = {}

    async def run_once(self) -> AgentResult:
        email_repo = EmailRepo(self._conn)
        job_repo = JobRepo(self._conn)

        # Ensure Gmail labels exist
        self._ensure_labels()

        # Fetch unread messages
        message_ids = self._gmail.list_unread_inbox(max_results=50)
        self.logger.info(f"Found {len(message_ids)} unread inbox messages")

        processed = 0
        for msg_id in message_ids:
            if self.is_over_budget():
                self.logger.warning("Daily budget reached — stopping email processing")
                break

            # Skip already-processed messages
            if email_repo.exists(msg_id):
                self.logger.debug(f"Already processed: {msg_id}")
                continue

            # Fetch full message
            msg = self._gmail.get_message(msg_id)
            if not msg:
                continue

            self.logger.info(f"Processing: {msg.subject!r} from {msg.from_address}")

            # Classify
            result, usage = await classify_email(
                self._llm, msg.from_address, msg.subject, msg.body_text
            )
            self.log_llm_usage(**usage)

            # Link to job in DB
            linked_job = self._find_linked_job(result.company_name, job_repo)

            # Act on classification
            action_taken, action_details = await self._act(msg, result, linked_job, job_repo)

            # Update job status if applicable
            if result.new_job_status and linked_job:
                job_repo.update_status(linked_job.id, result.new_job_status)
                self.logger.info(
                    f"Job status updated: {linked_job.title} @ {linked_job.company} "
                    f"→ {result.new_job_status}"
                )

            # Mark as read
            self._gmail.mark_read(msg_id)

            # Store in email_log
            email_log = EmailLog(
                gmail_message_id=msg.message_id,
                thread_id=msg.thread_id,
                from_address=msg.from_address,
                to_address=msg.to_address,
                subject=msg.subject,
                body_preview=msg.body_preview,
                received_at=msg.received_at,
                classification=result.classification,
                confidence=result.confidence,
                linked_job_id=linked_job.id if linked_job else None,
                action_taken=action_taken,
                action_details=action_details,
            )
            email_repo.insert(email_log)
            processed += 1

        self.logger.info(f"Email agent complete: {processed} messages processed")
        return AgentResult(success=True, emails_processed=processed)

    # ── Classification actions ────────────────────────────────────────────────

    async def _act(
        self,
        msg: GmailMessage,
        result: ClassificationResult,
        linked_job: Optional[Job],
        job_repo: JobRepo,
    ) -> tuple[str, str]:
        """
        Perform the action appropriate to the classification.
        Returns (action_taken, action_details).
        """
        cls = result.classification

        if result.should_forward:
            note = self._forward_note(result, linked_job)
            success = self._gmail.forward_message(msg, self._personal_email, note=note)
            label = _CLASSIFICATION_LABELS.get(cls)
            if label and label in self._label_ids:
                self._gmail.apply_label(msg.message_id, self._label_ids[label])
            action = "forwarded"
            details = f"→ {self._personal_email}" + (f" | label: {label}" if label else "")
            self.logger.info(f"  Forwarded to {self._personal_email}: {msg.subject!r}")
            return action, details

        if cls == "rejection":
            label_id = self._label_ids.get(_LABEL_REJECTION)
            if label_id:
                self._gmail.apply_label(msg.message_id, label_id)
            self.logger.info(f"  Rejection logged: {msg.from_address}")
            return "labeled_rejected", f"company={result.company_name}"

        if cls == "recruiter_outreach":
            return await self._handle_recruiter(msg, result, linked_job)

        if cls == "spam":
            self._gmail.archive(msg.message_id)
            self.logger.info(f"  Archived spam: {msg.subject!r}")
            return "archived", "classified as spam"

        return "logged", f"classification={cls}"

    async def _handle_recruiter(
        self,
        msg: GmailMessage,
        result: ClassificationResult,
        linked_job: Optional[Job],
    ) -> tuple[str, str]:
        """Auto-reply to recruiter outreach if the linked job score is high enough."""
        score = linked_job.match_score if linked_job and linked_job.match_score else 0.0

        if score < self._recruiter_threshold:
            self.logger.info(
                f"  Recruiter outreach ignored (score={score:.2f} < {self._recruiter_threshold})"
            )
            return "ignored", f"score={score:.2f} below threshold"

        # Generate auto-reply
        job_title = linked_job.title if linked_job else (result.company_name or "the role")
        company = linked_job.company if linked_job else (result.company_name or "your company")

        reply_text, usage = await self._llm.message(
            prompt=recruiter_reply_prompt(
                self._profile, msg.body_text, job_title, company
            ),
            model=self._llm.sonnet_model,
            max_tokens=512,
            purpose="recruiter_reply",
        )
        self.log_llm_usage(**usage)

        reply_to = msg.from_address
        subject = f"Re: {msg.subject}"
        success = self._gmail.send_message(reply_to, subject, reply_text)
        self.logger.info(f"  Auto-replied to recruiter: {reply_to}")
        return "auto_replied", f"to={reply_to}, score={score:.2f}"

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _find_linked_job(
        self, company_name: Optional[str], job_repo: JobRepo
    ) -> Optional[Job]:
        """Try to find a job in the DB matching the company name."""
        if not company_name:
            return None
        company_lower = company_name.lower().strip()
        # Search all non-new jobs for a company name match
        for status in ("applied", "interviewing", "qualified", "rejected", "offer"):
            jobs = job_repo.list_by_status(status)
            for job in jobs:
                if company_lower in job.company.lower() or job.company.lower() in company_lower:
                    return job
        return None

    def _forward_note(self, result: ClassificationResult, linked_job: Optional[Job]) -> str:
        """Build a short context note prepended to forwarded emails."""
        lines = [
            f"[JobHunter] Classification: {result.classification.upper()} "
            f"(confidence={result.confidence:.0%})",
        ]
        if linked_job:
            lines.append(
                f"Linked job: {linked_job.title} @ {linked_job.company} "
                f"(score={linked_job.match_score:.2f})"
            )
        if result.reasoning:
            lines.append(f"Reason: {result.reasoning}")
        return "\n".join(lines)

    def _ensure_labels(self) -> None:
        """Create Gmail labels used by this agent if they don't exist yet."""
        for label_name in [_LABEL_REJECTION, _LABEL_PROCESSED, _LABEL_INTERVIEW, _LABEL_OFFER]:
            label_id = self._gmail.get_or_create_label(label_name)
            if label_id:
                self._label_ids[label_name] = label_id
