"""Orchestrator for Arbor v2.

The orchestrator is a PURE FUNCTION called by the scheduler.
    Input:  current WAL state + triggering event
    Output: list of WAL entries to append

It never directly spawns agents, writes files, or modifies state.
The scheduler reads the returned entries and executes them.

Uses claude-opus-4-6 for task decomposition.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import anthropic

from arbor.config import ArborConfig
from arbor.wal import (
    AgentState,
    TaskState,
    WalEntry,
    WalEventType,
    WalState,
    WalWriter,
)
from arbor.prompts.orchestrator import (
    TASK_DECOMPOSITION_SYSTEM,
    build_decomposition_prompt,
)

logger = logging.getLogger(__name__)

_MAX_JSON_RETRIES = 3


@dataclass
class OrchestratorInput:
    """Input to the orchestrator pure function.

    Attributes:
        wal_state: Current reconstructed WAL state.
        event: The triggering WAL entry.
        config: ArborConfig.
    """

    wal_state: WalState
    event: WalEntry
    config: ArborConfig


@dataclass
class OrchestratorOutput:
    """Output from the orchestrator — WAL entries to append.

    Attributes:
        wal_entries: Ordered list of entries for the scheduler to write.
    """

    wal_entries: list[WalEntry]


# ── Orchestrator helpers ──────────────────────────────────────────────────────


def should_absorb(
    task: TaskState, active_agents: dict[str, AgentState], config: ArborConfig
) -> AgentState | None:
    """Check if an existing agent can absorb a task without spawning new.

    All four conditions must hold for absorption:
    1. Type match
    2. Context budget < 60%
    3. Depth ≤ target depth (1 for top-level tasks)
    4. Agent is active (not spawned-but-not-started)

    Args:
        task: The TaskState to potentially absorb.
        active_agents: Dict of agent_id → AgentState from WAL state.
        config: ArborConfig for budget threshold.

    Returns:
        Eligible AgentState to absorb into, or None if no candidate.
    """
    budget_threshold = config.context_budget_per_agent * 0.6
    for agent in active_agents.values():
        if (
            agent.agent_type == task.task_type
            and agent.status in ("active", "started")
            and agent.tokens_used < budget_threshold
            and agent.depth <= 1  # only absorb into top-level agents
        ):
            return agent
    return None


def decide_spawn_depth(
    task: TaskState,
    parent_task_id: str | None,
    state: WalState,
) -> int:
    """Determine the depth at which a new agent should be spawned.

    Decision tree:
    - If task is a sub-problem of an in-progress task → parent depth + 1
    - Otherwise → depth 1 (sibling at top level)

    Depth only increases for specialisation gaps or distinct review cycles.

    Args:
        task: The task to spawn an agent for.
        parent_task_id: ID of the task this is a sub-problem of, or None.
        state: Current WalState.

    Returns:
        Integer depth for the new agent.
    """
    if parent_task_id and parent_task_id in state.tasks:
        parent_task = state.tasks[parent_task_id]
        if parent_task.assigned_agent_id and parent_task.assigned_agent_id in state.agents:
            parent_depth = state.agents[parent_task.assigned_agent_id].depth
            return parent_depth + 1
    return 1


def _build_agent_id(agent_type: str, depth: int, state: WalState) -> str:
    """Generate the next agent ID for a given type and depth.

    Format: agent-{type}-{depth}-{sequence:03d}

    Args:
        agent_type: Type string.
        depth: Tree depth.
        state: Current WalState (used to count existing agents of this type+depth).

    Returns:
        New unique agent ID string.
    """
    existing = [
        a for a in state.agents.values()
        if a.agent_type == agent_type and a.depth == depth
    ]
    seq = len(existing) + 1
    return f"agent-{agent_type}-{depth}-{seq:03d}"


def _select_model(agent_type: str, complexity: int, config: ArborConfig) -> str:
    """Select the appropriate model for an agent based on type and complexity.

    Args:
        agent_type: Agent type string.
        complexity: Task complexity score 1–10.
        config: ArborConfig with model settings.

    Returns:
        Model ID string.
    """
    if agent_type == "orchestrator":
        return config.orchestrator_model
    if agent_type in ("reviewer", "infra", "qa"):
        return config.reviewer_model
    # dev/research: scale with complexity
    if complexity >= 8:
        return config.orchestrator_model  # opus for hard tasks
    if complexity <= 3:
        return config.reviewer_model  # haiku for trivial tasks
    return config.agent_model_default  # sonnet default


# ── LLM-backed decomposition ──────────────────────────────────────────────────


async def decompose_goal(
    goal: str,
    state: WalState,
    writer: WalWriter,
    config: ArborConfig,
    client: anthropic.AsyncAnthropic | None = None,
) -> None:
    """Call the orchestrator LLM to decompose a goal into tasks.

    Writes TASK_PLANNED WAL entries (and AGENT_SPAWNED entries for chains)
    directly via the writer. Retries up to _MAX_JSON_RETRIES times on
    malformed JSON responses.

    Args:
        goal: The user-submitted goal.
        state: Current WalState.
        writer: WalWriter to append entries to.
        config: ArborConfig.
        client: Optional AsyncAnthropic client (created if not provided).

    Raises:
        ValueError: If the LLM returns invalid JSON after all retries.
    """
    if client is None:
        client = anthropic.AsyncAnthropic()

    active_agents_summary = [
        {
            "agent_id": a.agent_id,
            "agent_type": a.agent_type,
            "depth": a.depth,
            "tokens_used": a.tokens_used,
            "context_budget": a.context_budget,
            "tasks": a.tasks,
        }
        for a in state.agents.values()
        if a.status in ("active", "started")
    ]

    user_prompt = build_decomposition_prompt(goal, active_agents_summary)

    last_error: Exception | None = None
    error_context = ""

    for attempt in range(1, _MAX_JSON_RETRIES + 1):
        prompt = user_prompt
        if error_context:
            prompt += f"\n\nPrevious attempt returned invalid JSON: {error_context}. Please fix."

        logger.info("Orchestrator decompose_goal attempt %d/%d", attempt, _MAX_JSON_RETRIES)

        response = await client.messages.create(
            model=config.orchestrator_model,
            max_tokens=4096,
            system=TASK_DECOMPOSITION_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
            raw = raw.strip()

        try:
            data = json.loads(raw)
            _write_task_planned_entries(data, state, writer)
            logger.info(
                "Orchestrator: decomposed goal into %d tasks", len(data.get("tasks", []))
            )
            return
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            last_error = exc
            error_context = str(exc)
            logger.warning("Orchestrator JSON parse error (attempt %d): %s", attempt, exc)

    raise ValueError(
        f"Orchestrator failed to return valid JSON after {_MAX_JSON_RETRIES} attempts: {last_error}"
    )


def _write_task_planned_entries(
    decomposition: dict, state: WalState, writer: WalWriter
) -> None:
    """Write TASK_PLANNED WAL entries from a decomposition dict.

    Also writes AGENT_SPAWNED entries for chains with colocation=single-agent.

    Args:
        decomposition: Parsed JSON from the orchestrator LLM.
        state: Current WalState (for agent ID generation).
        writer: WalWriter to append to.
    """
    run_id = state.run_id or "unknown"
    tasks = decomposition.get("tasks", [])
    chains = {c["chain_id"]: c for c in decomposition.get("chains", [])}

    for task in tasks:
        writer.write(
            WalEventType.TASK_PLANNED,
            run_id=run_id,
            payload={
                "task_id": task["task_id"],
                "task_type": task.get("task_type", "dev"),
                "goal": task.get("goal", ""),
                "complexity": task.get("complexity", 5),
                "chain_id": task.get("chain_id"),
                "dependencies": task.get("dependencies", []),
            },
        )

    # Pre-spawn agents for colocated chains
    for chain in decomposition.get("chains", []):
        if chain.get("colocation") == "single-agent":
            chain_tasks = chain.get("tasks", [])
            if not chain_tasks:
                continue
            agent_type = chain.get("agent_type", "dev")
            # Build a minimal TaskState to get complexity
            complexity = 5
            for t in tasks:
                if t.get("task_id") == chain_tasks[0]:
                    complexity = t.get("complexity", 5)
                    break

            model = _select_model(agent_type, complexity, _get_dummy_config())
            depth = 1
            agent_id = _build_agent_id(agent_type, depth, state)

            writer.write(
                WalEventType.AGENT_SPAWNED,
                run_id=run_id,
                payload={
                    "agent_id": agent_id,
                    "agent_type": agent_type,
                    "model": model,
                    "depth": depth,
                    "initial_task_id": chain_tasks[0],
                    "chain_id": chain["chain_id"],
                    "chain_tasks": chain_tasks,
                    "context_budget_tokens": 8000,
                    "status": "open",
                },
            )


def _get_dummy_config() -> ArborConfig:
    """Return a default config for model selection when no config is available."""
    from arbor.config import get_default_config
    return get_default_config()


async def assign_next_task(
    state: WalState,
    completed_task_id: str,
    writer: WalWriter,
    config: ArborConfig,
) -> None:
    """After a task completes and passes review, assign the next pending task.

    Checks for chain continuation first, then absorption, then spawns new agent.

    Args:
        state: Current WalState.
        completed_task_id: The task that just passed review.
        writer: WalWriter.
        config: ArborConfig.
    """
    run_id = state.run_id or "unknown"
    completed_task = state.tasks.get(completed_task_id)
    if not completed_task:
        return

    # Find tasks in same chain that are still pending
    if completed_task.chain_id:
        chain_tasks = state.chains.get(completed_task.chain_id, [])
        for task_id in chain_tasks:
            t = state.tasks.get(task_id)
            if t and t.status == "planned":
                # Continue chain on same agent
                agent_id = completed_task.assigned_agent_id
                if agent_id and agent_id in state.agents:
                    writer.write(
                        WalEventType.TASK_ASSIGNED,
                        run_id=run_id,
                        payload={
                            "task_id": task_id,
                            "agent_id": agent_id,
                            "chain_continuation": True,
                        },
                    )
                    return

    # Find any unblocked planned task and try to assign it
    for task_id, task in state.tasks.items():
        if task.status != "planned":
            continue
        # Check dependencies satisfied
        deps_satisfied = all(
            state.tasks.get(d, TaskState("", "", "")).status == "reviewed_pass"
            for d in task.dependencies
        )
        if not deps_satisfied:
            continue

        candidate = should_absorb(task, state.agents, config)
        if candidate:
            writer.write(
                WalEventType.TASK_ASSIGNED,
                run_id=run_id,
                payload={"task_id": task_id, "agent_id": candidate.agent_id},
            )
        else:
            depth = decide_spawn_depth(task, None, state)
            agent_id = _build_agent_id(task.task_type, depth, state)
            model = _select_model(task.task_type, task.complexity, config)
            writer.write(
                WalEventType.AGENT_SPAWNED,
                run_id=run_id,
                payload={
                    "agent_id": agent_id,
                    "agent_type": task.task_type,
                    "model": model,
                    "depth": depth,
                    "initial_task_id": task_id,
                    "context_budget_tokens": config.context_budget_per_agent,
                    "status": "open",
                },
            )
        return  # assign one task per call


async def handle_task_failure(
    state: WalState,
    task_id: str,
    all_feedbacks: list[dict],
    writer: WalWriter,
    config: ArborConfig,
) -> None:
    """Handle a task that failed all review attempts.

    Writes a bug MD entry at depth 4 with all three reviewer feedbacks,
    then writes a TASK_FAILED WAL entry.

    Args:
        state: Current WalState.
        task_id: The failing task ID.
        all_feedbacks: List of review feedback dicts from all 3 attempts.
        writer: WalWriter.
        config: ArborConfig.
    """
    run_id = state.run_id or "unknown"
    task = state.tasks.get(task_id)
    if not task:
        return

    # Determine bug file path
    module = task_id.split("-")[0] if "-" in task_id else "unknown"
    bug_filename = f"bug-{task_id}.md"
    bug_path = f"memory/{module}/bugs/{bug_filename}"

    # Build bug MD content
    feedback_sections = "\n\n".join(
        f"### Attempt {i+1}\n{json.dumps(fb, indent=2)}"
        for i, fb in enumerate(all_feedbacks)
    )

    # Detect oscillation: check if same dimension kept failing
    failed_dims: dict[str, int] = {}
    for fb in all_feedbacks:
        for dim_feedback in fb.get("feedback", []):
            dim = dim_feedback.get("dimension", "unknown")
            score = dim_feedback.get("score", 5)
            is_failing = score == "fail" or (isinstance(score, int) and score < 3)
            if is_failing:
                failed_dims[dim] = failed_dims.get(dim, 0) + 1
    oscillating = [d for d, count in failed_dims.items() if count >= 2]

    bug_content = f"""# Bug Report: {task_id}

**Task goal:** {task.goal}
**Review attempts:** 3 (all failed)
**Oscillating dimensions:** {', '.join(oscillating) if oscillating else 'none detected'}

## Failure History

{feedback_sections}

## Analysis

The task failed {len(all_feedbacks)} consecutive reviews.
{'Oscillation detected in: ' + ', '.join(oscillating) + '. The agent kept failing the same dimensions, suggesting a fundamental misunderstanding of the requirement.' if oscillating else 'No clear oscillation pattern — failures were in different dimensions.'}

## Recommended Action

1. Review the original task goal for ambiguity
2. Check if the required context (dependencies) was available to the agent
3. Consider splitting this task into smaller subtasks
"""

    writer.write(
        WalEventType.MD_WRITTEN,
        run_id=run_id,
        payload={
            "md_path": bug_path,
            "md_hash": f"sha256:bug-{task_id}",
            "depth": 4,
            "is_bug_report": True,
        },
    )

    writer.write(
        WalEventType.TASK_FAILED,
        run_id=run_id,
        payload={
            "task_id": task_id,
            "agent_id": task.assigned_agent_id,
            "reason": "exceeded max review attempts",
            "bug_md_path": bug_path,
            "review_attempts": len(all_feedbacks),
            "oscillating_dimensions": oscillating,
        },
    )

    logger.error(
        "Task %s failed after %d attempts. Bug MD: %s",
        task_id, len(all_feedbacks), bug_path,
    )


# Fix missing import
import re
