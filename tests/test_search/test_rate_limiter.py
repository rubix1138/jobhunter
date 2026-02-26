"""Tests for the token bucket rate limiter."""

import asyncio
import time
import pytest

from jobhunter.utils.rate_limiter import RateLimiter, TokenBucket


class TestTokenBucket:
    @pytest.mark.asyncio
    async def test_immediate_acquire_when_full(self):
        bucket = TokenBucket(capacity=10, rate=1.0)
        waited = await bucket.acquire()
        assert waited == 0.0

    @pytest.mark.asyncio
    async def test_tokens_depleted(self):
        # capacity=2, rate=5/s → after depleting, next token takes ~0.2s
        bucket = TokenBucket(capacity=2, rate=5.0)
        for _ in range(2):
            await bucket.acquire()
        start = time.monotonic()
        await bucket.acquire()
        elapsed = time.monotonic() - start
        assert elapsed > 0.1  # had to wait for refill

    @pytest.mark.asyncio
    async def test_tokens_refill_over_time(self):
        # 10 tokens/sec, start with 0
        bucket = TokenBucket(capacity=5, rate=10.0)
        bucket._tokens = 0.0
        bucket._last_refill = time.monotonic() - 0.2  # pretend 0.2s elapsed
        bucket._refill()
        assert bucket._tokens == pytest.approx(2.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_capacity_not_exceeded(self):
        bucket = TokenBucket(capacity=5, rate=100.0)
        await asyncio.sleep(0.1)
        bucket._refill()
        assert bucket._tokens <= 5.0

    @pytest.mark.asyncio
    async def test_available_property(self):
        bucket = TokenBucket(capacity=10, rate=1.0)
        assert bucket.available <= 10.0
        assert bucket.available >= 0.0


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_linkedin_page_does_not_block_initially(self):
        limiter = RateLimiter(linkedin_pages_per_hour=40, applications_per_day=25)
        start = time.monotonic()
        await limiter.linkedin_page()
        elapsed = time.monotonic() - start
        assert elapsed < 0.5  # should not block on first call

    @pytest.mark.asyncio
    async def test_application_does_not_block_initially(self):
        limiter = RateLimiter(linkedin_pages_per_hour=40, applications_per_day=25)
        start = time.monotonic()
        await limiter.application()
        elapsed = time.monotonic() - start
        assert elapsed < 0.5

    def test_from_settings(self):
        settings = {
            "rate_limits": {
                "linkedin_page_loads_per_hour": 30,
                "applications_per_day": 20,
            }
        }
        limiter = RateLimiter.from_settings(settings)
        assert limiter is not None

    def test_from_settings_defaults(self):
        limiter = RateLimiter.from_settings({})
        assert limiter is not None

    def test_token_availability(self):
        limiter = RateLimiter(linkedin_pages_per_hour=40, applications_per_day=25)
        assert limiter.page_tokens_available > 0
        assert limiter.app_tokens_available > 0
