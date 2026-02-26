"""Claude API wrapper with retry, token tracking, and cost logging."""

import asyncio
import os
from typing import Optional

import anthropic

from ..utils.logging import get_logger

logger = get_logger(__name__)

# Cost per million tokens (USD) — update when pricing changes
# cache_write: 25% surcharge on input price; cache_read: 90% discount on input price
_COST_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read":  1.50},
    "claude-sonnet-4-6":         {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_read":  0.30},
    "claude-haiku-4-5-20251001": {"input":  0.80, "output":  4.00, "cache_write":  1.00, "cache_read":  0.08},
}

_DEFAULT_SONNET = "claude-sonnet-4-6"
_DEFAULT_OPUS = "claude-opus-4-6"

# Retry settings
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 2.0


def _calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    prices = _COST_PER_MTOK.get(model, {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30})
    # Regular input = total input minus cache-read tokens (billed at the lower cache_read rate)
    regular_input = max(0, input_tokens - cache_read_tokens)
    return (
        regular_input * prices["input"]
        + output_tokens * prices["output"]
        + cache_creation_tokens * prices["cache_write"]
        + cache_read_tokens * prices["cache_read"]
    ) / 1_000_000


class ClaudeClient:
    """
    Async wrapper around the Anthropic API.

    Handles:
    - Model selection (sonnet for routine, opus for writing)
    - Automatic retry with exponential back-off on rate limits / server errors
    - Token and cost tracking (caller receives usage data to log)
    - Vision messages (base64 image + text prompt)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        sonnet_model: str = _DEFAULT_SONNET,
        opus_model: str = _DEFAULT_OPUS,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY environment variable is not set."
            )
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.sonnet_model = sonnet_model
        self.opus_model = opus_model
        self._max_retries = max_retries

    # ── Core message method ───────────────────────────────────────────────────

    async def message(
        self,
        prompt: str,
        model: Optional[str] = None,
        system: Optional[str] = None,
        system_blocks: Optional[list] = None,
        max_tokens: int = 4096,
        purpose: str = "unspecified",
    ) -> tuple[str, dict]:
        """
        Send a text prompt and return (response_text, usage_info).

        system_blocks: list of Anthropic content block dicts, used instead of system
                       when prompt caching is desired (blocks may include cache_control).
        usage_info keys: model, input_tokens, output_tokens, cache_creation_tokens,
                         cache_read_tokens, cost_usd, purpose
        """
        model = model or self.sonnet_model
        messages = [{"role": "user", "content": prompt}]
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system_blocks:
            kwargs["system"] = system_blocks
        elif system:
            kwargs["system"] = system

        response = await self._call_with_retry(kwargs)
        text = response.content[0].text
        usage = self._extract_usage(response, model, purpose)
        logger.debug(
            f"Claude {model} | {purpose} | "
            f"{usage['input_tokens']}+{usage['output_tokens']} tokens "
            f"| ${usage['cost_usd']:.4f}"
        )
        return text, usage

    async def vision_message(
        self,
        image_b64: str,
        prompt: str,
        model: Optional[str] = None,
        system: Optional[str] = None,
        max_tokens: int = 2048,
        purpose: str = "vision_fallback",
    ) -> tuple[str, dict]:
        """
        Send an image (base64-encoded PNG) plus a text prompt.
        Returns (response_text, usage_info).
        """
        model = model or self.sonnet_model
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": image_b64,
                },
            },
            {"type": "text", "text": prompt},
        ]
        messages = [{"role": "user", "content": content}]
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system:
            kwargs["system"] = system

        response = await self._call_with_retry(kwargs)
        text = response.content[0].text
        usage = self._extract_usage(response, model, purpose)
        return text, usage

    # ── Retry ─────────────────────────────────────────────────────────────────

    async def _call_with_retry(self, kwargs: dict):
        last_exc = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return await self._client.messages.create(**kwargs)
            except anthropic.RateLimitError as e:
                last_exc = e
                delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(f"Rate limit hit (attempt {attempt}), retrying in {delay:.0f}s")
                await asyncio.sleep(delay)
            except anthropic.InternalServerError as e:
                last_exc = e
                delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(f"API server error (attempt {attempt}), retrying in {delay:.0f}s")
                await asyncio.sleep(delay)
            except anthropic.APIError as e:
                # Non-retryable API error
                raise RuntimeError(f"Claude API error: {e}") from e
        raise RuntimeError(f"Claude API failed after {self._max_retries} retries: {last_exc}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _extract_usage(self, response, model: str, purpose: str) -> dict:
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        try:
            cache_creation = int(response.usage.cache_creation_input_tokens or 0)
        except (AttributeError, TypeError):
            cache_creation = 0
        try:
            cache_read = int(response.usage.cache_read_input_tokens or 0)
        except (AttributeError, TypeError):
            cache_read = 0
        cost_usd = _calculate_cost(model, input_tokens, output_tokens, cache_creation, cache_read)
        return {
            "model": model,
            "purpose": purpose,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation,
            "cache_read_tokens": cache_read,
            "cost_usd": cost_usd,
        }

    def calculate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        return _calculate_cost(model, input_tokens, output_tokens)
