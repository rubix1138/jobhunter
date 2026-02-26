"""Prompt templates for all Claude API calls."""

from ..utils.profile_loader import UserProfile


def job_scoring_system() -> str:
    return (
        "You are an expert technical recruiter evaluating job fit. "
        "You respond only with valid JSON — no prose, no markdown fences."
    )


def job_scoring_prompt(profile: UserProfile, jobs: list[dict]) -> str:
    """
    Build a batch scoring prompt.

    Each job in `jobs` should have keys: id, title, company, description.
    Returns a prompt requesting a JSON array of scoring objects.
    """
    profile_text = _format_profile(profile)

    jobs_text = ""
    for i, job in enumerate(jobs, 1):
        jobs_text += (
            f"\n--- JOB {i} (id: {job['id']}) ---\n"
            f"Title: {job['title']}\n"
            f"Company: {job['company']}\n"
            f"Description:\n{job['description'][:3000]}\n"
        )

    return f"""Score each job listing for this candidate. Return ONLY a JSON array — no other text.

CANDIDATE PROFILE:
{profile_text}

CANDIDATE PREFERENCES:
- Target titles: {', '.join(profile.preferences.job_titles)}
- Remote preference: {profile.preferences.remote_preference}
- Min salary: ${profile.preferences.min_salary:,} (0 = no minimum)
- Excluded companies: {', '.join(profile.preferences.excluded_companies) or 'none'}
- Deal breakers: {', '.join(profile.preferences.deal_breakers) or 'none'}

JOB LISTINGS:
{jobs_text}

Return a JSON array with one object per job, in the same order:
[
  {{
    "id": "<job id from above>",
    "score": <0.0-1.0 float>,
    "reasoning": "<2-3 sentence explanation>",
    "disqualified": <true if excluded company, deal breaker, or clearly wrong field>,
    "disqualify_reason": "<reason if disqualified, else null>"
  }}
]

Scoring rubric:
- 0.9-1.0: Exceptional match — title, stack, level, and location all align
- 0.7-0.8: Strong match — most criteria met, minor gaps
- 0.6-0.7: Decent match — worth applying, some concerns
- 0.4-0.6: Weak match — significant gaps in title, level, or stack
- 0.0-0.4: Poor match — wrong field, overqualified, or underqualified
"""


def _format_profile(profile: UserProfile) -> str:
    lines = [
        f"Name: {profile.full_name()}",
        f"Location: {profile.personal.location}",
        f"Work auth: {profile.personal.work_authorization}",
        f"Summary: {profile.summary[:500]}",
        "",
        "Skills:",
        f"  Languages: {', '.join(f'{l.name} ({l.proficiency})' for l in profile.skills.programming_languages)}",
        f"  Tools: {', '.join(profile.skills.frameworks_and_tools[:20])}",
        "",
        "Experience:",
    ]
    for exp in profile.experience[:4]:
        lines.append(
            f"  {exp.title} @ {exp.company} ({exp.start_date}–{exp.end_date})"
        )
        if exp.technologies:
            lines.append(f"    Tech: {', '.join(exp.technologies[:10])}")
    return "\n".join(lines)


# ── Email classification ───────────────────────────────────────────────────────

def email_classification_system() -> str:
    return (
        "You classify job-search emails into predefined categories. "
        "Respond only with valid JSON — no prose, no markdown fences."
    )


def email_classification_prompt(subject: str, body: str, from_address: str) -> str:
    return f"""Classify this job-search email. Return ONLY a JSON object.

From: {from_address}
Subject: {subject}
Body (first 1500 chars):
{body[:1500]}

Return:
{{
  "classification": "<one of: interview_invite, rejection, follow_up, assessment, offer, recruiter_outreach, spam, unknown>",
  "confidence": <0.0-1.0>,
  "company_name": "<extracted company name or null>",
  "reasoning": "<one sentence>"
}}
"""


# ── Recruiter auto-reply ───────────────────────────────────────────────────────

def recruiter_reply_prompt(
    profile: UserProfile,
    recruiter_email_body: str,
    job_title: str,
    company: str,
) -> str:
    return f"""Write a brief, professional reply to a recruiter outreach email.

CANDIDATE: {profile.full_name()}
TARGET ROLE: {job_title} at {company}
CANDIDATE SUMMARY: {profile.summary[:300]}

RECRUITER EMAIL:
{recruiter_email_body[:1000]}

Write a 3-4 sentence reply that:
- Expresses genuine interest (this role scored above our threshold)
- Briefly mentions 1-2 relevant skills
- Asks for a 15-minute call or next steps
- Sounds natural, not templated

Return only the email body text, no subject line.
"""
