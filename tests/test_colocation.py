"""Tests for Phase 4 — chain colocation, handoff trigger conditions."""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from arbor.config import get_default_config
from arbor.wal import (
    WalEventType, WalReader, WalWriter, WalState,
    AgentState, TaskState, build_state_from_wal,
)
from arbor.orchestrator import decompose_goal, assign_next_task


@pytest.fixture
def cfg():
    return get_default_config()


def _make_mock_client(json_response: str) -> MagicMock:
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=json_response)]
    mock_msg.usage = MagicMock(input_tokens=100, output_tokens=50)
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)
    return mock_client


# ── Chain identification at planning time ─────────────────────────────────────


class TestChainColocation:
    @pytest.mark.asyncio
    async def test_chain_tasks_assigned_to_single_agent(
        self, tmp_wal_path: Path, cfg
    ) -> None:
        """Three tasks in a chain should produce a single AGENT_SPAWNED entry."""
        decomp = json.dumps({
            "tasks": [
                {"task_id": "db-schema", "task_type": "dev", "goal": "schema",
                 "complexity": 3, "chain_id": "auth-chain", "dependencies": []},
                {"task_id": "user-model", "task_type": "dev", "goal": "model",
                 "complexity": 3, "chain_id": "auth-chain", "dependencies": ["db-schema"]},
                {"task_id": "auth-routes", "task_type": "dev", "goal": "routes",
                 "complexity": 4, "chain_id": "auth-chain", "dependencies": ["user-model"]},
            ],
            "chains": [
                {"chain_id": "auth-chain",
                 "tasks": ["db-schema", "user-model", "auth-routes"],
                 "agent_type": "dev", "estimated_tokens": 14000,
                 "colocation": "single-agent"}
            ],
            "cross_chain_dependencies": [],
        })
        writer = WalWriter(tmp_wal_path)
        state = WalState(run_id="run-1", goal="Build auth")
        await decompose_goal("Build auth", state, writer, cfg, client=_make_mock_client(decomp))

        entries = WalReader.read_all(tmp_wal_path)
        spawned = [e for e in entries if e.event == WalEventType.AGENT_SPAWNED]
        assert len(spawned) == 1
        assert spawned[0].payload["chain_id"] == "auth-chain"
        assert spawned[0].payload["chain_tasks"] == ["db-schema", "user-model", "auth-routes"]

    @pytest.mark.asyncio
    async def test_independent_tasks_get_separate_agents(
        self, tmp_wal_path: Path, cfg
    ) -> None:
        """Two independent tasks (no chain) should be spawned separately."""
        decomp = json.dumps({
            "tasks": [
                {"task_id": "auth-task", "task_type": "dev", "goal": "auth",
                 "complexity": 3, "chain_id": None, "dependencies": []},
                {"task_id": "infra-task", "task_type": "infra", "goal": "docker",
                 "complexity": 3, "chain_id": None, "dependencies": []},
            ],
            "chains": [],
            "cross_chain_dependencies": [],
        })
        writer = WalWriter(tmp_wal_path)
        state = WalState(run_id="run-1", goal="Build system")
        await decompose_goal("Build system", state, writer, cfg, client=_make_mock_client(decomp))

        entries = WalReader.read_all(tmp_wal_path)
        planned = [e for e in entries if e.event == WalEventType.TASK_PLANNED]
        # No chains → no pre-spawned agents, tasks stay planned for scheduler
        assert len(planned) == 2

    @pytest.mark.asyncio
    async def test_chain_continuation_assigns_to_same_agent(
        self, tmp_wal_path: Path, cfg
    ) -> None:
        """After a task completes, next chain task should go to same agent."""
        writer = WalWriter(tmp_wal_path)
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "test"})
        writer.write(WalEventType.TASK_PLANNED, "run-1", {
            "task_id": "t1", "task_type": "dev", "goal": "step 1",
            "chain_id": "chain-A", "dependencies": [],
        })
        writer.write(WalEventType.TASK_PLANNED, "run-1", {
            "task_id": "t2", "task_type": "dev", "goal": "step 2",
            "chain_id": "chain-A", "dependencies": ["t1"],
        })
        writer.write(WalEventType.AGENT_SPAWNED, "run-1", {
            "agent_id": "agent-dev-1-001", "agent_type": "dev", "model": "m",
            "depth": 1, "initial_task_id": "t1", "chain_id": "chain-A",
            "chain_tasks": ["t1", "t2"], "context_budget_tokens": 8000,
        })
        writer.write(WalEventType.AGENT_STARTED, "run-1", {"agent_id": "agent-dev-1-001"})
        writer.write(WalEventType.TASK_COMPLETED, "run-1", {
            "task_id": "t1", "agent_id": "agent-dev-1-001",
            "tokens_used": 500, "md_path": "memory/t1.md", "md_hash": "x",
        })
        writer.write(WalEventType.MD_WRITTEN, "run-1", {"md_path": "memory/t1.md", "md_hash": "x"})
        writer.write(WalEventType.REVIEW_STARTED, "run-1", {
            "reviewer_id": "rev-t1", "task_id": "t1", "agent_id": "agent-dev-1-001",
        })
        writer.write(WalEventType.REVIEW_RESULT, "run-1", {
            "reviewer_id": "rev-t1", "task_id": "t1",
            "result": "pass", "attempt": 1, "scores": {},
        })

        state = build_state_from_wal(WalReader.read_all(tmp_wal_path))
        await assign_next_task(state, "t1", writer, cfg)

        entries = WalReader.read_all(tmp_wal_path)
        assigned = [e for e in entries if e.event == WalEventType.TASK_ASSIGNED]
        assert len(assigned) == 1
        assert assigned[0].payload["task_id"] == "t2"
        assert assigned[0].payload["agent_id"] == "agent-dev-1-001"


# ── Handoff trigger conditions ────────────────────────────────────────────────


class TestHandoffTriggers:
    @pytest.mark.asyncio
    async def test_handoff_written_when_context_exceeds_60pct(
        self, tmp_wal_path: Path, cfg
    ) -> None:
        """When agent context > 60%, handoff MD should be written."""
        from arbor.agents.dev import DevAgent
        from arbor.memory.tree import MemoryTree

        tree = MemoryTree(tmp_path_for_tree(tmp_wal_path))

        # Create a mock LLM response
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="## Goal\ntest\n## Approach\ntest\n## Output\ntest\n## Handoff notes\nnone")]
        mock_response.usage = MagicMock(
            # high token count to exceed 60% of default 8000 budget
            input_tokens=2500, output_tokens=2500
        )
        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        agent = DevAgent("agent-dev-1-001", 1, cfg, tree, client=mock_client)
        writer = WalWriter(tmp_wal_path)

        task = {"task_id": "auth-task", "goal": "Build auth", "context_files": []}
        await agent.run(task, writer, "run-1")

        entries = WalReader.read_all(tmp_wal_path)
        handoff_entries = [e for e in entries if e.event == WalEventType.HANDOFF_WRITTEN]
        assert len(handoff_entries) == 1
        assert handoff_entries[0].payload["agent_id"] == "agent-dev-1-001"

    @pytest.mark.asyncio
    async def test_no_handoff_when_context_below_60pct(
        self, tmp_wal_path: Path, cfg
    ) -> None:
        """When agent context < 60%, no handoff should be written."""
        from arbor.agents.dev import DevAgent
        from arbor.memory.tree import MemoryTree

        tree = MemoryTree(tmp_path_for_tree(tmp_wal_path))

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="## Goal\ntest\n## Approach\ntest\n## Output\ncode\n## Handoff notes\nnone")]
        mock_response.usage = MagicMock(input_tokens=500, output_tokens=500)  # 12.5%
        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        agent = DevAgent("agent-dev-1-001", 1, cfg, tree, client=mock_client)
        writer = WalWriter(tmp_wal_path)

        task = {"task_id": "auth-task", "goal": "Build auth", "context_files": []}
        await agent.run(task, writer, "run-1")

        entries = WalReader.read_all(tmp_wal_path)
        handoff_entries = [e for e in entries if e.event == WalEventType.HANDOFF_WRITTEN]
        assert len(handoff_entries) == 0

    def test_handoff_md_contains_required_sections(self, cfg) -> None:
        """Handoff MD must contain all required sections."""
        from arbor.agents.dev import DevAgent
        from arbor.memory.tree import MemoryTree

        tree_path = Path("memory_test_tmp")
        tree = MemoryTree(tree_path)
        agent = DevAgent("agent-dev-1-001", 1, cfg, tree)
        agent._completed_tasks = ["task-a", "task-b"]
        agent.tokens_used = 5000

        content = agent.generate_handoff_md("task-b")
        assert "## Completed Tasks" in content
        assert "## Key Decisions" in content
        assert "## Active State" in content
        assert "## What Receiving Agent Must Do" in content
        assert "## Files to Read" in content
        assert "task-a" in content
        assert "task-b" in content

        # Clean up
        import shutil
        if tree_path.exists():
            shutil.rmtree(tree_path)


def tmp_path_for_tree(wal_path: Path) -> Path:
    """Create a temp memory directory beside the WAL."""
    tree_path = wal_path.parent.parent / "memory"
    tree_path.mkdir(exist_ok=True)
    return tree_path
