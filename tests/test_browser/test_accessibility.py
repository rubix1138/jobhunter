"""Tests for browser/accessibility.py — AX tree helpers."""

import re
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jobhunter.browser.accessibility import (
    find_by_aria_label,
    format_interactive_fields,
    get_ax_tree,
    search_ax_tree,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

EASY_APPLY_TREE = {
    "role": "WebArea",
    "name": "Job Listing",
    "children": [
        {
            "role": "main",
            "name": "",
            "children": [
                {
                    "role": "button",
                    "name": "Easy Apply to Senior Engineer at Acme Corp",
                },
                {
                    "role": "button",
                    "name": "Save job",
                },
                {
                    "role": "link",
                    "name": "Company website",
                },
            ],
        },
        {
            # Sidebar card for a DIFFERENT job
            "role": "listitem",
            "name": "",
            "children": [
                {
                    "role": "link",
                    "name": "Easy Apply to Staff Engineer at OtherCo",
                },
            ],
        },
    ],
}

INTEREST_ONLY_TREE = {
    "role": "WebArea",
    "name": "Job Listing",
    "children": [
        {
            "role": "button",
            "name": "I'm interested",
        }
    ],
}

SDUI_LINK_TREE = {
    "role": "WebArea",
    "name": "Job Listing",
    "children": [
        {
            "role": "link",
            "name": "Easy Apply to Data Engineer at BigCo",
        }
    ],
}

NESTED_TREE = {
    "role": "WebArea",
    "name": "",
    "children": [
        {
            "role": "region",
            "name": "Job details",
            "children": [
                {
                    "role": "group",
                    "name": "Apply",
                    "children": [
                        {
                            "role": "button",
                            "name": "Easy Apply",
                        }
                    ],
                }
            ],
        }
    ],
}


# ── TestSearchAxTree ──────────────────────────────────────────────────────────


class TestSearchAxTree:
    def test_finds_easy_apply_button_in_nested_tree(self):
        pattern = re.compile(r"easy\s*apply", re.IGNORECASE)
        results = search_ax_tree(EASY_APPLY_TREE, role="button", label_pattern=pattern)
        assert len(results) == 1
        assert "Easy Apply" in results[0]["name"]
        assert results[0]["role"] == "button"

    def test_finds_sdui_link_by_role(self):
        pattern = re.compile(r"easy\s*apply", re.IGNORECASE)
        results = search_ax_tree(SDUI_LINK_TREE, role="link", label_pattern=pattern)
        assert len(results) == 1
        assert results[0]["role"] == "link"

    def test_job_id_in_name_selects_correct_job(self):
        """Both the main job and a sidebar card have Easy Apply — job_id distinguishes them."""
        pattern = re.compile(r"easy\s*apply", re.IGNORECASE)
        # Search without role filter to get both button and link
        all_matches = search_ax_tree(EASY_APPLY_TREE, label_pattern=pattern)
        assert len(all_matches) == 2  # button (main) + link (sidebar)

        # Filter by job_id — only the main job's button contains the ID
        job_id = "12345"
        # Inject the job_id into the correct node's name for this test
        tree = {
            "role": "WebArea",
            "name": "",
            "children": [
                {"role": "button", "name": f"Easy Apply to Engineer (job {job_id})"},
                {"role": "link", "name": "Easy Apply to Other Job (job 99999)"},
            ],
        }
        results = search_ax_tree(tree, label_pattern=pattern)
        # Both match the pattern
        assert len(results) == 2
        # But filtering by job_id narrows to 1
        preferred = [r for r in results if job_id in (r.get("name") or "")]
        assert len(preferred) == 1
        assert job_id in preferred[0]["name"]

    def test_interest_only_pattern_detection(self):
        pattern = re.compile(r"i.?m\s+interested", re.IGNORECASE)
        results = search_ax_tree(INTEREST_ONLY_TREE, role="button", label_pattern=pattern)
        assert len(results) == 1
        assert results[0]["name"] == "I'm interested"

    def test_empty_tree_returns_empty_list(self):
        results = search_ax_tree({}, label_pattern=re.compile(r"easy apply"))
        assert results == []

    def test_none_tree_returns_empty_list(self):
        results = search_ax_tree(None, label_pattern=re.compile(r"easy apply"))
        assert results == []

    def test_role_filter_excludes_wrong_role(self):
        pattern = re.compile(r"easy\s*apply", re.IGNORECASE)
        # Only look for buttons — should not return the sidebar link
        button_results = search_ax_tree(EASY_APPLY_TREE, role="button", label_pattern=pattern)
        link_results = search_ax_tree(EASY_APPLY_TREE, role="link", label_pattern=pattern)
        assert all(r["role"] == "button" for r in button_results)
        assert all(r["role"] == "link" for r in link_results)
        assert len(button_results) == 1
        assert len(link_results) == 1

    def test_label_contains_filter(self):
        results = search_ax_tree(EASY_APPLY_TREE, label_contains="Save")
        assert len(results) == 1
        assert results[0]["name"] == "Save job"

    def test_no_filters_returns_all_named_nodes(self):
        """With no filters, all nodes that have a non-empty name are returned."""
        results = search_ax_tree(NESTED_TREE)
        names = [r["name"] for r in results]
        assert "Job details" in names
        assert "Apply" in names
        assert "Easy Apply" in names

    def test_deeply_nested_node_found(self):
        pattern = re.compile(r"^easy apply$", re.IGNORECASE)
        results = search_ax_tree(NESTED_TREE, role="button", label_pattern=pattern)
        assert len(results) == 1
        assert results[0]["name"] == "Easy Apply"


# ── TestGetAxTree ─────────────────────────────────────────────────────────────


class TestGetAxTree:
    @pytest.mark.asyncio
    async def test_returns_snapshot_on_success(self):
        snapshot = {"role": "WebArea", "name": "Test"}
        page = MagicMock()
        page.accessibility = MagicMock()
        page.accessibility.snapshot = AsyncMock(return_value=snapshot)

        result = await get_ax_tree(page)
        assert result == snapshot

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self):
        page = MagicMock()
        page.accessibility = MagicMock()
        page.accessibility.snapshot = AsyncMock(side_effect=Exception("CDP error"))

        result = await get_ax_tree(page)
        assert result is None


# ── TestFindByAriaLabel ───────────────────────────────────────────────────────


def _make_page_with_tree(tree: Optional[dict]) -> MagicMock:
    """Return a mock Page whose accessibility.snapshot() returns ``tree``."""
    page = MagicMock()
    page.accessibility = MagicMock()
    page.accessibility.snapshot = AsyncMock(return_value=tree)
    return page


class TestFindByAriaLabel:
    @pytest.mark.asyncio
    async def test_returns_locator_when_found_and_visible(self):
        page = _make_page_with_tree(EASY_APPLY_TREE)

        # get_by_role returns a locator whose is_visible returns True
        mock_locator = MagicMock()
        mock_locator.is_visible = AsyncMock(return_value=True)
        mock_locator_first = MagicMock()
        mock_locator_first.is_visible = AsyncMock(return_value=True)

        role_locator = MagicMock()
        role_locator.first = mock_locator_first
        page.get_by_role = MagicMock(return_value=role_locator)

        pattern = re.compile(r"easy\s*apply", re.IGNORECASE)
        result = await find_by_aria_label(page, pattern, roles=("button",))
        assert result is mock_locator_first

    @pytest.mark.asyncio
    async def test_returns_none_when_no_ax_match(self):
        # Tree has no Easy Apply button
        tree = {"role": "WebArea", "name": "Page", "children": []}
        page = _make_page_with_tree(tree)

        pattern = re.compile(r"easy\s*apply", re.IGNORECASE)
        result = await find_by_aria_label(page, pattern)
        assert result is None

    @pytest.mark.asyncio
    async def test_job_id_filter_passes_correct_label_to_get_by_role(self):
        """When job_id matches only one candidate, only that label is used."""
        tree = {
            "role": "WebArea",
            "name": "",
            "children": [
                {"role": "button", "name": "Easy Apply to Engineer at Acme (3456789)"},
                {"role": "button", "name": "Easy Apply to Other Job (9999999)"},
            ],
        }
        page = _make_page_with_tree(tree)

        calls = []

        def get_by_role_spy(role, *, name="", **kwargs):
            calls.append((role, name))
            mock = MagicMock()
            mock.first = MagicMock()
            # Only visible when the name contains "3456789"
            is_vis = "3456789" in name
            mock.first.is_visible = AsyncMock(return_value=is_vis)
            return mock

        page.get_by_role = get_by_role_spy

        pattern = re.compile(r"easy\s*apply", re.IGNORECASE)
        result = await find_by_aria_label(page, pattern, roles=("button",), job_id="3456789")

        assert result is not None
        # Verify the filtered candidate (containing job_id) was tried first
        assert any("3456789" in name for _, name in calls)

    @pytest.mark.asyncio
    async def test_falls_back_to_get_by_label_when_get_by_role_not_visible(self):
        page = _make_page_with_tree(SDUI_LINK_TREE)

        # get_by_role returns not-visible locator
        invisible_locator = MagicMock()
        invisible_locator.is_visible = AsyncMock(return_value=False)
        role_mock = MagicMock()
        role_mock.first = invisible_locator
        page.get_by_role = MagicMock(return_value=role_mock)

        # get_by_label returns visible locator
        visible_locator = MagicMock()
        visible_locator.is_visible = AsyncMock(return_value=True)
        label_mock = MagicMock()
        label_mock.first = visible_locator
        page.get_by_label = MagicMock(return_value=label_mock)

        pattern = re.compile(r"easy\s*apply", re.IGNORECASE)
        result = await find_by_aria_label(page, pattern, roles=("link",))
        assert result is visible_locator

    @pytest.mark.asyncio
    async def test_returns_none_when_snapshot_fails(self):
        page = MagicMock()
        page.accessibility = MagicMock()
        page.accessibility.snapshot = AsyncMock(side_effect=Exception("timeout"))

        pattern = re.compile(r"easy\s*apply", re.IGNORECASE)
        result = await find_by_aria_label(page, pattern)
        assert result is None


# ── TestFormatInteractiveFields ───────────────────────────────────────────────


class TestFormatInteractiveFields:
    def test_basic_fields_with_required_marker(self):
        tree = {
            "role": "WebArea",
            "name": "Form",
            "children": [
                {"role": "textbox", "name": "First name"},
                {"role": "combobox", "name": "Country", "required": True},
            ],
        }
        result = format_interactive_fields(tree)
        lines = result.strip().splitlines()
        assert len(lines) == 2
        assert lines[0] == "textbox 'First name'"
        assert lines[1] == "combobox 'Country' (required)"

    def test_skips_navigation_buttons(self):
        tree = {
            "role": "WebArea",
            "name": "Form",
            "children": [
                {"role": "button", "name": "Next"},
                {"role": "textbox", "name": "Phone"},
                {"role": "button", "name": "Submit"},
            ],
        }
        result = format_interactive_fields(tree)
        lines = [l for l in result.strip().splitlines() if l]
        # Only "Phone" textbox should appear; "Next" and "Submit" are buttons (not filler roles)
        assert len(lines) == 1
        assert "Phone" in lines[0]

    def test_radiogroup_with_empty_name_uses_text_child(self):
        """When radiogroup has no name, fall back to a text-child label (Workday pattern)."""
        tree = {
            "role": "WebArea",
            "name": "Form",
            "children": [
                {
                    "role": "radiogroup",
                    "name": "",  # empty — label is a sibling text node
                    "children": [
                        {"role": "text", "name": "Are you 18 years or older?"},
                        {"role": "radio", "name": "Yes"},
                        {"role": "radio", "name": "No"},
                    ],
                },
            ],
        }
        result = format_interactive_fields(tree)
        assert "radiogroup" in result
        assert "Are you 18 years or older?" in result
        assert "Yes" in result
        assert "No" in result

    def test_radiogroup_sibling_text_label(self):
        """Preceding text sibling at the same parent level is used as radiogroup label."""
        tree = {
            "role": "WebArea",
            "name": "Form",
            "children": [
                {
                    "role": "group",
                    "name": "",
                    "children": [
                        # text node precedes the unnamed radiogroup
                        {"role": "text", "name": "Are you legally eligible to work?"},
                        {
                            "role": "radiogroup",
                            "name": "",
                            "children": [
                                {"role": "radio", "name": "Yes"},
                                {"role": "radio", "name": "No"},
                            ],
                        },
                    ],
                }
            ],
        }
        result = format_interactive_fields(tree)
        assert "radiogroup" in result
        assert "Are you legally eligible to work?" in result
        assert "Yes" in result

    def test_radiogroup_with_named_group(self):
        """Named radiogroup emits correctly."""
        tree = {
            "role": "WebArea",
            "name": "Form",
            "children": [
                {
                    "role": "radiogroup",
                    "name": "Legally eligible to work?",
                    "required": True,
                    "children": [
                        {"role": "radio", "name": "Yes"},
                        {"role": "radio", "name": "No"},
                    ],
                },
            ],
        }
        result = format_interactive_fields(tree)
        assert "radiogroup 'Legally eligible to work?' (required) options: Yes, No" == result.strip()
