"""BaseApplicator ABC — question answering, stuck-page handling, vision fallback."""

import asyncio
import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

from patchright.async_api import Page

from ..browser.vision import VisionAnalyzer
from ..db.models import Application, Job, QACache
from ..llm.client import ClaudeClient
from ..utils.logging import get_logger
from ..utils.profile_loader import UserProfile

if TYPE_CHECKING:
    from ..db.repository import QACacheRepo

logger = get_logger(__name__)


def _normalize_question(q: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — stable cache key."""
    q = q.lower().strip()
    q = re.sub(r'[^\w\s]', ' ', q)
    q = re.sub(r'\s+', ' ', q).strip()
    return q[:500]


def _options_hash(options: Optional[list[str]]) -> str:
    """Stable 8-char hash of sorted option list; '' for text/textarea fields."""
    if not options:
        return ''
    return hashlib.md5(','.join(sorted(options)).encode(), usedforsecurity=False).hexdigest()[:8]


def _extract_host(url: Optional[str]) -> str:
    if not url:
        return ""
    try:
        return (urlparse(url).hostname or "").strip().lower()
    except Exception:
        return ""

# Minimum Claude confidence to auto-answer a question
MIN_AUTO_ANSWER_CONFIDENCE = 0.5

_TASK_INSTRUCTIONS = (
    "You answer job application questions on behalf of the candidate described above. "
    "Respond only with valid JSON — no prose, no markdown fences."
)


@dataclass
class QuestionAnswer:
    answer: str
    confidence: float          # 0.0-1.0
    source: str                # "profile" | "claude" | "vision"
    needs_review: bool = False


class BaseApplicator(ABC):
    """
    Abstract base for all applicator implementations.

    Provides:
    - Question answering from profile first, Claude Sonnet fallback
    - Vision-based stuck-page analysis
    - Common field-fill helpers
    """

    def __init__(
        self,
        page: Page,
        llm: ClaudeClient,
        profile: UserProfile,
        vision: Optional[VisionAnalyzer] = None,
        review_mode: bool = False,
        qa_cache: Optional["QACacheRepo"] = None,
    ) -> None:
        self._page = page
        self._llm = llm
        self._profile = profile
        self._vision = vision
        self._review_mode = review_mode
        self._qa_cache = qa_cache
        self.logger = get_logger(f"jobhunter.{self.__class__.__name__}")
        self._qa_log: list[dict] = []   # record of all Q&A for the application
        self.failure_reason: Optional[str] = None

    # ── Profile system blocks (prompt caching) ────────────────────────────────

    def _build_profile_system_blocks(self) -> list[dict]:
        """Build system prompt blocks with the full profile context.

        The first block is marked cache_control=ephemeral so the profile is only
        processed once per 5-minute window. Subsequent Q&A calls in the same
        application session read the cache at ~10% of the normal input token cost.
        """
        p = self._profile

        # Work experience
        exp_lines = []
        for job in p.experience:
            end = job.end_date or "Present"
            exp_lines.append(f"• {job.title} @ {job.company} ({job.start_date} – {end})")
            if job.description:
                exp_lines.append(f"  {job.description[:250]}")
            for ach in (job.achievements or [])[:3]:
                exp_lines.append(f"  - {ach}")

        # Education
        edu_lines = []
        for edu in p.education:
            line = f"• {edu.degree} — {edu.institution}"
            if edu.graduation_date:
                line += f" ({edu.graduation_date})"
            edu_lines.append(line)

        # Certifications
        cert_lines = [
            f"• {c.name}" + (f" ({c.issuer})" if c.issuer else "")
            for c in p.skills.certifications
        ]

        # Skills domains with years
        domain_lines = []
        for d in p.skills.domains:
            line = f"• {d.name}"
            if d.years:
                line += f" ({d.years} yrs)"
            if d.details:
                line += f": {d.details[:120]}"
            domain_lines.append(line)

        # Technical skills (flat lists)
        tech_items = []
        if p.skills.programming_languages:
            langs = ", ".join(
                lang if isinstance(lang, str) else lang.name
                for lang in p.skills.programming_languages[:12]
            )
            tech_items.append(f"Languages: {langs}")
        if p.skills.security_products:
            tech_items.append(f"Security products: {', '.join(p.skills.security_products[:12])}")
        if p.skills.frameworks_and_tools:
            tech_items.append(f"Frameworks/tools: {', '.join(p.skills.frameworks_and_tools[:12])}")
        if p.skills.infrastructure_and_platforms:
            tech_items.append(f"Infrastructure: {', '.join(p.skills.infrastructure_and_platforms[:12])}")

        aa = p.application_answers

        profile_text = f"""CANDIDATE PROFILE:

Name: {p.full_name()}
Location: {p.personal.location}
Work Authorization: {p.personal.work_authorization}
Willing to Relocate: {"Yes" if p.personal.willing_to_relocate else "No"}
Total Years of Experience: {aa.years_of_experience}
Desired Salary: {aa.desired_salary}

PROFESSIONAL SUMMARY:
{p.summary}

WORK EXPERIENCE:
{chr(10).join(exp_lines) if exp_lines else "(none listed)"}

EDUCATION:
{chr(10).join(edu_lines) if edu_lines else "(none listed)"}

CERTIFICATIONS & LICENSES:
{chr(10).join(cert_lines) if cert_lines else "(none listed)"}

SKILLS & DOMAIN EXPERTISE:
{chr(10).join(domain_lines) if domain_lines else "(none listed)"}

TECHNICAL SKILLS:
{chr(10).join(tech_items) if tech_items else "(none listed)"}"""

        return [
            {
                "type": "text",
                "text": profile_text,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": _TASK_INSTRUCTIONS,
            },
        ]

    # ── Abstract interface ────────────────────────────────────────────────────

    @abstractmethod
    async def apply(self, job: Job, application: Application) -> bool:
        """
        Attempt to submit the application.
        Returns True on successful submission, False on failure.
        Raise an exception for unrecoverable errors.
        """

    # ── Question answering ────────────────────────────────────────────────────

    def _qa_scope_key(self) -> str:
        """
        Build a cache scope key so Q&A answers do not bleed across ATS domains.
        """
        job = getattr(self, "_job", None)
        if not job:
            return "global"

        domain = _extract_host(getattr(job, "external_url", None)) or _extract_host(
            getattr(job, "job_url", None)
        )
        company = re.sub(r"[^a-z0-9]+", "_", (getattr(job, "company", "") or "").lower()).strip("_")

        if domain and company:
            return f"domain:{domain}|company:{company[:40]}"
        if domain:
            return f"domain:{domain}"
        if company:
            return f"company:{company[:40]}"
        return "global"

    def _scoped_question_key(self, question: str) -> str:
        """
        Namespace normalized question text by job scope to prevent cache poisoning.
        """
        base = _normalize_question(question)
        scope = self._qa_scope_key()
        if scope == "global":
            return base
        # Keep within DB field norms and prior cache key size expectations.
        return f"{scope}|{base}"[:500]

    async def answer_question(
        self,
        question: str,
        field_type: str = "text",
        options: Optional[list[str]] = None,
        context: str = "",
    ) -> QuestionAnswer:
        """
        Determine the best answer for an application question.

        Resolution order:
        1. Known static answers from profile.application_answers
        2. Custom answer patterns from profile.application_answers.custom_answers
        3. Claude Sonnet with profile context
        4. Vision fallback if Claude fails

        Args:
            question: The question text as it appears on the form.
            field_type: "text", "radio", "select", "checkbox", "textarea"
            options: For radio/select — the available choices.
            context: Additional context (company name, job title, etc.)
        """
        # 0. QA cache lookup
        if self._qa_cache:
            key = self._scoped_question_key(question)
            ohash = _options_hash(options)
            cached = self._qa_cache.get(key, ohash)
            if cached and cached.confidence >= 0.7:
                self.logger.debug(
                    f"QA cache hit (×{cached.times_used}): {question[:60]!r}"
                )
                return QuestionAnswer(
                    answer=cached.answer,
                    confidence=cached.confidence,
                    source="cache",
                )

        # 1. Try static profile answers
        profile_answer = self._answer_from_profile(question, options)
        if profile_answer is not None:
            return QuestionAnswer(
                answer=profile_answer,
                confidence=1.0,
                source="profile",
            )

        # 2. Claude Sonnet (with cached rich profile context)
        try:
            result = await self._answer_via_claude(question, field_type, options, context)
            if result.confidence >= MIN_AUTO_ANSWER_CONFIDENCE:
                self._write_qa_cache(question, options, field_type, result)
                return result
            self.logger.debug(
                f"Low confidence ({result.confidence:.2f}) for: {question[:60]!r} "
                "— trying strategic fallback"
            )
            # 3. Strategic fallback — use job description to infer what the employer wants
            strategic = await self._answer_strategically(question, field_type, options, context)
            if strategic is not None:
                self._write_qa_cache(question, options, field_type, strategic)
                return strategic
            # No strategic answer available — return original with review flag
            self.logger.warning(
                f"Low confidence ({result.confidence:.2f}) for question: {question!r}"
            )
            result.needs_review = True
            return result
        except Exception as e:
            self.logger.warning(f"Claude Q&A failed: {e}")

        # 4. Vision fallback
        if self._vision:
            try:
                profile_summary = f"{self._profile.full_name()}: {self._profile.summary[:200]}"
                answer_text = await self._vision.answer_form_question(
                    self._page, question, profile_summary
                )
                return QuestionAnswer(
                    answer=answer_text.strip(),
                    confidence=0.4,
                    source="vision",
                    needs_review=True,
                )
            except Exception as e:
                self.logger.warning(f"Vision Q&A failed: {e}")

        return QuestionAnswer(
            answer="",
            confidence=0.0,
            source="none",
            needs_review=True,
        )

    def _answer_from_profile(
        self, question: str, options: Optional[list[str]]
    ) -> Optional[str]:
        """Check static profile fields and custom answer patterns."""
        aa = self._profile.application_answers
        q_lower = question.lower().strip()

        # Static known fields
        static_map = {
            r"years.*(experience|exp)": str(aa.years_of_experience),
            r"salary|compensation|pay": aa.desired_salary,
            r"start.?date|available|notice": aa.start_date,
            r"sponsor|require.*visa|visa.*requir": "No" if not aa.sponsorship_required else "Yes",
            r"disability|disabled": aa.has_disability,
            r"veteran": aa.veteran_status,
            r"gender": aa.gender,
            r"race|ethnic": aa.ethnicity,
            r"how did you hear|referr|source": aa.how_did_you_hear,
            r"travel|willing to travel": aa.willing_to_travel,
            r"relocat": "Yes" if self._profile.personal.willing_to_relocate else "No",
            r"work.?authoriz|eligible to work|authorized|legally eligible": "Yes",
            r"18 years|18 or older|18\+|of legal age|legal age": "Yes",
            r"felony|convicted|criminal": "No",
            r"non.?compete|non.?compet": "No",
            r"first.?name": self._profile.personal.first_name,
            r"last.?name": self._profile.personal.last_name,
            r"email": self._profile.personal.email,
            r"phone|telephone|mobile": self._profile.personal.phone,
            r"city|location|where": self._profile.personal.location,
            r"linkedin": self._profile.personal.linkedin_url,
            r"github": self._profile.personal.github_url,
            r"portfolio|website": self._profile.personal.portfolio_url,
        }

        for pattern, value in static_map.items():
            if value and re.search(pattern, q_lower):
                # For radio/select, find closest matching option
                if options:
                    matched = _match_option(value, options)
                    if matched:
                        return matched
                else:
                    return str(value)

        # Certification / license check: "do you have [cert name]" → look up profile
        cert_match = re.search(
            r"(?:license|certification|certificate|licensed|certified)[:\s]+(.+?)[\?\.]*$",
            q_lower,
            re.MULTILINE,
        )
        if cert_match:
            queried_cert = cert_match.group(1).strip().lower()
            for cert in self._profile.skills.certifications:
                cert_name_lower = cert.name.lower()
                # Match full name or acronym in parentheses e.g. "(CISM)"
                acronym_match = re.search(r"\(([^)]+)\)", cert.name)
                acronym = acronym_match.group(1).lower() if acronym_match else ""
                if queried_cert in cert_name_lower or cert_name_lower in queried_cert:
                    answer = "Yes"
                    if options:
                        matched = _match_option(answer, options)
                        return matched if matched else answer
                    return answer
                if acronym and (queried_cert == acronym or acronym in queried_cert):
                    answer = "Yes"
                    if options:
                        matched = _match_option(answer, options)
                        return matched if matched else answer
                    return answer
            # Cert not found in profile → No
            answer = "No"
            if options:
                matched = _match_option(answer, options)
                return matched if matched else answer
            return answer

        # Custom answer patterns
        for pattern, answer in aa.custom_answers.items():
            if re.search(pattern.lower(), q_lower):
                return answer.strip()

        return None

    async def _answer_via_claude(
        self,
        question: str,
        field_type: str,
        options: Optional[list[str]],
        context: str,
    ) -> QuestionAnswer:
        """Use Claude Sonnet to answer a question using the cached rich profile context."""
        system_blocks = self._build_profile_system_blocks()
        options_text = f"\nAvailable options: {options}" if options else ""

        prompt = f"""Answer this job application question for the candidate.

ADDITIONAL CONTEXT: {context or 'N/A'}
FIELD TYPE: {field_type}
QUESTION: {question}{options_text}

Return JSON:
{{
  "answer": "<the answer text, or exact option text for radio/select>",
  "confidence": <0.0-1.0>,
  "reasoning": "<one sentence>"
}}

Rules:
- For radio/select: answer must be exactly one of the available options
- For yes/no questions: answer "Yes" or "No"
- For salary: use the candidate's desired_salary value
- Never fabricate credentials or lie
- Confidence < 0.5 means the question needs human review
"""
        text, usage = await self._llm.message(
            prompt=prompt,
            system_blocks=system_blocks,
            model=self._llm.sonnet_model,
            max_tokens=512,
            purpose="question_answering",
        )

        import json
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(ln for ln in lines if not ln.startswith("```")).strip()
        data = json.loads(text)
        return QuestionAnswer(
            answer=str(data.get("answer", "")),
            confidence=float(data.get("confidence", 0.5)),
            source="claude",
        )

    async def _answer_strategically(
        self,
        question: str,
        field_type: str,
        options: Optional[list[str]],
        context: str,
    ) -> Optional[QuestionAnswer]:
        """Strategic fallback: use the job description to infer the best answer.

        Called when Claude's profile-based answer has low confidence. Rather than
        guessing blindly, this prompt tells Claude to reason about what a hiring
        manager for this specific role and seniority level would want to see.
        """
        import json
        job = getattr(self, "_job", None)
        if not job:
            return None
        job_desc = getattr(job, "description", "") or ""
        if not job_desc:
            return None

        system_blocks = self._build_profile_system_blocks()
        options_text = f"\nAvailable options: {options}" if options else ""

        prompt = f"""The candidate is applying for: {job.title} at {job.company}
Experience level targeted: {getattr(job, 'experience_level', 'senior')}

JOB DESCRIPTION:
{job_desc[:2500]}

---
QUESTION: {question}
FIELD TYPE: {field_type}{options_text}

The candidate's profile alone does not provide a high-confidence answer.
Use the job description and role context to infer the best answer.

Reasoning approach:
- For years-of-experience in a domain: infer the minimum the employer expects from
  the seniority level; if the candidate plausibly has adjacent experience, provide
  a number at or slightly above that floor (be truthful, not inflated)
- For domain-specific experience the candidate lacks directly (e.g., healthcare):
  consider transferable experience from regulated industries, enterprise environments,
  or analogous domains and provide a reasonable honest estimate
- For yes/no qualification questions: Yes only if the candidate could truthfully
  claim it; No otherwise — do not fabricate credentials
- For select/radio: choose the option that best fits both the candidate's background
  and the employer's evident expectations

Return JSON:
{{
  "answer": "<specific answer — a number for numeric fields, exact option for select/radio>",
  "confidence": <0.6-0.9>,
  "reasoning": "<one sentence explaining the strategic reasoning>"
}}

Rules:
- Never fabricate certifications, degrees, or credentials the candidate does not have
- Provide a SPECIFIC answer (a number, not a range; exact option text, not a description)
"""
        try:
            text, usage = await self._llm.message(
                prompt=prompt,
                system_blocks=system_blocks,
                model=self._llm.sonnet_model,
                max_tokens=512,
                purpose="strategic_question_answering",
            )
            text = text.strip()
            if text.startswith("```"):
                lines = text.splitlines()
                text = "\n".join(ln for ln in lines if not ln.startswith("```")).strip()
            data = json.loads(text)
            answer = str(data.get("answer", "")).strip()
            if not answer:
                return None
            confidence = float(data.get("confidence", 0.7))
            self.logger.info(
                f"Strategic answer for {question[:50]!r}: {answer!r} "
                f"(conf={confidence:.2f}, reasoning={data.get('reasoning', '')[:80]})"
            )
            return QuestionAnswer(
                answer=answer,
                confidence=confidence,
                source="strategic",
            )
        except Exception as e:
            self.logger.warning(f"Strategic Q&A failed: {e}")
            return None

    def _write_qa_cache(
        self,
        question: str,
        options: Optional[list[str]],
        field_type: str,
        qa: QuestionAnswer,
    ) -> None:
        """Persist a high-confidence answer to the QA cache for future applications."""
        if not self._qa_cache or qa.confidence < 0.7 or qa.source in ("cache", "profile"):
            return
        try:
            self._qa_cache.upsert(QACache(
                question_key=self._scoped_question_key(question),
                options_hash=_options_hash(options),
                field_type=field_type,
                answer=qa.answer,
                confidence=qa.confidence,
                source=qa.source,
            ))
        except Exception as e:
            self.logger.debug(f"QA cache write failed: {e}")

    def record_qa(self, question: str, answer: QuestionAnswer) -> None:
        """Log a Q&A pair for storage in questions_json."""
        self._qa_log.append({
            "question": question,
            "answer": answer.answer,
            "confidence": answer.confidence,
            "source": answer.source,
            "needs_review": answer.needs_review,
        })

    def qa_log_json(self) -> str:
        """Return the Q&A log as a JSON string for DB storage."""
        import json
        return json.dumps(self._qa_log, indent=2)

    def has_low_confidence_answers(self) -> bool:
        """Return True if any answer has confidence below the threshold."""
        return any(qa["needs_review"] for qa in self._qa_log)

    # ── Review mode ───────────────────────────────────────────────────────────

    async def _pause_for_review(self, job_title: str, company: str) -> bool:
        """
        Pause before the final submit click and wait for user input.

        Returns True to proceed with submission, False to skip this job.
        Raises KeyboardInterrupt if the user types 'q' to quit entirely.
        Only active when review_mode=True; otherwise returns True immediately.
        """
        if not self._review_mode:
            return True

        sep = "=" * 62
        print(f"\n{sep}")
        print(f"  REVIEW MODE — Ready to submit application")
        print(f"  Role   : {job_title}")
        print(f"  Company: {company}")
        print(f"  Browser window shows the final form — review it now.")
        print(f"  [Enter] Submit   [s] Skip this job   [q] Quit all")
        print(f"{sep}")

        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(None, input, "> ")
        except EOFError:
            print("No interactive terminal — skipping (run directly in a shell to review).")
            return False
        response = response.strip().lower()

        if response == "q":
            print("Quitting review mode — no further applications will be submitted.")
            raise KeyboardInterrupt("User quit review mode")
        if response == "s":
            print(f"Skipping {job_title} @ {company}")
            return False

        print(f"Submitting {job_title} @ {company}...")
        return True

    # ── Stuck page handling ───────────────────────────────────────────────────

    async def handle_stuck_page(self, expected_action: str) -> Optional[str]:
        """
        Use Vision to analyse the current page when automation is stuck.

        Args:
            expected_action: What the automation was trying to do.

        Returns:
            Suggested next action as a string, or None if Vision is unavailable.
        """
        if not self._vision:
            self.logger.warning("Vision not available for stuck-page analysis")
            return None

        self.logger.info(f"Stuck page — requesting Vision analysis for: {expected_action}")
        return await self._vision.find_element_description(self._page, expected_action)


def _match_option(value: str, options: list[str]) -> Optional[str]:
    """Find the best matching option for a value (case-insensitive substring)."""
    value_lower = value.lower()
    # Exact match first
    for opt in options:
        if opt.lower() == value_lower:
            return opt
    # Substring match
    for opt in options:
        if value_lower in opt.lower() or opt.lower() in value_lower:
            return opt
    return None
