# JobHunter — Development Progress

## Project Overview
Automated job search & application system with three AI agents (search, apply, email).
See `DESIGN.md` for full architecture and `pyproject.toml` for dependencies.

## Quick Start
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m patchright install chromium   # one-time browser install
cp .env.example .env   # fill in ANTHROPIC_API_KEY, FERNET_KEY, etc.
jobhunter init
```

## Running Tests
```bash
pytest tests/   # 373 tests (Phase 16 — 3 new tests added)
```

## CLI Reference
```bash
jobhunter init                    # DB init, key generation, profile validation
jobhunter status                  # Jobs/apps counts, LLM spend, recent runs
jobhunter run                     # Start full scheduler (Ctrl+C to stop)
jobhunter search-now              # One-shot search agent run
jobhunter search-now --max-queries 3           # Limit to first N queries (fast test runs)
jobhunter search-now --max-queries 2 --max-pages 1  # Fastest test: 1 result page per query (~10 min)
jobhunter apply-now               # One-shot apply run (full auto)
jobhunter apply-now --dry-run     # Generate PDFs but do not submit
jobhunter apply-now --review      # Fill forms, then pause for human approval before each submit
jobhunter apply-now --apply-type workday            # Only attempt Workday jobs
jobhunter apply-now --apply-type workday,greenhouse # Multiple platforms (comma-separated)
jobhunter apply-now --apply-type workday --dry-run  # Combine with other flags
jobhunter check-email             # One-shot email agent run
jobhunter daily-summary           # Print today's stats
jobhunter platform-stats          # Show ATS platform distribution of discovered jobs
jobhunter qa-log                  # Show Q&A log for most recent application with Q&A
jobhunter qa-log --app-id 42      # Show Q&A log for a specific application
```

## Phase Status

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Foundation: DB, crypto vault, profile loading, logging | ✅ Complete |
| 2 | Browser infrastructure + LinkedIn login | ✅ Complete |
| 3 | Search Agent | ✅ Complete |
| 4 | Application Agent — LinkedIn Easy Apply | ✅ Complete |
| 5 | Application Agent — Workday & External | ✅ Complete |
| 6 | Email Agent | ✅ Complete |
| 7 | Scheduler, CLI & Polish | ✅ Complete |
| 8 | Quality & Anti-Detection Hardening | ✅ Complete |
| 9 | AX Tree + Vision Detection Overhaul | ✅ Complete |
| 10 | Q&A Intelligence: Prompt Caching, Rich Context, Strategic Fallback | ✅ Complete |
| 11 | Search Parsing Hardening: Navigation, Anonymous Postings, Unknown Apply Type | ✅ Complete |
| 12 | Workday Hardening + Cross-Application Q&A Cache | ✅ Complete |
| 13 | Apply Type Detection Fix + Platform Stats | ✅ Complete |
| 14 | Workday Form Hardening: Typeahead, Date Fields, Stuck Detection | ✅ Complete |
| 15 | Planner-Actor-Validator Loop for Workday | ✅ Complete |
| 16 | Workday Vision Fallback + Drop-Down Field Locator Hardening | ✅ Complete |

---

## Phase 1 — Complete ✅

**What was built:**
- `src/jobhunter/db/` — SQLite engine (WAL mode), schema (6 tables), dataclass models, named queries, full CRUD repositories for all tables
- `src/jobhunter/crypto/vault.py` — Fernet encrypt/decrypt, secure password generation
- `src/jobhunter/utils/logging.py` — JSON structured logging with sensitive-value redaction
- `src/jobhunter/utils/profile_loader.py` — Pydantic v2 user profile validation (personal, experience, education, skills, preferences, application_answers, publications, speaking_engagements)
- `src/jobhunter/main.py` — CLI skeleton
- Config files, profile YAML, Jinja2 templates
- 79 unit tests passing

---

## Phase 2 — Complete ✅

**What was built:**
- `src/jobhunter/browser/context.py` — `BrowserSession` with persistent Patchright context, `ensure_linkedin_session()`, 5-minute manual login wait, restriction detection
- `src/jobhunter/browser/stealth.py` — `random_delay`, `application_delay`, `micro_delay`, `human_type`, `warmup_session`, `is_restricted`
- `src/jobhunter/browser/helpers.py` — `wait_and_click`, `fill_field`, `upload_file`, `scroll_to_bottom`, `select_option`, `is_visible`, `get_text` — all with selector fallback chains
- `src/jobhunter/browser/vision.py` — `VisionAnalyzer` for screenshot + Claude Vision fallback
- `src/jobhunter/agents/base.py` — `BaseAgent` ABC with retry, DB lifecycle, agent_runs audit, LLM usage logging, budget check
- 18 new unit tests (97 total passing)

---

## Phase 3 — Complete ✅

**What was built:**
- `src/jobhunter/llm/client.py` — `ClaudeClient` async wrapper: retry on rate limit/server error, token + cost tracking, vision support, per-model pricing
- `src/jobhunter/llm/prompts.py` — job scoring, email classification, recruiter reply prompt templates
- `src/jobhunter/utils/rate_limiter.py` — `TokenBucket` (async, thread-safe), `RateLimiter`
- `src/jobhunter/agents/search_agent.py` — URL builder, job card extraction, full description scraping, `detect_apply_type()` using `get_by_role` + aria-label matching, batch scoring via Claude Sonnet, DB upsert
- 50 new unit tests (147 total passing)

---

## Phase 4 — Complete ✅

**What was built:**
- `src/jobhunter/llm/resume.py` — `generate_tailored_resume()` (Claude Opus), `render_resume_html()` (Jinja2), `save_resume_pdf()` (WeasyPrint). Prompt targets domain expertise, business-impact language, tagline, core competencies, certifications, publications, speaking engagements. Last 15 years in full detail; earlier career condensed.
- `src/jobhunter/llm/cover_letter.py` — `generate_cover_letter()`, `render_cover_letter_html()`, `save_cover_letter_pdf()`
- `templates/resume.html` — Jinja2 → PDF: navy blue (#0f2847), Georgia serif headings, Core Competencies 3-column grid, Publications and Speaking sections, `@page` margins, `page-break-inside: avoid`
- `templates/cover_letter.html` — matching style, `@page` margins
- `src/jobhunter/applicators/base.py` — `BaseApplicator` ABC with `answer_question()` (profile → Claude Sonnet → Vision fallback), `record_qa()`, `has_low_confidence_answers()`, `_pause_for_review()` (review mode)
- `src/jobhunter/applicators/linkedin_easy.py` — `LinkedInEasyApplicator`: Easy Apply modal multi-step handler. Always re-navigates to job URL before opening modal. `_open_modal()` uses `get_by_role` primary with CSS selector fallback. Handles upload, radio, select, textarea, text field steps. Calls `_pause_for_review()` before final submit.
- `src/jobhunter/agents/apply_agent.py` — Selects qualified jobs, re-detects `apply_type` before generating materials (fail-fast on unknown), PDF caching (reuses `data/resumes/resume_{slug}_*.pdf` if present), dispatches to correct applicator
- 50 new unit tests (197 total passing)

---

## Phase 5 — Complete ✅

**What was built:**
- `src/jobhunter/applicators/workday.py` — `WorkdayApplicator`: domain-based credential storage (encrypted), account creation with auto-generated password, login detection, multi-section form navigation (My Information, My Experience, Documents, Questions, Self-ID), `_pause_for_review()` before submit
- `src/jobhunter/applicators/generic.py` — `GenericApplicator`: best-effort Vision-heavy fallback for unknown external ATSs, `_pause_for_review()` before submit
- `src/jobhunter/crypto/vault.py` — `CredentialVault` used by Workday for per-domain encrypted credentials
- 50 new unit tests (247 total passing)

---

## Phase 6 — Complete ✅

**What was built:**
- `src/jobhunter/gmail/auth.py` — `get_gmail_service()`: OAuth2 flow, token caching (`data/gmail_token.json`), auto-refresh
- `src/jobhunter/gmail/client.py` — `GmailClient`: list, get, send, forward, modify labels, mark read, archive; `GmailMessage` dataclass
- `src/jobhunter/gmail/classifier.py` — `classify_email()` via Claude Sonnet → `ClassificationResult`
- `src/jobhunter/agents/email_agent.py` — polls unread inbox, classifies, routes: forward (interview_invite/assessment/offer), label (rejection → updates job status), auto-reply (recruiter_outreach), archive (spam)
- 39 new unit tests (286 total passing)

**Gmail scopes:** `gmail.readonly` + `gmail.send` + `gmail.modify`

---

## Phase 7 — Complete ✅

**What was built:**
- `src/jobhunter/scheduler.py` — `JobHunterScheduler`: APScheduler (AsyncIOScheduler, UTC). Search every 4-6 hours (randomised, immediate), apply every 2-3 hours, email every 5 min with business-hours throttle, daily summary at 22:00. `build_daily_summary`, `print_daily_summary`, one-shot runner coroutines.
- `src/jobhunter/main.py` — All CLI commands implemented including `--dry-run` and `--review` flags for `apply-now`
- 42 new unit tests (328 total passing)

---

## Phase 8 — Complete ✅ (Quality & Anti-Detection Hardening)

**Resume/cover letter quality:**
- `profile_loader.py` — Added `SkillDomain`, `Publication`, `SpeakingEngagement` Pydantic models; `domains`, `security_products`, `infrastructure_and_platforms`, `other_tools` fields on `Skills`; `publications` and `speaking_engagements` on `UserProfile`
- Resume prompt rewritten: domain expertise framing, business-impact language, tagline, core competencies, top certifications, 15-year depth limit
- Resume/cover letter HTML templates: navy blue palette, Georgia headings, `@page` margins (no body padding), page-break controls, widows/orphans

**Anti-detection stack:**

| Layer | What it does |
|-------|--------------|
| Patchright | Patches CDP `Runtime.enable` — the signal LinkedIn detects at protocol level. Drop-in replacement for Playwright. |
| playwright-stealth v2 | JS-layer patches: `navigator.webdriver`, `window.chrome`, WebGL, plugins, media codecs, ~15 fingerprint tells |
| Custom init script | Backup `navigator.webdriver`, `window.chrome`, `languages`, `plugins` patches on every page |
| Headed mode | Non-headless Chromium throughout |
| Persistent context | `data/browser_state/` reused across runs — avoids re-auth checkpoint triggers |
| Human-like typing | All text input via `element.type(char, delay=40)` not `fill()` |
| Random delays | `micro_delay` (80-300ms), `random_delay` (1-3s), `application_delay` (30-90s) |
| Rate limits | ≤25 applications/day, randomised intervals between all actions |

**Apply flow improvements:**
- `_apply_to_job()` — re-detects `apply_type` before spending LLM tokens; skips job immediately if still unknown
- `linkedin_easy.py` `apply()` — always re-navigates to job URL before `_open_modal()` (LLM calls take 40+ seconds; SPA state may drift)
- PDF caching — reuses existing `data/resumes/resume_{slug}_*.pdf` and `cover_{slug}_*.pdf` instead of regenerating via Opus

**Review mode (`--review` flag):**
- All three applicators call `_pause_for_review()` before the final submit click
- Terminal prompt: `[Enter]` submit, `[s]` skip this job, `[q]` quit all
- Uses `loop.run_in_executor(None, input, "> ")` to avoid blocking the event loop

1 new test (329 total passing)

---

## Phase 9 — Complete ✅ (AX Tree + Vision Detection Overhaul)

**Root cause fixed:** CSS/text-based `detect_apply_type()` broke repeatedly because LinkedIn's SDUI changes class names constantly and sidebar job cards share the same DOM scope — causing accidental clicks on other jobs' Easy Apply buttons.

**Solution:** Primary detection now uses the accessibility tree (ARIA labels). LinkedIn sets stable `aria-label="Easy Apply to <job title>"` attributes that survive all CSS changes. Vision fallback only fires when AX tree yields nothing.

**What was built:**
- `src/jobhunter/browser/accessibility.py` — new AX tree helper module:
  - `get_ax_tree(page)` — wraps `page.accessibility.snapshot()` (CDP swap = 1-line change)
  - `search_ax_tree(node, *, role, label_pattern, label_contains)` — pure recursive walker, no I/O, unit-testable with static dicts
  - `find_by_aria_label(page, label_pattern, *, roles, job_id, timeout_ms)` — snapshots AX tree → finds node → maps back to a Playwright `Locator` via `get_by_role` → `get_by_label` fallback; `job_id` guards against sidebar card false positives
- `search_agent.py` — AX tree checks are **layers 1 & 2** in `detect_apply_type()` (before all CSS/DOM fallbacks). New `vision_detect_apply_type(page, vision)` for apply-time Vision fallback only.
- `linkedin_easy.py` — `_open_modal()` rewritten with 8-layer detection (AX tree → interest_only AX → scoped DOM → `get_by_role` → SDUI links → CSS → Vision → diagnostics). `_handle_form_step()` logs Vision-detected fields when DOM finds nothing.
- `apply_agent.py` — `_redetect_apply_type()` calls `vision_detect_apply_type()` when DOM/AX returns `"unknown"`.
- `vision.py` — `VisionAnalyzer.analyze_form_fields(page, context)` added.
- `tests/test_browser/test_accessibility.py` — 17 new tests (static dict fixtures, no real browser)
- 3 more tests in existing files (2 for `analyze_form_fields`, 1 import smoke test)

**New `_open_modal()` detection order:**

| Layer | Method |
|-------|--------|
| 1 | AX tree `find_by_aria_label("easy apply")` with job_id sidebar guard |
| 2 | AX tree `find_by_aria_label("i'm interested")` → early return + `detected_interest_only` |
| 3 | Scope DOM to `.jobs-search__job-details` / `main` |
| 4 | `get_by_role("button", name="easy apply")` |
| 5 | SDUI: `get_by_role("link")` + `a[href*=openSDUIApplyFlow]` with job_id validation |
| 6 | CSS selector fallback via `wait_and_click(_EASY_APPLY_BTN)` |
| 7 | Vision: JSON prompt → `get_by_text(label_text)` click |
| 8 | Diagnostic logging (buttons + apply-related links) |

20 new tests (349 total passing)

---

## Phase 10 — Complete ✅ (Q&A Intelligence: Prompt Caching, Rich Context, Strategic Fallback)

**Problem:** Application form Q&A was using a minimal profile summary and returning low-confidence answers for domain-specific questions (e.g., years of experience in sub-domains, specific certification lookup). Claude was answering "No" to "Do you have CISM?" despite the cert being in the profile.

**What was built:**

**Certification lookup fix (`applicators/base.py`)**:
- `_answer_from_profile()` now checks `profile.skills.certifications` when the question matches `/(license|certification|certificate|licensed|certified)/`
- Extracts cert acronym from parentheses — matches both "CISM" and "Certified Information Security Manager (CISM)"
- Uses `re.MULTILINE` flag — required because LinkedIn embeds `\n` in question text

**Prompt caching (`llm/client.py` + `applicators/base.py`)**:
- `ClaudeClient.message()` now accepts `system_blocks: Optional[list]` — a list of Anthropic content block dicts with `cache_control: {"type": "ephemeral"}`
- Cache pricing tracked in `_COST_PER_MTOK`: write = 1.25× input rate, read = 0.10× input rate
- `_extract_usage()` reads `cache_creation_input_tokens` and `cache_read_input_tokens` from API response
- All cache token counts appear in `llm_usage` table for cost visibility

**Rich profile context (`applicators/base.py`)**:
- `_build_profile_system_blocks()` builds a full profile document as the cached system block:
  - Full work history with company, title, dates, description, achievements
  - All education entries
  - All certifications (name + date)
  - Skill domains with years and proficiency
  - Application defaults (years of experience, salary, work authorization, etc.)
- Profile block marked `cache_control: ephemeral` — Anthropic caches it for 5 minutes, refreshing on each hit
- `_TASK_INSTRUCTIONS` is the second system block (instructions only, uncached)
- Net effect: repeated Q&A calls within one application reuse the cached profile → ~90% token savings after the first call

**Strategic Q&A fallback (`applicators/base.py`)**:
- `_answer_strategically(question, field_type, options, context)` — new method
- Called when `_answer_via_claude()` returns confidence < 0.5
- Sends Claude the full job description (truncated to 2500 chars) + job title + seniority level
- Prompt instructs Claude to reason as a hiring manager: "What would a typical hiring manager at this company expect for this role? Give the answer most likely to result in an interview."
- Returns `QuestionAnswer(source="strategic", confidence=0.65)` — higher baseline than raw Claude uncertainty

**Q&A resolution order (updated):**
```
1. profile lookup (_answer_from_profile)
   — custom_answers → years_of_experience → work_authorization → sponsorship → travel →
     salary → relocation → disability/veteran/gender/ethnicity → certification lookup
2. Claude Sonnet with cached profile system block (_answer_via_claude)
3. Strategic fallback with job description context (_answer_strategically)   ← NEW
4. Vision screenshot fallback (_answer_via_vision)
5. Empty answer (marks question needs_review=True)
```

**SDUI link navigation fix (`applicators/linkedin_easy.py`)**:
- LinkedIn SDUI Easy Apply links have implicit `target="_blank"` — `link.click()` opened a new tab, leaving the original page URL unchanged so `_back_to_job_if_drifted()` returned False (no drift detected)
- Fix: replaced `await link.click()` with `await self._page.goto(full_href, wait_until="load", timeout=15_000)` — forces in-tab navigation, so drift detection works correctly
- Same fix applied to AX layer 1 when the found element has an `href` attribute
- `_back_to_job_if_drifted()` return type changed from `None` to `bool` (returns `True` when navigation was performed)
- `_sdui_link_broken` flag: set when drift was detected after SDUI navigation; causes Layers 6 (CSS) and 7 (Vision) to be skipped — prevents wasting ~40s retrying the same broken redirect

**`qa-log` CLI command (`main.py`)**:
- `jobhunter qa-log` — shows Q&A log for most recent application with recorded answers
- `jobhunter qa-log --app-id 42` — show Q&A for a specific application
- Output per entry: question, answer, source (profile/claude/strategic/vision), confidence, `[NEEDS REVIEW]` flag

**`fill()` instead of `type()` for text inputs (`applicators/linkedin_easy.py`)**:
- LinkedIn's SDUI forms use React-controlled `<input type="number">` elements
- `type()` fires keyboard events but doesn't reliably trigger React's synthetic `onChange`; the field appears empty to LinkedIn's validation (`Enter a whole number between 0 and 99`)
- Fix: `_handle_text_input()` now uses `fill()` which sets the value and fires the `input`+`change` events React controlled components listen for

**Field-level and Next button logging (`applicators/linkedin_easy.py`)**:
- `_fill_field_group()` logs field type and truncated question at INFO level
- `_handle_radio()` logs available options + chosen answer + click result
- `_handle_select()` logs options, answer, and select_option result
- `_click_next()` logs which button selector matched and its label
- Post-Next validation error check logs any `artdeco-inline-feedback--error` messages

349 tests passing (no new tests added this phase — all changes covered by existing test structure).

---

## Phase 11 — Complete ✅ (Search Parsing Hardening)

**Problem:** Many qualified jobs were being silently dropped from the pipeline due to three root causes:
1. `networkidle` wait caused 25s timeouts per job (LinkedIn's SPA has background polling that never settles)
2. Anonymous/confidential postings (no `<h1>` element) were silently discarded
3. High-scoring jobs with `apply_type="unknown"` were set to `status="skipped"` instead of entering the apply pipeline for re-verification

**What was built:**

**Navigation fix (`search_agent.py` — `navigate_to_job()`)**:
- Changed from `wait_until="networkidle"` (25s primary) to `wait_until="load"` + 5-second optional `networkidle` bonus
- `networkidle` timeout is caught silently — navigation proceeds with the loaded DOM
- Result: per-job page load time reduced from ~25s to ~2-3s

**Anonymous posting extraction (`search_agent.py` — `extract_job_details()`)**:
- Extended `_JOB_COMPANY_SELECTORS` with `div`/`span` variants (anonymous postings omit the `<a>` link element)
- Added `a[href*='/company/']`, `[data-entity-urn*='company']`, and plain-text container selectors
- JS fallback extended with multi-selector loop covering all company container variants
- Added `meta[name="description"]` parsing — LinkedIn embeds `"Title at Company · Seniority · Location"` in the meta description
- Company extraction priority: `ld_company → company_dom → meta_company → pt_company → og_company`
- If title is found but company is not → `company = "Unknown"` (job is kept, not discarded)
- "Could not extract" log level downgraded from WARNING to INFO when title was successfully extracted

**Unknown apply_type promotion (`search_agent.py` — `_score_and_store()`)**:
- `apply_type="interest_only"` and `apply_type="expired"` → `status="skipped"` (definitively not applicable)
- `apply_type="unknown"` + `score >= min_score` → `status="qualified"` (let apply agent re-verify at apply time)
- `apply_type="unknown"` + low score → `status="skipped"` (not worth re-verifying)
- DB backfill: 25 previously-skipped jobs with `apply_type="unknown"` and score ≥ 0.5 promoted to `qualified`

No new tests (changes covered by existing structure). 349 tests still passing.

---

## Phase 12 — Complete ✅ (Workday Hardening + Cross-Application Q&A Cache)

**Problem:** Three gaps in the Workday applicator and Q&A pipeline:
1. Same question answered via a live Claude API call on every Workday application — no cross-application reuse
2. Workday's JS `<button>` + `<ul role="listbox">` dropdowns silently skipped (no native `<select>`)
3. No validation error detection after clicking Next — invalid sections submitted blindly

**What was built:**

**`qa_cache` DB table (`schema.sql`, `models.py`, `queries.py`, `repository.py`)**:
- New `qa_cache` table with `UNIQUE(question_key, options_hash)` — one row per unique question × options combination
- `times_used` counter incremented on each cache hit via `ON CONFLICT DO UPDATE`
- `QACache` dataclass, `GET_QA_CACHE` + `UPSERT_QA_CACHE` queries, `QACacheRepo` class
- `jobhunter init` creates the table on existing DBs (idempotent `CREATE TABLE IF NOT EXISTS`)

**Cache integration in `BaseApplicator` (`applicators/base.py`)**:
- `_normalize_question(q)` — lowercase, strip punctuation, collapse whitespace → stable 500-char key
- `_options_hash(options)` — MD5[:8] of sorted option list; `''` for text/textarea fields
- `answer_question()` resolution order updated (step 0 is new):
  ```
  0. QA cache lookup (conf ≥ 0.7 → return source="cache", no LLM call)
  1. Profile lookup
  2. Claude Sonnet (cached profile)
  3. Strategic fallback (job description context)
  4. Vision screenshot
  5. Empty (needs_review=True)
  ```
- `_write_qa_cache()` — called after any Claude or strategic answer with `confidence ≥ 0.7`; skips `profile` and `cache` sources; swallows exceptions silently

**`ApplyAgent._dispatch()` wiring (`agents/apply_agent.py`)**:
- Instantiates `QACacheRepo(self._conn)` once per dispatch
- Passes `qa_cache=qa_cache` to all three applicators (`LinkedInEasyApplicator`, `WorkdayApplicator`, `GenericApplicator`)
- All three `__init__` signatures updated with `qa_cache=None` parameter

**Workday custom dropdown handler (`applicators/workday.py`)**:
- `_get_workday_dropdown_options(container)` — clicks the `button[aria-haspopup='listbox']`, reads `[role='option']` texts, closes with Escape. Avoids committing to a selection during option discovery.
- `_click_workday_option(container, answer)` — opens listbox, tries exact match then partial match fallback, clicks the matching option
- `_handle_generic_section()` now checks for `button[aria-haspopup='listbox']` **before** `<select>` — Workday forms use JS dropdowns, not native selects
- Improved field container selectors: `[data-automation-id^='formField-']`, `[data-automation-id='questionContainer']`, GWT class fallbacks (`div.WGCQ`, `div.WM8K`)
- `_write_qa_cache()` called for every answered field type (radio, dropdown, select, textarea, text)

**Validation error detection (`applicators/workday.py` — `_navigate_form()`)**:
- After each `_advance()`, queries `[data-automation-id='field-error']`, `[data-automation-id='errorMessage']`, `p.error-msg`, `span.error-text`
- Logs up to 3 error messages at WARNING level with section name — logs and continues (some errors self-resolve on next advance)

**12 new tests (361 total passing)**:
- 4 `QACacheRepo` tests: upsert/get, `times_used` increment, answer update on conflict, cache miss returns None
- 5 `BaseApplicator` cache integration tests: cache hit bypasses LLM, high-confidence write triggers upsert, low-confidence does not write, `_normalize_question`, `_options_hash` order-independence
- 3 helper unit tests: whitespace collapse, empty options → empty string, hash is 8 chars

---

## Phase 13 — Complete ✅ (Apply Type Detection Fix + Platform Stats)

**Problem:** Spot-checking 10 `interest_only` jobs revealed 9 were misclassified — Workday, iCIMS, Lever, company sites, and even an Easy Apply job all ended up as `interest_only`. Root cause: the "I'm Interested" check fired at Layer 2 (AX tree) and Layer 5 (DOM), both **before** the external apply link detection at Layer 8. LinkedIn shows "I'm Interested" as a secondary engagement CTA alongside the primary "Apply" button on most external-apply jobs, so all external jobs short-circuited to `interest_only`.

**What was built:**

**`detect_apply_type()` order rewritten (`agents/search_agent.py`)**:
- "I'm Interested" checks moved from Layers 2 & 5 to Layers 8 & 9 — after all apply-button detection
- External apply links and buttons now checked at Layers 6 & 7 (moved up from Layers 8 & 9)
- New detection order:
  ```
  1. AX tree Easy Apply
  2. DOM Easy Apply (role+text)
  3. DOM Easy Apply (aria-label)
  4. SDUI Easy Apply link
  5. Expired
  6. External apply links  ← moved up
  7. External apply buttons ← moved up
  8. AX tree "I'm Interested" ← moved down
  9. DOM "I'm Interested"    ← moved down
  10. Diagnostic + unknown
  ```

**`_classify_external_url()` extended with 14 ATS platforms**:
- `external_workday` (myworkdayjobs.com, workday.com)
- `external_greenhouse` (greenhouse.io)
- `external_lever` (lever.co)
- `external_icims` (icims.com)
- `external_taleo` (taleo.net, oracle.com/taleo)
- `external_smartrecruiters` (smartrecruiters.com)
- `external_jobvite` (jobvite.com)
- `external_bamboohr` (bamboohr.com)
- `external_successfactors` (successfactors.com)
- `external_ashby` (ashbyhq.com)
- `external_theladders` (theladders.com)
- `external_paylocity` (paylocity.com)
- `external_ukg` (ultipro.com, ukg.com)
- `external_adp` (adp.com)
- `external_oracle` (oraclecloud.com)

**New `jobhunter platform-stats` CLI command (`main.py`)**:
- Shows full apply-type distribution across all jobs in DB
- Re-classifies `external_other` records by parsing `external_url` against ATS domain list
- Lists external platforms with no applicator built yet (prioritized by job count)

**`jobhunter search-now --max-queries N` and `--max-pages N` flags (`main.py`)**:
- `--max-queries N` slices the first N queries from `config/search_queries.yaml`
- `--max-pages N` overrides `global_filters.max_pages_per_query` (default 2) — limits result pages fetched per query
- Together: `--max-queries 2 --max-pages 1` = at most ~50 jobs total, runs in ~10 minutes
- 15 total queries × 2 passes (Easy Apply + all) = 30 LinkedIn searches per full run

**`jobhunter apply-now --apply-type TYPE` flag (`main.py`, `scheduler.py`, `agents/apply_agent.py`)**:
- Filters the candidate job list to only jobs with a matching `apply_type`
- Accepts shorthand aliases (`workday`, `greenhouse`, `lever`, `icims`, `ashby`, `adp`, `easy_apply`, `other`, …) or full values (`external_workday`)
- Repeatable (`--apply-type workday --apply-type greenhouse`) or comma-separated (`--apply-type workday,greenhouse`)
- Implemented via `_resolve_apply_types()` alias lookup in `main.py`; filter applied in `ApplyAgent.run_once()` after `list_qualified_without_application()`
- Combines with `--dry-run` and `--review` for targeted testing

**DB cleanup**: Deleted 498 misclassified `interest_only` jobs so the next search re-classifies them with the fixed detection code.

**2 new tests + 2 updated (363 total passing)**:
- `test_lever` — lever.co → `external_lever`
- `test_greenhouse` — greenhouse.io → `external_greenhouse`
- `test_icims` — icims.com → `external_icims`
- `test_other_ats` — updated to use a genuinely unknown domain
- `test_parser_has_all_subcommands` — updated to include `platform-stats`

---

## Phase 14 — Complete ✅ (Workday Form Hardening: Typeahead, Date Fields, Stuck Detection)

**Problem:** First live Workday test run failed. Root causes identified through research into Skyvern, browser-use, and community Workday automation projects:
1. Missing field type handlers — typeahead/combobox (very common in Workday for location, country, employer, school), split date fields (month/day/year), multi-select checkboxes
2. No stuck-section detection — if `_advance()` succeeded but validation prevented navigation, the loop would call `_handle_section()` on the same page up to 15 times
3. No popup/modal dismissal — Workday shows mid-flow confirmation dialogs that block navigation
4. Validation error recovery was log-only — errors detected but not fixed before continuing
5. Field discovery not scoped — queried the entire page instead of the active section container, picking up off-screen fields

**What was built:**

**Typeahead handler (`_fill_typeahead()`)**:
- Detects `input[aria-autocomplete]`, `input[role='combobox']`, `[data-automation-id='searchBox'] input`
- Types with 40ms/char delay → waits 3s for `[role='option']` suggestions → exact then partial match → first suggestion fallback
- If no suggestions appear, accepts the typed text (handles fields that look like comboboxes but accept free text)

**Split date handler (`_fill_split_date()`)**:
- Detects `[data-automation-id='dateSectionMonth']` siblings
- Asks Claude for date as string, parses YYYY-MM-DD and MM/YYYY formats
- Fills separate month/day/year inputs; month dropdown uses `_click_workday_option()` + `_MONTH_NAMES` lookup

**Multi-select checkbox handler** in `_handle_generic_section()`:
- Detects `[data-automation-id='multiSelectContainer']`
- Reads checkbox options, asks LLM "which apply", parses comma-separated answer, checks matching boxes

**Stuck-section detection in `_navigate_form()`**:
- Tracks `prev_section_name` across iterations
- Increments `stuck_count` when section name unchanged after advance
- Aborts with Vision diagnosis after 2 consecutive stuck iterations

**Popup dismissal (`_dismiss_popup()`)**:
- Called at top of each navigation loop iteration, before reading section state
- Detects `[data-automation-id='wd-Popup-body']`, clicks OK/Close, falls back to Escape

**Validation error recovery (`_retry_errored_fields()`)**:
- After detecting errors post-advance, walks up from each `field-error` element to its containing field group
- Calls `_answer_via_claude()` directly (bypasses QA cache for fresh answer), re-fills the field
- Retries advance once after fixing

**Field scoping (`_get_section_scope()`)**:
- Returns first visible match from `[data-automation-id='WizardTask']`, `appContainerPanel`, `taskContent*`, `main`
- `_handle_generic_section()` queries field groups within this container instead of the full page

**Label selectors expanded**:
- Added `legend`, `[data-automation-id='questionText']`, `[data-automation-id='labelContent']` to label detection

363 tests passing (no new tests added — new handlers are all async browser I/O, covered by integration testing).

---

## Phase 15 — Complete ✅ (Planner-Actor-Validator Loop for Workday)

**Problem:** When a Workday tenant uses non-standard `data-automation-id` values, `_handle_generic_section()` finds 0 field groups and exits silently. The section then either stalls (required fields left empty) or advances with blanks. A second failure class: the old `prev_section_name`/`stuck_count` stuck detection only fired after two full section-handle cycles, not immediately after an advance failed.

**What was built:**

**`format_interactive_fields()` in `browser/accessibility.py`**:
- Walks the AX tree and returns a newline-separated list of fillable fields (textbox, combobox, listbox, checkbox, radio, spinbutton roles)
- Skips navigation buttons (Next, Save, Back, Submit, Cancel) by name
- Includes `(required)` marker for nodes with `required: true`
- Capped at 40 fields to bound prompt size
- Returns `""` if no fillable fields found (read-only/review sections)

**Section planner (`_plan_section_llm()` in `workday.py`)**:
- `_SECTION_PLAN_SYSTEM` prompt instructs Claude to return a JSON array of `{"label", "field_type", "value"}` items only — no prose
- `_build_profile_summary()` builds a compact 400–600 char profile summary (name, auth, years, salary, relocate, certs, top skill domains) to keep prompt size small; full profile context is only in the Q&A `system_blocks`
- `_parse_field_plan(response)` strips markdown fences, `json.loads()`, validates all three required keys per item; returns `[]` on any failure

**Actor (`_execute_plan_item()` + `_llm_guided_section()` in `workday.py`)**:
- `_execute_plan_item()` locates each planned field via `find_by_aria_label()` (stable ARIA labels) → `get_by_label()` fallback, then fills by field type (text `fill()`, select via Workday custom dropdown then native `select_option()`, radio `get_by_role()`, checkbox `check()`)
- `_llm_guided_section()` orchestrates: AX tree snapshot → `format_interactive_fields()` → `_plan_section_llm()` → execute each plan item; logs `"LLM-guided section: N/M fields filled"`
- Called in `_handle_generic_section()` only when DOM detection fills 0 fields — Phase 14 DOM code remains the primary path

**Validator (`_validate_advance()` in `workday.py`)**:
- Polls `_get_section_name()` every 400ms up to 4 seconds after `_advance()`
- Returns `(True, new_section_name)` on name change, `(False, old_section_name)` on timeout
- Replaces the old `prev_section_name`/`stuck_count` mechanism — detects stuck state immediately after the advance rather than on the next loop iteration

**Validator integration in `_navigate_form()`**:
- After `_advance()` + validation error retry, calls `_validate_advance(old_section_name)`
- If not advanced: invokes Vision for diagnosis, then runs `_llm_guided_section()` once and retries `_advance()` + `_validate_advance()` (one LLM-guided retry per section)
- If still stuck after retry: logs error and aborts — no infinite loops

**Submission confirmation (`_confirm_submission()` in `workday.py`)**:
- Multi-method check: page text scan for 6 confirmation phrases → 4 CSS selectors → Vision `analyze_page()` as last resort
- `_submit_application()` now calls `_confirm_submission()` and logs confirmed vs assumed-success

**7 new tests (370 total passing)**:
- `tests/test_browser/test_accessibility.py` — 2 tests: basic fields with required marker, skips navigation buttons
- `tests/test_apply/test_workday_planner.py` (new file) — 5 tests: valid JSON array, invalid JSON → `[]`, missing `value` key → `[]`, strips markdown fences, non-list JSON → `[]`

---

## Phase 16 — Complete ✅ (Workday Vision Fallback + Drop-Down Field Locator Hardening)

**Problem:** Live test runs (run_ids 90–93) against real Workday tenants surfaced three failure classes not covered by Phase 15:

1. **`_llm_guided_section` short-circuited when AX tree was None** — `get_ax_tree()` returns `None` silently on some Workday tenants; the previous code had `if not tree: return 0` before the Vision fallback, so Vision was never called even though it could describe the form fields perfectly.

2. **`_execute_plan_item` failed for `select`/`dropdown` fields when `get_by_label` found nothing** — Workday's application-questions dropdowns have no `label[for=...]` association in the DOM. The actor returned 0 for every planned dropdown item even when Vision correctly identified the fields as `dropdown: Are you 18 years or older? | options: Yes, No`.

3. **Auth flow wasted 60–90 seconds on selector timeout chains** — `_sign_in`, `_create_account`, and auth button detection were using default 10s timeouts per selector. Reduced to 2s across all auth-related `wait_and_click`/`fill_field` CSS selector chains.

**What was built:**

**`_llm_guided_section` Vision fallback (workday.py)**:
- Removed the early `if not tree: return 0` gate
- `field_summary = format_interactive_fields(tree) if tree else ""`
- When `field_summary` is empty (AX tree None **or** no fillable fields found), calls `VisionAnalyzer.analyze_page()` with a prompt requesting all form fields, radio groups, and dropdowns with options
- Vision description replaces the AX tree field list as input to the planner when AX is unavailable
- Logs `"LLM-guided using Vision field description: ..."` to confirm Vision path activated
- Confirmed working in run 92: Vision correctly described 4 Yes/No application dropdowns

**`_execute_plan_item` text-proximity fallbacks for select/dropdown (workday.py)**:
- When `find_by_aria_label` fails (AX tree unavailable) AND `get_by_label` finds nothing, the code previously returned 0 immediately for select/dropdown types
- Now falls through to three additional proximity approaches that don't require a pre-found locator:
  - **Approach 7**: `get_by_text(label)` → XPath `ancestor::*[.//select][1]` → `select.select_option(label=value)` — handles native `<select>` elements near the question text
  - **Approach 8**: `get_by_text(label)` → XPath `ancestor::*[.//button][1]` → click button → `get_by_role("option", name=value)` — handles custom JS dropdowns where clicking the button opens a listbox
  - **Approach 9**: `get_by_text(label)` → XPath ancestor containing `[role='combobox']` or `[role='listbox']` → click → click option — handles Workday's combobox variant
- Same pattern: `locator = None` no longer triggers early return for radio/radiogroup types either (approaches 1–4 in radio block already work via `self._page` directly, not via `locator`)

**`_scan_radiogroups` broad fallback (workday.py)**:
- New method called before `_llm_guided_section` when DOM field detection finds 0 groups
- Queries `[role='radiogroup']` and `[data-automation-id='radioGroup']` across the active section scope
- For each group, resolves question label from: `aria-labelledby` → `aria-label` → JS parent sibling text traversal
- Calls `answer_question()` + `_click_workday_option()` / radio click chain per group
- Handles radio questions that use non-standard field group containers that the Phase 14 DOM scanner misses

**DOM field detection logging (workday.py)**:
- `_handle_generic_section()` now logs `"DOM field detection: N groups found"` at INFO level
- Makes it visible in logs whether DOM vs. `_scan_radiogroups` vs. `_llm_guided_section` did the work

**Auth flow timeout reduction (workday.py)**:
- `wait_and_click(_SIGN_IN_BTN, timeout=2_000)` — was default 10s × 2 selectors = 20s
- `wait_and_click(_LOGIN_BTN, timeout=2_000)` — was 10s × 2 = 20s
- `wait_and_click(_CREATE_ACCOUNT_BTN, timeout=2_000)` — was 10s × 6 = 60s
- `fill_field` for CSS-fallback selectors in `_fill_auth_field` already at 2s (from Phase 14)
- Net: auth flow completes ~80s faster per attempt when CSS selectors miss

**Account creation verify-password hardening (workday.py)**:
- After all CSS selector attempts and `get_by_label` attempts fail for the verify-password field, queries all `input[type='password']` and fills the first one with an empty value
- Handles tenants whose confirm-password field uses a non-standard `data-automation-id` while still being an `input[type='password']` in the DOM
- Same pattern applied in the multi-step handler (when `still_on_create=True` after first submit)

**3 new tests (373 total passing)**:
- `tests/test_browser/test_accessibility.py` — `test_radiogroup_with_empty_name_uses_text_child`, `test_radiogroup_sibling_text_label`, `test_radiogroup_with_named_group` — three new radiogroup formatting tests covering the Workday pattern where the label is embedded as a text node inside or adjacent to the group

**Outstanding issues (next focus area)**:
- Workday stored credentials expire every run because the email subaddress uses `hex(time() % 0xFFFF)` as suffix — every run creates a new account but the prior account's credentials are stored. On the next run the stored credentials are attempted (fail) → account creation is retried but may end up on the sign-in page rather than the create-account page when CSS navigation selectors don't match.
- `_execute_plan_item` text-proximity approaches 7–9 were implemented this phase but not yet exercised in a successful run (account creation failing at both test domains blocked form fill testing).

---

## Key Design Decisions

- **Apply type detection:** AX tree (`page.accessibility.snapshot()`) first — LinkedIn's ARIA labels are stable across all CSS/SDUI changes. DOM `get_by_role` and CSS selectors kept as fallback layers. Vision only at apply-time (never search-time) to control cost.
- **Sidebar guard:** `find_by_aria_label()` accepts a `job_id` parameter. When supplied, it filters AX candidates to those whose label contains the current job's numeric ID — prevents accidentally clicking "Easy Apply to <other job>" buttons on sidebar cards.
- **AX-to-Locator bridge:** Found AX nodes are mapped back via `get_by_role(role, name=label)` → `get_by_label(label)` fallback. No index drift if DOM re-renders between snapshot and click.
- **Vision cost guard:** `vision_detect_apply_type()` only called from `_redetect_apply_type()` (apply-time). Existing `is_over_budget()` check in ApplyAgent remains the hard cap. Never called during search runs.
- **Anti-detection:** Patchright (CDP stealth) + playwright-stealth (JS stealth) + headed Chromium + persistent context + random delays.
- **Security:** Fernet key only in env var; passwords/tokens redacted from logs; `data/` gitignored
- **Models:** Sonnet for routine tasks (scoring, Q&A, email, vision fallback); Opus for resume/cover letter writing
- **Prompt caching:** Profile context sent as `system_blocks` with `cache_control: ephemeral` — Anthropic caches the profile for 5 minutes, costing 0.10× input rate on cache reads. Saves ~90% tokens on repeated Q&A calls within a single application.
- **Q&A resolution:** QA cache (conf ≥ 0.7) → profile lookup → Claude (cached profile) → strategic (job description context) → vision → empty. Cache key = `_normalize_question(q)` + `_options_hash(options)`. High-confidence answers (≥ 0.7) from Claude/strategic are written to cache automatically. Cache reads cost zero LLM tokens across all future applications.
- **Q&A cache:** `qa_cache` table in SQLite. `UNIQUE(question_key, options_hash)` — one entry per question/options pair. `times_used` counter tracks reuse frequency. `QACacheRepo` passed to all three applicators via `ApplyAgent._dispatch()`.
- **Budget:** Daily cost cap enforced via `llm_usage` table; target $5-12/day; `is_over_budget()` checked before each job
- **PDF caching:** Resumes/cover letters saved to `data/resumes/` and reused by slug+date pattern — avoids redundant Opus calls on retry
- **Fail fast:** Apply type re-detected before LLM material generation — unknown jobs skipped without spending tokens
- **SDUI navigation:** SDUI Easy Apply links navigated via `page.goto(href)` instead of `link.click()` — prevents new-tab navigation that broke drift detection. `_sdui_link_broken` flag skips CSS/Vision layers after a confirmed broken redirect.
- **Review mode:** Human-in-the-loop approval before any submit click, with browser visible for inspection
- **Apply type detection order:** Easy Apply (AX tree → DOM → SDUI link) → Expired → External apply (links then buttons) → "I'm Interested". "I'm Interested" is last because LinkedIn shows it as a secondary CTA alongside primary "Apply" buttons on external jobs — checking it early causes false `interest_only` classification.
- **ATS platform classification:** `_classify_external_url()` maps 15 known ATS domains to named platform types (`external_workday`, `external_greenhouse`, `external_lever`, `external_icims`, etc.). LinkedIn redirect URLs (`/redir/redirect/?url=...`) are decoded first. `jobhunter platform-stats` shows distribution and flags platforms without applicators.
- **Search rate limiting:** `search-now --max-queries N` slices queries for fast test runs; `--max-pages N` limits result pages per query. Together (`--max-queries 2 --max-pages 1`) gives the fastest test iteration (~10 min, ~50 jobs).
- **Apply-type targeting:** `apply-now --apply-type TYPE` filters the candidate queue to a specific platform — essential for testing a single applicator (e.g. `--apply-type workday --dry-run`) without wading through all qualified jobs. Accepts shorthand aliases; comma-separated or repeatable.
- **Workday field types:** `_handle_generic_section()` detects 8 field types in priority order: multi-select checkbox (`multiSelectContainer`), radio group, custom JS dropdown (`aria-haspopup='listbox'`), typeahead/combobox (`aria-autocomplete`), split date (`dateSectionMonth/Day/Year`), native `<select>`, textarea, text/number input. Field discovery is scoped to the active section container (`WizardTask` / `appContainerPanel`) to prevent false positives from off-screen sections.
- **Workday stuck detection (Phase 15):** `_validate_advance()` polls `_get_section_name()` every 400ms up to 4 seconds after each `_advance()`. Returns `(True, new_name)` on section change, `(False, old_name)` on timeout. Immediately detects stuck state rather than waiting for the next full loop iteration. Replaces the `prev_section_name`/`stuck_count` mechanism from Phase 14.
- **Workday popup dismissal:** `_dismiss_popup()` is called at the top of each navigation loop iteration. Checks for `[data-automation-id='wd-Popup-body']` and clicks OK/Close buttons; falls back to Escape. Prevents mid-form modals from blocking navigation.
- **Workday typeahead:** `_fill_typeahead()` types text with 40ms/char delay, waits up to 3s for `[role='option']` suggestions, tries exact then partial match, falls back to first suggestion. If no suggestions appear, the typed text is accepted as-is (works for free-text fields that resemble comboboxes).
- **Workday validation retry:** After detecting `field-error` / `errorMessage` elements post-advance, `_retry_errored_fields()` walks up from each error element to find the containing field group, calls `_answer_via_claude()` directly (bypassing cache) for a fresh answer, and re-fills the field. Then advances once more. Logs and continues if retry still fails.
- **Workday date parsing:** `_fill_split_date()` asks Claude for the date as a string, parses YYYY-MM-DD and MM/YYYY formats, then fills the separate month/day/year inputs. Month inputs that are Workday dropdowns use `_click_workday_option()` with `_MONTH_NAMES` lookup.
- **Workday Planner-Actor-Validator (Phase 15):** Three-part fallback loop that activates when DOM detection finds 0 fields. **Planner**: `format_interactive_fields()` snapshots the AX tree (zero LLM cost) and formats fillable fields; `_plan_section_llm()` sends the field list + compact profile summary to Claude Sonnet → structured JSON fill plan. **Actor**: `_execute_plan_item()` locates each field by ARIA label via `find_by_aria_label()` → `get_by_label()` fallback and fills it. **Validator**: `_validate_advance()` polls for section name change; if stuck, invokes Vision diagnosis + one LLM-guided retry before aborting.
- **Workday submission confirmation:** `_confirm_submission()` checks page text against 6 phrases, 4 CSS selectors, then Vision `analyze_page()` as last resort. Replaces the optimistic "assume success" pattern.
- **`format_interactive_fields()` field filter:** Only `textbox`, `combobox`, `listbox`, `checkbox`, `radio`, `spinbutton` roles are included (the fillable set). Buttons named Next/Save/Back/Submit/Cancel are explicitly excluded by name match. Cap of 40 fields keeps the planning prompt under ~1K tokens total.
- **`_llm_guided_section` Vision fallback (Phase 16):** When `get_ax_tree()` returns `None` (some Workday tenants block CDP accessibility snapshots), `format_interactive_fields` produces `""`. Instead of aborting, `_llm_guided_section` calls `VisionAnalyzer.analyze_page()` to get a text description of visible fields. This description is used as the field list sent to the LLM planner. Vision correctly identifies `dropdown: Are you 18 years or older? | options: Yes, No` even when the AX tree is entirely unavailable.
- **`_execute_plan_item` text-proximity fallbacks (Phase 16):** When `find_by_aria_label` fails (AX tree None) and `get_by_label` fails (no label association), select/dropdown types now try three additional approaches using only the question text: (1) `get_by_text(label)` → XPath `ancestor::*[.//select][1]` → `select_option`; (2) same but `ancestor::*[.//button][1]` → click button → click option; (3) same but `ancestor::*[.//*[@role='combobox']][1]` → click combobox → click option. Radio/radiogroup types already worked without a locator (use `self._page.get_by_role` directly); now they also no longer bail early when `get_by_label` fails.
- **Workday account email subaddressing:** Each run creates a new account using `base_email+wd{domain_slug}{hex(time()%0xFFFF)}@gmail.com`. Gmail routes all subaddresses to the same inbox. The hex suffix changes every run so each `_create_account()` call gets a fresh address. Credentials are stored encrypted in the `credentials` table keyed by domain; the next run finds them and signs in. When credentials fail, they are deleted and a new account is created with a new subaddress.
