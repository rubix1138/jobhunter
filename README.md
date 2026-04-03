# JobHunter

Automated job search and application assistant. This repo remains an independent
system and is also exposed through Alice's gateway as a bounded specialist.

## Purpose

- Search LinkedIn for jobs
- Score and queue opportunities
- Apply on LinkedIn and external ATS flows
- Process Gmail updates
- Generate referral materials
- Maintain a manual-review queue for blocked or risky cases

## Important Paths

- Repo: `/mnt/ai/code/jobhunter`
- Virtualenv: `/mnt/ai/code/jobhunter/.venv`
- Env template: `/mnt/ai/code/jobhunter/.env.example`
- Config dir: `/mnt/ai/code/jobhunter/config`
- Profile dir: `/mnt/ai/code/jobhunter/profile`
- Runtime data: `/mnt/ai/code/jobhunter/data`
- Tests: `/mnt/ai/code/jobhunter/tests`
- Local operator hints: `/mnt/ai/code/jobhunter/.claude`

## Required Runtime Inputs

- `.env`
- `profile/user_profile.yaml`
- `config/search_queries.yaml`
- `config/settings.yaml`
- `data/gmail_credentials.json`

## Main Commands

```bash
jobhunter init
jobhunter status
jobhunter run
jobhunter search-now [--max-queries N] [--max-pages N]
jobhunter apply-now [--apply-type TYPE] [--dry-run] [--review]
jobhunter check-email
jobhunter review-queue [--limit N]
jobhunter review-packet [--limit N] [--output PATH] [--csv]
.venv/bin/pytest -q
```

## Current Gateway Integration

The assistant gateway currently exposes:

- status via `.venv/bin/jobhunter status`
- operator inbox from `review-queue --limit 10`
- bounded actions:
  - `search-now --max-queries 1 --max-pages 1`
  - `check-email`
  - `apply-now --dry-run --review`

## Operational Notes

- Browser automation is Playwright/Patchright-based
- Data under `data/` is operational state, not design docs
- This repo is the most stateful specialist in the stack; do not treat runtime
  data, review queues, or browser state as disposable without checking impact

## Related Docs

- Box entrypoint: `/mnt/ai/start.md`
- Assistant gateway: `/mnt/ai/code/assistant/README.md`
- Historical plans: `/mnt/ai/plans`
