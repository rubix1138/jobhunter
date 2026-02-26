"""Claude Vision fallback for page analysis when DOM selectors fail."""

import base64
import json
from pathlib import Path
from typing import Optional

from patchright.async_api import Page

from ..utils.logging import get_logger

logger = get_logger(__name__)


async def screenshot_page(
    page: Page,
    save_path: Optional[str | Path] = None,
    full_page: bool = False,
) -> bytes:
    """
    Take a screenshot of the current page.

    Args:
        page: Active Playwright page.
        save_path: If provided, also save to disk.
        full_page: Capture full scrollable page (vs. viewport only).

    Returns:
        PNG image bytes.
    """
    options = {"full_page": full_page, "type": "png"}
    if save_path:
        path = Path(save_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        options["path"] = str(path)

    data = await page.screenshot(**options)
    logger.debug(f"Screenshot taken: {len(data)} bytes")
    return data


async def screenshot_element(
    page: Page,
    selector: str,
    save_path: Optional[str | Path] = None,
) -> Optional[bytes]:
    """
    Screenshot a specific element.

    Returns:
        PNG bytes, or None if element not found.
    """
    try:
        locator = page.locator(selector).first
        options = {"type": "png"}
        if save_path:
            path = Path(save_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            options["path"] = str(path)
        data = await locator.screenshot(**options)
        return data
    except Exception as e:
        logger.debug(f"screenshot_element failed for {selector!r}: {e}")
        return None


def image_to_base64(image_bytes: bytes) -> str:
    """Encode PNG bytes to base64 string for Claude API."""
    return base64.standard_b64encode(image_bytes).decode()


class VisionAnalyzer:
    """
    Uses Claude Vision to analyze page screenshots when DOM selectors fail.

    Requires an initialized LLM client (injected to avoid circular imports).
    """

    def __init__(self, llm_client) -> None:
        """
        Args:
            llm_client: Instance of jobhunter.llm.client.ClaudeClient
                        (injected at runtime in Phase 3).
        """
        self._client = llm_client

    async def analyze_page(
        self,
        page: Page,
        question: str,
        context: str = "",
    ) -> str:
        """
        Screenshot the current page and ask Claude a question about it.

        Args:
            page: Active Playwright page.
            question: What to ask Claude about the screenshot.
            context: Optional extra context (e.g., what action is being attempted).

        Returns:
            Claude's response text.
        """
        screenshot = await screenshot_page(page)
        b64 = image_to_base64(screenshot)

        prompt_parts = []
        if context:
            prompt_parts.append(f"Context: {context}\n\n")
        prompt_parts.append(question)

        response, _usage = await self._client.vision_message(
            image_b64=b64,
            prompt="".join(prompt_parts),
            purpose="vision_fallback",
        )
        logger.debug(f"Vision analysis complete: {len(response)} chars")
        return response

    async def find_element_description(
        self,
        page: Page,
        element_purpose: str,
    ) -> str:
        """
        Ask Claude to describe how to find a specific UI element on the page.

        Returns:
            Natural language description or suggested selector.
        """
        return await self.analyze_page(
            page,
            question=(
                f"I need to interact with: {element_purpose}\n\n"
                "Look at the screenshot and tell me:\n"
                "1. Is this element visible on the page?\n"
                "2. What is its approximate position (top/middle/bottom, left/center/right)?\n"
                "3. What CSS selector or text content could identify it?\n"
                "Be concise and specific."
            ),
            context="Browser automation — DOM selector fallback",
        )

    async def analyze_form_fields(self, page: Page, context: str = "") -> list[dict]:
        """Screenshot the page and ask Claude to list all visible form fields.

        Args:
            page: Active Playwright page (should show the form step).
            context: Optional context string (e.g., "Senior Engineer at Acme Corp").

        Returns:
            List of dicts with keys: label, type, required.
            Returns [] on API failure or JSON parse failure.
        """
        question = (
            "List all visible form fields on this page as a JSON array. "
            "Each element must have exactly these keys: "
            '"label" (string — the field label text), '
            '"type" (string — one of: text, textarea, radio, select, checkbox, file, other), '
            '"required" (boolean — true if the field is marked required). '
            "Respond with ONLY the JSON array, no other text. "
            'Example: [{"label": "Phone number", "type": "text", "required": true}]. '
            'If no form fields are visible, respond with [].'
        )
        try:
            response = await self.analyze_page(page, question=question, context=context)
            # Strip markdown code fences if present
            text = response.strip()
            if text.startswith("```"):
                lines = text.splitlines()
                text = "\n".join(
                    line for line in lines if not line.startswith("```")
                ).strip()
            return json.loads(text)
        except Exception as exc:
            logger.debug(f"analyze_form_fields failed: {exc}")
            return []

    async def answer_form_question(
        self,
        page: Page,
        question_text: str,
        profile_summary: str,
    ) -> str:
        """
        Use vision to understand a form question and suggest an answer.

        Args:
            page: Active Playwright page (should show the form question).
            question_text: The question being asked.
            profile_summary: Brief summary of the user's profile for context.

        Returns:
            Suggested answer string.
        """
        return await self.analyze_page(
            page,
            question=(
                f"Job application form question: {question_text!r}\n\n"
                f"Candidate profile: {profile_summary}\n\n"
                "Based on the screenshot and profile, what is the best answer to this question? "
                "Respond with just the answer text, nothing else."
            ),
            context="Job application form — answering a question",
        )
