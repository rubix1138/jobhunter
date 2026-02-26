"""High-level browser action helpers with selector fallback chains."""

import asyncio
from pathlib import Path
from typing import Optional, Sequence

from patchright.async_api import Locator, Page, TimeoutError as PlaywrightTimeout

from ..utils.logging import get_logger
from .stealth import micro_delay, random_delay

logger = get_logger(__name__)

# Default timeout for element interactions (ms)
_ELEMENT_TIMEOUT = 10_000
# Timeout when just checking visibility (ms)
_CHECK_TIMEOUT = 3_000


async def wait_and_click(
    page: Page,
    selectors: str | Sequence[str],
    timeout: int = _ELEMENT_TIMEOUT,
    delay_after: bool = True,
) -> bool:
    """
    Try each selector in order, clicking the first visible match.

    Args:
        page: Active Playwright page.
        selectors: A single CSS/XPath selector or a list to try in order.
        timeout: Per-selector wait timeout in ms.
        delay_after: Whether to add a small random delay after clicking.

    Returns:
        True if a click succeeded, False if all selectors failed.
    """
    if isinstance(selectors, str):
        selectors = [selectors]

    for sel in selectors:
        try:
            locator = page.locator(sel).first
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.scroll_into_view_if_needed()
            await micro_delay()
            await locator.click()
            if delay_after:
                await random_delay(0.5, 1.5)
            logger.debug(f"Clicked: {sel}")
            return True
        except PlaywrightTimeout:
            logger.debug(f"Selector not found/visible: {sel}")
        except Exception as e:
            logger.debug(f"Click failed for {sel!r}: {e}")

    logger.warning(f"wait_and_click: all selectors exhausted: {selectors}")
    return False


async def fill_field(
    page: Page,
    selectors: str | Sequence[str],
    value: str,
    clear_first: bool = True,
    timeout: int = _ELEMENT_TIMEOUT,
) -> bool:
    """
    Fill a form field, trying each selector in order.

    Args:
        page: Active Playwright page.
        selectors: Selector or list of selectors to try.
        value: Text to fill.
        clear_first: Whether to clear existing content before typing.
        timeout: Per-selector wait timeout in ms.

    Returns:
        True if fill succeeded, False if all selectors failed.
    """
    if isinstance(selectors, str):
        selectors = [selectors]

    for sel in selectors:
        try:
            locator = page.locator(sel).first
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.scroll_into_view_if_needed()
            if clear_first:
                await locator.triple_click()
                await locator.fill("")
            await locator.type(value, delay=random_delay.__defaults__[0] * 1000 // 10)
            await micro_delay()
            logger.debug(f"Filled {sel!r} with {len(value)} chars")
            return True
        except PlaywrightTimeout:
            logger.debug(f"fill_field: selector not found: {sel}")
        except Exception as e:
            logger.debug(f"fill_field failed for {sel!r}: {e}")

    logger.warning(f"fill_field: all selectors exhausted: {selectors}")
    return False


async def upload_file(
    page: Page,
    selectors: str | Sequence[str],
    file_path: str | Path,
    timeout: int = _ELEMENT_TIMEOUT,
) -> bool:
    """
    Upload a file via a file input element.

    Args:
        page: Active Playwright page.
        selectors: File input selector(s) to try.
        file_path: Path to the file to upload.
        timeout: Per-selector wait timeout in ms.

    Returns:
        True if upload was initiated, False if all selectors failed.
    """
    if isinstance(selectors, str):
        selectors = [selectors]

    file_path = Path(file_path)
    if not file_path.exists():
        logger.error(f"upload_file: file not found: {file_path}")
        return False

    for sel in selectors:
        try:
            locator = page.locator(sel).first
            await locator.wait_for(timeout=timeout)
            await locator.set_input_files(str(file_path))
            await random_delay(1.0, 2.0)
            logger.debug(f"Uploaded {file_path.name} via {sel!r}")
            return True
        except PlaywrightTimeout:
            logger.debug(f"upload_file: selector not found: {sel}")
        except Exception as e:
            logger.debug(f"upload_file failed for {sel!r}: {e}")

    logger.warning(f"upload_file: all selectors exhausted: {selectors}")
    return False


async def scroll_to_bottom(page: Page, pause_s: float = 0.8, max_scrolls: int = 20) -> int:
    """
    Scroll to the bottom of the page incrementally.

    Returns:
        Number of scroll steps taken.
    """
    last_height = await page.evaluate("document.body.scrollHeight")
    steps = 0
    for _ in range(max_scrolls):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(pause_s)
        new_height = await page.evaluate("document.body.scrollHeight")
        steps += 1
        if new_height == last_height:
            break
        last_height = new_height
    return steps


async def scroll_into_view(page: Page, selector: str) -> bool:
    """Scroll a specific element into the viewport."""
    try:
        await page.locator(selector).first.scroll_into_view_if_needed()
        return True
    except Exception:
        return False


async def select_option(
    page: Page,
    selectors: str | Sequence[str],
    value: str,
    timeout: int = _ELEMENT_TIMEOUT,
) -> bool:
    """
    Select an option from a <select> element by value or label.

    Returns:
        True if selection succeeded.
    """
    if isinstance(selectors, str):
        selectors = [selectors]

    for sel in selectors:
        try:
            locator = page.locator(sel).first
            await locator.wait_for(state="visible", timeout=timeout)
            # Try by value first, then by label
            try:
                await locator.select_option(value=value)
            except Exception:
                await locator.select_option(label=value)
            await micro_delay()
            logger.debug(f"Selected {value!r} in {sel!r}")
            return True
        except PlaywrightTimeout:
            logger.debug(f"select_option: selector not found: {sel}")
        except Exception as e:
            logger.debug(f"select_option failed for {sel!r}: {e}")

    logger.warning(f"select_option: all selectors exhausted: {selectors}")
    return False


async def is_visible(page: Page, selector: str, timeout: int = _CHECK_TIMEOUT) -> bool:
    """Return True if the element is visible within the timeout."""
    try:
        await page.locator(selector).first.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


async def get_text(page: Page, selector: str, timeout: int = _CHECK_TIMEOUT) -> Optional[str]:
    """Return the inner text of the first matching element, or None."""
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout)
        return (await locator.inner_text()).strip()
    except Exception:
        return None


async def wait_for_navigation_settle(page: Page, timeout: int = 5_000) -> None:
    """Wait for the page to stop making network requests (best-effort)."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeout:
        pass  # Acceptable — some pages never fully idle
