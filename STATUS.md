# STATUS

Last updated: 2026-03-03

## Current State

- Core scheduler/search/apply/email flows are operational.
- Stale run-state reconciliation is now automatic at command start.
- LinkedIn Easy Apply required-field loop handling has been hardened with validation-aware remediation + safer defaults.
- Email processing is stable after Gmail auth/token recovery and classification parser hardening.
- Domain blacklist support is active in both search and apply flows (currently excluding `remotehunter.com`).
- Browser UX improvements are in place for concurrent sessions (minimized launch + per-run window labels on Linux class).
- Security hardening pass completed across prompt, fetch, output, and auto-action surfaces:
  - `prepare-referral` URL validation + non-public target blocking (SSRF/local-target guardrails)
  - explicit untrusted-content delimiters in LLM prompts
  - recruiter auto-reply confidence + sender-domain trust gating
  - QA cache scoping by job domain/company to reduce cross-site poisoning
  - unsafe global radio fallback removed (fail-safe behavior when label scoping fails)
  - CSV formula injection mitigation in `review-packet --csv`
  - ANSI/control-char sanitization for review/daily-summary terminal output
- Apply dry-run metrics/reporting corrected:
  - generated-material count is tracked separately from submitted applications
  - scheduler/base summaries now report dry-run generation counts correctly

## Key Files Changed (Recent)

- **Modified**: `src/jobhunter/db/queries.py`, `src/jobhunter/db/repository.py`, `src/jobhunter/main.py`
  - stale-run reconciliation (`agent_runs.status='running'` -> `error` after threshold) integrated into CLI command entry points.
- **Modified**: `src/jobhunter/applicators/linkedin_easy.py`
  - validation-error capture + required-field remediation loop prevention.
  - safer fallback answers for stubborn required fields.
- **Modified**: `src/jobhunter/agents/search_agent.py`, `src/jobhunter/agents/apply_agent.py`, `config/settings.yaml`
  - configurable domain blacklist in both discovery and application execution paths.
- **Modified**: `src/jobhunter/gmail/auth.py`
  - revoked Gmail token refresh fallback to interactive OAuth flow.
- **Modified**: `src/jobhunter/gmail/classifier.py`
  - robust parsing for fenced/truncated LLM JSON output.
- **Modified**: `src/jobhunter/browser/context.py`, `src/jobhunter/scheduler.py`
  - browser start minimized option + run-specific window labeling (Linux Chromium class).
- **Modified**: `src/jobhunter/agents/referral_agent.py`
  - strict referral URL validation (`https` only, hostname required) + public-network target checks.
- **Modified**: `src/jobhunter/llm/prompts.py`
  - prompt-injection guardrails via untrusted-content delimiting and security instructions.
- **Modified**: `src/jobhunter/agents/email_agent.py`
  - recruiter sender trust checks (domain matching + free-email blocking) and confidence gating.
- **Modified**: `src/jobhunter/applicators/base.py`, `src/jobhunter/applicators/form_filling.py`
  - QA cache key scoping by job context; removed unsafe broad radio-button fallback.
- **Modified**: `src/jobhunter/main.py`, `src/jobhunter/scheduler.py`, **Added**: `src/jobhunter/utils/output_safety.py`
  - terminal output sanitization and CSV formula-injection defenses.
- **Added/Updated tests**:
  - `tests/test_db/test_repository.py`
  - `tests/test_scheduler/test_cli.py`
  - `tests/test_apply/test_linkedin_easy.py`
  - `tests/test_search/test_domain_blacklist.py`
  - `tests/test_email/test_gmail_auth.py`
  - `tests/test_email/test_classifier.py`
  - `tests/test_browser/test_context.py`
  - `tests/test_scheduler/test_scheduler.py`
  - `tests/test_scheduler/test_referral_security.py`
  - `tests/test_apply/test_question_answering.py`
  - `tests/test_apply/test_generic_applicator.py`
  - `tests/test_email/test_email_agent.py`
  - `tests/test_search/test_job_parsing.py`

## Recent Validation Commands

- `.venv/bin/pytest tests/test_db/test_repository.py tests/test_scheduler/test_cli.py -q` — **83 passed**
- `.venv/bin/pytest tests/test_apply/test_linkedin_easy.py tests/test_apply/test_question_answering.py tests/test_apply/test_generic_applicator.py -q` — **85 passed**
- `.venv/bin/pytest tests/test_search/test_domain_blacklist.py tests/test_apply/test_question_answering.py tests/test_search/test_job_parsing.py -q` — **77 passed**
- `.venv/bin/pytest tests/test_email/test_gmail_auth.py tests/test_email/test_gmail_client.py tests/test_email/test_email_agent.py -q` — **67 passed**
- `.venv/bin/pytest tests/test_email/test_classifier.py tests/test_email/test_email_agent.py -q` — **58 passed**
- `.venv/bin/pytest tests/test_browser/test_context.py tests/test_scheduler/test_scheduler.py tests/test_scheduler/test_cli.py -q` — **71 passed**
- `.venv/bin/pytest -q` — **492 passed**

## Next Actions

1. Run a full unbounded `search-now` pass and review newly queued qualified jobs after blacklist filtering.
2. Execute `apply-now` with targeted slices (`--apply-type ...`) and monitor `needs_review` queue delta.
3. Add optional per-command custom browser label override (CLI flag/env) for multi-window operator workflows.
