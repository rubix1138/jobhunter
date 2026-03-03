"""Referral material generator — fetch a job posting and produce tailored PDF resume + cover letter."""

import asyncio
import html
import ipaddress
import json
import re
import socket
from datetime import date
from pathlib import Path
from typing import Optional
from urllib import request as urllib_request
from urllib.parse import urlparse

from ..llm.client import ClaudeClient
from ..llm.cover_letter import (
    generate_cover_letter,
    render_cover_letter_html,
    save_cover_letter_pdf,
)
from ..llm.resume import (
    _strip_fences,
    generate_tailored_resume,
    render_resume_html,
    save_resume_pdf,
)
from ..utils.logging import get_logger
from ..utils.profile_loader import UserProfile

logger = get_logger(__name__)

_EXTRACTION_SYSTEM = (
    "You are a job posting parser. Extract structured data from the raw text of a job posting. "
    "Respond only with valid JSON — no prose, no markdown fences."
)

_EXTRACTION_PROMPT = """Extract the following fields from this job posting text.

JOB POSTING TEXT:
{text}

Return a JSON object with exactly these keys:
{{
  "title": "<job title>",
  "company": "<company name>",
  "description": "<full job description — preserve all requirements, responsibilities, and qualifications>"
}}

If a field cannot be determined, use an empty string.
"""

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s{3,}")


def _is_linkedin_hostname(hostname: str) -> bool:
    host = (hostname or "").strip(".").lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _validate_referral_url(url: str) -> tuple[str, str]:
    """Validate URL and return (normalized_url, hostname)."""
    parsed = urlparse((url or "").strip())
    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        raise ValueError("Only https:// URLs are allowed for referral fetches.")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("URL must include a valid hostname.")

    if host in {"localhost"} or host.endswith(".local"):
        raise ValueError("Localhost and .local domains are not allowed.")

    return parsed.geturl(), host


def _assert_public_hostname(hostname: str, port: int = 443) -> None:
    """
    Reject hostnames that resolve to non-public IPs (SSRF guard).
    """
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"Unable to resolve hostname: {hostname}") from e

    if not infos:
        raise ValueError(f"Unable to resolve hostname: {hostname}")

    for info in infos:
        sockaddr = info[4]
        ip_raw = sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else ""
        try:
            ip_obj = ipaddress.ip_address(ip_raw)
        except ValueError:
            raise ValueError(f"Invalid resolved IP for {hostname}: {ip_raw}")
        if not ip_obj.is_global:
            raise ValueError(
                f"Refusing to fetch non-public network target: {hostname} -> {ip_obj}"
            )


def _strip_html(raw: str) -> str:
    """Remove HTML tags and unescape entities; collapse excessive whitespace."""
    text = _HTML_TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub("\n\n", text)
    return text.strip()


async def _fetch_page_text(url: str, browser_session=None) -> str:
    """
    Fetch raw page text.

    LinkedIn URLs use the supplied BrowserSession (new tab).
    All other URLs use urllib (stdlib) so no extra dependencies are needed.
    """
    safe_url, hostname = _validate_referral_url(url)
    is_linkedin = _is_linkedin_hostname(hostname)

    if is_linkedin:
        if browser_session is None:
            raise ValueError(
                "A BrowserSession is required to fetch LinkedIn URLs. "
                "Pass --url with a non-LinkedIn URL, or run with a browser session."
            )
        logger.info(f"Fetching LinkedIn URL via browser: {safe_url}")
        page = await browser_session.new_page()
        try:
            await page.goto(safe_url, timeout=30_000)
            await page.wait_for_load_state("domcontentloaded")
            text = await page.inner_text("body")
        finally:
            await page.close()
        return text.strip()

    # Non-LinkedIn: use urllib in a thread so the coroutine stays async
    _assert_public_hostname(hostname)
    logger.info(f"Fetching URL via urllib: {safe_url}")

    def _do_fetch() -> str:
        req = urllib_request.Request(
            safe_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        with urllib_request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return _strip_html(raw)

    return await asyncio.to_thread(_do_fetch)


async def _extract_job_details(
    text: str,
    llm: ClaudeClient,
    title_override: Optional[str],
    company_override: Optional[str],
) -> tuple[str, str, str]:
    """
    Extract title, company, and description from page text.

    Overrides skip individual fields; if both overrides are supplied
    the LLM extraction call is skipped entirely.
    """
    if title_override and company_override:
        # Both fields are known — skip the extraction call, use full text as description
        logger.info("Both title and company supplied — skipping extraction LLM call")
        return title_override, company_override, text[:8000]

    prompt = _EXTRACTION_PROMPT.format(text=text[:6000])
    raw, _usage = await llm.message(
        prompt=prompt,
        system=_EXTRACTION_SYSTEM,
        model=llm.sonnet_model,
        max_tokens=2048,
        purpose="job_extraction",
    )

    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as e:
        logger.warning(f"Job extraction JSON parse failed ({e}); using raw text as description")
        data = {"title": "", "company": "", "description": text[:6000]}

    title = title_override or data.get("title", "")
    company = company_override or data.get("company", "")
    description = data.get("description", "") or text[:6000]

    return title, company, description


def _make_slug(company: str, title: str) -> str:
    """Build a filesystem-safe slug from company + title, max 50 chars."""
    raw = f"{company}_{title}".lower()
    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return slug[:50]


async def generate_referral_materials(
    url: str,
    profile: UserProfile,
    llm: ClaudeClient,
    output_dir: Path,
    template_dir: str = "templates",
    title_override: Optional[str] = None,
    company_override: Optional[str] = None,
    browser_session=None,
) -> tuple[Path, Path]:
    """
    Fetch a job posting URL and generate tailored resume + cover letter PDFs.

    Returns (resume_pdf_path, cover_letter_pdf_path).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Fetch page text
    page_text = await _fetch_page_text(url, browser_session=browser_session)
    logger.info(f"Fetched {len(page_text)} chars from {url}")

    # 2. Extract job details (LLM call unless both overrides supplied)
    title, company, description = await _extract_job_details(
        page_text, llm, title_override, company_override
    )

    if not title:
        title = "Position"
    if not company:
        company = "Company"

    logger.info(f"Job details: {title!r} at {company!r}")

    # 3. Generate tailored resume
    logger.info("Generating tailored resume (Claude Opus)…")
    tailored_data, _r_usage = await generate_tailored_resume(
        llm=llm,
        profile=profile,
        job_title=title,
        company=company,
        job_description=description,
    )

    tailored_summary = tailored_data.get("tailored_summary", profile.summary)

    # 4. Generate cover letter
    logger.info("Generating cover letter (Claude Opus)…")
    letter_data, _cl_usage = await generate_cover_letter(
        llm=llm,
        profile=profile,
        job_title=title,
        company=company,
        job_description=description,
        tailored_summary=tailored_summary,
    )

    # 5. Render HTML → PDF
    today_str = date.today().strftime("%Y-%m-%d")
    slug = _make_slug(company, title)

    resume_html = render_resume_html(profile, tailored_data, template_dir=template_dir)
    resume_path = save_resume_pdf(resume_html, output_dir / f"resume_{slug}_{today_str}.pdf")

    cover_html = render_cover_letter_html(
        profile, letter_data, title, company, template_dir=template_dir
    )
    cover_path = save_cover_letter_pdf(
        cover_html, output_dir / f"cover_{slug}_{today_str}.pdf"
    )

    return resume_path, cover_path
