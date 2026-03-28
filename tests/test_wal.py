"""Tests for arbor/wal.py — WAL writer, reader, and state reconstruction."""

import json
import pytest
from pathlib import Path

from arbor.wal import (
    WalEntry,
    WalEventType,
    WalWriter,
    WalReader,
    WalCorruptError,
    WalState,
    AgentState,
    TaskState,
    build_state_from_wal,
)


# ── WalWriter ─────────────────────────────────────────────────────────────────


class TestWalWriter:
    def test_creates_file_on_first_write(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        assert not tmp_wal_path.exists()
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "test"})
        assert tmp_wal_path.exists()

    def test_returns_wal_entry(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        entry = writer.write(WalEventType.RUN_START, "run-1", {"goal": "test"})
        assert isinstance(entry, WalEntry)
        assert entry.event == WalEventType.RUN_START
        assert entry.run_id == "run-1"
        assert entry.payload == {"goal": "test"}

    def test_wal_id_starts_at_w_0001(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        entry = writer.write(WalEventType.RUN_START, "run-1", {})
        assert entry.wal_id == "w-0001"

    def test_wal_id_is_monotonically_increasing(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        ids = [
            writer.write(WalEventType.RUN_START, "run-1", {}).wal_id,
            writer.write(WalEventType.TASK_PLANNED, "run-1", {"task_id": "t1", "task_type": "dev", "goal": "g"}).wal_id,
            writer.write(WalEventType.AGENT_SPAWNED, "run-1", {"agent_id": "a1", "agent_type": "dev", "model": "m", "depth": 1}).wal_id,
        ]
        nums = [int(i.split("-")[1]) for i in ids]
        assert nums == sorted(nums)
        assert len(nums) == len(set(nums)), "IDs must be unique"

    def test_no_gaps_in_ids(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        entries = [writer.write(WalEventType.RUN_START, "run-1", {}) for _ in range(5)]
        nums = [int(e.wal_id.split("-")[1]) for e in entries]
        assert nums == list(range(1, 6))

    def test_append_only_multiple_writes(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        for i in range(3):
            writer.write(WalEventType.RUN_START, f"run-{i}", {"i": i})
        entries = WalReader.read_all(tmp_wal_path)
        assert len(entries) == 3

    def test_new_writer_continues_ids_from_existing_wal(self, tmp_wal_path: Path) -> None:
        """Restarting WalWriter should continue from the last id in the file."""
        w1 = WalWriter(tmp_wal_path)
        e1 = w1.write(WalEventType.RUN_START, "run-1", {})
        e2 = w1.write(WalEventType.TASK_PLANNED, "run-1", {"task_id": "t", "task_type": "dev", "goal": "g"})

        # New writer — should scan file and continue
        w2 = WalWriter(tmp_wal_path)
        e3 = w2.write(WalEventType.AGENT_SPAWNED, "run-1", {"agent_id": "a", "agent_type": "dev", "model": "m", "depth": 1})

        nums = [int(e.wal_id.split("-")[1]) for e in [e1, e2, e3]]
        assert nums == [1, 2, 3]

    def test_writes_valid_ndjson(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "hello"})
        lines = tmp_wal_path.read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["event"] == "RUN_START"
        assert data["run_id"] == "run-1"
        assert "wal_id" in data
        assert "timestamp" in data


# ── WalReader ─────────────────────────────────────────────────────────────────


class TestWalReader:
    def test_read_all_empty_file(self, tmp_wal_path: Path) -> None:
        tmp_wal_path.touch()
        assert WalReader.read_all(tmp_wal_path) == []

    def test_read_all_missing_file_returns_empty(self, tmp_wal_path: Path) -> None:
        assert WalReader.read_all(tmp_wal_path) == []

    def test_read_all_returns_entries_in_order(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "g"})
        writer.write(WalEventType.TASK_PLANNED, "run-1", {"task_id": "t", "task_type": "dev", "goal": "g"})
        entries = WalReader.read_all(tmp_wal_path)
        assert len(entries) == 2
        assert entries[0].event == WalEventType.RUN_START
        assert entries[1].event == WalEventType.TASK_PLANNED

    def test_read_all_parses_all_event_types(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        for ev in WalEventType:
            writer.write(ev, "run-x", {"dummy": True})
        entries = WalReader.read_all(tmp_wal_path)
        found = {e.event for e in entries}
        assert found == set(WalEventType)

    def test_read_all_raises_on_corrupt_json(self, tmp_wal_path: Path) -> None:
        # First line is valid WAL JSON; second line is not JSON at all
        valid_line = json.dumps({
            "wal_id": "w-0001", "event": "RUN_START",
            "timestamp": "2025-01-01T00:00:00Z", "run_id": "r", "payload": {},
        })
        tmp_wal_path.write_text(valid_line + "\nNOT JSON\n")
        with pytest.raises(WalCorruptError, match="line 2"):
            WalReader.read_all(tmp_wal_path)

    def test_read_all_raises_on_missing_field(self, tmp_wal_path: Path) -> None:
        # Missing 'event' field
        line = json.dumps({"wal_id": "w-0001", "timestamp": "x", "run_id": "r", "payload": {}})
        tmp_wal_path.write_text(line + "\n")
        with pytest.raises(WalCorruptError):
            WalReader.read_all(tmp_wal_path)

    def test_replay_yields_entries(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "g"})
        writer.write(WalEventType.TASK_PLANNED, "run-1", {"task_id": "t", "task_type": "dev", "goal": "g"})
        replayed = list(WalReader.replay(tmp_wal_path))
        assert len(replayed) == 2

    def test_replay_empty_file(self, tmp_wal_path: Path) -> None:
        tmp_wal_path.touch()
        assert list(WalReader.replay(tmp_wal_path)) == []

    def test_skips_blank_lines(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "g"})
        # Append blank lines manually
        with open(tmp_wal_path, "a") as f:
            f.write("\n\n")
        writer.write(WalEventType.TASK_PLANNED, "run-1", {"task_id": "t", "task_type": "dev", "goal": "g"})
        entries = WalReader.read_all(tmp_wal_path)
        assert len(entries) == 2


# ── build_state_from_wal ──────────────────────────────────────────────────────


class TestBuildStateFromWal:
    def _write_sequence(self, writer: WalWriter) -> None:
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "Build API"})
        writer.write(WalEventType.TASK_PLANNED, "run-1", {
            "task_id": "t-auth", "task_type": "dev", "goal": "JWT auth", "complexity": 5,
        })
        writer.write(WalEventType.AGENT_SPAWNED, "run-1", {
            "agent_id": "agent-dev-1-001", "agent_type": "dev",
            "model": "claude-sonnet-4-6", "depth": 1,
            "initial_task_id": "t-auth", "context_budget_tokens": 8000,
        })

    def test_run_id_captured(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        writer.write(WalEventType.RUN_START, "run-42", {"goal": "test"})
        state = build_state_from_wal(WalReader.read_all(tmp_wal_path))
        assert state.run_id == "run-42"
        assert state.goal == "test"

    def test_task_planned_creates_task(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "g"})
        writer.write(WalEventType.TASK_PLANNED, "run-1", {
            "task_id": "t1", "task_type": "dev", "goal": "do X", "complexity": 7,
        })
        state = build_state_from_wal(WalReader.read_all(tmp_wal_path))
        assert "t1" in state.tasks
        assert state.tasks["t1"].task_type == "dev"
        assert state.tasks["t1"].complexity == 7

    def test_agent_spawned_creates_agent(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        self._write_sequence(writer)
        state = build_state_from_wal(WalReader.read_all(tmp_wal_path))
        assert "agent-dev-1-001" in state.agents
        assert state.agents["agent-dev-1-001"].status == "spawned"

    def test_agent_started_updates_status(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        self._write_sequence(writer)
        writer.write(WalEventType.AGENT_STARTED, "run-1", {"agent_id": "agent-dev-1-001"})
        state = build_state_from_wal(WalReader.read_all(tmp_wal_path))
        assert state.agents["agent-dev-1-001"].status == "active"

    def test_task_completed_increments_counter(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        self._write_sequence(writer)
        writer.write(WalEventType.AGENT_STARTED, "run-1", {"agent_id": "agent-dev-1-001"})
        writer.write(WalEventType.TASK_COMPLETED, "run-1", {
            "task_id": "t-auth", "agent_id": "agent-dev-1-001",
            "tokens_used": 500, "md_path": "memory/auth.md", "md_hash": "sha256:x",
        })
        state = build_state_from_wal(WalReader.read_all(tmp_wal_path))
        assert state.task_completion_count == 1
        assert state.agents["agent-dev-1-001"].tokens_used == 500

    def test_md_written_tracked(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "g"})
        writer.write(WalEventType.MD_WRITTEN, "run-1", {
            "md_path": "memory/auth.md", "md_hash": "sha256:abc",
        })
        state = build_state_from_wal(WalReader.read_all(tmp_wal_path))
        assert "memory/auth.md" in state.md_files

    def test_review_result_pass_updates_task(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        self._write_sequence(writer)
        writer.write(WalEventType.AGENT_STARTED, "run-1", {"agent_id": "agent-dev-1-001"})
        writer.write(WalEventType.TASK_COMPLETED, "run-1", {
            "task_id": "t-auth", "agent_id": "agent-dev-1-001",
            "tokens_used": 200, "md_path": "memory/auth.md", "md_hash": "x",
        })
        writer.write(WalEventType.MD_WRITTEN, "run-1", {"md_path": "memory/auth.md", "md_hash": "x"})
        writer.write(WalEventType.REVIEW_STARTED, "run-1", {
            "reviewer_id": "rev-t-auth", "task_id": "t-auth", "agent_id": "agent-dev-1-001",
        })
        writer.write(WalEventType.REVIEW_RESULT, "run-1", {
            "reviewer_id": "rev-t-auth", "task_id": "t-auth",
            "result": "pass", "attempt": 1, "scores": {},
        })
        state = build_state_from_wal(WalReader.read_all(tmp_wal_path))
        assert state.tasks["t-auth"].status == "reviewed_pass"
        assert state.tasks["t-auth"].review_result == "pass"

    def test_run_complete_sets_flag(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "g"})
        writer.write(WalEventType.RUN_COMPLETE, "run-1", {"tasks_completed": 0, "total_tokens": 0})
        state = build_state_from_wal(WalReader.read_all(tmp_wal_path))
        assert state.is_complete is True

    def test_empty_entries_returns_empty_state(self) -> None:
        state = build_state_from_wal([])
        assert state.run_id is None
        assert state.tasks == {}
        assert state.agents == {}

    def test_chain_membership_tracked(self, tmp_wal_path: Path) -> None:
        writer = WalWriter(tmp_wal_path)
        writer.write(WalEventType.RUN_START, "run-1", {"goal": "g"})
        writer.write(WalEventType.TASK_PLANNED, "run-1", {
            "task_id": "t1", "task_type": "dev", "goal": "step 1", "chain_id": "chain-A",
        })
        writer.write(WalEventType.TASK_PLANNED, "run-1", {
            "task_id": "t2", "task_type": "dev", "goal": "step 2", "chain_id": "chain-A",
        })
        state = build_state_from_wal(WalReader.read_all(tmp_wal_path))
        assert "chain-A" in state.chains
        assert "t1" in state.chains["chain-A"]
        assert "t2" in state.chains["chain-A"]
