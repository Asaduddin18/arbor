"""QA reviewer for Arbor v2.

Rubric: test_coverage, edge_case_handling, assertion_quality.
Pass threshold: all dimensions ≥ 3.
"""

from __future__ import annotations

import anthropic

from arbor.config import ArborConfig
from arbor.reviewers.base import BaseReviewer


class QAReviewer(BaseReviewer):
    """Reviewer for QA agent outputs."""

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
            reviewer_type="qa",
            model=config.reviewer_model,
            task_id=task_id,
            agent_id=agent_id,
            attempt=attempt,
            config=config,
            client=client,
        )

    def _apply_auto_fail(self, result: str, scores: dict) -> str:
        """Fail if any numeric dimension is below 3.

        Args:
            result: Current result.
            scores: Scores dict.

        Returns:
            Final result string.
        """
        for score in scores.values():
            try:
                if int(score) < 3:
                    return "fail"
            except (TypeError, ValueError):
                pass
        return result
