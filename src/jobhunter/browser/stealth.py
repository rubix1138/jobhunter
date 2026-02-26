"""Anti-detection helpers: random delays, session warmup, abort on restriction."""

import asyncio
import random
from typing import Optional

from patchright.async_api import Page

from ..utils.logging import get_logger

logger = get_logger(__name__)


async def random_delay(min_s: float = 1.0, max_s: float = 3.0) -> None:
    """Sleep for a random duration to mimic human timing."""
    delay = random.uniform(min_s, max_s)
    await asyncio.sleep(delay)


async def application_delay(min_s: float = 30.0, max_s: float = 90.0) -> None:
    """Longer delay between application submissions."""
    await random_delay(min_s, max_s)


async def micro_delay(min_ms: int = 80, max_ms: int = 300) -> None:
    """Very short delay to simulate natural typing/click timing."""
    await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


async def human_type(page: Page, selector: str, text: str) -> None:
    """Type text character by character with random delays to mimic human typing."""
    element = page.locator(selector).first
    await element.click()
    await micro_delay()
    for char in text:
        await element.type(char)
        await micro_delay(40, 150)


async def random_scroll(page: Page, min_px: int = 200, max_px: int = 800) -> None:
    """Scroll the page by a random amount."""
    pixels = random.randint(min_px, max_px)
    await page.mouse.wheel(0, pixels)
    await micro_delay(200, 600)


async def warmup_session(page: Page) -> None:
    """Brief LinkedIn feed interaction before searching — reduces detection risk."""
    logger.debug("Running session warmup on LinkedIn feed")
    try:
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        await random_delay(2.0, 4.0)
        # Scroll the feed naturally
        for _ in range(random.randint(2, 4)):
            await random_scroll(page, 300, 700)
            await random_delay(1.0, 2.5)
    except Exception as e:
        logger.warning(f"Session warmup failed (non-fatal): {e}")


async def is_restricted(page: Page) -> Optional[str]:
    """
    Check the current page for real restriction / CAPTCHA signals.

    Uses URL patterns and visible DOM elements rather than raw HTML, because
    LinkedIn embeds the word 'captcha' in its own JS on every page.
    Returns a description string if restricted, None if clear.
    """
    try:
        url = page.url.lower()

        # URL-based signals are the most reliable
        url_signals = [
            ("checkpoint/challenge", "Checkpoint challenge"),
            ("checkpoint/lg/login", "Checkpoint login"),
            ("authwall", "Auth wall"),
        ]
        for fragment, description in url_signals:
            if fragment in url:
                return description

        # Check for visible CAPTCHA iframe (e.g. hCaptcha, reCAPTCHA)
        captcha_frame = page.frame_locator("iframe[src*='captcha'], iframe[src*='recaptcha'], iframe[title*='captcha' i]").first
        try:
            if await captcha_frame.locator("body").is_visible(timeout=500):
                return "CAPTCHA iframe visible"
        except Exception:
            pass

        # Check visible page text for restriction messages
        # Use inner_text() which returns only rendered text, not JS/HTML source
        try:
            body_text = (await page.inner_text("body", timeout=2000)).lower()
            visible_signals = [
                ("verify you're a human", "Human verification challenge"),
                ("unusual activity", "Unusual activity warning"),
                ("account restricted", "Account restriction notice"),
                ("temporarily restricted", "Temporary restriction"),
            ]
            for keyword, description in visible_signals:
                if keyword in body_text:
                    return description
        except Exception:
            pass

        return None
    except Exception:
        return None
