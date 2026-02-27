# STATUS

Last updated: 2026-02-27

## Current State

- **Architecture pivot completed**: `WorkdayApplicator` (4,089 lines) and `GenericApplicator` (298 lines) deleted and replaced by `FormFillingAgent` (~577 lines) in `applicators/form_filling.py`.
- `apply_agent.py` dispatch simplified to two-way: `easy_apply` → LinkedInEasyApplicator, everything else → FormFillingAgent.
- `tests/test_apply` now has expanded Phase 19 coverage, including field-filling, navigation, auth, and main-loop edge cases.
- 93 `test_apply` tests passing locally. No import errors, no regressions in the apply suite.
- Easy Apply misclassification hardening shipped:
  - removed search-time `unknown -> easy_apply` force-mapping,
  - added preflight re-detection for `easy_apply` before material generation,
  - tightened SDUI link validation in detection and modal-open flows.
- Database reset completed for reevaluation: 12 previously skipped jobs reset to `qualified` + `apply_type='unknown'` and retried.

## Key Files Changed

- **Created**: `src/jobhunter/applicators/form_filling.py` — `FormFillingAgent` class + `_parse_field_plan()` + `_extract_domain()`
- **Modified**: `src/jobhunter/agents/apply_agent.py` — imports `FormFillingAgent` instead of `WorkdayApplicator`/`GenericApplicator`; dispatch is two-way
- **Modified**: `tests/test_apply/test_workday_planner.py` — imports `_parse_field_plan` from `form_filling`
- **Modified**: `tests/test_apply/test_workday.py` — tests `FormFillingAgent` + `_extract_domain`
- **Modified**: `tests/test_apply/test_generic_applicator.py` — tests `FormFillingAgent`
- **Deleted**: `src/jobhunter/applicators/workday.py`, `src/jobhunter/applicators/generic.py`

## FormFillingAgent Architecture

Universal applicator using AX tree + Vision + LLM planning (no platform-specific CSS selectors):
1. Navigate to external URL
2. Auth detection: guest flow → stored credential login → account creation (all via `get_by_role`/`get_by_label`)
3. Per-page loop (up to 15 pages):
   - AX tree snapshot → `format_interactive_fields()` → LLM planner → JSON fill plan
   - Execute plan items via ARIA locators with 9-approach fallback chain for select/dropdown
   - Broad radiogroup scan as supplemental pass
   - Resume upload via `input[type='file']`
   - Advance via `get_by_role("button")` matching Next/Continue/Submit labels
   - Stuck detection via URL + heading polling
4. Submission confirmation via text phrases + Vision

## Last Successful Commands

- `.venv/bin/python -m pytest tests/test_apply -q` — 93 passed
- `.venv/bin/python -m pytest tests/test_apply/test_generic_applicator.py -q` — 30 passed
- `.venv/bin/python -m pytest tests/test_apply/test_workday.py -q` — 20 passed
- `.venv/bin/jobhunter --log-level INFO apply-now --review` — run_id=147, `1 submitted / 9 failed-skipped` after requeue (expected external auth/stuck variance)

## Next Actions

1. Add targeted auth-capability handling for high-friction external domains (reduce repeated auth-wall failures).
2. Add remaining low-frequency unit tests (`_fill_select_field` text-proximity button/combobox variants, modal/upload internals).
3. Consider whether `workday_tenants` table is still needed (FormFillingAgent does not use it; table remains in schema but unused).
