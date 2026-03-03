"""Security tests for referral URL fetching safeguards."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jobhunter.agents.referral_agent import (
    _assert_public_hostname,
    _fetch_page_text,
    _is_linkedin_hostname,
    _validate_referral_url,
)


class TestReferralUrlValidation:
    def test_rejects_non_https_scheme(self):
        with pytest.raises(ValueError, match="Only https:// URLs are allowed"):
            _validate_referral_url("http://example.com/job")

    def test_rejects_missing_hostname(self):
        with pytest.raises(ValueError, match="valid hostname"):
            _validate_referral_url("https:///no-host")

    def test_rejects_localhost(self):
        with pytest.raises(ValueError, match="Localhost and .local domains"):
            _validate_referral_url("https://localhost/job")

    def test_accepts_https_with_hostname(self):
        url, host = _validate_referral_url("https://example.com/jobs/123")
        assert url == "https://example.com/jobs/123"
        assert host == "example.com"

    def test_linkedin_hostname_matching_is_strict(self):
        assert _is_linkedin_hostname("linkedin.com")
        assert _is_linkedin_hostname("www.linkedin.com")
        assert not _is_linkedin_hostname("linkedin.com.evil.org")
        assert not _is_linkedin_hostname("evil-linkedin.com")


class TestPublicHostnameGuard:
    def test_rejects_loopback_resolution(self):
        with patch("jobhunter.agents.referral_agent.socket.getaddrinfo") as mock_info:
            mock_info.return_value = [
                (2, 1, 6, "", ("127.0.0.1", 443)),
            ]
            with pytest.raises(ValueError, match="non-public network target"):
                _assert_public_hostname("example.com")

    def test_rejects_private_resolution(self):
        with patch("jobhunter.agents.referral_agent.socket.getaddrinfo") as mock_info:
            mock_info.return_value = [
                (2, 1, 6, "", ("10.0.0.9", 443)),
            ]
            with pytest.raises(ValueError, match="non-public network target"):
                _assert_public_hostname("example.com")

    def test_accepts_public_resolution(self):
        with patch("jobhunter.agents.referral_agent.socket.getaddrinfo") as mock_info:
            mock_info.return_value = [
                (2, 1, 6, "", ("93.184.216.34", 443)),
            ]
            _assert_public_hostname("example.com")


class TestFetchPageTextSecurity:
    @pytest.mark.asyncio
    async def test_blocks_file_scheme_before_fetch(self):
        with pytest.raises(ValueError, match="Only https:// URLs are allowed"):
            await _fetch_page_text("file:///etc/passwd")

    @pytest.mark.asyncio
    async def test_blocks_private_target_before_urlopen(self):
        with (
            patch("jobhunter.agents.referral_agent.socket.getaddrinfo") as mock_info,
            patch("jobhunter.agents.referral_agent.urllib_request.urlopen") as mock_open,
        ):
            mock_info.return_value = [(2, 1, 6, "", ("127.0.0.1", 443))]
            with pytest.raises(ValueError, match="non-public network target"):
                await _fetch_page_text("https://example.com/jobs/123")
            mock_open.assert_not_called()

    @pytest.mark.asyncio
    async def test_linkedin_requires_browser_session(self):
        with pytest.raises(ValueError, match="BrowserSession is required"):
            await _fetch_page_text("https://www.linkedin.com/jobs/view/123")

    @pytest.mark.asyncio
    async def test_linkedin_uses_browser_session(self):
        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        page.inner_text = AsyncMock(return_value="job body")
        page.close = AsyncMock()

        session = MagicMock()
        session.new_page = AsyncMock(return_value=page)

        text = await _fetch_page_text("https://www.linkedin.com/jobs/view/123", browser_session=session)

        assert text == "job body"
        page.goto.assert_awaited_once()
        page.wait_for_load_state.assert_awaited_once_with("domcontentloaded")
        page.close.assert_awaited_once()
