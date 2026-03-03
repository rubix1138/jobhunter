"""Tests for browser/context.py helper utilities."""

from jobhunter.browser.context import _window_class_from_label


class TestWindowClassFromLabel:
    def test_none_label_returns_none(self):
        assert _window_class_from_label(None) is None

    def test_empty_label_returns_none(self):
        assert _window_class_from_label("   ") is None

    def test_sanitizes_label_for_chromium_class(self):
        assert _window_class_from_label("search now / run#1") == "jobhunter-search-now-run-1"

    def test_truncates_to_safe_length(self):
        label = "x" * 200
        result = _window_class_from_label(label)
        assert result is not None
        assert result.startswith("jobhunter-")
        assert len(result) <= 63
