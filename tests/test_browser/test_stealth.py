"""Tests for stealth helpers — restriction detection uses mocked Playwright pages."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from jobhunter.browser.stealth import is_restricted


def _make_page(url: str = "https://www.linkedin.com/jobs/search/", body_text: str = "") -> MagicMock:
    """Return a mock Playwright Page with configurable URL and inner_text."""
    page = MagicMock()
    page.url = url
    page.inner_text = AsyncMock(return_value=body_text)

    # frame_locator().first.locator().is_visible() → False by default (no CAPTCHA iframe)
    mock_frame_loc = MagicMock()
    mock_frame_loc.locator.return_value.is_visible = AsyncMock(side_effect=Exception("not found"))
    page.frame_locator.return_value.first = mock_frame_loc

    return page


class TestIsRestricted:
    @pytest.mark.asyncio
    async def test_clean_page_returns_none(self):
        page = _make_page(body_text="LinkedIn Jobs Find your next role")
        result = await is_restricted(page)
        assert result is None

    @pytest.mark.asyncio
    async def test_checkpoint_url_detected(self):
        page = _make_page(url="https://www.linkedin.com/checkpoint/challenge/abc123")
        result = await is_restricted(page)
        assert result is not None
        assert "Checkpoint" in result

    @pytest.mark.asyncio
    async def test_authwall_url_detected(self):
        page = _make_page(url="https://www.linkedin.com/authwall?trk=xyz")
        result = await is_restricted(page)
        assert result is not None
        assert "wall" in result.lower() or "Auth" in result

    @pytest.mark.asyncio
    async def test_unusual_activity_in_visible_text(self):
        page = _make_page(body_text="We noticed unusual activity on your account")
        result = await is_restricted(page)
        assert result is not None

    @pytest.mark.asyncio
    async def test_account_restricted_in_visible_text(self):
        page = _make_page(body_text="Your account has been temporarily restricted")
        result = await is_restricted(page)
        assert result is not None

    @pytest.mark.asyncio
    async def test_verify_human_in_visible_text(self):
        page = _make_page(body_text="Please verify you're a human to continue")
        result = await is_restricted(page)
        assert result is not None

    @pytest.mark.asyncio
    async def test_word_captcha_in_js_source_not_flagged(self):
        """LinkedIn embeds 'captcha' in JS on every page — raw HTML must NOT trigger detection."""
        # This simulates inner_text() (rendered text only), which won't include JS
        page = _make_page(body_text="CISO jobs in Atlanta, GA | LinkedIn")
        result = await is_restricted(page)
        assert result is None

    @pytest.mark.asyncio
    async def test_page_error_returns_none(self):
        """Errors during check should not crash — return None (non-blocking)."""
        page = MagicMock()
        page.url = "https://www.linkedin.com/jobs/"
        page.inner_text = AsyncMock(side_effect=Exception("page closed"))
        page.frame_locator.side_effect = Exception("context destroyed")
        result = await is_restricted(page)
        assert result is None
