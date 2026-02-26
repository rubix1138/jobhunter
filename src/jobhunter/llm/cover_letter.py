"""Cover letter generation via Claude Opus."""

import json
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..utils.logging import get_logger
from ..utils.profile_loader import UserProfile
from .client import ClaudeClient

logger = get_logger(__name__)

_COVER_LETTER_SYSTEM = (
    "You are an expert career coach who writes compelling, authentic cover letters. "
    "You write in a natural, professional tone — never generic or template-sounding. "
    "Respond only with valid JSON — no prose, no markdown fences."
)


def _cover_letter_prompt(
    profile: UserProfile,
    job_title: str,
    company: str,
    job_description: str,
    tailored_summary: str,
) -> str:
    top_achievements = []
    for exp in profile.experience[:2]:
        top_achievements.extend(exp.achievements[:2])

    return f"""Write a compelling cover letter for this job application.

TARGET ROLE: {job_title} at {company}

JOB DESCRIPTION:
{job_description[:3000]}

CANDIDATE:
Name: {profile.full_name()}
Professional summary: {tailored_summary}
Top achievements: {chr(10).join('- ' + a for a in top_achievements[:4])}
Key skills: {', '.join(s.name for s in profile.skills.programming_languages[:5])}
            {', '.join(profile.skills.frameworks_and_tools[:5])}

Return a JSON object:
{{
  "opening_paragraph": "<1-2 sentences: hook + role + company — specific, not generic>",
  "body_paragraph_1": "<2-3 sentences: most relevant experience/achievement for THIS role>",
  "body_paragraph_2": "<2-3 sentences: why THIS company specifically, show you researched them>",
  "closing_paragraph": "<1-2 sentences: confident call to action, express enthusiasm>"
}}

Rules:
- Sound like a real person, not a template
- Reference specific details from the job description
- Never start with "I am writing to apply for..."
- Keep total length under 300 words
- Be specific about achievements — use numbers where available
"""


async def generate_cover_letter(
    llm: ClaudeClient,
    profile: UserProfile,
    job_title: str,
    company: str,
    job_description: str,
    tailored_summary: str,
) -> tuple[dict, dict]:
    """
    Generate a cover letter for a specific job.

    Returns (letter_data, usage_info).
    letter_data has keys: opening_paragraph, body_paragraph_1, body_paragraph_2, closing_paragraph.
    """
    prompt = _cover_letter_prompt(
        profile, job_title, company, job_description, tailored_summary
    )
    text, usage = await llm.message(
        prompt=prompt,
        system=_COVER_LETTER_SYSTEM,
        model=llm.opus_model,
        max_tokens=2048,
        purpose="cover_letter",
    )

    try:
        data = json.loads(_strip_fences(text))
    except json.JSONDecodeError as e:
        logger.warning(
            f"Failed to parse cover letter JSON: {e} — using raw text. "
            f"Response snippet: {text[:200]!r}"
        )
        data = {
            "opening_paragraph": text[:200],
            "body_paragraph_1": "",
            "body_paragraph_2": "",
            "closing_paragraph": f"I look forward to discussing this opportunity. — {profile.full_name()}",
        }

    return data, usage


def _strip_fences(text: str) -> str:
    """Remove markdown code fences that Claude sometimes adds despite instructions."""
    text = text.strip()
    if text.startswith("```"):
        text = text[text.index("\n") + 1:] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[: text.rfind("```")]
    return text.strip()


def render_cover_letter_html(
    profile: UserProfile,
    letter_data: dict,
    job_title: str,
    company: str,
    template_dir: str = "templates",
) -> str:
    """Render the cover letter as HTML using the Jinja2 template."""
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("cover_letter.html")
    return template.render(
        personal=profile.personal,
        job_title=job_title,
        company=company,
        date=date.today().strftime("%B %d, %Y"),
        opening_paragraph=letter_data.get("opening_paragraph", ""),
        body_paragraph_1=letter_data.get("body_paragraph_1", ""),
        body_paragraph_2=letter_data.get("body_paragraph_2", ""),
        closing_paragraph=letter_data.get("closing_paragraph", ""),
    )


def save_cover_letter_pdf(html: str, output_path: str | Path) -> Path:
    """Convert HTML to PDF using WeasyPrint and save to disk."""
    from weasyprint import HTML

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html).write_pdf(str(path))
    logger.info(f"Cover letter PDF saved: {path} ({path.stat().st_size // 1024}KB)")
    return path
