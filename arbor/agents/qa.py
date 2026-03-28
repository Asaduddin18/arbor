"""QA agent for Arbor v2.

Executes test writing and coverage tasks.
Default model: claude-haiku-4-5-20251001.
Reviewer pair: QAReviewer.
Cannot spawn children (QA is a leaf agent type).
"""

from __future__ import annotations

import logging

import anthropic

from arbor.agents.base import BaseAgent, AgentTaskResult
from arbor.config import ArborConfig
from arbor.memory.tree import MemoryTree
from arbor.memory.versioner import write_versioned_md
from arbor.prompts.agents import build_agent_system_prompt

logger = logging.getLogger(__name__)

_QA_TASK_INSTRUCTIONS = """
You are executing a QA/testing task. Produce a Markdown document with:

## Goal
## Approach
## Test Cases
## Output
## Handoff notes

Under ## Test Cases: list each test case with:
  - Name
  - Input
  - Expected output
  - Edge cases covered

Under ## Output: include the actual test code.
Ensure tests cover: happy path, edge cases (null, empty, boundary values), and error conditions.
"""


class QAAgent(BaseAgent):
    """QA agent for test writing and coverage.

    Args:
        agent_id: Unique agent identifier.
        depth: Tree depth.
        config: ArborConfig.
        memory_tree: MemoryTree.
        client: Optional AsyncAnthropic client.
    """

    def __init__(
        self,
        agent_id: str,
        depth: int,
        config: ArborConfig,
        memory_tree: MemoryTree,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            agent_type="qa",
            depth=depth,
            model=config.reviewer_model,  # haiku for QA
            config=config,
            memory_tree=memory_tree,
            client=client,
        )

    async def execute_task(
        self, task: dict, retry_feedback: str | None = None
    ) -> AgentTaskResult:
        """Execute a QA task.

        Args:
            task: Task dict with task_id and goal.
            retry_feedback: Feedback from failed review.

        Returns:
            AgentTaskResult.
        """
        task_id = task.get("task_id", "unknown")
        goal = task.get("goal", "")
        context = self._build_context(task)

        system_prompt = build_agent_system_prompt(
            agent_id=self.agent_id,
            agent_type=self.agent_type,
            depth=self.depth,
            context_budget=self.config.context_budget_per_agent,
            task_chain=[task],
            project_context=context,
            module_context="",
            dependencies="",
            working_dir=str(self.memory_tree.base_path),
        )
        system_prompt += "\n\n" + _QA_TASK_INSTRUCTIONS

        user_message = f"Execute this QA task:\n\n**Task ID:** {task_id}\n**Goal:** {goal}"
        if retry_feedback:
            user_message += f"\n\n{retry_feedback}"

        response = await self._client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        raw_text = response.content[0].text
        tokens_used = response.usage.input_tokens + response.usage.output_tokens
        md_content = self._extract_md_content(raw_text)

        md_rel_path = self._build_md_path(task)
        md_abs_path = self._resolve_md_abs_path(md_rel_path)
        md_hash = write_versioned_md(md_abs_path, md_content, wal_commit_id="pending")

        return AgentTaskResult(
            task_id=task_id,
            md_path=md_rel_path,
            md_hash=md_hash,
            tokens_used=tokens_used,
            raw_response=raw_text,
        )

    def _resolve_md_abs_path(self, rel_path: str):
        parts = rel_path.split("/")
        if parts[0] == "memory":
            parts = parts[1:]
        return self.memory_tree.base_path.joinpath(*parts)
