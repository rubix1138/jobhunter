"""Tests for Phase 15 Workday Planner — _parse_field_plan and _validate_advance."""

import pytest

from jobhunter.applicators.workday import _parse_field_plan


class TestParseFieldPlan:
    def test_valid_json_array(self):
        response = '[{"label": "First name", "field_type": "text", "value": "Jane"}]'
        result = _parse_field_plan(response)
        assert len(result) == 1
        assert result[0]["label"] == "First name"
        assert result[0]["field_type"] == "text"
        assert result[0]["value"] == "Jane"

    def test_invalid_json_returns_empty(self):
        result = _parse_field_plan("This is not JSON at all")
        assert result == []

    def test_missing_value_key_returns_empty(self):
        # Items missing required keys are excluded; if all excluded, result is []
        response = '[{"label": "Country", "field_type": "select"}]'
        result = _parse_field_plan(response)
        assert result == []

    def test_strips_markdown_fences(self):
        response = '```json\n[{"label": "A", "field_type": "text", "value": "B"}]\n```'
        result = _parse_field_plan(response)
        assert len(result) == 1
        assert result[0]["label"] == "A"

    def test_non_list_json_returns_empty(self):
        response = '{"label": "A", "field_type": "text", "value": "B"}'
        result = _parse_field_plan(response)
        assert result == []
