"""Tailored resume generation via Claude Opus."""

import json
from datetime import date
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..utils.logging import get_logger
from ..utils.profile_loader import UserProfile
from .client import ClaudeClient

logger = get_logger(__name__)

_RESUME_SYSTEM = (
    "You are an expert technical resume writer. You tailor resumes to specific job "
    "descriptions, emphasizing the most relevant skills and achievements. "
    "Respond only with valid JSON — no prose, no markdown fences."
)


def _resume_tailor_prompt(profile: UserProfile, job_title: str, company: str, job_description: str) -> str:
    # Focus on the most recent/relevant 5 roles (last ~15 years)
    recent_experience = profile.experience[:5]
    experience_text = ""
    for exp in recent_experience:
        achievements_text = "\n".join(
            f"  * {a[:200]}" for a in exp.achievements[:5]
        )
        experience_text += (
            f"\n- {exp.title} at {exp.company} ({exp.start_date}–{exp.end_date})\n"
            f"  {exp.description[:200].strip()}\n"
            f"  Achievements:\n{achievements_text}\n"
        )

    domains_text = "\n".join(
        f"  - {d.name} ({d.years} yrs, {d.proficiency}): {d.details}"
        for d in profile.skills.domains
    )

    certs_text = ", ".join(c.name for c in profile.skills.certifications[:6])

    return f"""You are an expert executive resume writer specializing in cybersecurity leadership roles.
Tailor this candidate's resume for the job below. Focus on business impact, executive presence,
and the specific priorities evident in the job description.

TARGET ROLE: {job_title} at {company}

JOB DESCRIPTION:
{job_description[:4000]}

CANDIDATE PROFILE:
Name: {profile.full_name()}
Summary: {profile.summary[:600]}

Domain Expertise:
{domains_text}

Certifications: {certs_text}

Recent Experience (focus here — earlier roles are supporting context only):
{experience_text}

Return a JSON object with EXACTLY these keys:
{{
  "tagline": "<8-12 word professional tagline, e.g. 'Cybersecurity Executive | CISO | Zero Trust & Security Operations'>",
  "tailored_summary": "<2-3 sentences: who they are, their biggest relevant achievement, why THIS role. Business language, not jargon-heavy. No first-person 'I'.>",
  "core_competencies": ["<9-12 two-to-four word competency phrases most relevant to this JD, e.g. 'Zero Trust Architecture', 'Security Operations Center', 'Cloud Security Strategy', 'Board-Level Communication'>"],
  "experience": [
    {{
      "company": "<company name>",
      "title": "<job title>",
      "start_date": "<YYYY-MM>",
      "end_date": "<YYYY-MM or present>",
      "location": "<location>",
      "achievements": ["<3-5 bullets: quantified achievements most relevant to this role. Business language — mention team sizes, budget scale, % improvements, $ impact where available>"],
      "technologies": ["<4-8 key technologies>"]
    }}
  ],
  "top_certifications": ["<3-5 certification names most impressive/relevant to this role>"],
  "skills_emphasis": ["<top 8-10 skills from candidate's domains most relevant to this JD>"],
  "keywords_added": ["<ATS keywords from JD incorporated naturally>"]
}}

Rules:
- TRUTHFUL ONLY — never invent credentials, metrics, or experience
- Reframe achievements using business language: team size, budget, operational scale, % improvement
- For the most recent role (Truist), use 4-5 strong bullets; older roles 2-3 max
- Omit roles older than 15 years from the experience array entirely
- Core competencies must match language in the JD — these feed ATS scanners
- tagline should be pipe-separated executive positioning statement
- Summary: start with a powerful noun phrase, not "I" or "Experienced"
"""


async def generate_tailored_resume(
    llm: ClaudeClient,
    profile: UserProfile,
    job_title: str,
    company: str,
    job_description: str,
) -> tuple[dict, dict]:
    """
    Generate a tailored resume for a specific job.

    Returns (tailored_data, usage_info).
    tailored_data has keys: tailored_summary, experience, skills_emphasis, keywords_added.
    """
    prompt = _resume_tailor_prompt(profile, job_title, company, job_description)
    text, usage = await llm.message(
        prompt=prompt,
        system=_RESUME_SYSTEM,
        model=llm.opus_model,
        max_tokens=4096,
        purpose="resume_tailoring",
    )

    try:
        data = json.loads(_strip_fences(text))
    except json.JSONDecodeError as e:
        logger.warning(
            f"Failed to parse resume JSON: {e} — using raw profile data. "
            f"Response snippet: {text[:200]!r}"
        )
        data = _fallback_resume_data(profile)

    return data, usage


def _strip_fences(text: str) -> str:
    """Remove markdown code fences that Claude sometimes adds despite instructions."""
    text = text.strip()
    if text.startswith("```"):
        # Remove opening fence (```json or ```)
        text = text[text.index("\n") + 1:] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[: text.rfind("```")]
    return text.strip()


def _fallback_resume_data(profile: UserProfile) -> dict:
    """Return unmodified profile data when Claude response can't be parsed."""
    return {
        "tagline": "",
        "tailored_summary": profile.summary,
        "core_competencies": [d.name for d in profile.skills.domains],
        "experience": [
            {
                "company": e.company,
                "title": e.title,
                "start_date": e.start_date,
                "end_date": e.end_date,
                "location": e.location,
                "achievements": e.achievements,
                "technologies": e.technologies,
            }
            for e in profile.experience[:5]
        ],
        "top_certifications": [c.name for c in profile.skills.certifications[:5]],
        "skills_emphasis": [d.name for d in profile.skills.domains[:8]],
        "keywords_added": [],
    }


def render_resume_html(
    profile: UserProfile,
    tailored_data: dict,
    template_dir: str = "templates",
) -> str:
    """Render the resume as HTML using the Jinja2 template."""
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("resume.html")

    # Top certifications: LLM-selected or fallback to first 5 from profile
    top_certs_raw = tailored_data.get("top_certifications", [])
    if not top_certs_raw:
        top_certs_raw = [c.name for c in profile.skills.certifications[:5]]

    # Publications: top 4 from profile (most impactful)
    publications = profile.publications[:4] if profile.publications else []

    # Speaking: top 6 highlights from profile
    speaking = profile.speaking_engagements[:6] if profile.speaking_engagements else []

    return template.render(
        personal=profile.personal,
        tagline=tailored_data.get("tagline", ""),
        tailored_summary=tailored_data.get("tailored_summary", profile.summary),
        core_competencies=tailored_data.get("core_competencies", []),
        experience=tailored_data.get("experience", []),
        education=profile.education,
        skills=profile.skills,
        skills_emphasis=tailored_data.get("skills_emphasis", []),
        top_certifications=top_certs_raw,
        publications=publications,
        speaking=speaking,
        today=date.today().strftime("%B %Y"),
    )


def save_resume_pdf(html: str, output_path: str | Path) -> Path:
    """Convert HTML to PDF using WeasyPrint and save to disk."""
    from weasyprint import HTML  # imported here to avoid slow startup elsewhere

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html).write_pdf(str(path))
    logger.info(f"Resume PDF saved: {path} ({path.stat().st_size // 1024}KB)")
    return path
