"""Token bucket rate limiter for LinkedIn page loads and application submissions."""

import asyncio
import time
from dataclasses import dataclass, field

from .logging import get_logger

logger = get_logger(__name__)


@dataclass
class TokenBucket:
    """
    Async token bucket rate limiter.

    Tokens refill at `rate` per second up to `capacity`.
    Each `acquire()` call consumes one token, blocking if the bucket is empty.
    """
    capacity: float          # Maximum tokens
    rate: float              # Tokens added per second
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        added = elapsed * self.rate
        self._tokens = min(self.capacity, self._tokens + added)
        self._last_refill = now

    async def acquire(self, tokens: float = 1.0) -> float:
        """
        Acquire `tokens` from the bucket, waiting if necessary.

        Returns the time spent waiting (seconds).
        """
        async with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0

            # Calculate wait time for enough tokens to accumulate
            deficit = tokens - self._tokens
            wait = deficit / self.rate
            logger.debug(f"Rate limit: waiting {wait:.2f}s for token")

        await asyncio.sleep(wait)

        async with self._lock:
            self._refill()
            self._tokens -= tokens

        return wait

    @property
    def available(self) -> float:
        """Current token count (approximate, not thread-safe for decisions)."""
        self._refill()
        return self._tokens


class RateLimiter:
    """
    Named collection of token buckets for different resource types.

    Usage:
        limiter = RateLimiter.from_settings(settings)
        await limiter.linkedin_page()     # before each LinkedIn page load
        await limiter.application()       # before each application submission
    """

    def __init__(
        self,
        linkedin_pages_per_hour: int = 40,
        applications_per_day: int = 25,
    ) -> None:
        # LinkedIn page loads: e.g. 40/hour → 40/3600 per second
        self._page_bucket = TokenBucket(
            capacity=min(linkedin_pages_per_hour, 10),  # burst up to 10
            rate=linkedin_pages_per_hour / 3600,
        )
        # Applications: spread over the day
        self._app_bucket = TokenBucket(
            capacity=min(applications_per_day, 5),
            rate=applications_per_day / 86400,
        )

    async def linkedin_page(self) -> None:
        """Acquire a LinkedIn page-load token. Blocks if rate limit reached."""
        waited = await self._page_bucket.acquire()
        if waited > 0:
            logger.info(f"LinkedIn rate limit: waited {waited:.1f}s before page load")

    async def application(self) -> None:
        """Acquire an application-submission token. Blocks if daily limit reached."""
        waited = await self._app_bucket.acquire()
        if waited > 0:
            logger.info(f"Application rate limit: waited {waited:.1f}s before submitting")

    @classmethod
    def from_settings(cls, settings: dict) -> "RateLimiter":
        rl = settings.get("rate_limits", {})
        return cls(
            linkedin_pages_per_hour=rl.get("linkedin_page_loads_per_hour", 40),
            applications_per_day=rl.get("applications_per_day", 25),
        )

    @property
    def page_tokens_available(self) -> float:
        return self._page_bucket.available

    @property
    def app_tokens_available(self) -> float:
        return self._app_bucket.available
