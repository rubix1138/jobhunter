"""Email classification via Claude Sonnet."""

import json
import re
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


def _extract_json_payload(text: str) -> str:
    """Extract likely JSON payload from plain text or fenced markdown."""
    raw = (text or "").strip()
    if not raw:
        return raw

    # Common case: model wraps payload in ```json ... ```
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        return fenced.group(1).strip()

    # Truncated fenced output (missing closing ```) is still salvageable.
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE).strip()
        raw = raw.removesuffix("```").strip()

    # If there's extra prose around JSON, slice to first/last brace.
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return raw[first_brace:last_brace + 1].strip()

    return raw


def _coerce_partial_payload(raw: str) -> Optional[dict]:
    """
    Best-effort parse for truncated JSON responses.
    Returns partial dict when key fields can be extracted, else None.
    """
    classification_match = re.search(r'"classification"\s*:\s*"([^"]+)"', raw)
    if not classification_match:
        return None

    data: dict = {"classification": classification_match.group(1)}

    confidence_match = re.search(r'"confidence"\s*:\s*([0-9]*\.?[0-9]+)', raw)
    if confidence_match:
        try:
            data["confidence"] = float(confidence_match.group(1))
        except ValueError:
            pass

    company_null = re.search(r'"company_name"\s*:\s*null', raw)
    if company_null:
        data["company_name"] = None
    else:
        company_match = re.search(r'"company_name"\s*:\s*"([^"]*)"', raw)
        if company_match:
            data["company_name"] = company_match.group(1)

    # Reasoning may be truncated; capture what exists.
    reasoning_match = re.search(r'"reasoning"\s*:\s*"([^"]*)', raw, flags=re.DOTALL)
    if reasoning_match:
        data["reasoning"] = reasoning_match.group(1).strip()

    return data


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

    payload = _extract_json_payload(text)

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = _coerce_partial_payload(payload)
        if data is None:
            logger.warning(f"Failed to parse classification response\nResponse: {text[:300]}")
            return ClassificationResult(
                classification="unknown",
                confidence=0.0,
                company_name=None,
                reasoning="Parse error",
                should_forward=True,
                new_job_status=None,
            ), usage

    try:
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

    except (ValueError, KeyError, TypeError) as e:
        logger.warning(f"Failed to normalize classification response: {e}\nResponse: {text[:300]}")
        return ClassificationResult(
            classification="unknown",
            confidence=0.0,
            company_name=None,
            reasoning="Parse error",
            should_forward=True,
            new_job_status=None,
        ), usage
