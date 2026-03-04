# System Description: JobHunter (codex)

## Overview

JobHunter is a multi-agent job search and application automation system. It runs locally on the
user's machine and interacts with external job platforms (LinkedIn, Workday, Greenhouse) and
external APIs (Anthropic Claude, Gmail) on behalf of the user.

## Components

### CLI (`jobhunter`)
- Entry point for all user interactions (APScheduler scheduler, one-shot runs, status queries)
- Reads configuration from `.env` (Fernet key, API keys, SMTP credentials, profile path)
- Initializes and manages a SQLite database (`data/jobhunter.db`)

### Search Agent
- Queries LinkedIn Jobs search via Patchright browser automation (headed Chromium, CDP stealth)
- Parses job listings: title, company, location, apply type (Easy Apply / External / Workday /
  Greenhouse / Expired)
- Stores discovered jobs in SQLite; deduplicates by job ID

### Apply Agent
- Reads pending jobs from SQLite; scores them with Claude Sonnet (job description vs. resume)
- Generates tailored resume and cover letter PDFs using Claude Opus + WeasyPrint
- Submits applications via:
  - **LinkedIn Easy Apply**: AX tree + DOM navigation via Patchright
  - **Workday**: Planner-Actor-Validator loop (Claude Sonnet vision + AX tree planning)
  - **Greenhouse**: Form detection + field filling
  - **External**: Opens apply URL in browser for manual/semi-automated completion
- Q&A resolution pipeline: QA cache → profile lookup → Claude w/ cached profile → vision → empty

### Email Agent
- Reads Gmail inbox via Gmail OAuth (stored refresh token)
- Classifies emails (interview invite, rejection, follow-up request) with Claude Sonnet
- Logs results to SQLite; optionally sends SMTP notifications

### Data Storage
- **SQLite** (`data/jobhunter.db`, WAL mode): jobs, applications, Q&A log, email log, LLM spend
- **Fernet-encrypted vault** (`data/vault.db`): passwords, OAuth tokens; key from `FERNET_KEY` env var
- **PDF cache** (`data/resumes/`): generated resume/cover letter PDFs, keyed by slug+date
- **Persistent browser context** (`data/browser/`): Chromium profile + session cookies (LinkedIn login)

### External Services / APIs
- **Anthropic API** (Claude Sonnet 3.5, Opus 3): scoring, Q&A, resume/cover letter generation, vision
- **LinkedIn** (browser automation): job search, Easy Apply form submission
- **Workday** (browser automation): application form submission
- **Greenhouse** (browser automation): application form submission
- **Gmail API** (OAuth 2.0): inbox reading
- **SMTP** (optional): notification emails

## Trust Boundaries

```
[User / Local Machine]
    │
    ├── CLI (jobhunter) ──────────────────── reads .env (secrets)
    │                                         reads profile YAML (PII)
    │
    ├── SQLite DB ────────────────────────── local file, WAL mode
    │   └── Fernet vault ─────────────────── encrypted at rest
    │
    ├── Patchright Browser ───────────────── headed Chromium (anti-detect)
    │   ├── [LinkedIn] ────── EXTERNAL ─────  job search + Easy Apply
    │   ├── [Workday]  ────── EXTERNAL ─────  application forms
    │   └── [Greenhouse] ─── EXTERNAL ─────  application forms
    │
    ├── [Anthropic API] ───── EXTERNAL ─────  LLM calls (resume PII sent)
    │
    └── [Gmail API] ────────── EXTERNAL ─────  OAuth; reads inbox
        └── [SMTP] ─────────── EXTERNAL ─────  sends notifications
```

## Data Sensitivity

| Data | Location | Sensitivity |
|------|----------|------------|
| Full resume / profile YAML | local file | High — PII (name, address, employment history) |
| Job application credentials | Fernet vault | Critical — username/password |
| OAuth refresh tokens (Gmail) | Fernet vault | Critical |
| Fernet encryption key | `FERNET_KEY` env var | Critical — compromise = full vault exposure |
| Anthropic API key | `ANTHROPIC_API_KEY` env var | High — billing + data access |
| LinkedIn session cookies | browser profile | High — account access |
| Resume/cover letter PDFs | `data/resumes/` | Medium — PII |
| LLM prompts (resume content) | sent to Anthropic | High — PII leaves local machine |
| Job listings / application records | SQLite | Low |

## Technology Stack

- **Language:** Python 3.11+
- **Browser automation:** Patchright (CDP stealth) + playwright-stealth v2
- **Database:** SQLite (WAL mode) + custom Fernet-encrypted vault
- **LLM:** Anthropic Claude API (Sonnet for Q&A/scoring/vision, Opus for generation)
- **PDF generation:** WeasyPrint
- **Scheduling:** APScheduler
- **Packaging:** pyproject.toml, pip-installable
- **Dependency management:** pip + pyproject.toml extras (`[dev]`)

## Deployment Context

- Runs entirely on the user's local Linux/macOS machine
- No web server, no remote database, no multi-user access
- Secrets in `.env` file (gitignored) + `FERNET_KEY` environment variable
- Browser runs headed (visible window) to avoid headless bot detection
