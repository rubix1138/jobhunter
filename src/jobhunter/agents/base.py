"""BaseAgent ABC — lifecycle, retry, structured logging, and agent_runs tracking."""

import asyncio
import json
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from ..db.engine import get_connection, run_migrations
from ..db.repository import AgentRunRepo, LlmUsageRepo
from ..utils.logging import get_logger


@dataclass
class AgentResult:
    """Returned by every agent run."""
    success: bool
    jobs_found: int = 0
    apps_submitted: int = 0
    emails_processed: int = 0
    error_message: Optional[str] = None
    details: dict = field(default_factory=dict)


class AgentError(Exception):
    """Raised when an agent encounters an unrecoverable error."""


class RetryableError(Exception):
    """Raised when an operation should be retried."""


class BaseAgent(ABC):
    """
    Abstract base class for all JobHunter agents.

    Subclasses implement `run_once()` and optionally override
    `before_run()` / `after_run()` for setup and teardown.

    Handles:
    - DB connection and agent_runs audit logging
    - Daily budget enforcement
    - Configurable retry with exponential back-off
    - Structured logging with agent context
    - Graceful error capture and status recording
    """

    #: Override in subclasses to set the agent name used in DB and logs
    name: str = "base_agent"

    def __init__(
        self,
        db_path: Optional[str] = None,
        max_retries: int = 3,
        retry_base_delay: float = 5.0,
        daily_budget_usd: float = 15.0,
    ) -> None:
        self._db_path = db_path
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._daily_budget_usd = daily_budget_usd

        self._conn = None
        self._run_repo: Optional[AgentRunRepo] = None
        self._llm_repo: Optional[LlmUsageRepo] = None
        self._run_id: Optional[int] = None
        self.logger = get_logger(f"jobhunter.{self.name}")

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _open_db(self) -> None:
        self._conn = get_connection(self._db_path)
        run_migrations(self._conn)
        self._run_repo = AgentRunRepo(self._conn)
        self._llm_repo = LlmUsageRepo(self._conn)

    def _close_db(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Budget ────────────────────────────────────────────────────────────────

    def is_over_budget(self) -> bool:
        """Return True if today's LLM spend exceeds the daily budget."""
        if self._llm_repo is None:
            return False
        cost = self._llm_repo.daily_cost()
        over = cost >= self._daily_budget_usd
        if over:
            self.logger.warning(
                f"Daily budget exceeded: ${cost:.4f} >= ${self._daily_budget_usd:.2f}"
            )
        return over

    def log_llm_usage(
        self,
        model: str,
        purpose: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: Optional[float] = None,
        job_id: Optional[int] = None,
        **_extra,  # absorb cache_creation_tokens, cache_read_tokens, etc.
    ) -> None:
        """Record a Claude API call in llm_usage."""
        if self._llm_repo is None:
            return
        from ..db.models import LlmUsage
        usage = LlmUsage(
            agent_name=self.name,
            model=model,
            purpose=purpose,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            job_id=job_id,
        )
        self._llm_repo.insert(usage)

    # ── Retry logic ───────────────────────────────────────────────────────────

    async def _with_retry(self, coro_fn, *args, **kwargs):
        """
        Call an async function with exponential back-off retry.

        Retries on RetryableError up to max_retries times.
        Re-raises AgentError and any non-retryable exceptions immediately.
        """
        last_exc = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return await coro_fn(*args, **kwargs)
            except AgentError:
                raise
            except RetryableError as e:
                last_exc = e
                delay = self._retry_base_delay * (2 ** (attempt - 1))
                self.logger.warning(
                    f"Retryable error (attempt {attempt}/{self._max_retries}): {e} "
                    f"— retrying in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
            except Exception as e:
                # Unexpected error — don't retry
                raise AgentError(str(e)) from e

        raise AgentError(f"Max retries ({self._max_retries}) exceeded: {last_exc}")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def before_run(self) -> None:
        """Optional setup hook called before run_once(). Override in subclasses."""

    async def after_run(self, result: AgentResult) -> None:
        """Optional teardown hook called after run_once(). Override in subclasses."""

    @abstractmethod
    async def run_once(self) -> AgentResult:
        """
        Execute one agent cycle. Must be implemented by every subclass.

        Returns an AgentResult describing what happened.
        Raise AgentError for unrecoverable failures.
        Raise RetryableError for transient failures that should be retried.
        """

    async def run(self) -> AgentResult:
        """
        Public entry point — wraps run_once() with full lifecycle management:
        DB open, agent_run audit record, before/after hooks, error capture.
        """
        self._open_db()
        self._run_id = self._run_repo.start(self.name)
        self.logger.info(f"{self.name} starting (run_id={self._run_id})")

        result = AgentResult(success=False)
        try:
            await self.before_run()
            result = await self._with_retry(self.run_once)
            await self.after_run(result)
            result.success = True
            # Build a result summary so "completed" is informative, not just "OK"
            details = result.details if isinstance(result.details, dict) else {}
            dry_run = bool(details.get("dry_run", False))
            generated = int(details.get("generated", 0) or 0)
            parts = []
            if result.jobs_found:
                parts.append(f"{result.jobs_found} jobs found")
            if dry_run and generated > 0:
                parts.append(f"{generated} generated")
            elif result.apps_submitted is not None:
                parts.append(f"{result.apps_submitted} submitted")
            if result.emails_processed:
                parts.append(f"{result.emails_processed} emails")
            summary = f" ({', '.join(parts)})" if parts else ""
            self.logger.info(
                f"{self.name} completed{summary}",
                extra={
                    "jobs_found": result.jobs_found,
                    "apps_submitted": result.apps_submitted,
                    "emails_processed": result.emails_processed,
                },
            )
        except AgentError as e:
            result.error_message = str(e)
            self.logger.error(f"{self.name} failed: {e}")
        except Exception as e:
            result.error_message = f"Unexpected: {e}\n{traceback.format_exc()}"
            self.logger.error(f"{self.name} unexpected error: {e}", exc_info=True)
        finally:
            status = "success" if result.success else "error"
            self._run_repo.finish(
                self._run_id,
                status=status,
                jobs_found=result.jobs_found,
                apps_submitted=result.apps_submitted,
                emails_processed=result.emails_processed,
                error_message=result.error_message,
                details=result.details or None,
            )
            self._close_db()
            self.logger.info(f"{self.name} run_id={self._run_id} finished: {status}")

        return result
