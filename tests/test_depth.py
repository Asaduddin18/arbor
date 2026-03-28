"""Tests for Phase 4 — absorption check, depth decision tree, depth minimization."""

import pytest
from pathlib import Path

from arbor.config import get_default_config, ArborConfig
from arbor.wal import WalState, AgentState, TaskState
from arbor.orchestrator import should_absorb, decide_spawn_depth, _build_agent_id
from arbor.scheduler import determine_next_actions, SchedulerAction


@pytest.fixture
def cfg():
    return get_default_config()


# ── Absorption check ──────────────────────────────────────────────────────────


class TestAbsorptionCheck:
    def test_absorbs_same_type_active_agent_below_budget(self, cfg) -> None:
        task = TaskState("t1", "dev", "do x")
        state = WalState(run_id="r")
        state.agents["a1"] = AgentState("a1", "dev", "m", 1, status="active",
                                         context_budget=8000, tokens_used=3000)
        assert should_absorb(task, state.agents, cfg) is not None

    def test_no_absorption_when_no_active_agents(self, cfg) -> None:
        task = TaskState("t1", "dev", "do x")
        state = WalState(run_id="r")
        assert should_absorb(task, state.agents, cfg) is None

    def test_no_absorption_different_type(self, cfg) -> None:
        task = TaskState("t1", "infra", "deploy")
        state = WalState(run_id="r")
        state.agents["a1"] = AgentState("a1", "dev", "m", 1, status="active",
                                         context_budget=8000, tokens_used=100)
        assert should_absorb(task, state.agents, cfg) is None

    def test_no_absorption_budget_at_60pct(self, cfg) -> None:
        """Exactly 60% used → should NOT absorb (condition is strictly < 0.6)."""
        task = TaskState("t1", "dev", "do x")
        state = WalState(run_id="r")
        state.agents["a1"] = AgentState("a1", "dev", "m", 1, status="active",
                                         context_budget=8000, tokens_used=4800)
        assert should_absorb(task, state.agents, cfg) is None

    def test_no_absorption_agent_not_started(self, cfg) -> None:
        task = TaskState("t1", "dev", "do x")
        state = WalState(run_id="r")
        state.agents["a1"] = AgentState("a1", "dev", "m", 1, status="spawned",
                                         context_budget=8000, tokens_used=100)
        assert should_absorb(task, state.agents, cfg) is None

    def test_absorbs_started_status(self, cfg) -> None:
        task = TaskState("t1", "dev", "do x")
        state = WalState(run_id="r")
        state.agents["a1"] = AgentState("a1", "dev", "m", 1, status="started",
                                         context_budget=8000, tokens_used=100)
        assert should_absorb(task, state.agents, cfg) is not None

    def test_no_absorption_deep_agent(self, cfg) -> None:
        """Agents at depth > 1 should not absorb top-level tasks."""
        task = TaskState("t1", "dev", "do x")
        state = WalState(run_id="r")
        state.agents["a1"] = AgentState("a1", "dev", "m", 2, status="active",
                                         context_budget=8000, tokens_used=100)
        assert should_absorb(task, state.agents, cfg) is None


# ── Depth decision tree ───────────────────────────────────────────────────────


class TestDepthDecisionTree:
    def test_independent_task_depth1(self) -> None:
        state = WalState(run_id="r")
        task = TaskState("t1", "dev", "do x")
        assert decide_spawn_depth(task, None, state) == 1

    def test_subproblem_depth_increases(self) -> None:
        state = WalState(run_id="r")
        state.agents["a1"] = AgentState("a1", "dev", "m", 1, status="active")
        state.tasks["parent"] = TaskState("parent", "dev", "parent task", assigned_agent_id="a1")
        task = TaskState("child", "dev", "child task")
        assert decide_spawn_depth(task, "parent", state) == 2

    def test_subproblem_of_depth2_agent_goes_to_depth3(self) -> None:
        state = WalState(run_id="r")
        state.agents["a2"] = AgentState("a2", "dev", "m", 2, status="active")
        state.tasks["parent"] = TaskState("parent", "dev", "parent", assigned_agent_id="a2")
        task = TaskState("child", "dev", "child")
        assert decide_spawn_depth(task, "parent", state) == 3

    def test_nonexistent_parent_defaults_to_depth1(self) -> None:
        state = WalState(run_id="r")
        task = TaskState("t1", "dev", "do x")
        assert decide_spawn_depth(task, "ghost-parent", state) == 1


# ── Depth only increases when justified ──────────────────────────────────────


class TestDepthMinimization:
    def test_scheduler_prefers_assign_over_spawn(self, cfg) -> None:
        """Scheduler should assign to existing eligible agent, not spawn new."""
        state = WalState(run_id="r")
        state.tasks["t1"] = TaskState("t1", "dev", "do x", status="planned")
        state.agents["a1"] = AgentState("a1", "dev", "m", 1, status="active",
                                         context_budget=8000, tokens_used=100)
        steps = determine_next_actions(state, cfg)
        spawn = [s for s in steps if s.action == SchedulerAction.SPAWN_AGENT]
        assign = [s for s in steps if s.action == SchedulerAction.ASSIGN_TASK and s.task_id == "t1"]
        assert assign  # should assign to existing
        assert not spawn  # should NOT spawn new

    def test_scheduler_spawns_when_no_eligible_agent(self, cfg) -> None:
        state = WalState(run_id="r")
        state.tasks["t1"] = TaskState("t1", "dev", "do x", status="planned")
        # No agents
        steps = determine_next_actions(state, cfg)
        spawn = [s for s in steps if s.action == SchedulerAction.SPAWN_AGENT and s.task_id == "t1"]
        assert spawn

    def test_agent_id_format(self) -> None:
        state = WalState(run_id="r")
        agent_id = _build_agent_id("dev", 1, state)
        assert agent_id == "agent-dev-1-001"

    def test_agent_id_increments(self) -> None:
        state = WalState(run_id="r")
        state.agents["agent-dev-1-001"] = AgentState("agent-dev-1-001", "dev", "m", 1)
        agent_id = _build_agent_id("dev", 1, state)
        assert agent_id == "agent-dev-1-002"

    def test_different_types_get_independent_sequences(self) -> None:
        state = WalState(run_id="r")
        state.agents["agent-dev-1-001"] = AgentState("agent-dev-1-001", "dev", "m", 1)
        # infra starts its own sequence
        infra_id = _build_agent_id("infra", 1, state)
        assert infra_id == "agent-infra-1-001"

    def test_scheduler_multiple_independent_tasks_spawn_separately(self, cfg) -> None:
        """Multiple independent tasks of different types each get their own spawn step."""
        state = WalState(run_id="r")
        state.tasks["dev-task"] = TaskState("dev-task", "dev", "build", status="planned")
        state.tasks["infra-task"] = TaskState("infra-task", "infra", "deploy", status="planned")
        steps = determine_next_actions(state, cfg)
        spawn_tasks = {s.task_id for s in steps if s.action == SchedulerAction.SPAWN_AGENT}
        assert "dev-task" in spawn_tasks
        assert "infra-task" in spawn_tasks
