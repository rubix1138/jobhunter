# JobHunter

Automated job search + application assistant built around LinkedIn discovery, external ATS form filling, Gmail triage, and a local SQLite audit trail.

## What It Does

- Runs three agents:
  - `search` agent to discover and score jobs
  - `apply` agent to submit applications (LinkedIn + external ATS)
  - `email` agent to classify and process inbox updates
- Stores everything in SQLite (`jobs`, `applications`, `email_log`, `agent_runs`, `llm_usage`, etc.)
- Generates tailored referral materials (`prepare-referral`)
- Supports manual-review queues for blocked/captcha/SSO edge cases
- Captures failure artifacts (`data/logs/failures/*.png|*.txt`) for debugging

## Recent Improvements (March 2026)

- Stale `agent_runs` auto-reconciliation (old `running` -> `error`) at command startup
- LinkedIn required-field loop remediation for sticky validation errors
- Domain blacklist support (for example `remotehunter.com`) during search/apply
- Gmail auth auto-recovery when refresh token is revoked (`invalid_grant`)
- Email-classifier parser hardening for fenced/truncated JSON responses
- Browser UX controls:
  - optional minimized Chromium launch
  - per-run window labeling (`--class=jobhunter-...` on Linux)

## Repository Layout

- `src/jobhunter/` - app code
- `tests/` - pytest suite
- `config/search_queries.yaml` - LinkedIn queries
- `config/settings.yaml` - runtime config (limits, thresholds, browser, filters)
- `profile/user_profile.yaml` - candidate profile + application defaults
- `data/` - runtime artifacts (DB, logs, browser state, resumes)

## Requirements

- Python `>=3.12` (tested with 3.13)
- Chromium via Playwright/Patchright
- API credentials:
  - Anthropic API key
  - Gmail OAuth client credentials

## Setup

1. Create and activate virtual environment.
2. Install package and dependencies.
3. Install Chromium for Playwright.
4. Configure `.env`.
5. Copy/edit profile + query/config files.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
python -m playwright install chromium
cp .env.example .env
```

Required runtime files:

- `.env` (API keys + paths)
- `profile/user_profile.yaml`
- `config/search_queries.yaml`
- `config/settings.yaml`
- `data/gmail_credentials.json` (OAuth client JSON from Google Cloud)

## CLI Commands

Core:

```bash
jobhunter init
jobhunter status
jobhunter run
jobhunter search-now [--max-queries N] [--max-pages N]
jobhunter apply-now [--apply-type TYPE] [--dry-run] [--review] [--reprobe-blocked-workday]
jobhunter check-email
jobhunter daily-summary
```

Operations / review:

```bash
jobhunter review-queue [--limit N]
jobhunter review-packet [--limit N] [--output PATH] [--csv] [--open]
jobhunter review-resolve --app-id ID --action retry|skip|resolved
jobhunter qa-log [--app-id ID]
jobhunter platform-stats
jobhunter prepare-referral --url URL [--title TITLE] [--company COMPANY] [--output-dir DIR]
```

## Browser Behavior (Multiple Agents)

`config/settings.yaml`:

```yaml
browser:
  start_minimized: true
```

Window labeling is automatic and run-specific (for example `search-now-pid12345`) and is passed to Chromium class on Linux (`jobhunter-search-now-pid12345`) to make windows easier to identify.

## Testing

```bash
.venv/bin/pytest -q
```

Targeted suites:

```bash
.venv/bin/pytest tests/test_email -q
.venv/bin/pytest tests/test_scheduler -q
.venv/bin/pytest tests/test_apply -q
```

## Notes

- LinkedIn login is manual on first run; browser session state persists in `data/browser_state/`.
- Gmail OAuth opens a local browser flow when needed and refreshes tokens automatically.
- This repo may contain local runtime data in `data/`; do not commit secrets/tokens.
