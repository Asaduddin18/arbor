"""Base agent class for Arbor v2.

All agent types inherit from BaseAgent. The lifecycle is:
    PLANNED → SPAWNED → ACTIVE → [TASK_LOOP] → HANDOFF | COMPLETE

Agents communicate only via the WAL and the memory tree.
They never spawn other agents directly — they emit spawn_request signals
which the orchestrator validates and the scheduler executes.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from arbor.config import ArborConfig
from arbor.memory.tree import MemoryTree
from arbor.memory.versioner import write_versioned_md
from arbor.memory.slicer import build_context_slice
from arbor.prompts.agents import build_agent_system_prompt
from arbor.wal import WalEventType, WalWriter

logger = logging.getLogger(__name__)


@dataclass
class AgentTaskResult:
    """Result of a single task execution.

    Attributes:
        task_id: The task that was executed.
        md_path: Filesystem path where the MD was written.
        md_hash: SHA-256 hash of the MD body.
        tokens_used: Tokens consumed by this task.
        chain_continues: True if the agent should continue to next task.
        next_task_id: ID of the next task in the chain (if chain_continues).
        spawn_requests: List of spawn request dicts from the LLM output.
        cross_branch_requests: List of cross-branch read request strings.
        raw_response: Full LLM response text (for debugging).
    """

    task_id: str
    md_path: str
    md_hash: str
    tokens_used: int
    chain_continues: bool = False
    next_task_id: str | None = None
    spawn_requests: list[dict] = field(default_factory=list)
    cross_branch_requests: list[str] = field(default_factory=list)
    raw_response: str = ""


class BaseAgent(ABC):
    """Abstract base class for all Arbor agents.

    Args:
        agent_id: Unique agent identifier (e.g. "agent-dev-1-001").
        agent_type: Type string (dev, infra, research, qa, audit).
        depth: Tree depth this agent operates at.
        model: LLM model ID to use.
        config: ArborConfig.
        memory_tree: MemoryTree instance for this run.
        client: Optional AsyncAnthropic client (created if not provided).
    """

    def __init__(
        self,
        agent_id: str,
        agent_type: str,
        depth: int,
        model: str,
        config: ArborConfig,
        memory_tree: MemoryTree,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.depth = depth
        self.model = model
        self.config = config
        self.memory_tree = memory_tree
        self._client = client or anthropic.AsyncAnthropic()
        self.tokens_used = 0
        self._task_queue: list[dict] = []
        self._completed_tasks: list[str] = []

    # ── Public lifecycle ──────────────────────────────────────────────────

    async def run(
        self,
        task: dict,
        writer: WalWriter,
        run_id: str,
        retry_feedback: str | None = None,
    ) -> AgentTaskResult:
        """Execute a single task and write WAL entries for it.

        Writes AGENT_STARTED (first task only), then TASK_COMPLETED +
        MD_WRITTEN after execution. Checks context budget after each task
        and triggers handoff if needed.

        Args:
            task: Task dict with task_id, goal, and optional chain info.
            writer: WalWriter to append WAL entries.
            run_id: Active run ID.
            retry_feedback: Structured feedback from a previous failed review.

        Returns:
            AgentTaskResult with all output metadata.
        """
        task_id = task.get("task_id", "unknown")
        logger.info("Agent %s starting task %s", self.agent_id, task_id)

        # Write AGENT_STARTED on first task
        if not self._completed_tasks:
            writer.write(
                WalEventType.AGENT_STARTED,
                run_id=run_id,
                payload={"agent_id": self.agent_id},
            )

        result = await self.execute_task(task, retry_feedback=retry_feedback)
        self.tokens_used += result.tokens_used
        self._completed_tasks.append(task_id)

        writer.write(
            WalEventType.TASK_COMPLETED,
            run_id=run_id,
            payload={
                "agent_id": self.agent_id,
                "task_id": task_id,
                "tokens_used": result.tokens_used,
                "md_path": result.md_path,
                "md_hash": result.md_hash,
                "chain_continues": result.chain_continues,
                "next_task_id": result.next_task_id,
            },
        )

        writer.write(
            WalEventType.MD_WRITTEN,
            run_id=run_id,
            payload={
                "md_path": result.md_path,
                "md_hash": result.md_hash,
                "agent_id": self.agent_id,
                "task_id": task_id,
            },
        )

        # Check context budget
        budget_used_pct = self.tokens_used / self.config.context_budget_per_agent
        if budget_used_pct > 0.6:
            logger.info(
                "Agent %s context at %.0f%% — triggering handoff", self.agent_id, budget_used_pct * 100
            )
            handoff_path, handoff_hash = await self._write_handoff(task_id, writer, run_id)

        return result

    # ── Abstract methods ──────────────────────────────────────────────────

    @abstractmethod
    async def execute_task(
        self, task: dict, retry_feedback: str | None = None
    ) -> AgentTaskResult:
        """Execute a single task. Implemented by each agent subclass.

        Args:
            task: Task dict with task_id, goal, and context.
            retry_feedback: Feedback from a failed review to inject into prompt.

        Returns:
            AgentTaskResult.
        """

    # ── Context building ──────────────────────────────────────────────────

    def _build_context(self, task: dict) -> str:
        """Build the context slice for a task from the memory tree.

        Reads up (project root + module overview) and any declared
        dependency files. Strips injection patterns. Enforces budget.

        Args:
            task: Task dict that may contain 'context_files' list.

        Returns:
            Assembled context string.
        """
        # Determine the module from the task_id or goal
        module = self._infer_module(task)

        files_to_read: list[tuple[Path, str | None]] = []

        # Always include depth-0 and depth-1 context
        root_path = self.memory_tree.resolve_path(0)
        if root_path.exists():
            files_to_read.append((root_path, None))

        if module:
            overview_path = self.memory_tree.resolve_path(1, module=module)
            if overview_path.exists():
                files_to_read.append((overview_path, None))

        # Add any declared context files
        for ctx in task.get("context_files", []):
            if "#" in ctx:
                file_path, anchor = ctx.split("#", 1)
            else:
                file_path, anchor = ctx, None
            full_path = self.memory_tree.base_path / file_path.lstrip("memory/")
            files_to_read.append((full_path, anchor))

        context_budget = self.config.context_budget_per_agent // 3  # use 1/3 for context
        return build_context_slice(files_to_read, budget=context_budget)

    def _infer_module(self, task: dict) -> str:
        """Infer the module name from a task's ID or goal.

        Args:
            task: Task dict.

        Returns:
            Module name string (e.g. "auth"), or empty string if unknown.
        """
        task_id = task.get("task_id", "")
        if "-" in task_id:
            return task_id.split("-")[0]
        return ""

    def _build_md_path(self, task: dict) -> str:
        """Determine the output MD file path for a task.

        Args:
            task: Task dict with task_id.

        Returns:
            Relative path string for the MD file.
        """
        task_id = task.get("task_id", "unknown")
        module = self._infer_module(task) or "general"
        return f"memory/{module}/{task_id}.md"

    # ── Handoff MD generation ─────────────────────────────────────────────

    async def _write_handoff(
        self, last_task_id: str, writer: WalWriter, run_id: str
    ) -> tuple[str, str]:
        """Generate and write a handoff MD when context budget is exceeded.

        Args:
            last_task_id: The last completed task ID.
            writer: WalWriter.
            run_id: Active run ID.

        Returns:
            Tuple of (handoff_md_path, handoff_md_hash).
        """
        handoff_content = self.generate_handoff_md(last_task_id)
        module = self._infer_module({"task_id": last_task_id}) or "general"
        handoff_filename = f"handoff-{last_task_id}"
        handoff_path = self.memory_tree.resolve_path(2, module=module, filename=handoff_filename)
        handoff_rel = f"memory/{module}/handoff-{last_task_id}.md"

        handoff_hash = write_versioned_md(
            handoff_path, handoff_content, wal_commit_id="handoff"
        )

        writer.write(
            WalEventType.HANDOFF_WRITTEN,
            run_id=run_id,
            payload={
                "agent_id": self.agent_id,
                "last_task_id": last_task_id,
                "handoff_path": handoff_rel,
                "handoff_hash": handoff_hash,
                "tokens_used": self.tokens_used,
                "completed_tasks": self._completed_tasks,
            },
        )
        logger.info("Agent %s wrote handoff MD: %s", self.agent_id, handoff_rel)
        return handoff_rel, handoff_hash

    def generate_handoff_md(self, last_task_id: str) -> str:
        """Generate handoff MD content capturing this agent's working state.

        The handoff MD allows a receiving agent to reconstruct working context
        without reading the full conversation history.

        Args:
            last_task_id: The last task this agent completed.

        Returns:
            Markdown string for the handoff file.
        """
        completed = "\n".join(f"- {t}" for t in self._completed_tasks) or "(none)"
        return f"""## Completed Tasks

{completed}

## Key Decisions

*(The agent should document key design decisions made during its task chain here.)*

## Active State

- Agent ID: {self.agent_id}
- Agent type: {self.agent_type}
- Tokens used: {self.tokens_used} / {self.config.context_budget_per_agent}
- Last completed task: {last_task_id}

## What Receiving Agent Must Do

1. Read this handoff file first
2. Read the completed task MD files listed above
3. Continue from the next unassigned task in the chain

## Files to Read

*(The receiving agent should read the MD files for each completed task listed above.)*
"""

    # ── Response parsing ──────────────────────────────────────────────────

    @staticmethod
    def _extract_json_blocks(text: str) -> list[dict]:
        """Extract JSON objects from fenced code blocks in LLM output.

        Args:
            text: Raw LLM response text.

        Returns:
            List of parsed JSON objects found in ```json ... ``` blocks.
        """
        results: list[dict] = []
        pattern = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)
        for match in pattern.finditer(text):
            try:
                obj = json.loads(match.group(1))
                results.append(obj)
            except json.JSONDecodeError:
                pass
        return results

    @staticmethod
    def _extract_md_content(text: str) -> str:
        """Extract the main Markdown content from an LLM response.

        Strips JSON blocks and trims whitespace.

        Args:
            text: Raw LLM response text.

        Returns:
            Cleaned Markdown content.
        """
        # Remove JSON code blocks
        cleaned = re.sub(r"```json\s*.*?\s*```", "", text, flags=re.DOTALL)
        # Remove other code fence markers but keep content
        return cleaned.strip()
