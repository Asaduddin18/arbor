"""Tests for arbor/orchestrator.py — task decomposition and absorption check."""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from arbor.config import get_default_config
from arbor.wal import (
    WalEventType,
    WalReader,
    WalWriter,
    WalState,
    AgentState,
    TaskState,
    build_state_from_wal,
)
from arbor.orchestrator import (
    should_absorb,
    decide_spawn_depth,
    decompose_goal,
    assign_next_task,
    handle_task_failure,
    _build_agent_id,
)


@pytest.fixture
def cfg():
    return get_default_config()


# ── should_absorb ─────────────────────────────────────────────────────────────


class TestShouldAbsorb:
    def test_returns_agent_when_all_conditions_met(self, cfg) -> None:
        task = TaskState("t1", "dev", "do x", status="planned")
        agents = {
            "a1": AgentState("a1", "dev", "m", 1, status="active",
                             context_budget=8000, tokens_used=2000)
        }
        result = should_absorb(task, agents, cfg)
        assert result is not None
        assert result.agent_id == "a1"

    def test_returns_none_budget_exceeded(self, cfg) -> None:
        task = TaskState("t1", "dev", "do x")
        agents = {
            "a1": AgentState("a1", "dev", "m", 1, status="active",
                             context_budget=8000, tokens_used=5000)  # 62.5%
        }
        assert should_absorb(task, agents, cfg) is None

    def test_returns_none_wrong_type(self, cfg) -> None:
        task = TaskState("t1", "infra", "do x")
        agents = {
            "a1": AgentState("a1", "dev", "m", 1, status="active",
                             context_budget=8000, tokens_used=100)
        }
        assert should_absorb(task, agents, cfg) is None

    def test_returns_none_agent_only_spawned(self, cfg) -> None:
        task = TaskState("t1", "dev", "do x")
        agents = {
            "a1": AgentState("a1", "dev", "m", 1, status="spawned",
                             context_budget=8000, tokens_used=100)
        }
        assert should_absorb(task, agents, cfg) is None

    def test_returns_none_deep_agent(self, cfg) -> None:
        task = TaskState("t1", "dev", "do x")
        agents = {
            "a1": AgentState("a1", "dev", "m", 2, status="active",  # depth 2, too deep
                             context_budget=8000, tokens_used=100)
        }
        assert should_absorb(task, agents, cfg) is None

    def test_exactly_at_60pct_threshold(self, cfg) -> None:
        task = TaskState("t1", "dev", "do x")
        # 60% exactly → should NOT absorb (< 0.6 is the condition)
        agents = {
            "a1": AgentState("a1", "dev", "m", 1, status="active",
                             context_budget=8000, tokens_used=4800)  # exactly 60%
        }
        assert should_absorb(task, agents, cfg) is None


# ── decide_spawn_depth ────────────────────────────────────────────────────────


class TestDecideSpawnDepth:
    def test_independent_task_spawns_at_depth1(self, cfg) -> None:
        state = WalState(run_id="run-1")
        task = TaskState("t1", "dev", "do x")
        assert decide_spawn_depth(task, parent_task_id=None, state=state) == 1

    def test_subproblem_spawns_one_deeper(self, cfg) -> None:
        state = WalState(run_id="run-1")
        state.agents["a1"] = AgentState("a1", "dev", "m", 1, status="active")
        state.tasks["parent-task"] = TaskState(
            "parent-task", "dev", "parent",
            assigned_agent_id="a1"
        )
        task = TaskState("child-task", "dev", "sub-problem")
        depth = decide_spawn_depth(task, parent_task_id="parent-task", state=state)
        assert depth == 2

    def test_parent_task_not_found_returns_depth1(self) -> None:
        state = WalState(run_id="run-1")
        task = TaskState("t1", "dev", "do x")
        assert decide_spawn_depth(task, parent_task_id="nonexistent", state=state) == 1


# ── decompose_goal ────────────────────────────────────────────────────────────


class TestDecomposeGoal:
    def _make_mock_client(self, json_response: str) -> MagicMock:
        """Build a mock AsyncAnthropic client that returns json_response."""
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=json_response)]
        mock_message.usage = MagicMock(input_tokens=100, output_tokens=50)

        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_message)
        return mock_client

    @pytest.mark.asyncio
    async def test_writes_task_planned_entries(self, tmp_wal_path: Path, cfg) -> None:
        decomp_json = json.dumps({
            "tasks": [
                {"task_id": "auth-setup", "task_type": "dev",
                 "goal": "Set up auth", "complexity": 5, "chain_id": None, "dependencies": []},
                {"task_id": "api-routes", "task_type": "dev",
                 "goal": "Build API", "complexity": 4, "chain_id": None, "dependencies": []},
            ],
            "chains": [],
            "cross_chain_dependencies": [],
        })
        mock_client = self._make_mock_client(decomp_json)
        writer = WalWriter(tmp_wal_path)
        state = WalState(run_id="run-1", goal="Build REST API")

        await decompose_goal("Build REST API", state, writer, cfg, client=mock_client)

        entries = WalReader.read_all(tmp_wal_path)
        planned = [e for e in entries if e.event == WalEventType.TASK_PLANNED]
        assert len(planned) == 2
        task_ids = {e.payload["task_id"] for e in planned}
        assert "auth-setup" in task_ids
        assert "api-routes" in task_ids

    @pytest.mark.asyncio
    async def test_retries_on_malformed_json(self, tmp_wal_path: Path, cfg) -> None:
        good_json = json.dumps({
            "tasks": [{"task_id": "t1", "task_type": "dev", "goal": "g", "complexity": 3,
                        "chain_id": None, "dependencies": []}],
            "chains": [],
            "cross_chain_dependencies": [],
        })
        # First response is bad, second is good
        bad_message = MagicMock()
        bad_message.content = [MagicMock(text="NOT JSON AT ALL")]
        bad_message.usage = MagicMock(input_tokens=10, output_tokens=5)

        good_message = MagicMock()
        good_message.content = [MagicMock(text=good_json)]
        good_message.usage = MagicMock(input_tokens=100, output_tokens=50)

        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[bad_message, good_message])

        writer = WalWriter(tmp_wal_path)
        state = WalState(run_id="run-1", goal="test")

        await decompose_goal("test", state, writer, cfg, client=mock_client)

        entries = WalReader.read_all(tmp_wal_path)
        planned = [e for e in entries if e.event == WalEventType.TASK_PLANNED]
        assert len(planned) == 1

    @pytest.mark.asyncio
    async def test_raises_after_all_retries_fail(self, tmp_wal_path: Path, cfg) -> None:
        bad_message = MagicMock()
        bad_message.content = [MagicMock(text="INVALID JSON")]
        bad_message.usage = MagicMock(input_tokens=10, output_tokens=5)

        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=bad_message)

        writer = WalWriter(tmp_wal_path)
        state = WalState(run_id="run-1", goal="test")

        with pytest.raises(ValueError, match="valid JSON"):
            await decompose_goal("test", state, writer, cfg, client=mock_client)

    @pytest.mark.asyncio
    async def test_strips_markdown_code_fences(self, tmp_wal_path: Path, cfg) -> None:
        inner_json = json.dumps({
            "tasks": [{"task_id": "t1", "task_type": "dev", "goal": "g", "complexity": 2,
                        "chain_id": None, "dependencies": []}],
            "chains": [],
            "cross_chain_dependencies": [],
        })
        fenced = f"```json\n{inner_json}\n```"
        mock_client = self._make_mock_client(fenced)
        writer = WalWriter(tmp_wal_path)
        state = WalState(run_id="run-1", goal="test")

        await decompose_goal("test", state, writer, cfg, client=mock_client)

        entries = WalReader.read_all(tmp_wal_path)
        planned = [e for e in entries if e.event == WalEventType.TASK_PLANNED]
        assert len(planned) == 1

    @pytest.mark.asyncio
    async def test_writes_agent_spawned_for_chain(self, tmp_wal_path: Path, cfg) -> None:
        decomp_json = json.dumps({
            "tasks": [
                {"task_id": "t1", "task_type": "dev", "goal": "step 1", "complexity": 3,
                 "chain_id": "chain-a", "dependencies": []},
                {"task_id": "t2", "task_type": "dev", "goal": "step 2", "complexity": 3,
                 "chain_id": "chain-a", "dependencies": ["t1"]},
            ],
            "chains": [
                {"chain_id": "chain-a", "tasks": ["t1", "t2"],
                 "agent_type": "dev", "estimated_tokens": 5000, "colocation": "single-agent"}
            ],
            "cross_chain_dependencies": [],
        })
        mock_client = self._make_mock_client(decomp_json)
        writer = WalWriter(tmp_wal_path)
        state = WalState(run_id="run-1", goal="test")

        await decompose_goal("test", state, writer, cfg, client=mock_client)

        entries = WalReader.read_all(tmp_wal_path)
        spawned = [e for e in entries if e.event == WalEventType.AGENT_SPAWNED]
        assert len(spawned) >= 1
        assert spawned[0].payload["agent_type"] == "dev"


# ── handle_task_failure ───────────────────────────────────────────────────────


class TestHandleTaskFailure:
    @pytest.mark.asyncio
    async def test_writes_task_failed_entry(self, tmp_wal_path: Path, cfg) -> None:
        writer = WalWriter(tmp_wal_path)
        state = WalState(run_id="run-1")
        state.tasks["auth-task"] = TaskState(
            "auth-task", "dev", "Build auth",
            assigned_agent_id="a1"
        )
        feedbacks = [
            {"attempt": 1, "feedback": [{"dimension": "code_correctness", "score": 2}]},
            {"attempt": 2, "feedback": [{"dimension": "code_correctness", "score": 2}]},
            {"attempt": 3, "feedback": [{"dimension": "goal_achievement", "score": 1}]},
        ]

        await handle_task_failure(state, "auth-task", feedbacks, writer, cfg)

        entries = WalReader.read_all(tmp_wal_path)
        failed = [e for e in entries if e.event == WalEventType.TASK_FAILED]
        assert len(failed) == 1
        assert failed[0].payload["task_id"] == "auth-task"

    @pytest.mark.asyncio
    async def test_writes_md_written_for_bug_file(self, tmp_wal_path: Path, cfg) -> None:
        writer = WalWriter(tmp_wal_path)
        state = WalState(run_id="run-1")
        state.tasks["auth-task"] = TaskState(
            "auth-task", "dev", "Build auth", assigned_agent_id="a1"
        )

        await handle_task_failure(state, "auth-task", [{}] * 3, writer, cfg)

        entries = WalReader.read_all(tmp_wal_path)
        md_written = [e for e in entries if e.event == WalEventType.MD_WRITTEN]
        assert any(e.payload.get("is_bug_report") for e in md_written)

    @pytest.mark.asyncio
    async def test_detects_oscillation_in_same_dimension(self, tmp_wal_path: Path, cfg) -> None:
        writer = WalWriter(tmp_wal_path)
        state = WalState(run_id="run-1")
        state.tasks["t1"] = TaskState("t1", "dev", "goal", assigned_agent_id="a1")

        feedbacks = [
            {"feedback": [{"dimension": "code_correctness", "score": 2}]},
            {"feedback": [{"dimension": "code_correctness", "score": 2}]},
            {"feedback": [{"dimension": "code_correctness", "score": 1}]},
        ]

        await handle_task_failure(state, "t1", feedbacks, writer, cfg)

        entries = WalReader.read_all(tmp_wal_path)
        failed = [e for e in entries if e.event == WalEventType.TASK_FAILED]
        assert "code_correctness" in failed[0].payload.get("oscillating_dimensions", [])
