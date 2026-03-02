# STATUS

Last updated: 2026-03-01

## Current State

- **Multisite apply test-fix loop active** (external ATS focus).
- Apply failures now persist structured reason/context in DB (`applications.error_message`) with `apply_type` and URL.
- Failure artifacts are captured on both exceptions and normal failed attempts (`data/logs/failures/*.png|*.txt`).
- Universal external-form hardening shipped:
  - auth transition verification (guest/login/create-account must actually clear auth),
  - preflight form-entry gate (avoid looping on listing pages),
  - domain cooldowns for known dead-end outcomes,
  - retry-cap policy to stop repeated retries on same jobs,
  - manual-review parking for hard gates (`SSO`, `captcha`, `email verification`, listing-only, unclear submit).
- Current queue is mostly constrained by external platform challenge gates (SSO/captcha/listing-only pages), not parser/classifier bugs.

## Key Files Changed (Recent)

- **Modified**: `src/jobhunter/agents/apply_agent.py`
  - detailed failure messages persisted,
  - failure artifact capture on all failed attempts,
  - Workday tenant block-memory integrated,
  - domain cooldown checks (`SSO`, `captcha/email verification`),
  - **retry-cap policy**:
    - max failed attempts per job: 3,
    - max consecutive same-failure streak per job: 2,
    - capped jobs are marked `skipped`.
- **Modified**: `src/jobhunter/applicators/form_filling.py`
  - failure reason propagation,
  - stricter auth-vs-form detection,
  - email-first login support and broader selector fallbacks,
  - explicit SSO-only wall detection,
  - preflight CTA fallback selectors for non-ARIA apply buttons,
  - stricter captcha marker detection.
- **Modified**: `tests/test_apply/test_question_answering.py` (failure-format helper tests).

## FormFillingAgent Architecture (Phase 19)

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

- `pytest tests/test_apply/test_question_answering.py -q` — **40 passed**
- `pytest tests/test_db/test_repository.py -q` — **40 passed**
- `.venv/bin/jobhunter --log-level INFO apply-now --apply-type oracle,lever` — run_id=176, **1 submitted / 2 failed**
- `.venv/bin/jobhunter --log-level INFO apply-now --apply-type workday,greenhouse,lever,icims,ashby,adp,oracle,other` — run_id=179, cooldown policies actively skipping known blocked domains.

## Next Actions

1. Add site-specific CTA entry for remaining Oracle listing-only tenant (`job 26`) and retest.
2. Add explicit retry-cap test coverage around `ApplyAgent._retry_cap_reason()`.
3. Continue broad search/apply loop to find fresh domains with higher completion probability while cooldowns suppress noisy repeats.
