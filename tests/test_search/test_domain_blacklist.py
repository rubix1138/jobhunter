"""Tests for search-agent domain blacklist matching."""

from unittest.mock import MagicMock

from jobhunter.agents.search_agent import SearchAgent


def _make_agent(exclude_domains: list[str]) -> SearchAgent:
    return SearchAgent(
        session=MagicMock(),
        llm=MagicMock(),
        profile=MagicMock(),
        queries=[],
        rate_limiter=MagicMock(),
        settings={
            "global_filters": {"exclude_domains": exclude_domains},
            "budget": {},
            "rate_limits": {},
            "thresholds": {},
        },
    )


class TestSearchDomainBlacklist:
    def test_matches_exact_domain(self):
        agent = _make_agent(["remotehunter.com"])
        assert agent._is_excluded_domain("https://remotehunter.com/apply/123") is True

    def test_matches_subdomain(self):
        agent = _make_agent(["remotehunter.com"])
        assert agent._is_excluded_domain("https://www.remotehunter.com/jobs/abc") is True

    def test_non_matching_domain(self):
        agent = _make_agent(["remotehunter.com"])
        assert agent._is_excluded_domain("https://example.com/jobs/abc") is False
