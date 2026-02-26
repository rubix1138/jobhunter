"""Persistent Playwright browser context and LinkedIn session management."""

import asyncio
from pathlib import Path
from typing import Optional

from playwright_stealth import Stealth
from patchright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from ..utils.logging import get_logger
from .stealth import is_restricted, random_delay, warmup_session

# JS init script applied to every page — patches common fingerprint tells
# that playwright-stealth may not cover on all browser versions.
_NAVIGATOR_PATCH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    if (!window.chrome) {
        window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
    }
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    Object.defineProperty(navigator, 'plugins', {
        get: () => { const arr = [1, 2, 3]; arr.item = i => arr[i]; arr.namedItem = () => null; arr.refresh = () => {}; return arr; }
    });
"""

logger = get_logger(__name__)

_LINKEDIN_HOME = "https://www.linkedin.com"
_LINKEDIN_LOGIN = "https://www.linkedin.com/login"
_LINKEDIN_FEED = "https://www.linkedin.com/feed/"

# Timeout for page navigation (ms)
_NAV_TIMEOUT = 30_000
# How long to wait for the user to complete manual login (ms) — 5 minutes
_MANUAL_LOGIN_TIMEOUT = 300_000


class BrowserSession:
    """
    Wraps a persistent Playwright browser context.

    The browser_state_dir stores cookies, localStorage, and session data across
    restarts so the user only needs to log in once (or when the session expires).
    """

    def __init__(
        self,
        state_dir: str = "data/browser_state",
        headless: bool = False,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._headless = headless

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> "BrowserSession":
        """Launch Playwright and open a persistent browser context."""
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(self._state_dir),
            headless=self._headless,
            # Do NOT override user-agent — default is less detectable
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
            args=[
                "--disable-blink-features=AutomationControlled",
                # --no-sandbox disables the network service sandbox and breaks DNS
                # in headed mode on Linux.  --disable-setuid-sandbox is the correct
                # replacement: it only drops the setuid privilege requirement.
                "--disable-setuid-sandbox",
            ],
            ignore_default_args=["--enable-automation"],
        )
        self._context.set_default_timeout(_NAV_TIMEOUT)
        self._context.set_default_navigation_timeout(_NAV_TIMEOUT)

        # Apply JS-layer stealth patches to every page in this context.
        # Patchright fixes CDP-level detection; playwright-stealth patches
        # navigator.webdriver, window.chrome, plugins, WebGL, etc.
        #
        # IMPORTANT: We bypass patchright's add_init_script() wrapper intentionally.
        # Patchright's wrapper calls install_inject_route() which installs a "**/*"
        # route handler that redirects ALL document navigations to the unresolvable
        # host "patchright-init-script-inject.internal", causing net::ERR_NAME_NOT_RESOLVED
        # on every page.goto() when using a headed persistent context.
        # Sending "addInitScript" directly to the channel still delivers scripts via
        # Page.addScriptToEvaluateOnNewDocument (CDP), which is exactly what we want.
        _impl_ctx = self._context._impl_obj
        stealth_script = Stealth().script_payload
        if stealth_script:
            await _impl_ctx._channel.send(
                "addInitScript", None, dict(source=stealth_script)
            )
        await _impl_ctx._channel.send(
            "addInitScript", None, dict(source=_NAVIGATOR_PATCH_SCRIPT)
        )

        # Reuse existing page or open a new one
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

        logger.info("Browser session started", extra={"headless": self._headless})
        return self

    async def stop(self) -> None:
        """Close browser context and Playwright cleanly."""
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass  # Connection may already be closed (e.g. after Ctrl+C)
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        logger.info("Browser session stopped")

    async def __aenter__(self) -> "BrowserSession":
        return await self.start()

    async def __aexit__(self, *_) -> None:
        await self.stop()

    # ── Page access ───────────────────────────────────────────────────────────

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("BrowserSession not started — call start() first")
        return self._page

    async def new_page(self) -> Page:
        """Open an additional tab in the same context."""
        return await self._context.new_page()

    # ── LinkedIn session management ───────────────────────────────────────────

    async def is_linkedin_logged_in(self) -> bool:
        """Return True if the current session has an active LinkedIn login."""
        try:
            await self._page.goto(_LINKEDIN_FEED, wait_until="domcontentloaded")
            await random_delay(1.0, 2.0)
            url = self._page.url
            # Redirected to login/auth/checkpoint → not authenticated
            if any(k in url for k in ("login", "authwall", "signup", "checkpoint")):
                return False
            # Successfully on the feed (or another authenticated page)
            return "/feed" in url or "linkedin.com/in/" in url
        except Exception as e:
            logger.warning(f"Error checking LinkedIn login state: {e}")
            return False

    async def wait_for_manual_login(self) -> bool:
        """
        Navigate to the LinkedIn login page and wait for the user to authenticate
        manually (passkey, SSO, password — whatever they normally use).

        The browser window will be visible. The user has up to 5 minutes to
        complete login. Returns True once the feed is reached, False on timeout.
        """
        logger.info("Navigating to LinkedIn login page for manual authentication")
        try:
            await self._page.goto(_LINKEDIN_LOGIN, wait_until="domcontentloaded")
        except Exception:
            # Page may already be navigating; proceed to wait regardless
            pass

        timeout_s = _MANUAL_LOGIN_TIMEOUT // 1000
        print(
            f"\n[JobHunter] LinkedIn login required.\n"
            f"  → A browser window is open at linkedin.com/login\n"
            f"  → Please sign in using your normal method (passkey, etc.)\n"
            f"  → Waiting up to {timeout_s}s for you to complete login...\n"
        )
        logger.info(f"Waiting up to {timeout_s}s for manual LinkedIn login")

        try:
            await self._page.wait_for_url("**/feed/**", timeout=_MANUAL_LOGIN_TIMEOUT)
            logger.info("Manual login detected — feed URL reached")
            return True
        except Exception:
            logger.error(
                f"Manual login timed out after {timeout_s}s — "
                "session was not established"
            )
            print(
                "\n[JobHunter] Login timed out. "
                "Re-run the command to try again.\n"
            )
            return False

    async def ensure_linkedin_session(self) -> bool:
        """
        Verify the LinkedIn session is active; prompt for manual login if not.

        On first run (or after session expiry), opens the LinkedIn login page
        and waits for the user to authenticate using their normal method
        (passkey, 1Password, etc.). Session cookies are persisted to
        data/browser_state/ so subsequent runs skip login entirely.

        Returns True if the session is ready, False if login was not completed.
        """
        if await self.is_linkedin_logged_in():
            logger.debug("LinkedIn session already active")
            return True

        logger.info("LinkedIn session expired or not found — manual login required")
        success = await self.wait_for_manual_login()
        # wait_for_manual_login() only returns True after reaching /feed/ — no re-check needed
        return success

    async def check_for_restriction(self) -> Optional[str]:
        """
        Check current page for CAPTCHA / restriction signals.
        Returns description string if restricted, None if clear.
        """
        signal = await is_restricted(self._page)
        if signal:
            logger.warning(f"LinkedIn restriction detected: {signal}")
        return signal

    async def run_warmup(self) -> None:
        """Visit LinkedIn feed and scroll before searching."""
        await warmup_session(self._page)
