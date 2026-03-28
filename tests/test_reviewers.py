"""Tests for arbor/reviewers/ — pass/fail logic, feedback injection, 3-strike escalation."""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from arbor.config import get_default_config
from arbor.reviewers.base import BaseReviewer, ReviewResult
from arbor.reviewers.code import CodeReviewer
from arbor.reviewers.fact import FactReviewer
from arbor.reviewers.infra import InfraReviewer
from arbor.reviewers.qa import QAReviewer
from arbor.prompts.reviewers import build_feedback_injection, _is_failing
from arbor.wal import WalEventType, WalReader, WalWriter


@pytest.fixture
def cfg():
    return get_default_config()


def _make_mock_client(json_response: str) -> MagicMock:
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=json_response)]
    mock_msg.usage = MagicMock(input_tokens=50, output_tokens=30)
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)
    return mock_client


# ── CodeReviewer ──────────────────────────────────────────────────────────────


class TestCodeReviewer:
    @pytest.mark.asyncio
    async def test_pass_result(self, cfg) -> None:
        resp = json.dumps({
            "result": "pass",
            "scores": {
                "goal_achievement": 4, "code_correctness": 4,
                "security": "pass", "error_handling": 4, "documentation_quality": 4,
            },
            "feedback": [],
            "hallucination_candidates": [],
        })
        reviewer = CodeReviewer("rev-1", "t1", "a1", 1, cfg, client=_make_mock_client(resp))
        result = await reviewer.review("Build auth", "# Output\n\nGood code here")
        assert result.result == "pass"

    @pytest.mark.asyncio
    async def test_fail_result_low_score(self, cfg) -> None:
        resp = json.dumps({
            "result": "pass",  # LLM says pass but score is low
            "scores": {
                "goal_achievement": 2, "code_correctness": 4,
                "security": "pass", "error_handling": 4, "documentation_quality": 4,
            },
            "feedback": [{"dimension": "goal_achievement", "score": 2, "note": "Token not stored"}],
            "hallucination_candidates": [],
        })
        reviewer = CodeReviewer("rev-1", "t1", "a1", 1, cfg, client=_make_mock_client(resp))
        result = await reviewer.review("Build auth", "...")
        assert result.result == "fail"

    @pytest.mark.asyncio
    async def test_security_auto_fail(self, cfg) -> None:
        resp = json.dumps({
            "result": "pass",  # LLM says pass but security fails
            "scores": {
                "goal_achievement": 5, "code_correctness": 5,
                "security": "fail", "error_handling": 5, "documentation_quality": 5,
            },
            "feedback": [{"dimension": "security", "score": "fail", "note": "Hardcoded password"}],
            "hallucination_candidates": [],
        })
        reviewer = CodeReviewer("rev-1", "t1", "a1", 1, cfg, client=_make_mock_client(resp))
        result = await reviewer.review("Build auth", "...")
        assert result.result == "fail"
        assert result.scores["security"] == "fail"

    @pytest.mark.asyncio
    async def test_hallucination_candidates_forwarded(self, cfg) -> None:
        resp = json.dumps({
            "result": "pass",
            "scores": {"goal_achievement": 4, "code_correctness": 4,
                       "security": "pass", "error_handling": 4, "documentation_quality": 4},
            "feedback": [],
            "hallucination_candidates": ["Claims bcrypt rounds 12 is minimum — unverifiable"],
        })
        reviewer = CodeReviewer("rev-1", "t1", "a1", 1, cfg, client=_make_mock_client(resp))
        result = await reviewer.review("Build auth", "...")
        assert len(result.hallucination_candidates) == 1

    @pytest.mark.asyncio
    async def test_retries_on_invalid_json(self, cfg) -> None:
        bad_msg = MagicMock()
        bad_msg.content = [MagicMock(text="NOT JSON")]
        bad_msg.usage = MagicMock(input_tokens=10, output_tokens=5)

        good_json = json.dumps({
            "result": "pass",
            "scores": {"goal_achievement": 4, "code_correctness": 4,
                       "security": "pass", "error_handling": 4, "documentation_quality": 4},
            "feedback": [],
            "hallucination_candidates": [],
        })
        good_msg = MagicMock()
        good_msg.content = [MagicMock(text=good_json)]
        good_msg.usage = MagicMock(input_tokens=50, output_tokens=30)

        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[bad_msg, good_msg])

        reviewer = CodeReviewer("rev-1", "t1", "a1", 1, cfg, client=mock_client)
        result = await reviewer.review("task", "output")
        assert result.result == "pass"

    @pytest.mark.asyncio
    async def test_run_and_record_writes_wal_entry(self, tmp_wal_path, cfg) -> None:
        resp = json.dumps({
            "result": "pass",
            "scores": {"goal_achievement": 4, "code_correctness": 4,
                       "security": "pass", "error_handling": 4, "documentation_quality": 4},
            "feedback": [],
            "hallucination_candidates": [],
        })
        reviewer = CodeReviewer("rev-t1", "t1", "a1", 1, cfg, client=_make_mock_client(resp))
        writer = WalWriter(tmp_wal_path)

        await reviewer.run_and_record("Build auth", "# Output", writer, "run-1")

        entries = WalReader.read_all(tmp_wal_path)
        review_results = [e for e in entries if e.event == WalEventType.REVIEW_RESULT]
        assert len(review_results) == 1
        assert review_results[0].payload["result"] == "pass"
        assert review_results[0].payload["task_id"] == "t1"


# ── FactReviewer ──────────────────────────────────────────────────────────────


class TestFactReviewer:
    @pytest.mark.asyncio
    async def test_internal_consistency_fail(self, cfg) -> None:
        resp = json.dumps({
            "result": "pass",
            "scores": {"source_support": 4, "internal_consistency": "fail",
                       "cross_file_consistency": "pass", "actionability": 4},
            "feedback": [{"dimension": "internal_consistency", "score": "fail",
                          "note": "TTL stated as 8h then 24h"}],
            "hallucination_candidates": [],
        })
        reviewer = FactReviewer("rev-1", "t1", "a1", 1, cfg, client=_make_mock_client(resp))
        result = await reviewer.review("Research caching", "...")
        assert result.result == "fail"

    @pytest.mark.asyncio
    async def test_all_pass(self, cfg) -> None:
        resp = json.dumps({
            "result": "pass",
            "scores": {"source_support": 4, "internal_consistency": "pass",
                       "cross_file_consistency": "pass", "actionability": 4},
            "feedback": [],
            "hallucination_candidates": [],
        })
        reviewer = FactReviewer("rev-1", "t1", "a1", 1, cfg, client=_make_mock_client(resp))
        result = await reviewer.review("Research topic", "...")
        assert result.result == "pass"


# ── InfraReviewer ─────────────────────────────────────────────────────────────


class TestInfraReviewer:
    @pytest.mark.asyncio
    async def test_secrets_auto_fail(self, cfg) -> None:
        resp = json.dumps({
            "result": "pass",
            "scores": {"reproducibility": 5, "secrets_check": "fail",
                       "compatibility": 5, "idempotency": 5},
            "feedback": [{"dimension": "secrets_check", "score": "fail",
                          "note": "Hardcoded API key found"}],
            "hallucination_candidates": [],
        })
        reviewer = InfraReviewer("rev-1", "t1", "a1", 1, cfg, client=_make_mock_client(resp))
        result = await reviewer.review("Set up Docker", "...")
        assert result.result == "fail"

    @pytest.mark.asyncio
    async def test_all_pass(self, cfg) -> None:
        resp = json.dumps({
            "result": "pass",
            "scores": {"reproducibility": 4, "secrets_check": "pass",
                       "compatibility": 4, "idempotency": 4},
            "feedback": [],
            "hallucination_candidates": [],
        })
        reviewer = InfraReviewer("rev-1", "t1", "a1", 1, cfg, client=_make_mock_client(resp))
        result = await reviewer.review("Set up Docker", "...")
        assert result.result == "pass"


# ── QAReviewer ────────────────────────────────────────────────────────────────


class TestQAReviewer:
    @pytest.mark.asyncio
    async def test_low_coverage_fails(self, cfg) -> None:
        resp = json.dumps({
            "result": "pass",
            "scores": {"test_coverage": 2, "edge_case_handling": 4, "assertion_quality": 4},
            "feedback": [{"dimension": "test_coverage", "score": 2, "note": "No error cases tested"}],
            "hallucination_candidates": [],
        })
        reviewer = QAReviewer("rev-1", "t1", "a1", 1, cfg, client=_make_mock_client(resp))
        result = await reviewer.review("Write tests", "...")
        assert result.result == "fail"


# ── Feedback injection ────────────────────────────────────────────────────────


class TestFeedbackInjection:
    def test_format_contains_failing_dimensions(self) -> None:
        feedbacks = [
            {"dimension": "goal_achievement", "score": 2,
             "note": "Token not stored after login"},
            {"dimension": "code_correctness", "score": 2,
             "note": "session middleware initialized after auth"},
        ]
        result = build_feedback_injection(feedbacks, attempt=1, max_attempts=3)
        assert "goal_achievement" in result
        assert "code_correctness" in result
        assert "Token not stored" in result
        assert "attempt 1 of 3" in result

    def test_format_contains_fix_only_instruction(self) -> None:
        feedbacks = [{"dimension": "goal_achievement", "score": 1, "note": "incomplete"}]
        result = build_feedback_injection(feedbacks, attempt=2, max_attempts=3)
        assert "Fix ONLY" in result

    def test_passing_dimensions_not_included(self) -> None:
        feedbacks = [
            {"dimension": "goal_achievement", "score": 5, "note": ""},
            {"dimension": "code_correctness", "score": 2, "note": "broken"},
        ]
        result = build_feedback_injection(feedbacks, attempt=1, max_attempts=3)
        assert "code_correctness" in result
        # goal_achievement passed — should not appear in failing list
        lines_with_goal = [l for l in result.splitlines() if "goal_achievement" in l and "Failed" in l]
        assert not lines_with_goal

    def test_is_failing_numeric(self) -> None:
        assert _is_failing({"score": 2}) is True
        assert _is_failing({"score": 3}) is False
        assert _is_failing({"score": "fail"}) is True
        assert _is_failing({"score": "pass"}) is False


# ── 3-strike escalation ───────────────────────────────────────────────────────


class TestThreeStrikeEscalation:
    """Test the full 3-strike path: 3 REVIEW_RESULT(fail) → TASK_FAILED + bug MD."""

    @pytest.mark.asyncio
    async def test_three_failures_trigger_task_failed(self, tmp_wal_path, cfg) -> None:
        from arbor.orchestrator import handle_task_failure
        from arbor.wal import WalState, TaskState

        writer = WalWriter(tmp_wal_path)
        state = WalState(run_id="run-1")
        state.tasks["auth-task"] = TaskState(
            "auth-task", "dev", "Build JWT auth", assigned_agent_id="a1"
        )

        # Simulate 3 failed review results already written
        for i in range(1, 4):
            writer.write(WalEventType.REVIEW_RESULT, "run-1", {
                "reviewer_id": f"rev-auth-task-{i}", "task_id": "auth-task",
                "agent_id": "a1", "result": "fail", "attempt": i,
                "scores": {"goal_achievement": 2}, "feedback": [
                    {"dimension": "goal_achievement", "score": 2, "note": f"failure {i}"}
                ], "hallucination_candidates": [],
            })

        feedbacks = [
            {"attempt": i, "feedback": [{"dimension": "goal_achievement", "score": 2, "note": f"fail {i}"}]}
            for i in range(1, 4)
        ]
        await handle_task_failure(state, "auth-task", feedbacks, writer, cfg)

        entries = WalReader.read_all(tmp_wal_path)
        failed_entries = [e for e in entries if e.event == WalEventType.TASK_FAILED]
        assert len(failed_entries) == 1
        assert failed_entries[0].payload["task_id"] == "auth-task"
        assert failed_entries[0].payload["review_attempts"] == 3

    @pytest.mark.asyncio
    async def test_bug_md_contains_all_feedbacks(self, tmp_wal_path, cfg) -> None:
        from arbor.orchestrator import handle_task_failure
        from arbor.wal import WalState, TaskState

        writer = WalWriter(tmp_wal_path)
        state = WalState(run_id="run-1")
        state.tasks["t1"] = TaskState("t1", "dev", "goal", assigned_agent_id="a1")

        feedbacks = [
            {"attempt": i, "feedback": [{"dimension": "code_correctness", "score": 2, "note": f"attempt {i}"}]}
            for i in range(1, 4)
        ]

        await handle_task_failure(state, "t1", feedbacks, writer, cfg)

        entries = WalReader.read_all(tmp_wal_path)
        md_entries = [e for e in entries if e.event == WalEventType.MD_WRITTEN and e.payload.get("is_bug_report")]
        assert len(md_entries) == 1
        bug_path = md_entries[0].payload["md_path"]
        assert "bugs" in bug_path

    @pytest.mark.asyncio
    async def test_oscillation_detected_in_bug_report(self, tmp_wal_path, cfg) -> None:
        from arbor.orchestrator import handle_task_failure
        from arbor.wal import WalState, TaskState

        writer = WalWriter(tmp_wal_path)
        state = WalState(run_id="run-1")
        state.tasks["t1"] = TaskState("t1", "dev", "goal", assigned_agent_id="a1")

        # Same dimension fails all 3 times → oscillation
        feedbacks = [
            {"attempt": i, "feedback": [{"dimension": "security", "score": "fail", "note": "hardcoded key"}]}
            for i in range(1, 4)
        ]

        await handle_task_failure(state, "t1", feedbacks, writer, cfg)

        entries = WalReader.read_all(tmp_wal_path)
        failed = [e for e in entries if e.event == WalEventType.TASK_FAILED]
        assert "security" in failed[0].payload.get("oscillating_dimensions", [])
