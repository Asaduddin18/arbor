"""Base reviewer class for Arbor v2.

Reviewers are spawned by the SCHEDULER (never by agents) after a
TASK_COMPLETED + MD_WRITTEN pair is detected in the WAL.

The reviewer spawn is itself a WAL entry (REVIEW_STARTED) written before
the reviewer runs. If the reviewer crashes, the scheduler detects the missing
REVIEW_RESULT and re-spawns.

Output: JSON only, with result (pass/fail), scores, feedback,
and hallucination_candidates.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from arbor.config import ArborConfig
from arbor.prompts.reviewers import (
    REVIEWER_SYSTEM,
    build_reviewer_prompt,
    build_feedback_injection,
    REVIEWER_RUBRICS,
)
from arbor.wal import WalEventType, WalWriter

logger = logging.getLogger(__name__)

_MAX_JSON_RETRIES = 2


@dataclass
class ReviewResult:
    """Result from a reviewer agent.

    Attributes:
        result: "pass" or "fail".
        scores: Dict mapping dimension name to score (int or "pass"/"fail").
        feedback: List of feedback dicts with dimension, score, note.
        hallucination_candidates: Claims flagged as potentially unverifiable.
        attempt: Which review attempt this was (1-indexed).
    """

    result: str  # "pass" | "fail"
    scores: dict
    feedback: list[dict] = field(default_factory=list)
    hallucination_candidates: list[str] = field(default_factory=list)
    attempt: int = 1


class BaseReviewer:
    """Abstract base reviewer.

    Args:
        reviewer_id: Unique reviewer identifier.
        reviewer_type: Type string (code, fact, infra, qa).
        model: LLM model ID.
        task_id: The task being reviewed.
        agent_id: The agent whose output is under review.
        attempt: Current attempt number (1-indexed).
        config: ArborConfig.
        client: Optional AsyncAnthropic client.
    """

    def __init__(
        self,
        reviewer_id: str,
        reviewer_type: str,
        model: str,
        task_id: str,
        agent_id: str,
        attempt: int,
        config: ArborConfig,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.reviewer_id = reviewer_id
        self.reviewer_type = reviewer_type
        self.model = model
        self.task_id = task_id
        self.agent_id = agent_id
        self.attempt = attempt
        self.config = config
        self._client = client or anthropic.AsyncAnthropic()

    async def review(self, task_goal: str, md_content: str) -> ReviewResult:
        """Score agent output against the task goal using this reviewer's rubric.

        Calls the LLM with JSON-only output instructions. Retries up to
        _MAX_JSON_RETRIES times on malformed JSON.

        Args:
            task_goal: The original task goal string from the WAL.
            md_content: Full content of the agent's output MD file.

        Returns:
            ReviewResult with pass/fail, scores, and feedback.

        Raises:
            ValueError: If LLM returns invalid JSON after all retries.
        """
        rubric = REVIEWER_RUBRICS.get(self.reviewer_type, "")
        user_prompt = build_reviewer_prompt(
            self.reviewer_type, task_goal, md_content, rubric
        )

        last_error: Exception | None = None

        for attempt in range(1, _MAX_JSON_RETRIES + 1):
            prompt = user_prompt
            if last_error:
                prompt += f"\n\nPrevious response was not valid JSON: {last_error}. Output JSON only."

            logger.info(
                "Reviewer %s reviewing task %s (attempt %d/%d)",
                self.reviewer_id, self.task_id, attempt, _MAX_JSON_RETRIES,
            )

            response = await self._client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=REVIEWER_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )

            raw = response.content[0].text.strip()

            # Strip code fences if present
            if raw.startswith("```"):
                import re
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)
                raw = raw.strip()

            try:
                data = json.loads(raw)
                result = self._parse_result(data)
                logger.info(
                    "Reviewer %s: task %s → %s (attempt %d)",
                    self.reviewer_id, self.task_id, result.result, self.attempt,
                )
                return result
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "Reviewer JSON parse error (attempt %d): %s", attempt, exc
                )

        raise ValueError(
            f"Reviewer {self.reviewer_id} failed to return valid JSON "
            f"after {_MAX_JSON_RETRIES} attempts: {last_error}"
        )

    def _parse_result(self, data: dict) -> ReviewResult:
        """Parse the raw LLM JSON into a ReviewResult.

        Subclasses can override to apply type-specific auto-fail rules.

        Args:
            data: Parsed JSON dict from LLM.

        Returns:
            ReviewResult.
        """
        result = data.get("result", "fail")
        scores = data.get("scores", {})
        feedback = data.get("feedback", [])
        hallucination_candidates = data.get("hallucination_candidates", [])

        # Apply auto-fail rules (overridden by subclasses)
        result = self._apply_auto_fail(result, scores)

        return ReviewResult(
            result=result,
            scores=scores,
            feedback=feedback,
            hallucination_candidates=hallucination_candidates,
            attempt=self.attempt,
        )

    def _apply_auto_fail(self, result: str, scores: dict) -> str:
        """Apply type-specific auto-fail rules.

        Override in subclasses to add rubric-specific auto-fail triggers.

        Args:
            result: Current result string ("pass" or "fail").
            scores: Score dictionary.

        Returns:
            Possibly-overridden result string.
        """
        return result

    async def run_and_record(
        self,
        task_goal: str,
        md_content: str,
        writer: WalWriter,
        run_id: str,
    ) -> ReviewResult:
        """Run review and write REVIEW_RESULT WAL entry.

        Args:
            task_goal: The task goal string.
            md_content: Agent's MD output.
            writer: WalWriter to append to.
            run_id: Active run ID.

        Returns:
            ReviewResult.
        """
        result = await self.review(task_goal, md_content)

        writer.write(
            WalEventType.REVIEW_RESULT,
            run_id=run_id,
            payload={
                "reviewer_id": self.reviewer_id,
                "task_id": self.task_id,
                "agent_id": self.agent_id,
                "result": result.result,
                "attempt": result.attempt,
                "scores": result.scores,
                "feedback": result.feedback,
                "hallucination_candidates": result.hallucination_candidates,
                "md_committed": result.result == "pass",
            },
        )

        return result

    def build_feedback_for_retry(self, result: ReviewResult) -> str:
        """Format this review result as structured feedback for the agent to retry.

        Args:
            result: The failing ReviewResult.

        Returns:
            Formatted feedback string to inject into the agent's next prompt.
        """
        return build_feedback_injection(
            result.feedback,
            attempt=self.attempt,
            max_attempts=self.config.max_review_attempts,
        )
