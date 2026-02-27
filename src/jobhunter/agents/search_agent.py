"""Search Agent — LinkedIn job discovery, parsing, scoring, and storage."""

import json
import urllib.parse
from typing import Optional

from patchright.async_api import Page, TimeoutError as PlaywrightTimeout

from ..browser.accessibility import find_by_aria_label
from ..browser.context import BrowserSession
from ..browser.helpers import is_visible, scroll_to_bottom, wait_and_click
from ..browser.stealth import random_delay
from ..db.models import Job
from ..db.repository import JobRepo
from ..llm.client import ClaudeClient
from ..llm.prompts import job_scoring_prompt, job_scoring_system
from ..utils.logging import get_logger
from ..utils.profile_loader import UserProfile
from ..utils.rate_limiter import RateLimiter
from .base import AgentError, AgentResult, BaseAgent, RetryableError

logger = get_logger(__name__)

# ── LinkedIn search URL builder ────────────────────────────────────────────────

_SEARCH_BASE = "https://www.linkedin.com/jobs/search/?"

# Selectors for job search results — multiple fallbacks per target
_JOB_CARD_SELECTORS = [
    "li.jobs-search-results__list-item",
    "li.scaffold-layout__list-item",
    "div.job-card-container",
]
_JOB_CARD_LINK_SELECTORS = [
    "a.job-card-list__title",
    "a.job-card-container__link",
    "a[data-control-name='jobdetails_topcard_inapply']",
]
_JOB_TITLE_SELECTORS = [
    # Current LinkedIn unified top-card (2024-2026)
    "h1.job-details-jobs-unified-top-card__job-title",
    "h1[class*='job-title']",
    "h1[class*='jobs-unified-top-card']",
    ".job-details-jobs-unified-top-card__job-title",
    # Older layouts
    "h1.t-24",
    "h1.t-24.t-bold",
    "h2.jobs-unified-top-card__job-title",
    # Generic fallback — any h1 on the job detail page
    "main h1",
    "h1",
]
_JOB_COMPANY_SELECTORS = [
    # Current LinkedIn unified top-card — link form
    "div.job-details-jobs-unified-top-card__company-name a",
    ".job-details-jobs-unified-top-card__company-name a",
    "a[data-tracking-control-name*='topcard-org-name']",
    "a[data-tracking-control-name*='company']",
    # Current LinkedIn unified top-card — span/div (anonymous postings omit the link)
    "div.job-details-jobs-unified-top-card__company-name",
    ".job-details-jobs-unified-top-card__company-name",
    # Older layouts
    ".jobs-unified-top-card__company-name a",
    ".jobs-unified-top-card__company-name",
    "a.ember-view.t-black.t-normal",
    "span.jobs-unified-top-card__company-name a",
    "span.jobs-unified-top-card__company-name",
    # App-aware links (React SPA variant)
    "a.app-aware-link[href*='/company/']",
    # Attribute-based (most stable across redesigns)
    "a[href*='/company/']",
    # Entity URN attribute (data layer)
    "[data-entity-urn*='company']",
]
_JOB_LOCATION_SELECTORS = [
    # Current layout
    "div.job-details-jobs-unified-top-card__primary-description-container span.tvm__text",
    "div.job-details-jobs-unified-top-card__primary-description-container span",
    ".job-details-jobs-unified-top-card__primary-description-without-tagline span",
    # Older layouts
    "span.jobs-unified-top-card__bullet",
    "span.tvm__text.tvm__text--low-emphasis",
]
_JOB_DESC_SELECTORS = [
    "div.jobs-description__content",
    "div.jobs-description-content__text",
    "div#job-details",
    "article.jobs-description__container",
    # Attribute-based fallback
    "[class*='jobs-description']",
]
_EASY_APPLY_SELECTORS = [
    "button.jobs-apply-button span",
    "div.jobs-apply-button--top-card span",
]
_NEXT_PAGE_SELECTORS = [
    "button[aria-label='View next page']",
    "li.artdeco-pagination__indicator--number.selected + li button",
]


def build_search_url(
    keywords: str,
    location: str = "United States",
    work_types: Optional[list[int]] = None,
    experience_levels: Optional[list[int]] = None,
    job_types: Optional[list[str]] = None,
    date_posted: str = "r604800",
    sort_by: str = "DD",
    easy_apply_only: bool = False,
    start: int = 0,
) -> str:
    """Construct a LinkedIn job search URL from parameters."""
    params: dict[str, str] = {
        "keywords": keywords,
        "location": location,
        "f_TPR": date_posted,
        "sortBy": sort_by,
        "start": str(start),
    }
    if work_types:
        params["f_WT"] = ",".join(str(w) for w in work_types)
    if experience_levels:
        params["f_E"] = ",".join(str(e) for e in experience_levels)
    if job_types:
        params["f_JT"] = ",".join(job_types)
    if easy_apply_only:
        params["f_AL"] = "true"

    return _SEARCH_BASE + urllib.parse.urlencode(params)


# ── Job card / detail extraction ───────────────────────────────────────────────

async def extract_job_ids_from_page(page: Page) -> list[str]:
    """Extract LinkedIn job IDs from the current search results page."""
    job_ids = []
    try:
        # Job IDs are stored in data attributes or can be parsed from URLs
        cards = await page.query_selector_all(
            "li.jobs-search-results__list-item, li.scaffold-layout__list-item"
        )
        for card in cards:
            # Try data-occludable-job-id first, then data-job-id
            jid = await card.get_attribute("data-occludable-job-id")
            if not jid:
                jid = await card.get_attribute("data-job-id")
            if not jid:
                # Parse from any anchor href containing /jobs/view/<id>/
                anchors = await card.query_selector_all("a[href*='/jobs/view/']")
                for a in anchors:
                    href = await a.get_attribute("href") or ""
                    jid = _parse_job_id_from_url(href)
                    if jid:
                        break
            if jid:
                job_ids.append(str(jid).strip())
    except Exception as e:
        logger.warning(f"Error extracting job IDs: {e}")
    return list(dict.fromkeys(job_ids))  # preserve order, deduplicate


def _parse_job_id_from_url(url: str) -> Optional[str]:
    """Extract numeric job ID from a LinkedIn job URL."""
    import re
    match = re.search(r"/jobs/view/(\d+)", url)
    return match.group(1) if match else None


async def navigate_to_job(page: Page, job_id: str) -> bool:
    """Navigate directly to a job detail page by ID."""
    url = f"https://www.linkedin.com/jobs/view/{job_id}/"
    try:
        # Use "load" as primary — LinkedIn's SPA has indefinite background XHR that
        # prevents networkidle from ever firing, causing 25s timeouts per job.
        await page.goto(url, wait_until="load", timeout=20_000)

        # Attempt a brief networkidle bonus (5s) to let React finish hydrating.
        # LinkedIn's long-polling XHR will time this out most of the time — that's fine.
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightTimeout:
            pass  # Expected for LinkedIn SPA pages — proceed with extraction

        await random_delay(1.0, 2.0)
        return True
    except PlaywrightTimeout:
        logger.warning(f"Timeout navigating to job {job_id}")
        return False
    except Exception as e:
        if "closed" in str(e).lower() or "target" in str(e).lower():
            raise  # Re-raise so caller can handle browser closure
        logger.warning(f"Navigation error for job {job_id}: {e}")
        return False


async def extract_job_details(page: Page, job_id: str) -> Optional[dict]:
    """
    Extract structured job data from the current job detail page.
    Returns None if extraction fails.
    """
    try:
        # Title
        title = None
        for sel in _JOB_TITLE_SELECTORS:
            el = page.locator(sel).first
            if await el.is_visible():
                title = (await el.inner_text()).strip()
                break

        # Company
        company = None
        for sel in _JOB_COMPANY_SELECTORS:
            el = page.locator(sel).first
            if await el.is_visible():
                company = (await el.inner_text()).strip()
                break

        # Location
        location = None
        for sel in _JOB_LOCATION_SELECTORS:
            el = page.locator(sel).first
            if await el.is_visible():
                location = (await el.inner_text()).strip()
                break

        # Description
        description = None
        for sel in _JOB_DESC_SELECTORS:
            el = page.locator(sel).first
            if await el.is_visible():
                description = (await el.inner_text()).strip()
                break

        # JS fallback — LinkedIn embeds Open Graph / structured data on every job page
        if not title or not company:
            try:
                extracted = await page.evaluate("""() => {
                    // 1. JSON-LD structured data (most reliable — full JobPosting schema)
                    let ld_title = '', ld_company = '';
                    const ldScripts = document.querySelectorAll('script[type="application/ld+json"]');
                    for (const s of ldScripts) {
                        try {
                            const d = JSON.parse(s.textContent);
                            const job = d['@type'] === 'JobPosting' ? d
                                      : (d['@graph'] || []).find(x => x['@type'] === 'JobPosting');
                            if (job) {
                                ld_title = job.title || '';
                                ld_company = (job.hiringOrganization && job.hiringOrganization.name) || '';
                                break;
                            }
                        } catch(e) {}
                    }

                    // 2. Direct DOM — h1 for title, company from multiple possible elements
                    const h1El = document.querySelector('h1');

                    // Company: try link first (normal postings), then any container text
                    // (anonymous postings replace the link with plain text)
                    const companySelectors = [
                        'a[href*="/company/"]',
                        '.job-details-jobs-unified-top-card__company-name',
                        '.jobs-unified-top-card__company-name',
                        'a.app-aware-link[href*="/company/"]',
                        '[data-entity-urn*="company"]',
                        // meta description often contains "at CompanyName"
                    ];
                    let company_dom = '';
                    for (const sel of companySelectors) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const txt = el.innerText ? el.innerText.trim() : (el.textContent || '').trim();
                            if (txt && txt.length > 0 && txt.length < 120) {
                                company_dom = txt;
                                break;
                            }
                        }
                    }

                    // 3. meta description — LinkedIn puts "N applicants · Posted X ago" after company
                    //    Format: "Title at Company · Seniority · Location"
                    const metaDesc = (document.querySelector('meta[name="description"]') || {}).content || '';
                    const descAt = metaDesc.indexOf(' at ');
                    const meta_company = descAt > 0 ? metaDesc.substring(descAt + 4).split('·')[0].trim() : '';

                    // 4. Page title — "Job Title at Company | LinkedIn"
                    //    Use lastIndexOf to handle titles that contain " at "
                    const pageTitle = document.title || '';
                    const stripped = pageTitle.replace(/ \\| LinkedIn$/, '').replace(/ - LinkedIn$/, '');
                    const atIdx = stripped.lastIndexOf(' at ');
                    const pt_title = atIdx > 0 ? stripped.substring(0, atIdx).trim() : stripped.trim();
                    const pt_company = atIdx > 0 ? stripped.substring(atIdx + 4).trim() : '';

                    // 5. og:title
                    const ogTitle = (document.querySelector('meta[property="og:title"]') || {}).content || '';
                    const ogAt = ogTitle.lastIndexOf(' at ');
                    const og_title = ogAt > 0 ? ogTitle.substring(0, ogAt).trim() : ogTitle.replace(/ \\| LinkedIn$/, '').trim();
                    const og_company = ogAt > 0 ? ogTitle.substring(ogAt + 4).replace(/ \\| LinkedIn$/, '').trim() : '';

                    return {
                        ld_title, ld_company,
                        h1: h1El ? h1El.innerText.trim() : '',
                        company_dom, meta_company,
                        pt_title, pt_company,
                        og_title, og_company,
                    };
                }""")

                # Apply in priority order: JSON-LD > DOM > meta description > page title > og:title
                if not title:
                    title = (extracted.get("ld_title")
                             or extracted.get("h1")
                             or extracted.get("pt_title")
                             or extracted.get("og_title")) or None
                if not company:
                    company = (extracted.get("ld_company")
                               or extracted.get("company_dom")
                               or extracted.get("meta_company")
                               or extracted.get("pt_company")
                               or extracted.get("og_company")) or None
                if title:
                    title = title.strip() or None
                if company:
                    company = company.strip() or None
            except Exception as js_err:
                logger.debug(f"JS fallback extraction failed for {job_id}: {js_err}")

        if not title or not company:
            try:
                diag = await page.evaluate("""() => ({
                    url: location.href,
                    pageTitle: document.title,
                    h1s: Array.from(document.querySelectorAll('h1')).map(e => e.innerText.trim()).slice(0,3),
                    metaDesc: (document.querySelector('meta[name="description"]') || {}).content || '',
                })""")
                page_title_raw = diag.get('pageTitle', '')
                # Last-resort: extract title from browser tab title "Job Title | LinkedIn"
                if not title and page_title_raw:
                    title = page_title_raw.replace(' | LinkedIn', '').replace(' - LinkedIn', '').strip() or None
                # If still no company, try the meta description "at Company" pattern
                if not company:
                    meta_desc = diag.get('metaDesc', '')
                    at_idx = meta_desc.find(' at ')
                    if at_idx > 0:
                        candidate = meta_desc[at_idx + 4:].split('·')[0].strip()
                        if candidate and len(candidate) < 120:
                            company = candidate
                log_msg = (
                    f"Partial extraction for job {job_id} — "
                    f"h1s={diag.get('h1s',[])} → title={title!r} company={company!r}"
                )
                if not title:
                    logger.warning(log_msg)
                else:
                    # Title extracted via fallback — informational, not a real failure
                    logger.info(log_msg)
            except Exception:
                logger.warning(f"Could not extract title/company for job {job_id}")
            if not title:
                return None
            # Have title but no company — proceed with placeholder so job isn't silently dropped
            if not company:
                company = "Unknown"

        # Detect apply type
        apply_type, external_url = await detect_apply_type(page)

        return {
            "linkedin_job_id": job_id,
            "title": title,
            "company": company,
            "location": location,
            "description": description or "",
            "job_url": f"https://www.linkedin.com/jobs/view/{job_id}/",
            "apply_type": apply_type,
            "external_url": external_url,
        }
    except Exception as e:
        logger.warning(f"Error extracting job details for {job_id}: {e}")
        return None


async def detect_apply_type(page: Page) -> tuple[str, Optional[str]]:
    """
    Detect whether a job uses Easy Apply or redirects to an external site.

    Uses role/text-based locators that survive LinkedIn's CSS class churn.
    Returns (apply_type, external_url).
    apply_type is one of: easy_apply | external_workday | external_other | unknown

    NOTE: Searches are scoped to the main job detail area to avoid false positives
    from "Easy Apply" badges on sidebar job cards. The get_by_text("Easy Apply")
    check was intentionally removed — it matches sidebar elements and produces
    false positives for jobs that use external apply.
    """
    import asyncio as _asyncio
    import re as _re

    # LinkedIn is a heavy SPA — wait for the apply button area to render.
    # 1s is not enough; the "Easy Apply" / "I'm interested" button can take
    # 2-3s to appear after DOMContentLoaded.
    await _asyncio.sleep(3.0)

    # Scope searches to the job detail area — avoid sidebar job cards that also
    # show Easy Apply buttons/badges for OTHER jobs on the page.
    _JOB_DETAIL_SCOPES = [
        ".jobs-search__job-details",  # search results detail pane
        ".scaffold-layout__detail",   # alternate layout
        "div[class*='jobs-details']", # attribute-based fallback
        "main",                        # last resort — still better than full page
    ]

    async def _get_detail_scope():
        for sel in _JOB_DETAIL_SCOPES:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=1000):
                    return loc
            except Exception:
                pass
        return page  # fall back to full page if no scope found

    scope = await _get_detail_scope()

    # Extract current job ID for sidebar guard
    _url_match_early = _re.search(r"/jobs/view/(\d+)", page.url)
    _current_job_id_early = _url_match_early.group(1) if _url_match_early else None

    def _looks_like_easy_apply_href(href: str) -> bool:
        """Return True when href resembles LinkedIn's actual Easy Apply flow."""
        h = (href or "").lower()
        return (
            "opensduiapplyflow" in h
            or "/apply/" in h
            or "easyapply" in h
        )

    # ── Layer 1: AX tree — Easy Apply (most stable, survives all CSS churn) ──
    try:
        _easy_pattern = _re.compile(r"easy\s*apply", _re.IGNORECASE)
        ax_btn = await find_by_aria_label(
            page,
            _easy_pattern,
            roles=("button", "link"),
            job_id=_current_job_id_early,
            timeout_ms=3_000,
        )
        if ax_btn is not None:
            logger.debug("Detected Easy Apply via AX tree")
            return "easy_apply", None
    except Exception as _ax_err:
        logger.debug(f"AX tree Easy Apply check failed: {_ax_err}")

    # ── Layer 2: Easy Apply: role + text (DOM fallback) ───────────────────────
    try:
        btn = scope.get_by_role(
            "button", name=_re.compile(r"easy\s*apply", _re.IGNORECASE)
        ).first
        if await btn.is_visible(timeout=3000):
            logger.debug("Detected Easy Apply via role+text")
            return "easy_apply", None
    except Exception:
        pass

    # ── Layer 3: Easy Apply: aria-label attr selector ─────────────────────────
    try:
        btn = scope.locator("button[aria-label*='Easy Apply' i]").first
        if await btn.is_visible(timeout=2000):
            logger.debug("Detected Easy Apply via aria-label")
            return "easy_apply", None
    except Exception:
        pass

    # ── Layer 4: Easy Apply via SDUI link (<a>-based apply flow) ────────────────
    # LinkedIn increasingly serves Easy Apply as an <a href="/jobs/view/<id>/apply/?openSDUIApplyFlow=true">
    # instead of a <button>. get_by_role("button") is completely blind to these.
    #
    # CRITICAL: LinkedIn pages also show sidebar job cards, each with their own
    # "Easy Apply" links for OTHER jobs. We use _current_job_id_early (extracted
    # above) and only accept links whose href contains that ID.
    _current_job_id = _current_job_id_early  # alias for clarity in this section

    try:
        link = scope.get_by_role(
            "link", name=_re.compile(r"easy\s*apply", _re.IGNORECASE)
        ).first
        if await link.is_visible(timeout=2000):
            href = (await link.get_attribute("href") or "")
            if (not _current_job_id or _current_job_id in href) and _looks_like_easy_apply_href(href):
                logger.debug("Detected Easy Apply via SDUI link (get_by_role)")
                return "easy_apply", None
            else:
                logger.debug(
                    f"Easy Apply link ignored (job/href mismatch or non-apply href): "
                    f"job_id={_current_job_id!r}, href={href[:120]!r}"
                )
    except Exception:
        pass

    try:
        link = scope.locator("a[href*='openSDUIApplyFlow']").first
        if await link.is_visible(timeout=1000):
            href = (await link.get_attribute("href") or "")
            if (not _current_job_id or _current_job_id in href) and _looks_like_easy_apply_href(href):
                logger.debug("Detected Easy Apply via SDUI href attribute")
                return "easy_apply", None
    except Exception:
        pass

    # ── Layer 5: Expired / closed listing ─────────────────────────────────────
    try:
        closed = page.locator("text='No longer accepting applications'").first
        if await closed.is_visible(timeout=1000):
            logger.debug("Detected 'No longer accepting applications'")
            return "expired", None
    except Exception:
        pass

    # ── Layer 6: External apply links (anchor whose text starts with "Apply") ──
    # LinkedIn often wraps external apply URLs in a redirect:
    #   https://www.linkedin.com/redir/redirect/?url=<encoded_external_url>&isSdui=true
    # We must decode the `url` query parameter to classify them correctly.
    #
    # IMPORTANT: Check external apply BEFORE "I'm Interested" — many LinkedIn jobs
    # show both an "Apply on company website" link AND an "I'm Interested" secondary
    # CTA. We must detect the primary apply action, not the secondary engagement button.
    #
    # Check both the scoped container AND the full page — the "Apply on company
    # website" button can live outside the job-details pane in the split-view layout.
    _apply_re = _re.compile(r"^apply", _re.IGNORECASE)
    for _apply_src in (scope, page):
        try:
            links = _apply_src.get_by_role("link", name=_apply_re)
            count = await links.count()
            for i in range(min(count, 5)):
                el = links.nth(i)
                try:
                    if not await el.is_visible(timeout=800):
                        continue
                    href = await el.get_attribute("href") or ""
                    # Unwrap LinkedIn redirect URLs before classifying
                    resolved = _resolve_linkedin_redirect(href)
                    if resolved and "linkedin.com" not in resolved:
                        logger.debug(f"Detected external apply link (resolved): {resolved[:80]}")
                        return _classify_external_url(resolved), resolved
                    elif href.startswith("http") and "linkedin.com" not in href:
                        logger.debug(f"Detected external apply link: {href[:80]}")
                        return _classify_external_url(href), href
                except Exception:
                    pass
        except Exception:
            pass

    # ── Layer 7: External apply buttons ───────────────────────────────────────
    for _btn_src in (scope, page):
        try:
            btn = _btn_src.get_by_role("button", name=_apply_re).first
            if await btn.is_visible(timeout=2000):
                href = await btn.get_attribute("href")
                if href and href.startswith("http"):
                    return _classify_external_url(href), href
                for attr in ("data-job-url", "data-apply-url", "data-url"):
                    attr_val = await btn.get_attribute(attr)
                    if attr_val and attr_val.startswith("http"):
                        return _classify_external_url(attr_val), attr_val
                logger.debug("Detected external Apply button (URL not extractable)")
                return "external_other", None
        except Exception:
            pass

    # ── Layer 8: LinkedIn Recruiter "I'm interested" — AX tree ───────────────
    # Checked AFTER external apply — many jobs show both an "Apply" link (primary)
    # AND an "I'm Interested" button (secondary engagement CTA). We only treat a
    # job as interest_only when no apply action of any kind was found above.
    try:
        _interest_pattern = _re.compile(r"i.?m\s+interested", _re.IGNORECASE)
        ax_interest = await find_by_aria_label(
            page,
            _interest_pattern,
            roles=("button",),
            timeout_ms=3_000,
        )
        if ax_interest is not None:
            logger.debug("Detected 'I'm interested' via AX tree — recruiter-sourced listing")
            return "interest_only", None
    except Exception as _ax_err2:
        logger.debug(f"AX tree interest_only check failed: {_ax_err2}")

    # ── Layer 9: LinkedIn Recruiter "I'm interested" — DOM fallback ───────────
    _interest_re = _re.compile(r"i.?m\s+interested", _re.IGNORECASE)
    for _interest_src in (page, scope):
        try:
            btn = _interest_src.get_by_role("button", name=_interest_re).first
            if await btn.is_visible(timeout=2000):
                logger.debug("Detected 'I'm interested' button — recruiter-sourced listing, no apply flow")
                return "interest_only", None
        except Exception:
            pass

    # ── Diagnostic: log visible buttons AND apply-related links so we can tune selectors ─
    try:
        all_btns = page.get_by_role("button")
        count = await all_btns.count()
        visible = []
        for i in range(min(count, 30)):
            try:
                btn = all_btns.nth(i)
                if await btn.is_visible(timeout=200):
                    txt = (await btn.inner_text()).strip().replace("\n", " ")
                    if txt:
                        visible.append(txt[:40])
            except Exception:
                pass
        # Also enumerate links — critical for SDUI Easy Apply (<a> not <button>)
        vis_links = []
        try:
            all_links = page.get_by_role("link")
            lcount = await all_links.count()
            for i in range(min(lcount, 30)):
                try:
                    lnk = all_links.nth(i)
                    if await lnk.is_visible(timeout=200):
                        txt = (await lnk.inner_text()).strip().replace("\n", " ")
                        if txt and any(kw in txt.lower() for kw in ("apply", "easy", "interest")):
                            href = (await lnk.get_attribute("href") or "")[:50]
                            vis_links.append(f"{txt[:30]}[{href}]")
                except Exception:
                    pass
        except Exception:
            pass
        logger.info(
            f"detect_apply_type unknown | url={page.url[:80]} | "
            f"buttons={visible} | apply_links={vis_links}"
        )
    except Exception:
        pass

    return "unknown", None


async def vision_detect_apply_type(page, vision) -> tuple[str, Optional[str]]:
    """
    Use Claude Vision to detect the apply type when DOM/AX tree detection returns 'unknown'.

    Called only from apply_agent._redetect_apply_type() — NOT during normal search runs,
    to keep per-job vision costs scoped to apply time where budgeting is tighter.

    Args:
        page: Active Playwright page positioned on the job detail page.
        vision: VisionAnalyzer instance (injected by ApplyAgent).

    Returns:
        (apply_type, external_url) tuple — same as detect_apply_type().
    """
    logger.info("vision_detect_apply_type: screenshotting page for Claude Vision analysis")
    try:
        response = await vision.analyze_page(
            page,
            question=(
                "Look at this LinkedIn job page screenshot and answer ONLY with one of these "
                "exact strings (no extra text):\n"
                "  easy_apply   — if you see an 'Easy Apply' button\n"
                "  external     — if you see an 'Apply' button/link that leads to an external site\n"
                "  interest_only — if you see an 'I'm interested' button (recruiter listing)\n"
                "  expired      — if you see 'No longer accepting applications'\n"
                "  unknown      — if you cannot determine which apply type is shown\n"
            ),
            context="Apply type detection fallback — DOM and AX tree both returned unknown",
        )
        resp = response.strip().lower()
        if "easy_apply" in resp:
            logger.info("Vision detected: easy_apply")
            return "easy_apply", None
        elif "interest_only" in resp:
            logger.info("Vision detected: interest_only")
            return "interest_only", None
        elif "expired" in resp:
            logger.info("Vision detected: expired")
            return "expired", None
        elif "external" in resp:
            logger.info("Vision detected: external (classifying as external_other)")
            return "external_other", None
        else:
            logger.info(f"Vision could not classify apply type (response={resp!r})")
            return "unknown", None
    except Exception as exc:
        logger.warning(f"vision_detect_apply_type failed: {exc}")
        return "unknown", None


def _resolve_linkedin_redirect(href: str) -> Optional[str]:
    """
    If href is a LinkedIn redirect URL (linkedin.com/redir/redirect/?url=...),
    extract and decode the actual destination URL.  Returns None otherwise.

    LinkedIn wraps external apply links like this:
      https://www.linkedin.com/redir/redirect/?url=https%3A%2F%2Facme.wd5.myworkdayjobs.com%2F...&isSdui=true
    The `url` query parameter holds the real destination.
    """
    import urllib.parse as _up
    if "linkedin.com/redir/redirect" not in href:
        return None
    try:
        qs = _up.parse_qs(_up.urlparse(href).query)
        url_param = qs.get("url", [""])[0]
        return _up.unquote(url_param) if url_param else None
    except Exception:
        return None


def _classify_external_url(url: str) -> str:
    url_lower = url.lower()
    _ATS = [
        ("myworkdayjobs.com",      "external_workday"),
        ("workday.com",            "external_workday"),
        ("greenhouse.io",          "external_greenhouse"),
        ("lever.co",               "external_lever"),
        ("icims.com",              "external_icims"),
        ("taleo.net",              "external_taleo"),
        ("smartrecruiters.com",    "external_smartrecruiters"),
        ("jobvite.com",            "external_jobvite"),
        ("bamboohr.com",           "external_bamboohr"),
        ("successfactors.com",     "external_successfactors"),
        ("ashbyhq.com",            "external_ashby"),
        ("theladders.com",         "external_theladders"),
        ("paylocity.com",          "external_paylocity"),
        ("ultipro.com",            "external_ukg"),
        ("ukg.com",                "external_ukg"),
        ("adp.com",                "external_adp"),
        ("oraclecloud.com",        "external_oracle"),
        ("oracle.com/taleo",       "external_taleo"),
    ]
    for domain, platform in _ATS:
        if domain in url_lower:
            return platform
    return "external_other"


# ── Batch scoring ──────────────────────────────────────────────────────────────

async def score_jobs_batch(
    llm: ClaudeClient,
    profile: UserProfile,
    jobs: list[dict],
) -> list[dict]:
    """
    Score a batch of jobs against the user profile using Claude Sonnet.

    Returns list of dicts with keys: id, score, reasoning, disqualified, disqualify_reason.
    Falls back to score=0.5 for any job that fails parsing.
    """
    if not jobs:
        return []

    prompt = job_scoring_prompt(profile, jobs)
    text, usage = await llm.message(
        prompt=prompt,
        system=job_scoring_system(),
        model=llm.sonnet_model,
        max_tokens=4096,
        purpose="job_scoring",
    )

    try:
        results = json.loads(text)
        if not isinstance(results, list):
            raise ValueError("Expected a JSON array")
        return results, usage
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Failed to parse scoring response: {e}\nResponse: {text[:500]}")
        # Return safe defaults so we don't lose the jobs
        return [
            {"id": j["id"], "score": 0.5, "reasoning": "Scoring failed", "disqualified": False}
            for j in jobs
        ], usage


# ── Search Agent ───────────────────────────────────────────────────────────────

class SearchAgent(BaseAgent):
    """
    Searches LinkedIn for jobs matching the configured queries,
    scores them against the user profile, and stores results in the DB.
    """

    name = "search_agent"

    def __init__(
        self,
        session: BrowserSession,
        llm: ClaudeClient,
        profile: UserProfile,
        queries: list[dict],
        rate_limiter: RateLimiter,
        settings: dict,
        db_path: Optional[str] = None,
    ) -> None:
        super().__init__(
            db_path=db_path,
            daily_budget_usd=settings.get("budget", {}).get("daily_limit_usd", 15.0),
        )
        self._session = session
        self._llm = llm
        self._profile = profile
        self._queries = queries
        self._limiter = rate_limiter
        self._settings = settings
        self._max_pages = settings.get("global_filters", {}).get("max_pages_per_query", 2)
        self._apps_per_run = settings.get("rate_limits", {}).get("applications_per_run", 10)
        self._max_new_per_run = settings.get("rate_limits", {}).get("max_new_jobs_per_run", 75)
        self._min_score = settings.get("thresholds", {}).get("min_match_score", 0.6)
        self._exclude_kw = [
            k.lower()
            for k in settings.get("global_filters", {}).get("exclude_keywords", [])
        ]

    async def run_once(self) -> AgentResult:
        page = self._session.page
        job_repo = JobRepo(self._conn)

        # Ensure LinkedIn session is active
        if not await self._session.ensure_linkedin_session():
            raise AgentError("Could not establish LinkedIn session")

        # Warmup: brief feed visit before searching
        await self._session.run_warmup()

        total_found = 0
        total_new = 0
        total_scored = 0

        # ── Process each query: collect IDs → extract details → score & save ─
        for query in self._queries:
            for easy_apply_only in (True, False):
                self.logger.info(
                    f"Searching: {query['name']} "
                    f"({'Easy Apply' if easy_apply_only else 'all'})"
                )
                new_ids = await self._collect_job_ids(page, query, easy_apply_only, job_repo)
                total_found += len(new_ids)

                to_score: list[dict] = []
                id_to_job: dict[str, dict] = {}

                # Navigate to each new job and extract details
                for job_id in new_ids:
                    await self._limiter.linkedin_page()
                    try:
                        if not await navigate_to_job(page, job_id):
                            continue
                    except Exception as nav_err:
                        self.logger.warning(f"Browser error navigating to {job_id}: {nav_err} — stopping this query")
                        break

                    details = await extract_job_details(page, job_id)
                    if not details:
                        continue

                    # Do not force unknown -> easy_apply even when LinkedIn's Easy
                    # Apply filter is active. The filter is noisy and can include
                    # recruiter-sourced or stale cards that are not true Easy Apply.

                    # Apply global keyword exclusions
                    if self._is_excluded(details["title"]):
                        self.logger.debug(f"Excluded by keyword filter: {details['title']}")
                        continue

                    details["search_query"] = query["name"]
                    details["easy_apply_forced"] = easy_apply_only
                    id_to_job[job_id] = details
                    to_score.append({
                        "id": job_id,
                        "title": details["title"],
                        "company": details["company"],
                        "description": details["description"],
                    })
                    total_new += 1

                    if self.is_over_budget():
                        self.logger.warning("Daily budget reached — stopping search")
                        break

                    if total_new >= self._max_new_per_run:
                        self.logger.info(
                            f"Per-run job cap reached ({self._max_new_per_run}) — "
                            "stopping early to keep run time bounded"
                        )
                        break

                # Score and save immediately after each query pass —
                # so Ctrl+C doesn't lose already-extracted jobs
                if to_score:
                    scored = await self._score_and_store(to_score, id_to_job, job_repo)
                    total_scored += scored

        return AgentResult(
            success=True,
            jobs_found=total_new,
            details={
                "total_seen": total_found,
                "new_jobs": total_new,
                "scored": total_scored,
                "queries_run": len(self._queries) * 2,
            },
        )

    async def _collect_job_ids(
        self,
        page: Page,
        query: dict,
        easy_apply_only: bool,
        job_repo: JobRepo,
    ) -> list[str]:
        """Paginate through search results and return IDs not already in the DB."""
        new_ids = []
        for page_num in range(self._max_pages):
            start = page_num * 25
            url = build_search_url(
                keywords=query["keywords"],
                location=query.get("location", "United States"),
                work_types=query.get("work_type"),
                experience_levels=query.get("experience_levels"),
                job_types=query.get("job_types"),
                date_posted=query.get("date_posted", "r604800"),
                sort_by=query.get("sort_by", "DD"),
                easy_apply_only=easy_apply_only,
                start=start,
            )

            await self._limiter.linkedin_page()
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except PlaywrightTimeout:
                raise RetryableError(f"Timeout loading search page: {url}")
            except Exception as e:
                if "closed" in str(e).lower() or "target" in str(e).lower():
                    raise AgentError(f"Browser closed during search — LinkedIn may have ended the session: {e}")
                raise

            await random_delay(2.0, 4.0)

            # Check for restriction
            if await self._session.check_for_restriction():
                raise AgentError("LinkedIn restriction detected during search — aborting")

            ids_on_page = await extract_job_ids_from_page(page)
            if not ids_on_page:
                self.logger.debug(f"No job IDs on page {page_num + 1} — stopping pagination")
                break

            # Filter to only new jobs
            page_new = [jid for jid in ids_on_page if not job_repo.exists(jid)]
            new_ids.extend(page_new)

            self.logger.info(
                f"  Page {page_num + 1}: {len(ids_on_page)} listings, "
                f"{len(page_new)} new"
            )

            if len(page_new) == 0:
                # All jobs on this page already seen — stop paginating
                break

        return new_ids

    async def _score_and_store(
        self,
        to_score: list[dict],
        id_to_job: dict[str, dict],
        job_repo: JobRepo,
    ) -> int:
        """Score jobs in batches of 10 and persist to DB. Returns count stored."""
        if not to_score:
            return 0

        stored = 0
        batch_size = 10
        for i in range(0, len(to_score), batch_size):
            batch = to_score[i : i + batch_size]
            self.logger.info(
                f"Scoring batch {i // batch_size + 1} "
                f"({len(batch)} jobs)"
            )

            scores, usage = await score_jobs_batch(self._llm, self._profile, batch)
            self.log_llm_usage(**usage)

            for result in scores:
                job_id = str(result.get("id", ""))
                details = id_to_job.get(job_id)
                if not details:
                    continue

                score = float(result.get("score", 0.5))
                reasoning = result.get("reasoning", "")
                disqualified = result.get("disqualified", False)

                apply_type = details.get("apply_type", "unknown")
                if disqualified:
                    status = "disqualified"
                elif apply_type in ("interest_only", "expired"):
                    # Recruiter-sourced (interest_only) and closed listings (expired)
                    # are definitively not applicable — skip immediately.
                    status = "skipped"
                elif apply_type == "unknown" and score >= self._min_score:
                    # Apply type detection failed but the job scores well.
                    # Mark as "qualified" and let the apply agent re-verify on approach.
                    # The apply agent re-detects before spending any LLM tokens, so
                    # still-unknown jobs will be skipped without cost at apply time.
                    status = "qualified"
                    self.logger.info(
                        f"  [unknown apply_type → qualified] will re-verify at apply time"
                    )
                elif apply_type == "unknown":
                    # Low-scoring unknown — not worth re-verifying
                    status = "skipped"
                elif score >= self._min_score:
                    status = "qualified"
                else:
                    status = "disqualified"

                job = Job(
                    linkedin_job_id=job_id,
                    title=details["title"],
                    company=details["company"],
                    location=details.get("location"),
                    description=details.get("description"),
                    job_url=details["job_url"],
                    external_url=details.get("external_url"),
                    apply_type=details.get("apply_type", "unknown"),
                    match_score=score,
                    match_reasoning=reasoning,
                    search_query=details.get("search_query"),
                    status=status,
                )

                job_repo.upsert(job)
                stored += 1
                self.logger.info(
                    f"  [{status}] {details['title']} @ {details['company']} "
                    f"(score={score:.2f})"
                )

        return stored

    def _is_excluded(self, title: str) -> bool:
        title_lower = title.lower()
        return any(kw in title_lower for kw in self._exclude_kw)
