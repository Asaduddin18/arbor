"""Code reviewer for Arbor v2.

Rubric: goal_achievement, code_correctness, security (auto-fail), error_handling,
        documentation_quality.
Pass threshold: all numeric ≥ 3, security = pass.
Default model: claude-haiku-4-5-20251001.
"""

from __future__ import annotations

import logging

import anthropic

from arbor.config import ArborConfig
from arbor.reviewers.base import BaseReviewer

logger = logging.getLogger(__name__)

_AUTO_FAIL_DIMENSIONS = {"security"}


class CodeReviewer(BaseReviewer):
    """Reviewer for development agent outputs.

    Auto-fails on any security dimension failure regardless of other scores.

    Args:
        reviewer_id: Unique reviewer identifier.
        task_id: Task being reviewed.
        agent_id: Agent whose output is under review.
        attempt: Attempt number (1-indexed).
        config: ArborConfig.
        client: Optional AsyncAnthropic client.
    """

    def __init__(
        self,
        reviewer_id: str,
        task_id: str,
        agent_id: str,
        attempt: int,
        config: ArborConfig,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        super().__init__(
            reviewer_id=reviewer_id,
            reviewer_type="code",
            model=config.reviewer_model,
            task_id=task_id,
            agent_id=agent_id,
            attempt=attempt,
            config=config,
            client=client,
        )

    def _apply_auto_fail(self, result: str, scores: dict) -> str:
        """Auto-fail if security dimension is 'fail'.

        Args:
            result: Current result.
            scores: Score dictionary from LLM.

        Returns:
            "fail" if security fails, otherwise original result.
        """
        for dim in _AUTO_FAIL_DIMENSIONS:
            if scores.get(dim) == "fail":
                logger.warning(
                    "CodeReviewer: auto-fail triggered for task %s "
                    "(dimension=%s scored 'fail')",
                    self.task_id, dim,
                )
                return "fail"

        # Also fail if any numeric dimension is below threshold (< 3)
        for dim, score in scores.items():
            if dim not in _AUTO_FAIL_DIMENSIONS:
                try:
                    if int(score) < 3:
                        return "fail"
                except (TypeError, ValueError):
                    pass

        return result
