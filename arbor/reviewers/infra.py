"""Infra reviewer for Arbor v2.

Rubric: reproducibility, secrets_check (auto-fail), compatibility, idempotency.
Auto-fail on secrets_check = fail.
"""

from __future__ import annotations

import logging

import anthropic

from arbor.config import ArborConfig
from arbor.reviewers.base import BaseReviewer

logger = logging.getLogger(__name__)


class InfraReviewer(BaseReviewer):
    """Reviewer for infra agent outputs."""

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
            reviewer_type="infra",
            model=config.reviewer_model,
            task_id=task_id,
            agent_id=agent_id,
            attempt=attempt,
            config=config,
            client=client,
        )

    def _apply_auto_fail(self, result: str, scores: dict) -> str:
        """Auto-fail on secrets_check = fail or any numeric < 3.

        Args:
            result: Current result.
            scores: Scores dict.

        Returns:
            Final result string.
        """
        if scores.get("secrets_check") == "fail":
            logger.warning(
                "InfraReviewer: auto-fail on secrets_check for task %s", self.task_id
            )
            return "fail"
        for dim, score in scores.items():
            if dim != "secrets_check":
                try:
                    if int(score) < 3:
                        return "fail"
                except (TypeError, ValueError):
                    pass
        return result
