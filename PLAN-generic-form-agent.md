# Plan: Universal Form-Filling Agent

## Problem Statement

The current architecture has platform-specific applicators with hand-coded selectors per ATS.
After 16 phases + Codex iterations, `workday.py` has grown to **4,089 lines / 80 methods** and
still can't reliably complete applications across 3 test tenants. The self-identify section alone
has 6 specialized retry methods. Meanwhile 14 other ATS platforms have zero applicator code.

This is a combinatorial trap: `platforms × tenant variations × form layouts × field types`
grows faster than anyone can write handlers.

## Key Insight

The solution already exists — it's buried as a fallback. The Phase 15 **Planner-Actor-Validator**
loop and the **GenericApplicator** both demonstrate platform-agnostic form filling that works on
any page. They just need to be promoted from "last resort" to "the way everything works."

## What Changes

### Before (Current)
```
ApplyAgent._dispatch()
  ├── LinkedInEasyApplicator  (784 lines, LinkedIn-specific selectors)
  ├── WorkdayApplicator       (4089 lines, 80 methods, 3 tenants still broken)
  └── GenericApplicator       (298 lines, Vision-heavy, works on anything)
```

### After (Proposed)
```
ApplyAgent._dispatch()
  ├── LinkedInEasyApplicator  (keep — Easy Apply modal is simple & stable enough)
  └── FormFillingAgent        (new — universal agent for ALL external ATS)
        Uses: AX tree → LLM planner → actor → validator → Vision fallback
        Auth: pluggable per-domain credential strategy
```

## What to Keep (Unchanged)

These modules are already platform-agnostic and battle-tested:

| Module | Why Keep |
|--------|----------|
| `applicators/base.py` (625 lines) | Q&A pipeline, profile caching, QA cache, review mode — 100% reusable |
| `browser/accessibility.py` (278 lines) | AX tree helpers, `format_interactive_fields()` — 100% reusable |
| `browser/vision.py` (205 lines) | VisionAnalyzer — 100% reusable |
| `browser/helpers.py` | `wait_and_click`, `fill_field`, etc. — 100% reusable |
| `browser/stealth.py` | Anti-detection — 100% reusable |
| `browser/context.py` | Browser session — 100% reusable |
| `llm/client.py` (194 lines) | Claude client with prompt caching — 100% reusable |
| `agents/search_agent.py` | Job discovery — unchanged |
| `agents/email_agent.py` | Email monitoring — unchanged |
| `llm/resume.py`, `llm/cover_letter.py` | PDF generation — unchanged |
| `db/*` | All tables including `qa_cache`, `workday_tenants` — unchanged |
| `crypto/vault.py` | Credential storage — unchanged |
| `applicators/linkedin_easy.py` | Keep as-is — Easy Apply is a contained modal, not a full ATS |

## What to Build: `FormFillingAgent`

One new file: `applicators/form_agent.py` (~400-500 lines)

### Core Loop

```python
class FormFillingAgent(BaseApplicator):
    """Universal form-filling agent for any ATS platform."""

    async def apply(self, job, application) -> bool:
        # 1. Navigate to external URL
        await self._page.goto(job.external_url, wait_until="domcontentloaded")

        # 2. Handle auth if needed (pluggable strategy)
        if not await self._handle_auth_if_needed(job):
            return False

        # 3. Click the Apply button (generic detection)
        if not await self._find_and_click_apply():
            return False

        # 4. Main form loop
        for step in range(MAX_STEPS):
            state = await self._assess_current_state()

            if state == "submitted":
                return await self._confirm_submission()
            if state in ("captcha", "login_required"):
                return False
            if state == "file_upload":
                await self._upload_resume()
            elif state == "form":
                await self._fill_current_page()
                await self._advance_or_submit()

        return False
```

### `_fill_current_page()` — The Heart

This merges the best of GenericApplicator + Workday's Planner-Actor-Validator:

```python
async def _fill_current_page(self) -> int:
    """Fill all fields on the current form page."""

    # Layer 1: AX tree snapshot (free, fast)
    tree = await get_ax_tree(self._page)
    field_summary = format_interactive_fields(tree) if tree else ""

    # Layer 2: Vision fallback if AX tree unavailable
    if not field_summary and self._vision:
        field_summary = await self._vision.analyze_page(
            self._page, "List every form field, radio group, and dropdown...")

    if not field_summary:
        return 0  # Read-only page, nothing to fill

    # Layer 3: LLM planner — ask Claude what to fill
    plan = await self._plan_fields(field_summary)

    # Layer 4: Execute plan items
    filled = 0
    for item in plan:
        filled += await self._fill_field(item["label"], item["type"], item["value"])

    return filled
```

### `_fill_field()` — Multi-Strategy Field Location + Fill

Extracted and generalized from `_execute_plan_item()`:

```
Strategy 1: find_by_aria_label() — AX tree → Locator mapping (most reliable)
Strategy 2: page.get_by_label() — HTML label association
Strategy 3: page.get_by_text(label) → ancestor with input/select/button — text proximity
Strategy 4: Vision coordinate click — last resort for truly unusual DOMs
```

For each located field, fill by detected type:
- **text/textarea**: `fill()` (triggers React onChange)
- **select**: `select_option(label=value)`, fallback to custom dropdown click
- **radio**: `get_by_role("radio", name=value).check()`
- **checkbox**: `.check()` / `.uncheck()`
- **combobox/typeahead**: `type(value, delay=40)` → wait for suggestions → click match
- **file**: `set_input_files()`

### `_assess_current_state()` — Merged from GenericApplicator

Uses Vision to classify the page state. Returns one of:
`form | file_upload | submitted | login_required | captcha | error | review`

### `_advance_or_submit()` — Generic Navigation

```python
async def _advance_or_submit(self) -> bool:
    # 1. Look for explicit Next/Continue/Submit buttons by role
    for name in ["Next", "Continue", "Submit", "Save and Continue", "Apply"]:
        btn = self._page.get_by_role("button", name=re.compile(name, re.I))
        if await btn.count() > 0 and await btn.first.is_visible():
            if "submit" in name.lower() or "apply" in name.lower():
                if not await self._pause_for_review(...):
                    return False
            await btn.first.click()
            return True

    # 2. AX tree search for button-role elements
    tree = await get_ax_tree(self._page)
    nav_buttons = search_ax_tree(tree, role="button",
                                  label_pattern=r"(?i)next|continue|submit|apply")
    ...

    # 3. Vision fallback
    ...
```

### `_handle_auth_if_needed()` — Pluggable Auth

Instead of 1700 lines of Workday auth code, use a simple strategy:

```python
async def _handle_auth_if_needed(self, job) -> bool:
    state = await self._assess_current_state()
    if state != "login_required":
        return True  # No auth needed

    domain = extract_domain(job.external_url)

    # Try stored credentials
    cred = self._cred_repo.get_by_domain(domain)
    if cred:
        if await self._try_login_with_credentials(cred):
            return True

    # Try account creation (using profile + subaddressed email)
    if await self._try_create_account(domain):
        return True

    # Mark as needs manual auth
    self.logger.warning(f"Auth wall at {domain} — marking needs_review")
    return False
```

The login/account-creation methods use the same AX tree + Vision approach
as form filling — find email/password fields by label, fill them. No
platform-specific selectors needed.

### `_validate_advance()` — Reused from Phase 15

Poll for observable state change (URL, page title, visible section header,
AX tree structure) to detect stuck state immediately.

## What Gets Deleted

| File/Code | Lines | Replacement |
|-----------|-------|-------------|
| `workday.py` (entire file) | 4,089 | `FormFillingAgent` handles all external ATS |
| `generic.py` (entire file) | 298 | Merged into `FormFillingAgent` |
| `workday_tenants` table | — | Keep for now (auth mode hints), but no longer drives code paths |
| `_click_workday_option` | 94 | Generic dropdown handler in `_fill_field()` |
| `_fill_typeahead` | 54 | Generic combobox handler in `_fill_field()` |
| `_fill_split_date` | 60 | LLM answers the question; generic text fill |
| `_fill_self_identify_*` (6 methods) | ~580 | LLM reads the section like a human |
| `_create_account` | ~240 | Generic auth with AX tree field detection |
| `_sign_in` | ~90 | Generic auth with AX tree field detection |
| All Workday selector constants | ~170 | Zero platform-specific selectors |

**Net: ~4,400 lines deleted, ~500 lines added.**

## Why This Works

1. **The LLM sees what a human sees.** Workday's self-identify section has unstable selectors
   but stable *visible labels*. "Name", "Date", "Please select one of the following" are always
   readable by Vision and present in the AX tree, even when `data-automation-id` values change
   between tenants.

2. **AX tree is free.** `page.accessibility.snapshot()` costs zero LLM tokens and returns
   structured field information. It's the sweet spot between DOM parsing (brittle) and Vision
   (expensive).

3. **The Q&A pipeline is already universal.** `answer_question()` with its 5-layer resolution
   (cache → profile → Claude → strategic → Vision) works identically across all platforms.
   Nothing about it is Workday-specific.

4. **One agent, all 15 platforms.** Instead of building applicators for Greenhouse, Lever, iCIMS,
   Taleo, etc., `FormFillingAgent` handles them all from day one. The only per-platform code
   that *might* be needed is auth hints in the `workday_tenants` table (or a renamed
   `ats_auth_hints` table).

5. **GenericApplicator already proves it.** The existing 298-line `generic.py` already fills
   arbitrary forms using Vision + label scanning. It just needs the AX tree planner added
   to make it efficient (fewer Vision calls) and the field-filling strategies from
   `_execute_plan_item()` to make it reliable.

## Cost Analysis

| Approach | LLM calls per application | Estimated cost |
|----------|--------------------------|----------------|
| Current Workday (Phase 16) | 3-8 Q&A + 1-2 planning + 0-2 Vision | $0.15-0.40 |
| Proposed FormFillingAgent | 1 state assessment + 1-3 planning + 3-8 Q&A + 0-1 Vision | $0.20-0.50 |
| Difference | ~$0.05-0.10 more per app | Trivially more |

The marginal cost increase is negligible compared to the engineering time saved by not
maintaining 4,000 lines of platform-specific code per ATS.

## Migration Steps

1. **Create `applicators/form_agent.py`** — new FormFillingAgent class
2. **Extract reusable methods** from `workday.py`:
   - `_plan_section_llm()` → `_plan_fields()`
   - `_execute_plan_item()` → `_fill_field()` (generalized, no Workday selectors)
   - `_validate_advance()` → reused as-is
   - `_confirm_submission()` → reused as-is
3. **Merge GenericApplicator** patterns:
   - `_assess_page()` → `_assess_current_state()`
   - `_fill_visible_fields()` → absorbed into `_fill_current_page()` Layer 3 fallback
   - `_attempt_advance()` → `_advance_or_submit()`
4. **Build generic auth handler** — AX tree + Vision approach to find email/password fields
5. **Update `apply_agent.py` dispatch** — route all `external_*` types to FormFillingAgent
6. **Delete `workday.py` and `generic.py`**
7. **Test against the same 3 Workday tenants** + at least 1 Greenhouse + 1 Lever job
8. **Update DESIGN.md and CLAUDE.md**

## Risk Mitigation

- **LinkedIn Easy Apply stays untouched** — it already works and is a contained modal
- **Q&A cache carries over** — all previously answered questions still cached
- **Credentials carry over** — `credentials` table unchanged
- **Rollback is trivial** — `workday.py` and `generic.py` are in git history
- **Incremental testing** — can run FormFillingAgent alongside existing applicators using
  `--apply-type` flag before cutting over
