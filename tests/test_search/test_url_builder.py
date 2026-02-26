"""Tests for LinkedIn search URL construction."""

import urllib.parse
import pytest

from jobhunter.agents.search_agent import build_search_url


def parse_qs(url: str) -> dict:
    qs = url.split("?", 1)[1] if "?" in url else ""
    return urllib.parse.parse_qs(qs)


class TestBuildSearchUrl:
    def test_required_params_present(self):
        url = build_search_url("Python Engineer")
        params = parse_qs(url)
        assert "keywords" in params
        assert params["keywords"] == ["Python Engineer"]
        assert "location" in params
        assert "f_TPR" in params
        assert "sortBy" in params
        assert "start" in params

    def test_default_location(self):
        url = build_search_url("Engineer")
        assert "United+States" in url or "United States" in url.replace("%20", " ")

    def test_easy_apply_filter(self):
        url = build_search_url("Engineer", easy_apply_only=True)
        params = parse_qs(url)
        assert params.get("f_AL") == ["true"]

    def test_no_easy_apply_by_default(self):
        url = build_search_url("Engineer")
        assert "f_AL" not in parse_qs(url)

    def test_work_types(self):
        url = build_search_url("Engineer", work_types=[2, 3])
        params = parse_qs(url)
        assert "f_WT" in params
        assert "2" in params["f_WT"][0] and "3" in params["f_WT"][0]

    def test_experience_levels(self):
        url = build_search_url("Engineer", experience_levels=[4, 5])
        params = parse_qs(url)
        assert "f_E" in params

    def test_job_types(self):
        url = build_search_url("Engineer", job_types=["F", "C"])
        params = parse_qs(url)
        assert "f_JT" in params

    def test_pagination_start(self):
        url = build_search_url("Engineer", start=25)
        params = parse_qs(url)
        assert params["start"] == ["25"]

    def test_date_posted(self):
        url = build_search_url("Engineer", date_posted="r86400")
        params = parse_qs(url)
        assert params["f_TPR"] == ["r86400"]

    def test_sort_by(self):
        url = build_search_url("Engineer", sort_by="R")
        params = parse_qs(url)
        assert params["sortBy"] == ["R"]

    def test_url_starts_with_linkedin(self):
        url = build_search_url("Engineer")
        assert url.startswith("https://www.linkedin.com/jobs/search/")
