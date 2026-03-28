"""Fact reviewer for Arbor v2.

Rubric: source_support, internal_consistency (pass/fail), cross_file_consistency
        (pass/fail), actionability.
Pass threshold: all numeric ≥ 3, both pass/fail = pass.
"""

from __future__ import annotations

import logging

import anthropic

from arbor.config import ArborConfig
from arbor.reviewers.base import BaseReviewer

logger = logging.getLogger(__name__)

_PASS_FAIL_DIMENSIONS = {"internal_consistency", "cross_file_consistency"}


class FactReviewer(BaseReviewer):
    """Reviewer for research agent outputs."""

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
            reviewer_type="fact",
            model=config.reviewer_model,
            task_id=task_id,
            agent_id=agent_id,
            attempt=attempt,
            config=config,
            client=client,
        )

    def _apply_auto_fail(self, result: str, scores: dict) -> str:
        """Fail if any pass/fail dimension is 'fail' or numeric < 3.

        Args:
            result: Current result.
            scores: Scores dict.

        Returns:
            Final result string.
        """
        for dim in _PASS_FAIL_DIMENSIONS:
            if scores.get(dim) == "fail":
                logger.warning(
                    "FactReviewer: fail on %s for task %s", dim, self.task_id
                )
                return "fail"
        for dim, score in scores.items():
            if dim not in _PASS_FAIL_DIMENSIONS:
                try:
                    if int(score) < 3:
                        return "fail"
                except (TypeError, ValueError):
                    pass
        return result
