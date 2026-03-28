"""Shared pytest fixtures for Arbor v2 tests."""

import json
import pytest
from pathlib import Path

from arbor.config import ArborConfig, get_default_config
from arbor.wal import (
    WalEntry,
    WalEventType,
    WalState,
    WalWriter,
    AgentState,
    TaskState,
    build_state_from_wal,
)


@pytest.fixture
def tmp_wal_path(tmp_path: Path) -> Path:
    """Return a path to a fresh (empty) WAL file in a temp directory."""
    wal_dir = tmp_path / "arbor-run"
    wal_dir.mkdir()
    return wal_dir / "wal.ndjson"


@pytest.fixture
def default_config() -> ArborConfig:
    """Return a default ArborConfig."""
    return get_default_config()


@pytest.fixture
def wal_writer(tmp_wal_path: Path) -> WalWriter:
    """Return a WalWriter pointed at a fresh temp WAL."""
    return WalWriter(tmp_wal_path)


@pytest.fixture
def sample_wal_state() -> WalState:
    """Return a pre-built WalState with one agent and two tasks."""
    state = WalState(run_id="run-test01", goal="Build a test API")
    state.agents["agent-dev-1-001"] = AgentState(
        agent_id="agent-dev-1-001",
        agent_type="dev",
        model="claude-sonnet-4-6",
        depth=1,
        status="active",
        context_budget=8000,
        tokens_used=1200,
        tasks=["task-auth", "task-api"],
        completed_tasks=["task-auth"],
    )
    state.tasks["task-auth"] = TaskState(
        task_id="task-auth",
        task_type="dev",
        goal="Implement JWT auth",
        status="reviewed_pass",
        assigned_agent_id="agent-dev-1-001",
        review_attempts=1,
        review_result="pass",
        md_path="memory/auth/jwt-auth.md",
        md_hash="sha256:abc123",
    )
    state.tasks["task-api"] = TaskState(
        task_id="task-api",
        task_type="dev",
        goal="Build REST API endpoints",
        status="planned",
        assigned_agent_id=None,
    )
    state.md_files["memory/auth/jwt-auth.md"] = "w-0003"
    state.task_completion_count = 1
    return state


@pytest.fixture
def run_start_entries(tmp_wal_path: Path) -> list[WalEntry]:
    """Write a minimal RUN_START + TASK_PLANNED WAL and return all entries."""
    writer = WalWriter(tmp_wal_path)
    e1 = writer.write(
        WalEventType.RUN_START,
        run_id="run-abc",
        payload={"goal": "test goal"},
    )
    e2 = writer.write(
        WalEventType.TASK_PLANNED,
        run_id="run-abc",
        payload={
            "task_id": "task-001",
            "task_type": "dev",
            "goal": "Do something",
            "complexity": 3,
        },
    )
    return [e1, e2]
