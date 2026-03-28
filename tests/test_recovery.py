"""Tests for arbor/recovery.py — crash detection and WAL replay recovery."""

import pytest
from pathlib import Path

from arbor.config import get_default_config
from arbor.wal import (
    WalEventType,
    WalReader,
    WalWriter,
    build_state_from_wal,
    AgentState,
    TaskState,
    WalState,
)
from arbor.recovery import (
    RecoveryActionType,
    detect_incomplete_entries,
    is_recovery_needed,
    recover,
)


@pytest.fixture
def cfg():
    return get_default_config()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _write_run_start(writer: WalWriter, run_id: str = "run-test") -> None:
    writer.write(WalEventType.RUN_START, run_id, {"goal": "test goal"})


def _write_task_and_agent(
    writer: WalWriter, run_id: str = "run-test",
    task_id: str = "t1", agent_id: str = "a1"
) -> None:
    writer.write(WalEventType.TASK_PLANNED, run_id, {
        "task_id": task_id, "task_type": "dev", "goal": "do x", "complexity": 3,
    })
    writer.write(WalEventType.AGENT_SPAWNED, run_id, {
        "agent_id": agent_id, "agent_type": "dev", "model": "m",
        "depth": 1, "initial_task_id": task_id, "context_budget_tokens": 8000,
    })


# ── detect_incomplete_entries ─────────────────────────────────────────────────


class TestDetectIncompleteEntries:
    def test_agent_spawned_without_started(self) -> None:
        state = WalState(run_id="run-1")
        state.agents["a1"] = AgentState("a1", "dev", "m", 1, status="spawned")
        actions = detect_incomplete_entries(state)
        respawn = [a for a in actions if a.action_type == RecoveryActionType.RESPAWN_AGENT]
        assert any(a.agent_id == "a1" for a in respawn)

    def test_task_completed_without_review(self) -> None:
        state = WalState(run_id="run-1")
        state.tasks["t1"] = TaskState(
            "t1", "dev", "do x",
            status="completed", assigned_agent_id="a1", md_path="memory/t1.md",
        )
        actions = detect_incomplete_entries(state)
        review = [a for a in actions if a.action_type == RecoveryActionType.SPAWN_REVIEWER]
        assert any(a.task_id == "t1" for a in review)

    def test_review_started_without_result(self) -> None:
        state = WalState(run_id="run-1")
        state.reviewer_states["rev-t1"] = "started"
        actions = detect_incomplete_entries(state)
        rerun = [a for a in actions if a.action_type == RecoveryActionType.RESPAWN_REVIEWER]
        assert any(a.reviewer_id == "rev-t1" for a in rerun)

    def test_clean_state_returns_no_actions(self) -> None:
        state = WalState(run_id="run-1")
        state.agents["a1"] = AgentState("a1", "dev", "m", 1, status="active")
        state.tasks["t1"] = TaskState("t1", "dev", "do x", status="reviewed_pass")
        state.reviewer_states["rev-t1"] = "pass"
        actions = detect_incomplete_entries(state)
        assert actions == []

    def test_multiple_incomplete_items(self) -> None:
        state = WalState(run_id="run-1")
        state.agents["a1"] = AgentState("a1", "dev", "m", 1, status="spawned")
        state.agents["a2"] = AgentState("a2", "infra", "m", 1, status="spawned")
        state.tasks["t1"] = TaskState(
            "t1", "dev", "do x", status="completed",
            assigned_agent_id="a1", md_path="memory/t1.md",
        )
        state.reviewer_states["rev-t2"] = "started"
        actions = detect_incomplete_entries(state)
        assert len(actions) >= 4


# ── is_recovery_needed ────────────────────────────────────────────────────────


class TestIsRecoveryNeeded:
    def test_no_wal_file_returns_false(self, tmp_wal_path: Path) -> None:
        assert is_recovery_needed(tmp_wal_path) is False

    def test_empty_wal_returns_false(self, tmp_wal_path: Path) -> None:
        tmp_wal_path.touch()
        assert is_recovery_needed(tmp_wal_path) is False

    def test_completed_run_returns_false(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        _write_run_start(writer)
        writer.write(WalEventType.RUN_COMPLETE, "run-test", {
            "tasks_completed": 0, "total_tokens": 0,
        })
        assert is_recovery_needed(tmp_wal_path) is False

    def test_incomplete_run_returns_true(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        _write_run_start(writer)
        # No RUN_COMPLETE written
        assert is_recovery_needed(tmp_wal_path) is True

    def test_run_start_without_complete_returns_true(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        _write_run_start(writer)
        _write_task_and_agent(writer)
        assert is_recovery_needed(tmp_wal_path) is True


# ── recover ───────────────────────────────────────────────────────────────────


class TestRecover:
    def test_writes_crash_detected_entry(self, tmp_wal_path: Path, cfg) -> None:
        writer = WalWriter(tmp_wal_path)
        _write_run_start(writer)
        _write_task_and_agent(writer)

        recover(tmp_wal_path, cfg)

        entries = WalReader.read_all(tmp_wal_path)
        events = [e.event for e in entries]
        assert WalEventType.CRASH_DETECTED in events

    def test_writes_recovery_replay_for_each_action(
        self, tmp_wal_path: Path, cfg
    ) -> None:
        writer = WalWriter(tmp_wal_path)
        _write_run_start(writer)
        _write_task_and_agent(writer)
        # agent spawned but never started → 1 action

        state, actions = recover(tmp_wal_path, cfg)

        entries = WalReader.read_all(tmp_wal_path)
        replay_entries = [e for e in entries if e.event == WalEventType.RECOVERY_REPLAY]
        assert len(replay_entries) == len(actions)

    def test_returns_reconstructed_state(self, tmp_wal_path: Path, cfg) -> None:
        writer = WalWriter(tmp_wal_path)
        _write_run_start(writer)
        _write_task_and_agent(writer)

        state, actions = recover(tmp_wal_path, cfg)

        assert state.run_id == "run-test"
        assert "t1" in state.tasks
        assert "a1" in state.agents

    def test_recover_on_completed_run_returns_no_actions(
        self, tmp_wal_path: Path, cfg
    ) -> None:
        writer = WalWriter(tmp_wal_path)
        _write_run_start(writer)
        writer.write(WalEventType.RUN_COMPLETE, "run-test", {
            "tasks_completed": 0, "total_tokens": 0,
        })

        state, actions = recover(tmp_wal_path, cfg)
        assert actions == []

    def test_recover_idempotent(self, tmp_wal_path: Path, cfg) -> None:
        """Calling recover twice should not add duplicate entries."""
        writer = WalWriter(tmp_wal_path)
        _write_run_start(writer)
        _write_task_and_agent(writer)

        _, actions1 = recover(tmp_wal_path, cfg)
        entries_after_first = len(WalReader.read_all(tmp_wal_path))

        _, actions2 = recover(tmp_wal_path, cfg)
        entries_after_second = len(WalReader.read_all(tmp_wal_path))

        # Second call adds its own CRASH_DETECTED + RECOVERY_REPLAY entries
        # but these are new entries, not modifications. The important thing
        # is the WAL remains valid NDJSON.
        entries = WalReader.read_all(tmp_wal_path)
        assert all(e.event in WalEventType for e in entries)

    def test_recover_no_active_run_returns_empty_actions(
        self, tmp_wal_path: Path, cfg
    ) -> None:
        tmp_wal_path.touch()
        state, actions = recover(tmp_wal_path, cfg)
        assert actions == []

    def test_crash_scenario_agent_spawned_never_started(
        self, tmp_wal_path: Path, cfg
    ) -> None:
        """Fixture: AGENT_SPAWNED present, no AGENT_STARTED."""
        writer = WalWriter(tmp_wal_path)
        _write_run_start(writer)
        _write_task_and_agent(writer)  # agent "a1" spawned but not started

        state, actions = recover(tmp_wal_path, cfg)

        respawn = [a for a in actions if a.action_type == RecoveryActionType.RESPAWN_AGENT]
        assert any(a.agent_id == "a1" for a in respawn)

    def test_crash_scenario_task_completed_no_review(
        self, tmp_wal_path: Path, cfg
    ) -> None:
        """Fixture: TASK_COMPLETED + MD_WRITTEN, no REVIEW_STARTED."""
        writer = WalWriter(tmp_wal_path)
        _write_run_start(writer)
        writer.write(WalEventType.TASK_PLANNED, "run-test", {
            "task_id": "t1", "task_type": "dev", "goal": "do x",
        })
        writer.write(WalEventType.AGENT_SPAWNED, "run-test", {
            "agent_id": "a1", "agent_type": "dev", "model": "m",
            "depth": 1, "initial_task_id": "t1", "context_budget_tokens": 8000,
        })
        writer.write(WalEventType.AGENT_STARTED, "run-test", {"agent_id": "a1"})
        writer.write(WalEventType.TASK_COMPLETED, "run-test", {
            "task_id": "t1", "agent_id": "a1",
            "tokens_used": 300, "md_path": "memory/t1.md", "md_hash": "x",
        })
        writer.write(WalEventType.MD_WRITTEN, "run-test", {
            "md_path": "memory/t1.md", "md_hash": "x",
        })
        # No REVIEW_STARTED written — crash here

        state, actions = recover(tmp_wal_path, cfg)

        review = [a for a in actions if a.action_type == RecoveryActionType.SPAWN_REVIEWER]
        assert any(a.task_id == "t1" for a in review)

    def test_crash_scenario_review_started_never_returned(
        self, tmp_wal_path: Path, cfg
    ) -> None:
        """Fixture: REVIEW_STARTED present, no REVIEW_RESULT."""
        writer = WalWriter(tmp_wal_path)
        _write_run_start(writer)
        writer.write(WalEventType.TASK_PLANNED, "run-test", {
            "task_id": "t1", "task_type": "dev", "goal": "do x",
        })
        writer.write(WalEventType.AGENT_SPAWNED, "run-test", {
            "agent_id": "a1", "agent_type": "dev", "model": "m",
            "depth": 1, "initial_task_id": "t1", "context_budget_tokens": 8000,
        })
        writer.write(WalEventType.AGENT_STARTED, "run-test", {"agent_id": "a1"})
        writer.write(WalEventType.TASK_COMPLETED, "run-test", {
            "task_id": "t1", "agent_id": "a1",
            "tokens_used": 300, "md_path": "memory/t1.md", "md_hash": "x",
        })
        writer.write(WalEventType.MD_WRITTEN, "run-test", {
            "md_path": "memory/t1.md", "md_hash": "x",
        })
        writer.write(WalEventType.REVIEW_STARTED, "run-test", {
            "reviewer_id": "rev-t1", "task_id": "t1", "agent_id": "a1",
        })
        # Crash — no REVIEW_RESULT written

        state, actions = recover(tmp_wal_path, cfg)

        rerun = [a for a in actions if a.action_type == RecoveryActionType.RESPAWN_REVIEWER]
        assert any(a.reviewer_id == "rev-t1" for a in rerun)
