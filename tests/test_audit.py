"""Tests for Phase 5 — audit agent, flag injection, and audit triggers."""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from arbor.config import get_default_config
from arbor.agents.audit import AuditAgent, AuditResult, FileAuditResult
from arbor.memory.flag_injector import inject_audit_flag, has_audit_flag, remove_audit_flag
from arbor.memory.versioner import write_versioned_md
from arbor.wal import WalEventType, WalReader, WalWriter, WalState, TaskState
from arbor.scheduler import determine_next_actions, SchedulerAction


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


# ── Flag injector ─────────────────────────────────────────────────────────────


class TestFlagInjector:
    def test_inject_prepends_flag_to_file(self, tmp_path: Path) -> None:
        md = tmp_path / "test.md"
        write_versioned_md(md, "# Session Manager\n\nTTL is 8h. Also TTL is 24h.", "w-1")
        inject_audit_flag(md, "audit-003", 0.54, ["TTL contradiction: 8h vs 24h"])
        content = md.read_text(encoding="utf-8")
        assert "AUDIT FLAG" in content
        assert "audit-003" in content
        assert "0.54" in content
        assert "TTL contradiction" in content

    def test_inject_flag_appears_before_body(self, tmp_path: Path) -> None:
        md = tmp_path / "test.md"
        write_versioned_md(md, "# Body content here", "w-1")
        inject_audit_flag(md, "audit-001", 0.4, ["issue 1"])
        content = md.read_text(encoding="utf-8")
        flag_pos = content.index("AUDIT FLAG")
        body_pos = content.index("Body content here")
        assert flag_pos < body_pos

    def test_has_audit_flag_true_after_inject(self, tmp_path: Path) -> None:
        md = tmp_path / "test.md"
        write_versioned_md(md, "content", "w-1")
        inject_audit_flag(md, "audit-001", 0.5, [])
        assert has_audit_flag(md) is True

    def test_has_audit_flag_false_for_clean_file(self, tmp_path: Path) -> None:
        md = tmp_path / "test.md"
        write_versioned_md(md, "clean content", "w-1")
        assert has_audit_flag(md) is False

    def test_has_audit_flag_false_for_missing_file(self, tmp_path: Path) -> None:
        assert has_audit_flag(tmp_path / "nonexistent.md") is False

    def test_no_double_injection(self, tmp_path: Path) -> None:
        md = tmp_path / "test.md"
        write_versioned_md(md, "content", "w-1")
        inject_audit_flag(md, "audit-001", 0.5, ["issue 1"])
        inject_audit_flag(md, "audit-002", 0.4, ["issue 2"])  # second injection
        content = md.read_text(encoding="utf-8")
        assert content.count("AUDIT FLAG") == 1  # only one flag

    def test_remove_audit_flag(self, tmp_path: Path) -> None:
        md = tmp_path / "test.md"
        write_versioned_md(md, "# Clean content\n\nThis is the body.", "w-1")
        inject_audit_flag(md, "audit-001", 0.5, ["some issue"])
        assert has_audit_flag(md) is True

        remove_audit_flag(md)
        assert has_audit_flag(md) is False
        content = md.read_text()
        assert "Clean content" in content

    def test_remove_on_unflagged_file_is_noop(self, tmp_path: Path) -> None:
        md = tmp_path / "test.md"
        write_versioned_md(md, "content", "w-1")
        original = md.read_text()
        remove_audit_flag(md)  # should not raise
        assert md.read_text() == original

    def test_inject_raises_for_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            inject_audit_flag(tmp_path / "nonexistent.md", "audit-001", 0.5, [])


# ── AuditAgent ────────────────────────────────────────────────────────────────


class TestAuditAgent:
    def _ttl_contradiction_content(self) -> str:
        return (
            "# Session Manager\n\n"
            "## Overview\n\nThe session TTL is 8 hours.\n\n"
            "## Configuration\n\nSet SESSION_TTL=24h in config.\n"
        )

    def _clean_content(self) -> str:
        return (
            "# JWT Implementation\n\n"
            "## Overview\n\nJWT tokens expire after 1 hour.\n\n"
            "## Output\n\nAll claims are consistent.\n"
        )

    @pytest.mark.asyncio
    async def test_audit_detects_contradiction(self, tmp_path: Path, cfg) -> None:
        md = tmp_path / "session.md"
        write_versioned_md(md, self._ttl_contradiction_content(), "w-1")

        audit_resp = json.dumps({
            "audit_id": "audit-001",
            "results": [{
                "md_path": str(md),
                "confidence_score": 0.54,
                "flagged": True,
                "claims_checked": 6,
                "issues": ["Session TTL stated as 8h in Overview but 24h in Configuration"],
            }],
        })
        agent = AuditAgent("audit-001", cfg, client=_make_mock_client(audit_resp))
        result = await agent.run_audit([md])

        assert len(result.results) == 1
        assert result.results[0].flagged is True
        assert result.results[0].confidence_score == 0.54
        assert "TTL" in result.results[0].issues[0]

    @pytest.mark.asyncio
    async def test_audit_clean_file_not_flagged(self, tmp_path: Path, cfg) -> None:
        md = tmp_path / "jwt.md"
        write_versioned_md(md, self._clean_content(), "w-1")

        audit_resp = json.dumps({
            "audit_id": "audit-001",
            "results": [{
                "md_path": str(md),
                "confidence_score": 0.91,
                "flagged": False,
                "claims_checked": 4,
                "issues": [],
            }],
        })
        agent = AuditAgent("audit-001", cfg, client=_make_mock_client(audit_resp))
        result = await agent.run_audit([md])

        assert result.results[0].flagged is False
        assert result.results[0].confidence_score == 0.91

    @pytest.mark.asyncio
    async def test_run_and_record_writes_audit_result_wal(
        self, tmp_path: Path, tmp_wal_path: Path, cfg
    ) -> None:
        md = tmp_path / "session.md"
        write_versioned_md(md, self._ttl_contradiction_content(), "w-1")

        audit_resp = json.dumps({
            "audit_id": "audit-001",
            "results": [{
                "md_path": str(md),
                "confidence_score": 0.54,
                "flagged": True,
                "claims_checked": 5,
                "issues": ["TTL contradiction"],
            }],
        })
        agent = AuditAgent("audit-001", cfg, client=_make_mock_client(audit_resp))
        writer = WalWriter(tmp_wal_path)

        await agent.run_and_record([md], writer, "run-1")

        entries = WalReader.read_all(tmp_wal_path)
        audit_results = [e for e in entries if e.event == WalEventType.AUDIT_RESULT]
        assert len(audit_results) == 1
        assert audit_results[0].payload["audit_id"] == "audit-001"

    @pytest.mark.asyncio
    async def test_run_and_record_writes_md_flagged_for_low_score(
        self, tmp_path: Path, tmp_wal_path: Path, cfg
    ) -> None:
        md = tmp_path / "session.md"
        write_versioned_md(md, self._ttl_contradiction_content(), "w-1")

        audit_resp = json.dumps({
            "audit_id": "audit-001",
            "results": [{
                "md_path": str(md),
                "confidence_score": 0.54,
                "flagged": True,
                "claims_checked": 5,
                "issues": ["TTL contradiction"],
            }],
        })
        agent = AuditAgent("audit-001", cfg, client=_make_mock_client(audit_resp))
        writer = WalWriter(tmp_wal_path)

        await agent.run_and_record([md], writer, "run-1")

        entries = WalReader.read_all(tmp_wal_path)
        flagged = [e for e in entries if e.event == WalEventType.MD_FLAGGED]
        assert len(flagged) == 1
        assert flagged[0].payload["confidence_score"] == 0.54

    @pytest.mark.asyncio
    async def test_run_and_record_injects_flag_into_file(
        self, tmp_path: Path, tmp_wal_path: Path, cfg
    ) -> None:
        md = tmp_path / "session.md"
        write_versioned_md(md, self._ttl_contradiction_content(), "w-1")

        audit_resp = json.dumps({
            "audit_id": "audit-001",
            "results": [{
                "md_path": str(md),
                "confidence_score": 0.54,
                "flagged": True,
                "claims_checked": 5,
                "issues": ["TTL contradiction"],
            }],
        })
        agent = AuditAgent("audit-001", cfg, client=_make_mock_client(audit_resp))
        writer = WalWriter(tmp_wal_path)

        await agent.run_and_record([md], writer, "run-1", memory_base=tmp_path)

        assert has_audit_flag(md) is True

    @pytest.mark.asyncio
    async def test_next_agent_context_includes_audit_flag(
        self, tmp_path: Path, cfg
    ) -> None:
        """After flagging, the file content should show the warning to any reader."""
        from arbor.memory.slicer import build_context_slice

        md = tmp_path / "session.md"
        write_versioned_md(md, "# Session\n\nTTL is 8h... actually 24h.", "w-1")
        inject_audit_flag(md, "audit-001", 0.54, ["TTL contradiction"])

        context = build_context_slice([(md, None)], budget=4000)
        assert "AUDIT FLAG" in context


# ── Audit triggers ────────────────────────────────────────────────────────────


class TestAuditTriggers:
    def test_periodic_trigger_fires_after_n_completions(self, cfg) -> None:
        """Scheduler should emit SPAWN_AUDIT after audit_every_n_tasks completions."""
        state = WalState(run_id="r")
        state.task_completion_count = cfg.audit_every_n_tasks
        state.last_audit_at_count = 0
        state.tasks["t1"] = TaskState("t1", "dev", "x", status="in_progress")

        steps = determine_next_actions(state, cfg)
        audit = [s for s in steps if s.action == SchedulerAction.SPAWN_AUDIT]
        assert audit

    def test_periodic_trigger_does_not_fire_before_n(self, cfg) -> None:
        state = WalState(run_id="r")
        state.task_completion_count = cfg.audit_every_n_tasks - 1
        state.last_audit_at_count = 0
        state.tasks["t1"] = TaskState("t1", "dev", "x", status="in_progress")

        steps = determine_next_actions(state, cfg)
        audit = [s for s in steps if s.action == SchedulerAction.SPAWN_AUDIT]
        assert not audit

    def test_no_double_audit_if_already_running(self, cfg) -> None:
        from arbor.wal import AgentState
        state = WalState(run_id="r")
        state.task_completion_count = cfg.audit_every_n_tasks
        state.last_audit_at_count = 0
        state.agents["audit-1"] = AgentState("audit-1", "audit", "m", 0, status="active")
        state.tasks["t1"] = TaskState("t1", "dev", "x", status="in_progress")

        steps = determine_next_actions(state, cfg)
        audit = [s for s in steps if s.action == SchedulerAction.SPAWN_AUDIT]
        assert not audit

    def test_audit_not_triggered_when_at_last_count(self, cfg) -> None:
        """No audit if we already ran one at this count."""
        state = WalState(run_id="r")
        state.task_completion_count = cfg.audit_every_n_tasks
        state.last_audit_at_count = cfg.audit_every_n_tasks  # already ran
        state.tasks["t1"] = TaskState("t1", "dev", "x", status="in_progress")

        steps = determine_next_actions(state, cfg)
        audit = [s for s in steps if s.action == SchedulerAction.SPAWN_AUDIT]
        assert not audit
