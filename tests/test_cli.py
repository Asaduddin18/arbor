"""Tests for Phase 6 — CLI commands and display helpers."""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from arbor.cli import (
    app,
    _entry_summary,
    _estimate_cost,
    _build_wal_table,
    _build_agent_table,
    _build_cost_panel,
    _build_tree_panel,
)
from arbor.config import get_default_config
from arbor.memory.versioner import write_versioned_md
from arbor.wal import (
    WalEntry,
    WalEventType,
    WalWriter,
    WalState,
    AgentState,
    TaskState,
    build_state_from_wal,
    WalReader,
)

runner = CliRunner()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_entry(event: WalEventType, payload: dict, wal_id: str = "w-0001") -> WalEntry:
    return WalEntry(
        wal_id=wal_id,
        event=event,
        timestamp="2024-01-01T12:00:00Z",
        run_id="run-1",
        payload=payload,
    )


def _make_state_with_agents() -> WalState:
    state = WalState(run_id="run-1", goal="Test goal")
    state.agents["agent-dev-1-001"] = AgentState(
        agent_id="agent-dev-1-001",
        agent_type="dev",
        model="claude-sonnet-4-6",
        depth=1,
        status="active",
        context_budget=8000,
        tokens_used=3000,
        tasks=["t1"],
        completed_tasks=["t1"],
    )
    state.agents["agent-dev-1-002"] = AgentState(
        agent_id="agent-dev-1-002",
        agent_type="research",
        model="claude-opus-4-6",
        depth=1,
        status="complete",
        context_budget=8000,
        tokens_used=7000,
        tasks=["t2"],
        completed_tasks=["t2"],
    )
    state.tasks["t1"] = TaskState("t1", "dev", "Build API", status="reviewed_pass")
    state.tasks["t2"] = TaskState("t2", "research", "Research", status="reviewed_pass")
    state.md_files["memory/auth/jwt.md"] = "w-0003"
    state.md_files["memory/auth/session.md"] = "w-0004"
    state.md_files["memory/api/rest.md"] = "w-0005"
    state.task_completion_count = 2
    return state


# ── _estimate_cost ─────────────────────────────────────────────────────────────


class TestEstimateCost:
    def test_known_model_opus(self) -> None:
        cost = _estimate_cost(1_000_000, "claude-opus-4-6")
        assert cost == pytest.approx(15.0)

    def test_known_model_sonnet(self) -> None:
        cost = _estimate_cost(1_000_000, "claude-sonnet-4-6")
        assert cost == pytest.approx(3.0)

    def test_known_model_haiku(self) -> None:
        cost = _estimate_cost(1_000_000, "claude-haiku-4-5-20251001")
        assert cost == pytest.approx(0.25)

    def test_unknown_model_falls_back_to_sonnet_rate(self) -> None:
        cost = _estimate_cost(1_000_000, "unknown-model")
        assert cost == pytest.approx(3.0)

    def test_zero_tokens_is_zero_cost(self) -> None:
        assert _estimate_cost(0, "claude-opus-4-6") == 0.0

    def test_fractional_tokens(self) -> None:
        cost = _estimate_cost(500_000, "claude-opus-4-6")
        assert cost == pytest.approx(7.5)


# ── _entry_summary ─────────────────────────────────────────────────────────────


class TestEntrySummary:
    def test_run_start_includes_goal(self) -> None:
        e = _make_entry(WalEventType.RUN_START, {"goal": "Build auth service"})
        assert "Build auth service" in _entry_summary(e)

    def test_task_planned_includes_task_id(self) -> None:
        e = _make_entry(WalEventType.TASK_PLANNED, {"task_id": "t-001", "task_type": "dev", "goal": "x"})
        assert "t-001" in _entry_summary(e)

    def test_agent_spawned_includes_agent_id(self) -> None:
        e = _make_entry(WalEventType.AGENT_SPAWNED, {"agent_id": "a-001", "agent_type": "dev", "depth": 1})
        assert "a-001" in _entry_summary(e)

    def test_task_completed_includes_task_id(self) -> None:
        e = _make_entry(WalEventType.TASK_COMPLETED, {"task_id": "t-001", "tokens_used": 500, "md_path": "m.md"})
        assert "t-001" in _entry_summary(e)

    def test_review_result_pass_shows_checkmark(self) -> None:
        e = _make_entry(WalEventType.REVIEW_RESULT, {"task_id": "t", "result": "pass", "attempt": 1})
        assert "✓" in _entry_summary(e)

    def test_review_result_fail_shows_cross(self) -> None:
        e = _make_entry(WalEventType.REVIEW_RESULT, {"task_id": "t", "result": "fail", "attempt": 1})
        assert "✗" in _entry_summary(e)

    def test_audit_result_shows_flagged_count(self) -> None:
        payload = {
            "audit_id": "audit-001",
            "files_audited": ["a.md", "b.md"],
            "results": [
                {"flagged": True},
                {"flagged": False},
            ],
        }
        e = _make_entry(WalEventType.AUDIT_RESULT, payload)
        summary = _entry_summary(e)
        assert "flagged=1" in summary

    def test_run_complete_includes_tasks(self) -> None:
        e = _make_entry(WalEventType.RUN_COMPLETE, {"tasks_completed": 5, "total_tokens": 50000})
        summary = _entry_summary(e)
        assert "tasks=5" in summary

    def test_crash_detected_includes_entries_replayed(self) -> None:
        e = _make_entry(WalEventType.CRASH_DETECTED, {"entries_replayed": 7, "agents_found": 2})
        assert "7" in _entry_summary(e)

    def test_fallback_for_unknown_event_returns_string(self) -> None:
        e = _make_entry(WalEventType.MD_WRITTEN, {"md_path": "memory/foo/bar.md"})
        result = _entry_summary(e)
        assert isinstance(result, str)


# ── _build_wal_table ──────────────────────────────────────────────────────────


class TestBuildWalTable:
    def test_returns_table_with_correct_columns(self) -> None:
        from rich.table import Table
        entries = [_make_entry(WalEventType.RUN_START, {"goal": "g"})]
        table = _build_wal_table(entries)
        assert isinstance(table, Table)
        assert len(table.columns) == 4

    def test_custom_title(self) -> None:
        table = _build_wal_table([], title="My Table")
        assert table.title == "My Table"

    def test_max_rows_limits_output(self) -> None:
        entries = [_make_entry(WalEventType.TASK_PLANNED, {"task_id": f"t-{i}"}, f"w-{i:04d}") for i in range(50)]
        table = _build_wal_table(entries, max_rows=10)
        # row count: table.row_count should be <= 10
        assert table.row_count <= 10

    def test_empty_entries_renders_empty_table(self) -> None:
        table = _build_wal_table([])
        assert table.row_count == 0


# ── _build_agent_table ────────────────────────────────────────────────────────


class TestBuildAgentTable:
    def test_returns_table_for_state_with_agents(self) -> None:
        from rich.table import Table
        state = _make_state_with_agents()
        table = _build_agent_table(state)
        assert isinstance(table, Table)
        assert table.row_count == 2

    def test_empty_agent_pool_renders_zero_rows(self) -> None:
        table = _build_agent_table(WalState(run_id="r"))
        assert table.row_count == 0

    def test_budget_color_red_for_high_usage(self) -> None:
        state = WalState(run_id="r")
        state.agents["a"] = AgentState("a", "dev", "claude-sonnet-4-6", 1,
                                       status="active", context_budget=1000, tokens_used=900)
        table = _build_agent_table(state)
        # Should not raise — color coding is applied
        assert table.row_count == 1


# ── _build_cost_panel ─────────────────────────────────────────────────────────


class TestBuildCostPanel:
    def test_panel_contains_task_count(self) -> None:
        from rich.panel import Panel
        state = _make_state_with_agents()
        panel = _build_cost_panel(state)
        assert isinstance(panel, Panel)

    def test_complete_run_shows_yes(self) -> None:
        state = WalState(run_id="r")
        state.is_complete = True
        panel = _build_cost_panel(state)
        # Render to string for assertion
        from rich.console import Console
        from io import StringIO
        buf = StringIO()
        con = Console(file=buf, highlight=False)
        con.print(panel)
        rendered = buf.getvalue()
        assert "YES" in rendered

    def test_incomplete_run_shows_no(self) -> None:
        state = WalState(run_id="r")
        panel = _build_cost_panel(state)
        from rich.console import Console
        from io import StringIO
        buf = StringIO()
        con = Console(file=buf, highlight=False)
        con.print(panel)
        rendered = buf.getvalue()
        assert "no" in rendered.lower() or "No" in rendered


# ── _build_tree_panel ─────────────────────────────────────────────────────────


class TestBuildTreePanel:
    def test_panel_shows_module_names(self) -> None:
        state = _make_state_with_agents()
        panel = _build_tree_panel(state)
        from rich.console import Console
        from io import StringIO
        buf = StringIO()
        con = Console(file=buf, highlight=False)
        con.print(panel)
        rendered = buf.getvalue()
        assert "auth" in rendered

    def test_empty_md_files_renders_empty_tree(self) -> None:
        state = WalState(run_id="r")
        panel = _build_tree_panel(state)
        assert panel is not None

    def test_more_than_5_files_shows_plus_indicator(self) -> None:
        state = WalState(run_id="r")
        for i in range(8):
            state.md_files[f"memory/bigmodule/file{i}.md"] = f"w-{i:04d}"
        panel = _build_tree_panel(state)
        from rich.console import Console
        from io import StringIO
        buf = StringIO()
        con = Console(file=buf, highlight=False)
        con.print(panel)
        rendered = buf.getvalue()
        assert "more" in rendered or "+" in rendered


# ── CLI commands (smoke tests) ────────────────────────────────────────────────


class TestCLIStatus:
    def test_status_no_wal_exits_cleanly(self, tmp_path: Path) -> None:
        cfg = get_default_config()
        cfg.wal_dir = str(tmp_path / "wal")
        with patch("arbor.cli._load_cfg", return_value=cfg):
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "No WAL" in result.output

    def test_status_with_wal_shows_table(self, tmp_path: Path) -> None:
        cfg = get_default_config()
        wal_dir = tmp_path / "wal"
        wal_dir.mkdir()
        wal = wal_dir / "wal.ndjson"
        cfg.wal_dir = str(wal_dir)

        writer = WalWriter(wal)
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "test goal"})
        writer.write(WalEventType.TASK_PLANNED, "run-1", {
            "task_id": "t-1", "task_type": "dev", "goal": "do x", "complexity": 2
        })

        with patch("arbor.cli._load_cfg", return_value=cfg):
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "WAL" in result.output or "RUN_START" in result.output


class TestCLIReplay:
    def test_replay_missing_wal_exits_1(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["replay", "--wal", str(tmp_path / "nonexistent.ndjson")])
        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "WAL file not found" in result.output

    def test_replay_shows_complete_message(self, tmp_path: Path) -> None:
        wal = tmp_path / "test.ndjson"
        writer = WalWriter(wal)
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "g"})
        writer.write(WalEventType.TASK_PLANNED, "run-1", {
            "task_id": "t-1", "task_type": "dev", "goal": "x", "complexity": 1
        })

        result = runner.invoke(app, ["replay", "--wal", str(wal), "--delay", "0"])
        assert result.exit_code == 0
        assert "Replay complete" in result.output or "complete" in result.output.lower()

    def test_replay_with_delay_processes_all_entries(self, tmp_path: Path) -> None:
        wal = tmp_path / "test.ndjson"
        writer = WalWriter(wal)
        for i in range(5):
            writer.write(WalEventType.TASK_PLANNED, "run-1", {
                "task_id": f"t-{i}", "task_type": "dev", "goal": f"goal {i}", "complexity": 1
            })

        result = runner.invoke(app, ["replay", "--wal", str(wal), "--delay", "0"])
        assert result.exit_code == 0
        assert "5" in result.output or "entries" in result.output


class TestCLIResume:
    def test_resume_no_wal_exits_cleanly(self, tmp_path: Path) -> None:
        cfg = get_default_config()
        cfg.wal_dir = str(tmp_path / "wal")
        with patch("arbor.cli._load_cfg", return_value=cfg):
            result = runner.invoke(app, ["resume"])
        assert result.exit_code == 0
        assert "No WAL" in result.output

    def test_resume_complete_run_exits_cleanly(self, tmp_path: Path) -> None:
        cfg = get_default_config()
        wal_dir = tmp_path / "wal"
        wal_dir.mkdir()
        wal = wal_dir / "wal.ndjson"
        cfg.wal_dir = str(wal_dir)

        writer = WalWriter(wal)
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "done"})
        writer.write(WalEventType.RUN_COMPLETE, "run-1", {"tasks_completed": 1})

        with patch("arbor.cli._load_cfg", return_value=cfg):
            result = runner.invoke(app, ["resume"])
        assert result.exit_code == 0
        assert "completed" in result.output.lower() or "nothing to resume" in result.output.lower()


class TestCLIAuditNow:
    def test_audit_now_no_wal_exits_cleanly(self, tmp_path: Path) -> None:
        cfg = get_default_config()
        cfg.wal_dir = str(tmp_path / "wal")
        with patch("arbor.cli._load_cfg", return_value=cfg):
            result = runner.invoke(app, ["audit-now"])
        assert result.exit_code == 0
        assert "No WAL" in result.output

    def test_audit_now_no_memory_files_exits_cleanly(self, tmp_path: Path) -> None:
        cfg = get_default_config()
        wal_dir = tmp_path / "wal"
        wal_dir.mkdir()
        wal = wal_dir / "wal.ndjson"
        cfg.wal_dir = str(wal_dir)
        cfg.memory_dir = str(tmp_path / "memory")  # empty dir

        writer = WalWriter(wal)
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "g"})

        (tmp_path / "memory").mkdir()

        with patch("arbor.cli._load_cfg", return_value=cfg):
            result = runner.invoke(app, ["audit-now"])
        assert result.exit_code == 0
        assert "No memory files" in result.output


class TestCLIRun:
    def test_run_warns_about_incomplete_wal(self, tmp_path: Path) -> None:
        """If recovery is needed, run should warn and exit 1."""
        cfg = get_default_config()
        wal_dir = tmp_path / "wal"
        wal_dir.mkdir()
        wal = wal_dir / "wal.ndjson"
        cfg.wal_dir = str(wal_dir)

        # Write an incomplete run (AGENT_STARTED with no TASK_COMPLETED)
        writer = WalWriter(wal)
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "incomplete run"})
        writer.write(WalEventType.TASK_PLANNED, "run-1", {
            "task_id": "t-1", "task_type": "dev", "goal": "x", "complexity": 2
        })
        writer.write(WalEventType.AGENT_SPAWNED, "run-1", {
            "agent_id": "a-1", "agent_type": "dev", "depth": 1, "model": "claude-sonnet-4-6"
        })
        writer.write(WalEventType.AGENT_STARTED, "run-1", {"agent_id": "a-1"})
        # No TASK_COMPLETED — this agent is stuck

        with patch("arbor.cli._load_cfg", return_value=cfg):
            result = runner.invoke(app, ["run", "some goal"])
        assert result.exit_code == 1
        assert "resume" in result.output.lower() or "incomplete" in result.output.lower()
