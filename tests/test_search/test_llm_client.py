"""Tests for ClaudeClient — cost calculation and retry logic (mocked API)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from jobhunter.llm.client import ClaudeClient, _calculate_cost


class TestCostCalculation:
    def test_sonnet_cost(self):
        # 1M input + 1M output at $3/$15 per MTok
        cost = _calculate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000)
        assert cost == pytest.approx(18.0)

    def test_opus_cost(self):
        cost = _calculate_cost("claude-opus-4-6", 1_000_000, 1_000_000)
        assert cost == pytest.approx(90.0)

    def test_small_usage(self):
        # 1000 input + 200 output with sonnet
        cost = _calculate_cost("claude-sonnet-4-6", 1000, 200)
        expected = (1000 * 3.0 + 200 * 15.0) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_unknown_model_uses_sonnet_pricing(self):
        cost = _calculate_cost("claude-unknown-model", 1_000_000, 0)
        assert cost == pytest.approx(3.0)

    def test_zero_tokens(self):
        assert _calculate_cost("claude-sonnet-4-6", 0, 0) == 0.0


class TestClaudeClientInit:
    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            ClaudeClient(api_key=None)

    def test_explicit_key_accepted(self):
        client = ClaudeClient(api_key="sk-ant-test")
        assert client is not None

    def test_default_models(self):
        client = ClaudeClient(api_key="sk-ant-test")
        assert "sonnet" in client.sonnet_model
        assert "opus" in client.opus_model


class TestClaudeClientMessage:
    def _make_mock_response(self, text: str, input_tokens: int = 100, output_tokens: int = 50):
        response = MagicMock()
        response.content = [MagicMock(text=text)]
        response.usage.input_tokens = input_tokens
        response.usage.output_tokens = output_tokens
        return response

    @pytest.mark.asyncio
    async def test_message_returns_text_and_usage(self):
        client = ClaudeClient(api_key="sk-ant-test")
        mock_response = self._make_mock_response('[{"score": 0.8}]', 500, 100)

        with patch.object(
            client._client.messages, "create", new=AsyncMock(return_value=mock_response)
        ):
            text, usage = await client.message("test prompt", purpose="test")

        assert text == '[{"score": 0.8}]'
        assert usage["input_tokens"] == 500
        assert usage["output_tokens"] == 100
        assert usage["cost_usd"] > 0
        assert usage["purpose"] == "test"

    @pytest.mark.asyncio
    async def test_message_passes_system_prompt(self):
        client = ClaudeClient(api_key="sk-ant-test")
        mock_response = self._make_mock_response("response")

        with patch.object(
            client._client.messages, "create", new=AsyncMock(return_value=mock_response)
        ) as mock_create:
            await client.message("prompt", system="You are helpful")

        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs.get("system") == "You are helpful"

    @pytest.mark.asyncio
    async def test_calculate_cost_method(self):
        client = ClaudeClient(api_key="sk-ant-test")
        cost = client.calculate_cost("claude-sonnet-4-6", 1000, 500)
        assert cost == pytest.approx(_calculate_cost("claude-sonnet-4-6", 1000, 500))


class TestClaudeClientRetry:
    @pytest.mark.asyncio
    async def test_retries_on_rate_limit(self):
        import anthropic
        client = ClaudeClient(api_key="sk-ant-test", max_retries=3)
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="ok")]
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5

        call_count = 0
        async def fake_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise anthropic.RateLimitError(
                    message="rate limit",
                    response=MagicMock(status_code=429, headers={}),
                    body={},
                )
            return mock_response

        with patch.object(client._client.messages, "create", new=fake_create):
            with patch("asyncio.sleep", new=AsyncMock()):
                text, _ = await client.message("prompt")

        assert text == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        import anthropic
        client = ClaudeClient(api_key="sk-ant-test", max_retries=2)

        async def always_rate_limit(**kwargs):
            raise anthropic.RateLimitError(
                message="rate limit",
                response=MagicMock(status_code=429, headers={}),
                body={},
            )

        with patch.object(client._client.messages, "create", new=always_rate_limit):
            with patch("asyncio.sleep", new=AsyncMock()):
                with pytest.raises(RuntimeError, match="failed after"):
                    await client.message("prompt")
