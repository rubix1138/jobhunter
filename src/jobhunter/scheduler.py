"""APScheduler wiring — runs all agents on configured schedules with graceful shutdown."""

import asyncio
import random
import signal
from datetime import datetime, date
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .agents.apply_agent import ApplyAgent
from .agents.email_agent import EmailAgent
from .agents.search_agent import SearchAgent
from .browser.context import BrowserSession
from .db.engine import get_connection, run_migrations
from .gmail.auth import get_gmail_service
from .gmail.client import GmailClient
from .llm.client import ClaudeClient
from .utils.logging import get_logger
from .utils.profile_loader import UserProfile
from .utils.rate_limiter import RateLimiter

logger = get_logger(__name__)


class JobHunterScheduler:
    """
    Orchestrates all three agents on their configured schedules.

    - Search agent: every 4-6 hours (randomised), runs immediately on start
    - Apply agent: every 2-3 hours (randomised)
    - Email agent: every 5 min during business hours, 30 min off-hours
    - Daily summary: logged at 22:00

    Handles SIGINT/SIGTERM for graceful shutdown.
    """

    def __init__(
        self,
        settings: dict,
        profile: UserProfile,
        queries: list[dict],
        db_path: str,
    ) -> None:
        self._settings = settings
        self._profile = profile
        self._queries = queries
        self._db_path = db_path

        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._session: Optional[BrowserSession] = None
        self._llm: Optional[ClaudeClient] = None
        self._gmail: Optional[GmailClient] = None
        self._shutdown_event = asyncio.Event()
        self._last_email_run: Optional[datetime] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Build agents, schedule jobs, and block until shutdown signal."""
        logger.info("JobHunter scheduler starting")

        self._llm = _build_llm(self._settings)

        self._session = BrowserSession()
        await self._session.start()
        await self._session.ensure_linkedin_session()

        gmail_svc = get_gmail_service()
        self._gmail = GmailClient(gmail_svc)

        self._schedule_jobs()
        self._register_signal_handlers()

        self._scheduler.start()
        logger.info(
            "Scheduler started — search/apply/email agents are live. "
            "Press Ctrl+C to stop."
        )
        await self._shutdown_event.wait()

    async def stop(self) -> None:
        """Stop the scheduler and close browser session."""
        logger.info("JobHunter scheduler stopping")
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)
        if self._session:
            await self._session.stop()
        logger.info("Shutdown complete")

    # ── Scheduling ─────────────────────────────────────────────────────────────

    def _schedule_jobs(self) -> None:
        sched = self._settings.get("scheduler", {})

        # Search: randomised interval between min and max, run immediately
        search_interval = random.randint(
            sched.get("search_interval_min", 240),
            sched.get("search_interval_max", 360),
        )
        self._scheduler.add_job(
            self._run_search,
            "interval",
            minutes=search_interval,
            id="search",
            next_run_time=datetime.now(),
        )
        logger.info(f"Search scheduled every {search_interval} minutes")

        # Apply: randomised interval
        apply_interval = random.randint(
            sched.get("apply_interval_min", 120),
            sched.get("apply_interval_max", 180),
        )
        self._scheduler.add_job(
            self._run_apply,
            "interval",
            minutes=apply_interval,
            id="apply",
        )
        logger.info(f"Apply scheduled every {apply_interval} minutes")

        # Email: fixed 5-minute poll; actual frequency governed by _run_email logic
        self._scheduler.add_job(
            self._run_email,
            "interval",
            minutes=5,
            id="email",
            next_run_time=datetime.now(),
        )
        logger.info("Email polling scheduled every 5 minutes (throttled off-hours)")

        # Daily summary at 22:00
        self._scheduler.add_job(
            self._daily_summary,
            "cron",
            hour=22,
            minute=0,
            id="daily_summary",
        )

    def _register_signal_handlers(self) -> None:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig,
                lambda: asyncio.create_task(self._handle_shutdown()),
            )

    async def _handle_shutdown(self) -> None:
        logger.info("Shutdown signal received — finishing current jobs then exiting")
        self._shutdown_event.set()

    # ── Agent runners ──────────────────────────────────────────────────────────

    async def _run_search(self) -> None:
        logger.info("Scheduled search run starting")
        try:
            rate_limiter = RateLimiter.from_settings(self._settings)
            agent = SearchAgent(
                session=self._session,
                llm=self._llm,
                profile=self._profile,
                queries=self._queries,
                rate_limiter=rate_limiter,
                settings=self._settings,
                db_path=self._db_path,
            )
            result = await agent.run()
            logger.info(f"Search run complete: {result.jobs_found} jobs found")
        except Exception as e:
            logger.error(f"Search run failed: {e}", exc_info=True)

    async def _run_apply(self) -> None:
        logger.info("Scheduled apply run starting")
        try:
            agent = ApplyAgent(
                session=self._session,
                llm=self._llm,
                profile=self._profile,
                settings=self._settings,
                db_path=self._db_path,
            )
            result = await agent.run()
            logger.info(f"Apply run complete: {result.apps_submitted} submitted")
        except Exception as e:
            logger.error(f"Apply run failed: {e}", exc_info=True)

    async def _run_email(self) -> None:
        sched = self._settings.get("scheduler", {})
        biz_start = sched.get("business_hours_start", 8)
        biz_end = sched.get("business_hours_end", 20)
        now = datetime.now()
        in_biz_hours = biz_start <= now.hour < biz_end
        min_interval = (
            sched.get("email_interval_business", 5)
            if in_biz_hours
            else sched.get("email_interval_offhours", 30)
        )

        if self._last_email_run is not None:
            elapsed = (now - self._last_email_run).total_seconds() / 60
            if elapsed < min_interval:
                return  # too soon

        logger.info(
            f"Email run starting (business_hours={in_biz_hours}, interval={min_interval}m)"
        )
        try:
            agent = EmailAgent(
                gmail=self._gmail,
                llm=self._llm,
                profile=self._profile,
                settings=self._settings,
                db_path=self._db_path,
            )
            result = await agent.run()
            self._last_email_run = datetime.now()
            logger.info(f"Email run complete: {result.emails_processed} processed")
        except Exception as e:
            logger.error(f"Email run failed: {e}", exc_info=True)

    async def _daily_summary(self) -> None:
        logger.info("Generating daily summary")
        try:
            conn = get_connection(self._db_path)
            run_migrations(conn)
            summary = build_daily_summary(conn)
            conn.close()
            print_daily_summary(summary)
            logger.info("Daily summary generated", extra=summary)
        except Exception as e:
            logger.error(f"Daily summary failed: {e}", exc_info=True)


# ── One-shot agent runners (used by CLI subcommands) ─────────────────────────

async def run_search_once(settings: dict, profile: UserProfile, queries: list[dict], db_path: str) -> None:
    """Run the search agent a single time (used by `search-now` command)."""
    llm = _build_llm(settings)
    session = BrowserSession()
    await session.start()
    await session.ensure_linkedin_session()
    try:
        rate_limiter = RateLimiter.from_settings(settings)
        agent = SearchAgent(
            session=session,
            llm=llm,
            profile=profile,
            queries=queries,
            rate_limiter=rate_limiter,
            settings=settings,
            db_path=db_path,
        )
        result = await agent.run()
        print(f"Search complete: {result.jobs_found} jobs found")
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nSearch stopped by user — progress already saved.")
    finally:
        await session.stop()


async def run_apply_once(
    settings: dict,
    profile: UserProfile,
    db_path: str,
    dry_run: bool = False,
    review_mode: bool = False,
    apply_type_filter: Optional[list[str]] = None,
) -> None:
    """Run the apply agent a single time (used by `apply-now` command)."""
    llm = _build_llm(settings)
    session = BrowserSession()
    await session.start()
    await session.ensure_linkedin_session()
    try:
        agent = ApplyAgent(
            session=session,
            llm=llm,
            profile=profile,
            settings=settings,
            db_path=db_path,
            dry_run=dry_run,
            review_mode=review_mode,
            apply_type_filter=apply_type_filter,
        )
        result = await agent.run()
        if dry_run:
            print(f"Dry run complete: {result.apps_submitted} resumes generated in data/resumes/")
        elif review_mode:
            print(f"Review run complete: {result.apps_submitted} applications submitted after review")
        else:
            print(f"Apply complete: {result.apps_submitted} applications submitted")
    finally:
        await session.stop()


async def run_email_once(settings: dict, profile: UserProfile, db_path: str) -> None:
    """Run the email agent a single time (used by `check-email` command)."""
    llm = _build_llm(settings)
    gmail_svc = get_gmail_service()
    gmail = GmailClient(gmail_svc)
    agent = EmailAgent(
        gmail=gmail,
        llm=llm,
        profile=profile,
        settings=settings,
        db_path=db_path,
    )
    result = await agent.run()
    print(f"Email check complete: {result.emails_processed} messages processed")


# ── Daily summary ─────────────────────────────────────────────────────────────

def build_daily_summary(conn) -> dict:
    """Query the DB for today's activity metrics."""
    today = date.today().isoformat()

    jobs_found = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE date(discovered_at) = ?", (today,)
    ).fetchone()[0]

    # Applications count by created_at; status 'submitted' indicates a completed submission
    apps_submitted = conn.execute(
        "SELECT COUNT(*) FROM applications "
        "WHERE date(created_at) = ? AND status = 'submitted'",
        (today,),
    ).fetchone()[0]

    # email_log uses processed_at (set when the record is created)
    emails_processed = conn.execute(
        "SELECT COUNT(*) FROM email_log WHERE date(processed_at) = ?", (today,)
    ).fetchone()[0]

    rejections = conn.execute(
        "SELECT COUNT(*) FROM email_log "
        "WHERE date(processed_at) = ? AND classification = 'rejection'",
        (today,),
    ).fetchone()[0]

    interviews = conn.execute(
        "SELECT COUNT(*) FROM email_log "
        "WHERE date(processed_at) = ? AND classification = 'interview_invite'",
        (today,),
    ).fetchone()[0]

    llm_cost = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE date(created_at) = ?",
        (today,),
    ).fetchone()[0]

    return {
        "date": today,
        "jobs_found": jobs_found,
        "apps_submitted": apps_submitted,
        "emails_processed": emails_processed,
        "rejections": rejections,
        "interviews": interviews,
        "llm_cost_usd": round(float(llm_cost), 4),
    }


def print_daily_summary(summary: dict) -> None:
    """Print a formatted daily summary to stdout."""
    sep = "=" * 52
    print(f"\n{sep}")
    print(f"  JobHunter Daily Summary — {summary['date']}")
    print(sep)
    print(f"  Jobs found today:      {summary['jobs_found']:>6}")
    print(f"  Applications sent:     {summary['apps_submitted']:>6}")
    print(f"  Emails processed:      {summary['emails_processed']:>6}")
    print(f"  Rejections received:   {summary['rejections']:>6}")
    print(f"  Interview invites:     {summary['interviews']:>6}")
    print(f"  LLM spend today:       ${summary['llm_cost_usd']:>9.4f}")
    print(f"{sep}\n")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_llm(settings: dict) -> ClaudeClient:
    models = settings.get("models", {})
    return ClaudeClient(
        sonnet_model=models.get("routine", "claude-sonnet-4-6"),
        opus_model=models.get("writing", "claude-opus-4-6"),
    )
