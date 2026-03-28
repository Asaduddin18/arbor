"""Dev agent for Arbor v2.

Executes development tasks — code implementation, API design, feature building.
Writes MD files at depth 2 (task completion records).
Default model: claude-sonnet-4-6.
Reviewer pair: CodeReviewer.
"""

from __future__ import annotations

import logging
from pathlib import Path

from arbor.agents.base import BaseAgent, AgentTaskResult
from arbor.config import ArborConfig
from arbor.memory.tree import MemoryTree
from arbor.memory.versioner import write_versioned_md
from arbor.prompts.agents import build_agent_system_prompt

import anthropic

logger = logging.getLogger(__name__)

_DEV_TASK_INSTRUCTIONS = """
You are executing a development task. Produce a Markdown document that:

1. Has these exact section headings (with ## prefix):
   ## Goal
   ## Approach
   ## Output
   ## Handoff notes

2. Under ## Goal: restate the task goal in your own words.
3. Under ## Approach: describe the design decisions and reasoning.
4. Under ## Output: the actual work product — code, config, schema, etc.
   Wrap code in fenced blocks with the appropriate language tag.
5. Under ## Handoff notes: what a subsequent agent needs to know.
   List files written, key decisions, open questions, blockers.

Be specific and factual. Do not claim to have done things you haven't done.
If something is uncertain, say so explicitly.
"""


class DevAgent(BaseAgent):
    """Development agent for code implementation tasks.

    Args:
        agent_id: Unique agent identifier.
        depth: Tree depth (typically 1 or 2).
        config: ArborConfig.
        memory_tree: MemoryTree for this run.
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
            agent_type="dev",
            depth=depth,
            model=config.agent_model_default,  # claude-sonnet-4-6
            config=config,
            memory_tree=memory_tree,
            client=client,
        )

    async def execute_task(
        self, task: dict, retry_feedback: str | None = None
    ) -> AgentTaskResult:
        """Execute a development task and write an MD file.

        Args:
            task: Task dict with task_id, goal, and optional context_files.
            retry_feedback: Structured reviewer feedback for retry attempts.

        Returns:
            AgentTaskResult with MD path, hash, and token count.
        """
        task_id = task.get("task_id", "unknown")
        goal = task.get("goal", "")

        # Build context slice from memory tree
        context = self._build_context(task)

        # Build system prompt
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
        system_prompt += "\n\n" + _DEV_TASK_INSTRUCTIONS

        # Build user message
        user_message = f"Execute this task:\n\n**Task ID:** {task_id}\n**Goal:** {goal}"
        if retry_feedback:
            user_message += f"\n\n{retry_feedback}"

        logger.info("DevAgent %s calling LLM for task %s", self.agent_id, task_id)
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        raw_text = response.content[0].text
        tokens_used = response.usage.input_tokens + response.usage.output_tokens

        # Extract MD content (strip JSON blocks for WAL metadata)
        md_content = self._extract_md_content(raw_text)

        # Extract any JSON blocks (spawn requests, cross-branch reads)
        json_blocks = self._extract_json_blocks(raw_text)
        spawn_requests = [b["spawn_request"] for b in json_blocks if "spawn_request" in b]
        cross_branch_requests = [
            b["cross_branch_read_request"]
            for b in json_blocks
            if "cross_branch_read_request" in b
        ]

        # Determine output MD path and write
        md_rel_path = self._build_md_path(task)
        md_abs_path = self._resolve_md_abs_path(md_rel_path)
        md_hash = write_versioned_md(md_abs_path, md_content, wal_commit_id="pending")

        logger.info(
            "DevAgent %s completed task %s → %s (tokens=%d)",
            self.agent_id, task_id, md_rel_path, tokens_used,
        )

        return AgentTaskResult(
            task_id=task_id,
            md_path=md_rel_path,
            md_hash=md_hash,
            tokens_used=tokens_used,
            spawn_requests=spawn_requests,
            cross_branch_requests=cross_branch_requests,
            raw_response=raw_text,
        )

    def _resolve_md_abs_path(self, rel_path: str) -> Path:
        """Convert a relative memory path to an absolute filesystem path.

        Args:
            rel_path: Relative path like "memory/auth/jwt-impl.md".

        Returns:
            Absolute Path.
        """
        # rel_path starts with "memory/" — resolve relative to tree base's parent
        parts = rel_path.split("/")
        if parts[0] == "memory":
            parts = parts[1:]  # strip "memory/" prefix
        return self.memory_tree.base_path.joinpath(*parts)
