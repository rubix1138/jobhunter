# JobHunter — Automated Job Search & Application System

## Context

Build an automated system with three AI agents to search LinkedIn for jobs, apply with custom-tailored resumes/cover letters (handling both LinkedIn Easy Apply and external sites like Workday), and monitor a dedicated Gmail inbox to track application status and escalate when human intervention is needed.

---

## Tech Stack

| Component | Choice |
|-----------|--------|
| Language | Python 3.12+ |
| Browser Automation | Patchright (stealth Playwright fork) + playwright-stealth + Claude Vision fallback |
| LLM | Claude API — Sonnet for routine tasks, Opus for resume/cover letter writing |
| Database | SQLite (WAL mode) |
| Email | Gmail API (OAuth2) |
| Credential Storage | Fernet-encrypted column in SQLite |
| Scheduling | APScheduler (AsyncIO) |
| PDF Generation | WeasyPrint + Jinja2 templates |
| Config | YAML (settings, search queries, user profile) + .env for secrets |

---

## Project Structure

```
jobhunter/
├── pyproject.toml
├── .env.example
├── config/
│   ├── settings.yaml            # Rate limits, schedules, thresholds
│   └── search_queries.yaml      # LinkedIn search criteria
├── profile/
│   └── user_profile.yaml        # Work history, skills, preferences, common Q&A answers
├── templates/
│   ├── resume_base.md           # Jinja2 resume template
│   └── cover_letter_base.md     # Jinja2 cover letter template
├── src/jobhunter/
│   ├── main.py                  # CLI entry point (run, search-now, apply-now, check-email, status, init, platform-stats)
│   ├── scheduler.py             # APScheduler setup, agent registration
│   ├── db/
│   │   ├── engine.py            # SQLite connection factory, WAL mode, migrations
│   │   ├── schema.sql           # DDL for all tables
│   │   ├── models.py            # Dataclass models
│   │   ├── queries.py           # Named SQL queries
│   │   └── repository.py        # CRUD: JobRepo, ApplicationRepo, CredentialRepo, EmailRepo
│   ├── crypto/
│   │   └── vault.py             # Fernet encrypt/decrypt, CredentialVault class
│   ├── browser/
│   │   ├── context.py           # Persistent Playwright context, session management
│   │   ├── stealth.py           # Anti-detection: random delays, fingerprint consistency
│   │   ├── vision.py            # Screenshot + Claude Vision fallback; analyze_form_fields()
│   │   ├── accessibility.py     # AX tree helpers: get_ax_tree, search_ax_tree, find_by_aria_label, format_interactive_fields
│   │   └── helpers.py           # wait_and_click, fill_field, upload_file, scroll
│   ├── agents/
│   │   ├── base.py              # BaseAgent ABC: lifecycle, retry, logging, agent_runs tracking
│   │   ├── search_agent.py      # LinkedIn search, parse, score, store
│   │   ├── apply_agent.py       # Job selection, material generation, applicator dispatch
│   │   └── email_agent.py       # Gmail poll, classify, link to job, act/forward
│   ├── applicators/
│   │   ├── base.py              # BaseApplicator ABC: answer_question(), handle_stuck_page()
│   │   ├── linkedin_easy.py     # Easy Apply modal multi-step handler
│   │   ├── workday.py           # Account creation, login, Workday form navigation; Planner-Actor-Validator loop
│   │   └── generic.py           # Best-effort external ATS (heavy Vision usage)
│   ├── llm/
│   │   ├── client.py            # Claude API wrapper: model selection, retry, token/cost tracking
│   │   ├── prompts.py           # All prompt templates
│   │   ├── resume.py            # Profile + job description -> tailored resume (Opus)
│   │   └── cover_letter.py      # Cover letter generation (Opus)
│   ├── gmail/
│   │   ├── auth.py              # OAuth2 flow + token refresh
│   │   ├── client.py            # Gmail API wrapper: list, get, send, modify labels
│   │   └── classifier.py        # Email classification via Claude Sonnet
│   └── utils/
│       ├── logging.py           # Structured JSON logging
│       ├── profile_loader.py    # YAML profile parser + Pydantic validation
│       └── rate_limiter.py      # Token bucket for LinkedIn/API rate limiting
├── data/                        # Runtime (gitignored)
│   ├── jobhunter.db
│   ├── browser_state/
│   ├── resumes/
│   ├── logs/
│   └── gmail_token.json
└── tests/
```

---

## Database Schema

**7 tables:**

1. **jobs** — Every discovered listing. Key fields: `linkedin_job_id` (unique, for dedup), `title`, `company`, `description`, `job_url`, `external_url`, `apply_type` (easy_apply | external_workday | external_greenhouse | external_lever | external_icims | external_taleo | external_smartrecruiters | external_jobvite | external_bamboohr | external_successfactors | external_ashby | external_theladders | external_paylocity | external_ukg | external_adp | external_oracle | external_other | interest_only | expired | unknown), `match_score`, `status` (new -> qualified -> applied -> interviewing -> rejected/offer)
2. **applications** — One per application attempt. Links to job. Stores `resume_path`, `cover_letter_path`, full text of both, `questions_json` (Q&A record), `status`, `attempt_count`
3. **credentials** — Fernet-encrypted username/password per domain for external sites
4. **email_log** — Every processed email. `gmail_message_id`, classification, confidence, linked job, action taken
5. **agent_runs** — Audit log per agent execution (timing, counts, errors)
6. **llm_usage** — Token/cost tracking per API call
7. **qa_cache** — Cross-application Q&A cache. Keyed by `(question_key, options_hash)`. `times_used` counter. High-confidence answers (≥ 0.7) written automatically; cache reads cost zero LLM tokens.

### Detailed Schema (schema.sql)

```sql
-- jobs table
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    linkedin_job_id TEXT UNIQUE NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    location        TEXT,
    employment_type TEXT,
    experience_level TEXT,
    salary_range    TEXT,
    description     TEXT,
    job_url         TEXT NOT NULL,
    external_url    TEXT,
    apply_type      TEXT NOT NULL DEFAULT 'unknown',
    company_domain  TEXT,
    match_score     REAL,
    match_reasoning TEXT,
    search_query    TEXT,
    status          TEXT NOT NULL DEFAULT 'new',
    discovered_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_apply_type ON jobs(apply_type);
CREATE INDEX IF NOT EXISTS idx_jobs_discovered ON jobs(discovered_at);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);

-- applications table
CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    resume_path     TEXT,
    cover_letter_path TEXT,
    resume_text     TEXT,
    cover_letter_text TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    error_message   TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    questions_json  TEXT,
    submitted_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_applications_job ON applications(job_id);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);

-- credentials table
CREATE TABLE IF NOT EXISTS credentials (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain          TEXT NOT NULL,
    company         TEXT,
    username        TEXT NOT NULL,
    password        TEXT NOT NULL,
    extra_data      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(domain, username)
);
CREATE INDEX IF NOT EXISTS idx_credentials_domain ON credentials(domain);

-- email_log table
CREATE TABLE IF NOT EXISTS email_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_message_id TEXT UNIQUE NOT NULL,
    thread_id       TEXT,
    from_address    TEXT NOT NULL,
    to_address      TEXT,
    subject         TEXT NOT NULL,
    body_preview    TEXT,
    received_at     TEXT NOT NULL,
    classification  TEXT,
    confidence      REAL,
    linked_job_id   INTEGER REFERENCES jobs(id),
    action_taken    TEXT,
    action_details  TEXT,
    processed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_email_classification ON email_log(classification);
CREATE INDEX IF NOT EXISTS idx_email_linked_job ON email_log(linked_job_id);
CREATE INDEX IF NOT EXISTS idx_email_received ON email_log(received_at);

-- agent_runs table
CREATE TABLE IF NOT EXISTS agent_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name      TEXT NOT NULL,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    jobs_found      INTEGER DEFAULT 0,
    apps_submitted  INTEGER DEFAULT 0,
    emails_processed INTEGER DEFAULT 0,
    error_message   TEXT,
    details_json    TEXT
);

-- llm_usage table
CREATE TABLE IF NOT EXISTS llm_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name      TEXT NOT NULL,
    model           TEXT NOT NULL,
    purpose         TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    cost_usd        REAL,
    job_id          INTEGER REFERENCES jobs(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_agent ON llm_usage(agent_name);
CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage(created_at);

-- qa_cache table
CREATE TABLE IF NOT EXISTS qa_cache (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    question_key TEXT NOT NULL,
    options_hash TEXT NOT NULL DEFAULT '',
    field_type   TEXT NOT NULL,
    answer       TEXT NOT NULL,
    confidence   REAL NOT NULL,
    source       TEXT NOT NULL,
    times_used   INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(question_key, options_hash)
);
CREATE INDEX IF NOT EXISTS idx_qa_cache_key ON qa_cache(question_key);
```

---

## Agent Designs

### Search Agent (every 4-6 hours, randomized)

1. Verify LinkedIn session (persistent browser context) — login if expired, handle 2FA by waiting for manual completion
2. For each query in `search_queries.yaml`, build LinkedIn search URLs (one pass with Easy Apply filter, one without)
3. Scroll results, extract job cards, deduplicate by `linkedin_job_id`
4. Navigate to each new job: `wait_until="load"` primary + 5-second optional `networkidle` bonus (LinkedIn's SPA background polling prevents `networkidle` from ever settling — silently times out and proceeds)
5. Extract full description + company via multi-layer fallback: JSON-LD → DOM selectors (including span/div variants for anonymous postings) → JS evaluation with `meta[name="description"]` parsing → page title pattern match. Company = "Unknown" if title found but company not — job is kept.
6. Detect `apply_type` using a 10-layer detection stack (see Phase 13 for order). Key insight: "I'm Interested" is checked **last** — LinkedIn shows it alongside "Apply" buttons on external jobs, so checking it early caused systematic misclassification of Workday/iCIMS/Lever jobs as `interest_only`.
7. Batch score with Claude Sonnet (profile vs job description → 0.0-1.0 score + reasoning)
8. Store in DB:
   - `apply_type in (interest_only, expired)` → `status="skipped"` (definitively not applicable)
   - `apply_type="unknown"` + `score >= min_score` → `status="qualified"` (apply agent will re-verify)
   - `apply_type="unknown"` + low score → `status="skipped"`
   - score >= 0.6 → `status="qualified"`, below → `status="disqualified"`

**LinkedIn Search URL Parameters:**
| Parameter | Description | Values |
|---|---|---|
| `keywords` | Search terms | URL-encoded string |
| `location` | Geographic filter | City, state, or country |
| `f_TPR` | Date posted | `r86400` (24h), `r604800` (week) |
| `f_E` | Experience level | `1`-`6` (intern to executive) |
| `f_WT` | Work type | `1` (onsite), `2` (remote), `3` (hybrid) |
| `f_AL` | Easy Apply only | `true` |
| `f_JT` | Job type | `F` (full-time), `C` (contract), etc. |
| `sortBy` | Sort order | `R` (relevant), `DD` (date) |
| `start` | Pagination | `0`, `25`, `50`, ... |

### Application Agent (every 2-3 hours, up to 10 per run)

1. Select `qualified` jobs with no application, ordered by match score. If `--apply-type` filter is set (e.g. `--apply-type workday`), candidates are filtered to that platform before the run limit is applied.
2. **Fail-fast check**: Re-detect `apply_type` from live page before spending LLM tokens. Uses AX tree first, then DOM fallbacks, then Vision if still `unknown`. Skip if unresolvable.
3. **PDF caching**: Check `data/resumes/resume_{slug}_*.pdf` and `cover_{slug}_*.pdf`. Reuse if present; otherwise generate via Opus and save.
4. **Per job (if no cached PDFs):** Generate tailored resume + cover letter via Claude Opus, convert to PDF via WeasyPrint
5. Delegate to the right applicator based on `apply_type`:
   - **LinkedInEasyApplicator**: Always re-navigates to job URL (avoids stale SPA state after 40+ second LLM calls). `_open_modal()` tries AX tree first (ARIA labels survive all CSS changes), then `get_by_role`, SDUI links, CSS selectors, and Vision as successive fallbacks. Iterates steps: upload resume, fill radio/select/textarea/text fields, submit. Calls `_pause_for_review()` before final click.
   - **WorkdayApplicator**: Check for stored credentials (per-domain, encrypted) or create a new account (auto-generated password, Gmail subaddress). Login, upload resume, fill sections (including JS `<button>+<ul role="listbox">` dropdowns via `_click_workday_option()`), handle EEO questions, detect validation errors after each Next click, submit with review pause. When DOM detection finds 0 field groups (non-standard `data-automation-id` values), runs `_scan_radiogroups` then falls back to the **Planner-Actor-Validator** loop: (1) **Planner** — AX tree snapshot via `format_interactive_fields`; if AX unavailable, Vision describes form fields instead; Claude Sonnet produces a JSON fill plan. (2) **Actor** — `_execute_plan_item` locates each field by ARIA label → `get_by_label` → three XPath text-proximity approaches (ancestor `<select>`, ancestor `<button>+listbox`, ancestor `[role='combobox']`). (3) **Validator** — `_validate_advance()` polls for section name change (400ms × 4s); Vision diagnosis + one LLM-guided retry on stuck. Submission confirmed via `_confirm_submission()` (page text → CSS selectors → Vision).
   - **GenericApplicator**: Best-effort with heavy Claude Vision usage for unknown form layouts, review pause before submit.
6. All applicators use a 5-level Q&A resolution chain:
   - **QA cache lookup** — `qa_cache` table keyed by `(normalize(question), options_hash)`. Threshold: confidence ≥ 0.7. Cache hit returns `source="cache"` immediately — zero LLM tokens.
   - **Profile lookup** — `custom_answers`, `years_of_experience`, work authorization, certifications (regex + acronym match with `re.MULTILINE`), salary, relocation, disability/gender/ethnicity
   - **Claude Sonnet (cached profile)** — full work history + education + certs + skill domains sent as a `cache_control: ephemeral` system block; Anthropic caches it for 5 minutes → ~90% token savings on repeated calls within one application
   - **Strategic fallback** — when Claude confidence < 0.5, a second call is made with the full job description, asking Claude to reason about what a hiring manager would expect for the specific role and seniority level. Returns `source="strategic", confidence=0.65`.
   - **Vision** — screenshot fallback for fields Claude cannot resolve from text alone
   - Questions with final confidence < 0.5 → mark `needs_review`. High-confidence (≥ 0.7) Claude/strategic answers are written to `qa_cache` automatically for future applications.

### Email Agent (every 5 min during business hours, 30 min off-hours)

1. Poll Gmail API for unread inbox messages
2. Classify each email with Claude Sonnet: interview_invite, rejection, follow_up, assessment, offer, recruiter_outreach, spam, unknown
3. Link to job in DB by matching company name
4. Act based on classification:
   - **interview_invite / assessment / offer / follow_up / unknown**: Forward to personal email
   - **rejection**: Log, update job status, label in Gmail
   - **recruiter_outreach**: Auto-reply if match score > threshold, else ignore
   - **spam**: Archive
5. Update `jobs.status` based on classification (interviewing, rejected, offer)

---

## User Profile Format (profile/user_profile.yaml)

```yaml
personal:
  first_name: ""
  last_name: ""
  email: ""                    # Dedicated Gmail inbox
  personal_email: ""           # Where to forward important emails
  phone: ""
  location: ""
  linkedin_url: ""
  github_url: ""               # Optional
  portfolio_url: ""            # Optional
  willing_to_relocate: false
  work_authorization: ""

summary: |
  Professional summary paragraph...

experience:
  - company: ""
    title: ""
    start_date: "YYYY-MM"
    end_date: "present"        # or "YYYY-MM"
    location: ""
    description: |
      Role description...
    achievements:
      - "Achievement with metrics..."
    technologies:
      - "Tech1"

education:
  - institution: ""
    degree: ""
    graduation_date: "YYYY-MM"
    gpa: ""                    # Optional

skills:
  programming_languages:
    - name: ""
      years: 0
      proficiency: "expert"    # expert, advanced, intermediate, beginner
  frameworks_and_tools:
    - "Tool1"
  certifications:
    - name: ""
      date: "YYYY-MM"
  domains:                     # Named domain expertise (used in resume prompt)
    - name: ""
      details: ""
      years: 0
      proficiency: "advanced"
  security_products:           # Optional product/vendor lists
    - "Product1"
  infrastructure_and_platforms:
    - "Platform1"
  other_tools:
    - "Tool1"

publications:
  - title: ""
    publisher: ""
    year: 2024

speaking_engagements:
  - title: ""
    venue: ""                  # DEF CON, Black Hat, RSA, etc.
    year: "2024"

preferences:
  job_titles:
    - "Target Title"
  target_companies: []         # Optional
  excluded_companies: []
  min_salary: 0
  max_salary: 0
  preferred_salary: 0
  remote_preference: "remote_preferred"  # remote_only, remote_preferred, hybrid_ok, onsite_ok
  locations: []
  industries: []               # Optional
  company_size: []             # Optional
  deal_breakers: []

application_answers:
  years_of_experience: 0
  desired_salary: ""
  start_date: ""
  sponsorship_required: false
  has_disability: "prefer_not_to_answer"
  veteran_status: "not_a_veteran"
  gender: "prefer_not_to_answer"
  ethnicity: "prefer_not_to_answer"
  how_did_you_hear: "LinkedIn"
  willing_to_travel: ""
  custom_answers:
    "Question pattern": |
      Answer template...
```

---

## Anti-Detection Strategy (LinkedIn)

LinkedIn detection operates at multiple layers; the stack addresses each:

### CDP Layer (Protocol-level)
- **Patchright** replaces Playwright as the browser automation library. It patches the `Runtime.enable` CDP command (Playwright issue [#34025](https://github.com/microsoft/playwright/issues/34025)) which standard Playwright leaks to anti-bot systems. Drop-in replacement — same async API, only the package name differs.
- Install: `python -m patchright install chromium`

### JS Fingerprint Layer
- **playwright-stealth v2** (`Stealth().hook_playwright_context(context)`) patches ~15 JS fingerprint tells on every page: `navigator.webdriver`, `window.chrome`, WebGL vendor/renderer, media codecs, plugins, iframe content window, etc.
- Custom `add_init_script` as belt-and-suspenders backup for `navigator.webdriver`, `window.chrome`, `navigator.languages`, `navigator.plugins`

### Browser Configuration
- **Headed mode** (not headless) throughout — headless mode has additional detection signals
- **`--disable-blink-features=AutomationControlled`** — prevents Chromium from setting the automation flag
- **`ignore_default_args=["--enable-automation"]`** — removes the "Chrome is being controlled" banner and associated flag

### Session Layer
- **Persistent context** (`data/browser_state/`) — reuses real cookies, localStorage, IndexedDB across runs. Avoids re-authentication checkpoint challenges on every run.
- Session warmup: visit feed, scroll briefly before searching
- `is_restricted()` checks URL for `checkpoint/challenge`, `authwall`; checks DOM for CAPTCHA iframes and restriction text. Aborts immediately on detection.

### Element Detection Layer
- **Accessibility tree first**: `browser/accessibility.py` snapshots `page.accessibility.snapshot()` and searches for elements by ARIA role + label. LinkedIn sets stable `aria-label="Easy Apply to <job title>"` attributes that survive all CSS/SDUI changes.
- **Sidebar guard**: `find_by_aria_label()` accepts a `job_id` parameter. Candidates whose label contains the current job's numeric ID are preferred — prevents clicking sidebar cards for other jobs.
- **AX-to-Locator bridge**: AX tree used for discovery only; found nodes are mapped back to real Playwright `Locator` objects via `get_by_role(role, name=label)` → `get_by_label(label)` fallback for clicking.
- **DOM fallback layers**: `get_by_role` text matching, aria-label attribute selectors, SDUI href matching — all retained as successive fallbacks after AX tree.
- **Vision last resort**: `vision_detect_apply_type()` called only at apply-time (never during search) to control cost.

### Behavioral Layer
- Random delays: `micro_delay` (80-300ms between keystrokes/clicks), `random_delay` (1-3s between actions), `application_delay` (30-90s between submissions)
- All text input via `element.type(char, delay=40)` — character-by-character with variable timing. Never `fill()` which sends the whole string at once.

### Rate Limits
- Max 25 applications/day (configurable, at LinkedIn's safe boundary)
- Max 10 applications/run
- Randomised search and apply intervals (4-6h and 2-3h respectively)

---

## Security

- Fernet key in env var, never in repo
- `.env`, `data/`, `gmail_token.json` all in `.gitignore`
- `data/` directory and all sensitive files `chmod 600/700`
- Log sanitization filter to redact passwords/tokens
- Auto-generated passwords for Workday accounts: 20+ chars, mixed character classes

---

## Estimated Daily Claude API Cost: $5-12

| Task | Model | Frequency | ~Cost/call |
|------|-------|-----------|------------|
| Job match scoring | Sonnet | ~50/day | $0.01 |
| Resume tailoring | Opus | ~15/day | $0.15 |
| Cover letter | Opus | ~15/day | $0.10 |
| Question answering | Sonnet | ~75/day | $0.005 |
| Vision fallback | Sonnet | ~5/day | $0.03 |
| Email classification | Sonnet | ~20/day | $0.005 |
| Auto-reply | Sonnet | ~2/day | $0.01 |

Daily budget enforcement via `llm_usage` table — stop non-critical calls if budget exceeded.

---

## Implementation Phases

### Phase 1: Foundation
DB schema + migrations, config/profile loading (Pydantic), crypto vault, structured logging, project scaffolding with pyproject.toml. **Independently testable** with unit tests for CRUD, encryption round-trip, profile validation.

### Phase 2: Browser Infrastructure + LinkedIn Login
Playwright persistent context, stealth module, helper functions, LinkedIn login flow (including 2FA wait), BaseAgent ABC. **Testable** with manual browser launch + login.

### Phase 3: Search Agent
Claude API client wrapper (retry, token tracking, cost logging), match scoring prompts, LinkedIn search URL construction, job parsing/dedup, rate limiter. **Testable** with live LinkedIn search.

### Phase 4: Application Agent — LinkedIn Easy Apply
Resume tailoring (Opus), cover letter generation (Opus), PDF conversion, BaseApplicator with question answering, LinkedInEasyApplicator (multi-step modal handler), Vision fallback module. **Testable** with real Easy Apply jobs.

### Phase 5: Application Agent — Workday & External Sites
WorkdayApplicator (account creation, credential storage, form navigation), GenericApplicator (Vision-heavy fallback for unknown ATSs). **Testable** against real Workday portals.

### Phase 6: Email Agent
Gmail OAuth2 setup, Gmail API client, email classifier, forwarding logic, auto-reply generation, job status updates from email events. **Testable** with real Gmail inbox.

### Phase 7: Scheduler, CLI & Polish
APScheduler wiring, CLI subcommands (run, search-now, apply-now, check-email, status, init), daily summary report, budget enforcement, graceful shutdown, end-to-end testing.

### Phase 8: Quality & Anti-Detection Hardening
Resume/cover letter quality improvements (domain expertise framing, tagline, core competencies, certifications, publications, speaking engagements). Full anti-detection stack: Patchright (CDP), playwright-stealth (JS layer), headed Chromium, persistent context, human-like typing, random delays, rate limits. Apply flow: re-detection before LLM tokens, PDF caching, review mode.

### Phase 9: AX Tree + Vision Detection Overhaul
Replaced fragile CSS-based apply button detection with accessibility tree (ARIA label) lookup as the primary method. AX tree survives all LinkedIn SDUI CSS changes. Added `browser/accessibility.py` with `get_ax_tree`, `search_ax_tree`, `find_by_aria_label`. Updated `_open_modal()` to use an 8-layer detection stack. Added `vision_detect_apply_type()` as apply-time Vision fallback. Added `VisionAnalyzer.analyze_form_fields()` for form field diagnostics.

### Phase 10: Q&A Intelligence — Prompt Caching, Rich Context, Strategic Fallback
Enhanced Q&A accuracy with three improvements:
1. **Certification regex fix** — `re.MULTILINE` flag ensures cert name detection works when LinkedIn embeds `\n` in question text. Acronym extraction from parentheses (e.g. "CISM" from "Certified Information Security Manager (CISM)").
2. **Prompt caching** — `ClaudeClient.message()` accepts `system_blocks` list. `BaseApplicator._build_profile_system_blocks()` sends full profile (work history, education, certs, skill domains) as a cached system block. Cache reads cost 0.10× input rate. `llm_usage` table tracks `cache_creation_tokens` and `cache_read_tokens`.
3. **Strategic fallback** — `_answer_strategically()` triggers when Claude confidence < 0.5. Sends full job description to Claude and asks it to reason about what a hiring manager would expect for the specific role and seniority level. Returns `source="strategic", confidence=0.65`.

Also fixed SDUI link navigation: replaced `link.click()` with `page.goto(href)` to prevent new-tab navigation that broke drift detection. Added `_sdui_link_broken` flag to skip CSS/Vision retries after a confirmed bad redirect. Added `qa-log` CLI command to view recorded Q&A for any application.

### Phase 13: Apply Type Detection Fix + Platform Stats + Test Iteration Tools
Root cause identified via spot-check: 9/10 sampled `interest_only` jobs were actually Workday, iCIMS, Lever, or other external-apply jobs. "I'm Interested" check was at Layer 2 (AX tree) and Layer 5 (DOM) — both before external link detection. Fixed by reordering to check external apply links/buttons at Layers 6-7 and "I'm Interested" at Layers 8-9. Extended `_classify_external_url()` with 14 named ATS platforms. Added `jobhunter platform-stats` command to track which ATSs are most common (informs which applicator to build next).

Added three test-iteration CLI flags:
- `search-now --max-queries N` — limits to first N search query configs
- `search-now --max-pages N` — overrides `max_pages_per_query`, limits result pages per query; `--max-queries 2 --max-pages 1` completes in ~10 min
- `apply-now --apply-type TYPE` — filters apply queue to a specific platform. Accepts shorthand (`workday`, `greenhouse`, `lever`, `icims`, `ashby`, `adp`, …) or full values (`external_workday`). Comma-separated or repeatable. Implemented via `_resolve_apply_types()` alias map; filter applied in `ApplyAgent.run_once()` after candidate selection.

Deleted 498 misclassified DB records and re-ran search to repopulate correctly.

### Phase 15: Planner-Actor-Validator Loop for Workday

When a Workday tenant uses non-standard `data-automation-id` values, the DOM-based field detection in `_handle_generic_section()` finds 0 field groups and exits without filling anything. The section then either stalls (required fields empty) or advances with blanks.

Three components added to `workday.py` as a fallback layer (DOM code runs first):

**Planner**: `format_interactive_fields(tree)` in `accessibility.py` walks the AX tree and returns a compact field list (role + name + required marker, capped at 40). `_plan_section_llm()` sends the field list + a compact profile summary (name, auth, years, salary, certs, top domains — ~600 chars) to Claude Sonnet with a JSON-only system prompt → `_parse_field_plan()` validates the response into a list of `{"label", "field_type", "value"}` dicts.

**Actor**: `_execute_plan_item()` locates each field via `find_by_aria_label()` (stable ARIA labels) → `get_by_label()` fallback, then fills by type (text `fill()`, select via Workday custom dropdown → native `select_option()`, radio `get_by_role("radio", name=value)`, checkbox `check()`). `_llm_guided_section()` orchestrates the full plan and returns fields-filled count.

**Validator**: `_validate_advance(old_section)` polls `_get_section_name()` every 400ms up to 4 seconds. Returns `(True, new_section)` on name change, `(False, old_section)` on timeout. Replaces the `prev_section_name`/`stuck_count` loop from Phase 14. On timeout: Vision diagnosis → one `_llm_guided_section()` retry → re-advance → re-validate → abort if still stuck.

Also added `_confirm_submission()`: page text scan (6 phrases) → 4 CSS selectors → Vision `analyze_page()`. `_submit_application()` now calls it and logs confirmed vs assumed-success.

7 new tests (370 total): 2 in `test_accessibility.py` (`format_interactive_fields`), 5 in new `test_workday_planner.py` (`_parse_field_plan`).

### Phase 16: Workday Vision Fallback + Drop-Down Field Locator Hardening

Live test runs (run_ids 90–93) against real Workday tenants identified two Phase 15 gaps and one performance issue.

**`_llm_guided_section` Vision fallback**: `get_ax_tree()` returns `None` silently on some Workday tenants (CDP accessibility snapshot blocked). The Phase 15 code had `if not tree: return 0` before the Vision path, causing the entire LLM-guided fallback to be skipped. Fixed by computing `field_summary = format_interactive_fields(tree) if tree else ""` and branching on `if not field_summary:` — Vision is then called regardless of whether the AX tree was available. Vision correctly described all application-questions dropdowns in live testing.

**`_execute_plan_item` text-proximity locator approaches**: When both `find_by_aria_label` (AX tree unavailable) and `get_by_label` (no label-for association) fail, the actor previously returned 0 immediately for select/dropdown types. Three new text-proximity XPath approaches added as approaches 7–9: find question text via `get_by_text(label)`, then walk up to the nearest ancestor containing a `<select>`, a `<button>`, or a `[role='combobox']` respectively, and interact with it directly. Radio/radiogroup now also skips the early-return when `get_by_label` fails (approaches 1–4 use `self._page` directly and work without a pre-found locator).

**`_scan_radiogroups` broad fallback**: Called before `_llm_guided_section` when DOM finds 0 field groups. Queries `[role='radiogroup']` site-wide, resolves question labels from `aria-labelledby` → `aria-label` → JS sibling text, then calls `answer_question()` and clicks the matching radio.

**Auth flow timeout reduction**: Reduced per-selector timeout from 10s to 2s for sign-in and create-account CSS selector chains. Net savings: ~80s per auth attempt when CSS selectors miss (common on tenants using non-standard `data-automation-id` values).

**Account creation verify-password scan**: After all CSS/label approaches fail for the confirm-password field, queries `input[type='password']` and fills the first empty one. Handles tenants where the confirm field uses a non-standard `data-automation-id`.

3 new tests (373 total): three radiogroup formatting tests in `test_accessibility.py` covering the Workday pattern where label text is embedded as a child text node or preceding sibling.

### Phase 12: Workday Hardening + Cross-Application Q&A Cache
Three targeted fixes for Workday applications and repeated-question efficiency:

1. **Cross-application Q&A cache** — new `qa_cache` SQLite table (7th table in schema). `BaseApplicator.answer_question()` gains a step-0 cache lookup: normalized question + options hash → stored answer, returned instantly at zero LLM cost. High-confidence answers (≥ 0.7) from Claude/strategic are written to cache automatically. `QACacheRepo` instantiated once per dispatch and passed to all three applicators. After 1-2 applications the cache accumulates answers to recurrent questions ("years of experience", "work authorization", "sponsorship required") and those questions never trigger LLM calls again.

2. **Workday JS dropdown support** — `_handle_generic_section()` now detects `button[aria-haspopup='listbox']` before checking `<select>`. Two new helpers: `_get_workday_dropdown_options()` (opens listbox, reads texts, closes with Escape) and `_click_workday_option()` (exact-then-partial match click). Required fields that were previously silently skipped now get answered.

3. **Validation error detection** — after each `_advance()` in `_navigate_form()`, queries `[data-automation-id='field-error']` and related selectors. Logs up to 3 error messages at WARNING level. Continues advancing (some errors self-resolve); guards against silently submitting incomplete sections.

---

## Verification

- **Phase 1**: `pytest tests/test_db/ tests/test_crypto/` — CRUD operations, encryption round-trip, profile loading with invalid data
- **Phase 2**: Manual — launch browser, verify LinkedIn login, confirm session persists across restarts
- **Phase 3**: `python -m jobhunter search-now` — verify jobs appear in DB with scores
- **Phase 4**: `python -m jobhunter apply-now` — verify Easy Apply submission on a real listing
- **Phase 5**: Manual — test Workday account creation + application on a real portal
- **Phase 6**: `python -m jobhunter check-email` — verify classification, forwarding, DB status updates
- **Phase 7**: `python -m jobhunter run` — full scheduler runs all agents on schedule, verify with `python -m jobhunter status`
- **Phase 8-9**: `pytest tests/` (349 tests) — unit coverage for AX tree helpers, form field analysis, apply detection fallback chain
- **Phase 10**: `jobhunter apply-now --review` — verify cert detection, strategic Q&A fallback, and SDUI navigation work on live applications; `jobhunter qa-log` to inspect recorded answers
- **Phase 12**: `pytest tests/` (361 tests) — QA cache DB tests + BaseApplicator cache integration tests; `jobhunter init` to create `qa_cache` table on existing DB; `jobhunter apply-now --review` on a Workday job to verify dropdown handling and "QA cache hit" log lines on the second application
- **Phase 13**: `pytest tests/` (363 tests); `jobhunter search-now --max-queries 2 --max-pages 1` to verify correct classification (Workday/iCIMS/Lever jobs no longer land as `interest_only`); `jobhunter platform-stats` to see ATS distribution; `jobhunter apply-now --apply-type workday --dry-run` to test Workday applicator in isolation
- **Phase 15**: `pytest tests/` (370 tests); `jobhunter apply-now --apply-type workday --review` — watch logs for `"LLM-guided section: N/M fields filled"` (planner activated on non-standard tenant), `"Section 'X' did not advance"` (validator caught stuck state), and confirmation text in page content after submit
- **Phase 16**: `pytest tests/` (373 tests); `jobhunter apply-now --apply-type workday --review` — watch logs for `"LLM-guided using Vision field description: ..."` (Vision fallback firing when AX tree is None), `"DOM field detection: N groups found"`, `"Broad radiogroup scan: N groups found"`. Approach 7/8/9 activation visible at DEBUG level when `get_by_label` fails for a select/dropdown plan item.
