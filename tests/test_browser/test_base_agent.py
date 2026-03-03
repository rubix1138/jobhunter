"""Tests for BaseAgent ABC — lifecycle, retry, budget enforcement."""

import pytest

from jobhunter.agents.base import (
    AgentError,
    AgentResult,
    BaseAgent,
    RetryableError,
)
from jobhunter.db.engine import init_db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = init_db(path)
    conn.close()
    return path


class SuccessAgent(BaseAgent):
    name = "success_agent"

    async def run_once(self) -> AgentResult:
        return AgentResult(success=True, jobs_found=3)


class FailAgent(BaseAgent):
    name = "fail_agent"

    async def run_once(self) -> AgentResult:
        raise AgentError("Something broke")


class RetryAgent(BaseAgent):
    name = "retry_agent"

    def __init__(self, *args, fail_times=2, **kwargs):
        super().__init__(*args, **kwargs)
        self._calls = 0
        self._fail_times = fail_times

    async def run_once(self) -> AgentResult:
        self._calls += 1
        if self._calls <= self._fail_times:
            raise RetryableError(f"Transient failure {self._calls}")
        return AgentResult(success=True, jobs_found=1)


class HookAgent(BaseAgent):
    name = "hook_agent"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.before_called = False
        self.after_called = False

    async def before_run(self) -> None:
        self.before_called = True

    async def after_run(self, result: AgentResult) -> None:
        self.after_called = True

    async def run_once(self) -> AgentResult:
        return AgentResult(success=True)


class UnexpectedErrorAgent(BaseAgent):
    name = "unexpected_agent"

    async def run_once(self) -> AgentResult:
        raise ValueError("totally unexpected")


class DryRunSummaryAgent(BaseAgent):
    name = "dry_run_summary_agent"

    async def run_once(self) -> AgentResult:
        return AgentResult(
            success=True,
            apps_submitted=0,
            details={"dry_run": True, "generated": 2},
        )


class TestBaseAgentLifecycle:
    @pytest.mark.asyncio
    async def test_success_run(self, db_path):
        agent = SuccessAgent(db_path=db_path)
        result = await agent.run()
        assert result.success is True
        assert result.jobs_found == 3

    @pytest.mark.asyncio
    async def test_agent_error_captured(self, db_path):
        agent = FailAgent(db_path=db_path)
        result = await agent.run()
        assert result.success is False
        assert "Something broke" in result.error_message

    @pytest.mark.asyncio
    async def test_unexpected_error_captured(self, db_path):
        agent = UnexpectedErrorAgent(db_path=db_path)
        result = await agent.run()
        assert result.success is False
        assert result.error_message is not None

    @pytest.mark.asyncio
    async def test_hooks_called(self, db_path):
        agent = HookAgent(db_path=db_path)
        await agent.run()
        assert agent.before_called is True
        assert agent.after_called is True

    @pytest.mark.asyncio
    async def test_agent_run_recorded_success(self, db_path):
        from jobhunter.db.engine import get_connection
        from jobhunter.db.repository import AgentRunRepo
        agent = SuccessAgent(db_path=db_path)
        await agent.run()
        conn = get_connection(db_path)
        repo = AgentRunRepo(conn)
        runs = repo.list_recent("success_agent")
        conn.close()
        assert len(runs) == 1
        assert runs[0].status == "success"
        assert runs[0].jobs_found == 3

    @pytest.mark.asyncio
    async def test_agent_run_recorded_error(self, db_path):
        from jobhunter.db.engine import get_connection
        from jobhunter.db.repository import AgentRunRepo
        agent = FailAgent(db_path=db_path)
        await agent.run()
        conn = get_connection(db_path)
        repo = AgentRunRepo(conn)
        runs = repo.list_recent("fail_agent")
        conn.close()
        assert runs[0].status == "error"
        assert "Something broke" in runs[0].error_message

    @pytest.mark.asyncio
    async def test_dry_run_summary_uses_generated_count(self, db_path, caplog):
        agent = DryRunSummaryAgent(db_path=db_path)
        with caplog.at_level("INFO"):
            result = await agent.run()
        assert result.success is True
        assert "2 generated" in caplog.text


class TestBaseAgentRetry:
    @pytest.mark.asyncio
    async def test_retries_on_retryable_error(self, db_path):
        agent = RetryAgent(
            db_path=db_path,
            fail_times=2,
            max_retries=3,
            retry_base_delay=0.01,  # fast for tests
        )
        result = await agent.run()
        assert result.success is True
        assert agent._calls == 3

    @pytest.mark.asyncio
    async def test_fails_after_max_retries(self, db_path):
        agent = RetryAgent(
            db_path=db_path,
            fail_times=10,
            max_retries=3,
            retry_base_delay=0.01,
        )
        result = await agent.run()
        assert result.success is False
        assert "Max retries" in result.error_message

    @pytest.mark.asyncio
    async def test_no_retry_on_agent_error(self, db_path):
        calls = 0

        class ImmediateFailAgent(BaseAgent):
            name = "immediate_fail"
            async def run_once(self) -> AgentResult:
                nonlocal calls
                calls += 1
                raise AgentError("hard fail")

        agent = ImmediateFailAgent(db_path=db_path, max_retries=3, retry_base_delay=0.01)
        result = await agent.run()
        assert result.success is False
        assert calls == 1  # No retry on AgentError


class TestBudgetEnforcement:
    @pytest.mark.asyncio
    async def test_budget_not_exceeded(self, db_path):
        agent = SuccessAgent(db_path=db_path, daily_budget_usd=100.0)
        agent._open_db()
        assert agent.is_over_budget() is False
        agent._close_db()

    @pytest.mark.asyncio
    async def test_log_llm_usage(self, db_path):
        from jobhunter.db.engine import get_connection
        from jobhunter.db.repository import LlmUsageRepo
        agent = SuccessAgent(db_path=db_path)
        agent._open_db()
        agent.log_llm_usage(
            model="claude-sonnet-4-6",
            purpose="test",
            input_tokens=500,
            output_tokens=100,
            cost_usd=0.005,
        )
        agent._close_db()
        conn = get_connection(db_path)
        repo = LlmUsageRepo(conn)
        assert repo.daily_cost() == pytest.approx(0.005)
        conn.close()
