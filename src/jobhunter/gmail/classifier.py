"""Email classification via Claude Sonnet."""

import json
from dataclasses import dataclass
from typing import Optional

from ..llm.client import ClaudeClient
from ..llm.prompts import email_classification_prompt, email_classification_system
from ..utils.logging import get_logger

logger = get_logger(__name__)

# Valid classification labels
CLASSIFICATIONS = {
    "interview_invite",
    "rejection",
    "follow_up",
    "assessment",
    "offer",
    "recruiter_outreach",
    "spam",
    "unknown",
}

# Labels that should be forwarded to personal email
FORWARD_CLASSIFICATIONS = {
    "interview_invite",
    "assessment",
    "offer",
    "follow_up",
    "unknown",
}

# Labels that trigger a job status update
STATUS_MAP = {
    "interview_invite": "interviewing",
    "offer": "offer",
    "rejection": "rejected",
    "assessment": "interviewing",
}


@dataclass
class ClassificationResult:
    classification: str
    confidence: float
    company_name: Optional[str]
    reasoning: str
    should_forward: bool
    new_job_status: Optional[str]


async def classify_email(
    llm: ClaudeClient,
    from_address: str,
    subject: str,
    body: str,
) -> tuple[ClassificationResult, dict]:
    """
    Classify a job-search email using Claude Sonnet.

    Returns (ClassificationResult, usage_info).
    Falls back to 'unknown' classification on any parse error.
    """
    prompt = email_classification_prompt(subject, body, from_address)
    text, usage = await llm.message(
        prompt=prompt,
        system=email_classification_system(),
        model=llm.sonnet_model,
        max_tokens=512,
        purpose="email_classification",
    )

    try:
        data = json.loads(text)
        classification = data.get("classification", "unknown")
        if classification not in CLASSIFICATIONS:
            logger.warning(f"Unknown classification returned: {classification!r} — using 'unknown'")
            classification = "unknown"

        confidence = float(data.get("confidence", 0.5))
        company_name = data.get("company_name") or None
        reasoning = data.get("reasoning", "")

        result = ClassificationResult(
            classification=classification,
            confidence=confidence,
            company_name=company_name,
            reasoning=reasoning,
            should_forward=classification in FORWARD_CLASSIFICATIONS,
            new_job_status=STATUS_MAP.get(classification),
        )
        logger.debug(
            f"Email classified: {classification!r} "
            f"(confidence={confidence:.2f}, company={company_name!r})"
        )
        return result, usage

    except (json.JSONDecodeError, ValueError, KeyError) as e:
        logger.warning(f"Failed to parse classification response: {e}\nResponse: {text[:300]}")
        return ClassificationResult(
            classification="unknown",
            confidence=0.0,
            company_name=None,
            reasoning="Parse error",
            should_forward=True,
            new_job_status=None,
        ), usage
