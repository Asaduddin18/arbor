"""Tests for arbor/scheduler.py — state reconstruction and action determination."""

import pytest
from pathlib import Path

from arbor.config import ArborConfig, get_default_config
from arbor.wal import (
    WalEventType,
    WalReader,
    WalState,
    WalWriter,
    AgentState,
    TaskState,
    build_state_from_wal,
)
from arbor.scheduler import (
    Scheduler,
    SchedulerAction,
    SchedulerStep,
    determine_next_actions,
    _find_absorb_candidate,
    _all_tasks_complete,
)


@pytest.fixture
def cfg() -> ArborConfig:
    return get_default_config()


# ── determine_next_actions ────────────────────────────────────────────────────


class TestDetermineNextActions:
    def test_empty_state_returns_no_actions(self, cfg: ArborConfig) -> None:
        state = WalState()
        steps = determine_next_actions(state, cfg)
        assert steps == []

    def test_run_start_no_tasks_returns_plan_tasks(self, cfg: ArborConfig) -> None:
        state = WalState(run_id="run-1", goal="Build API")
        steps = determine_next_actions(state, cfg)
        assert any(s.action == SchedulerAction.PLAN_TASKS for s in steps)

    def test_complete_run_returns_no_actions(self, cfg: ArborConfig) -> None:
        state = WalState(run_id="run-1", is_complete=True)
        steps = determine_next_actions(state, cfg)
        assert steps == []

    def test_spawned_agent_without_started_triggers_respawn(self, cfg: ArborConfig) -> None:
        state = WalState(run_id="run-1")
        state.tasks["t1"] = TaskState("t1", "dev", "do x", status="assigned", assigned_agent_id="a1")
        state.agents["a1"] = AgentState("a1", "dev", "model", 1, status="spawned")
        steps = determine_next_actions(state, cfg)
        respawn = [s for s in steps if s.action == SchedulerAction.SPAWN_AGENT and s.agent_id == "a1"]
        assert respawn, "Should re-spawn agent that never started"

    def test_task_completed_with_md_triggers_reviewer(self, cfg: ArborConfig) -> None:
        state = WalState(run_id="run-1")
        state.tasks["t1"] = TaskState(
            "t1", "dev", "do x",
            status="completed",
            assigned_agent_id="a1",
            md_path="memory/t1.md",
        )
        state.agents["a1"] = AgentState("a1", "dev", "model", 1, status="active")
        steps = determine_next_actions(state, cfg)
        reviewer_steps = [s for s in steps if s.action == SchedulerAction.SPAWN_REVIEWER]
        assert any(s.task_id == "t1" for s in reviewer_steps)

    def test_review_fail_below_max_triggers_retry(self, cfg: ArborConfig) -> None:
        state = WalState(run_id="run-1")
        state.tasks["t1"] = TaskState(
            "t1", "dev", "do x",
            status="reviewed_fail",
            assigned_agent_id="a1",
            review_attempts=1,  # below max of 3
        )
        state.agents["a1"] = AgentState("a1", "dev", "model", 1, status="active")
        steps = determine_next_actions(state, cfg)
        retry = [s for s in steps if s.action == SchedulerAction.ASSIGN_TASK and s.task_id == "t1"]
        assert retry, "Should retry task after single review fail"

    def test_review_fail_at_max_no_retry(self, cfg: ArborConfig) -> None:
        state = WalState(run_id="run-1")
        state.tasks["t1"] = TaskState(
            "t1", "dev", "do x",
            status="failed",  # already failed — scheduler wrote TASK_FAILED
            assigned_agent_id="a1",
            review_attempts=3,
        )
        state.agents["a1"] = AgentState("a1", "dev", "model", 1, status="active")
        steps = determine_next_actions(state, cfg)
        retry = [s for s in steps if s.action == SchedulerAction.ASSIGN_TASK and s.task_id == "t1"]
        assert not retry

    def test_planned_task_no_agent_triggers_spawn(self, cfg: ArborConfig) -> None:
        state = WalState(run_id="run-1")
        state.tasks["t1"] = TaskState("t1", "dev", "do x", status="planned")
        steps = determine_next_actions(state, cfg)
        spawn = [s for s in steps if s.action == SchedulerAction.SPAWN_AGENT and s.task_id == "t1"]
        assert spawn

    def test_planned_task_with_eligible_agent_triggers_assign(self, cfg: ArborConfig) -> None:
        state = WalState(run_id="run-1")
        state.tasks["t1"] = TaskState("t1", "dev", "do x", status="planned")
        state.agents["a1"] = AgentState(
            "a1", "dev", "model", 1, status="active",
            context_budget=8000, tokens_used=1000,  # < 60%
        )
        steps = determine_next_actions(state, cfg)
        assign = [s for s in steps if s.action == SchedulerAction.ASSIGN_TASK and s.task_id == "t1"]
        assert assign

    def test_audit_trigger_after_n_completions(self, cfg: ArborConfig) -> None:
        state = WalState(run_id="run-1")
        state.task_completion_count = cfg.audit_every_n_tasks
        state.last_audit_at_count = 0
        # Need at least one non-complete task so we don't hit MARK_COMPLETE
        state.tasks["t1"] = TaskState("t1", "dev", "do x", status="assigned")
        steps = determine_next_actions(state, cfg)
        audit = [s for s in steps if s.action == SchedulerAction.SPAWN_AUDIT]
        assert audit

    def test_no_audit_if_already_running(self, cfg: ArborConfig) -> None:
        state = WalState(run_id="run-1")
        state.task_completion_count = cfg.audit_every_n_tasks
        state.last_audit_at_count = 0
        state.agents["audit-1"] = AgentState("audit-1", "audit", "m", 0, status="active")
        state.tasks["t1"] = TaskState("t1", "dev", "do x", status="assigned")
        steps = determine_next_actions(state, cfg)
        audit = [s for s in steps if s.action == SchedulerAction.SPAWN_AUDIT]
        assert not audit

    def test_all_tasks_pass_triggers_mark_complete(self, cfg: ArborConfig) -> None:
        state = WalState(run_id="run-1")
        state.tasks["t1"] = TaskState("t1", "dev", "do x", status="reviewed_pass")
        state.tasks["t2"] = TaskState("t2", "dev", "do y", status="reviewed_pass")
        steps = determine_next_actions(state, cfg)
        complete = [s for s in steps if s.action == SchedulerAction.MARK_COMPLETE]
        assert complete

    def test_mixed_tasks_does_not_trigger_complete(self, cfg: ArborConfig) -> None:
        state = WalState(run_id="run-1")
        state.tasks["t1"] = TaskState("t1", "dev", "do x", status="reviewed_pass")
        state.tasks["t2"] = TaskState("t2", "dev", "do y", status="planned")
        steps = determine_next_actions(state, cfg)
        complete = [s for s in steps if s.action == SchedulerAction.MARK_COMPLETE]
        assert not complete


# ── _find_absorb_candidate ────────────────────────────────────────────────────


class TestFindAbsorbCandidate:
    def test_returns_eligible_agent(self, cfg: ArborConfig) -> None:
        task = TaskState("t1", "dev", "do x", status="planned")
        state = WalState(run_id="run-1")
        state.agents["a1"] = AgentState(
            "a1", "dev", "model", 1, status="active",
            context_budget=8000, tokens_used=1000,
        )
        result = _find_absorb_candidate(task, state, cfg)
        assert result is not None
        assert result.agent_id == "a1"

    def test_returns_none_when_budget_exceeded(self, cfg: ArborConfig) -> None:
        task = TaskState("t1", "dev", "do x", status="planned")
        state = WalState(run_id="run-1")
        state.agents["a1"] = AgentState(
            "a1", "dev", "model", 1, status="active",
            context_budget=8000, tokens_used=5000,  # 62.5% > 60%
        )
        result = _find_absorb_candidate(task, state, cfg)
        assert result is None

    def test_returns_none_wrong_type(self, cfg: ArborConfig) -> None:
        task = TaskState("t1", "infra", "do x", status="planned")
        state = WalState(run_id="run-1")
        state.agents["a1"] = AgentState(
            "a1", "dev", "model", 1, status="active",
            context_budget=8000, tokens_used=100,
        )
        result = _find_absorb_candidate(task, state, cfg)
        assert result is None

    def test_returns_none_when_agent_spawned_not_active(self, cfg: ArborConfig) -> None:
        task = TaskState("t1", "dev", "do x", status="planned")
        state = WalState(run_id="run-1")
        state.agents["a1"] = AgentState(
            "a1", "dev", "model", 1, status="spawned",
            context_budget=8000, tokens_used=100,
        )
        result = _find_absorb_candidate(task, state, cfg)
        assert result is None


# ── Scheduler integration ─────────────────────────────────────────────────────


class TestSchedulerIntegration:
    @pytest.mark.asyncio
    async def test_start_run_writes_run_start(self, tmp_wal_path: Path, cfg: ArborConfig) -> None:
        scheduler = Scheduler(wal_path=tmp_wal_path, config=cfg)
        run_id = scheduler.start_run("Build an API")
        entries = WalReader.read_all(tmp_wal_path)
        assert len(entries) == 1
        assert entries[0].event == WalEventType.RUN_START
        assert entries[0].run_id == run_id
        assert entries[0].payload["goal"] == "Build an API"

    @pytest.mark.asyncio
    async def test_step_with_no_tasks_calls_orchestrator(
        self, tmp_wal_path: Path, cfg: ArborConfig
    ) -> None:
        called = []

        async def mock_orchestrator(goal, state, writer):
            called.append(goal)

        scheduler = Scheduler(
            wal_path=tmp_wal_path, config=cfg, orchestrator_fn=mock_orchestrator
        )
        scheduler.start_run("Build something")
        await scheduler.step()
        assert len(called) == 1
        assert called[0] == "Build something"

    @pytest.mark.asyncio
    async def test_run_writes_run_complete_when_all_tasks_pass(
        self, tmp_wal_path: Path, cfg: ArborConfig
    ) -> None:
        """Scheduler marks run complete when orchestrator produces a pre-reviewed task."""

        async def mock_orchestrator(goal, state, writer):
            # Write a task and immediately mark it as reviewed_pass
            writer.write(
                WalEventType.TASK_PLANNED,
                run_id=state.run_id,
                payload={"task_id": "t1", "task_type": "dev", "goal": "do x", "complexity": 1},
            )
            # Also simulate task already completed + reviewed for simplicity
            writer.write(WalEventType.AGENT_SPAWNED, state.run_id, {
                "agent_id": "a1", "agent_type": "dev", "model": "m",
                "depth": 1, "initial_task_id": "t1", "context_budget_tokens": 8000,
            })
            writer.write(WalEventType.AGENT_STARTED, state.run_id, {"agent_id": "a1"})
            writer.write(WalEventType.TASK_COMPLETED, state.run_id, {
                "task_id": "t1", "agent_id": "a1",
                "tokens_used": 100, "md_path": "memory/t1.md", "md_hash": "x",
            })
            writer.write(WalEventType.MD_WRITTEN, state.run_id, {
                "md_path": "memory/t1.md", "md_hash": "x",
            })
            writer.write(WalEventType.REVIEW_STARTED, state.run_id, {
                "reviewer_id": "rev-t1", "task_id": "t1", "agent_id": "a1",
            })
            writer.write(WalEventType.REVIEW_RESULT, state.run_id, {
                "reviewer_id": "rev-t1", "task_id": "t1",
                "result": "pass", "attempt": 1, "scores": {},
            })

        scheduler = Scheduler(
            wal_path=tmp_wal_path, config=cfg, orchestrator_fn=mock_orchestrator
        )
        await scheduler.run("Build something", max_iterations=10)

        entries = WalReader.read_all(tmp_wal_path)
        events = [e.event for e in entries]
        assert WalEventType.RUN_COMPLETE in events

    @pytest.mark.asyncio
    async def test_step_returns_false_when_complete(
        self, tmp_wal_path: Path, cfg: ArborConfig
    ) -> None:
        writer = WalWriter(tmp_wal_path)
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "g"})
        writer.write(WalEventType.RUN_COMPLETE, "run-1", {"tasks_completed": 0, "total_tokens": 0})

        scheduler = Scheduler(wal_path=tmp_wal_path, config=cfg)
        result = await scheduler.step()
        assert result is False
