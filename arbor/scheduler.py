"""Scheduler for Arbor v2.

The scheduler is a DETERMINISTIC, STATELESS event loop. It is NOT an LLM.

Loop:
    1. Read WAL top-to-bottom → reconstruct state
    2. Determine next actions (pure function, no I/O)
    3. Write WAL entries for those actions
    4. Execute the actions (spawn agents, reviewers, etc.)
    5. Repeat until RUN_COMPLETE

The scheduler is the ONLY component that spawns agents and reviewers.
Agents communicate back via WAL entries — they never call the scheduler directly.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Awaitable

from arbor.config import ArborConfig
from arbor.wal import (
    AgentState,
    TaskState,
    WalEventType,
    WalEntry,
    WalReader,
    WalState,
    WalWriter,
    build_state_from_wal,
)

logger = logging.getLogger(__name__)


class SchedulerAction(str, Enum):
    """Actions the scheduler can take based on WAL state."""

    PLAN_TASKS = "PLAN_TASKS"
    SPAWN_AGENT = "SPAWN_AGENT"
    ASSIGN_TASK = "ASSIGN_TASK"
    SPAWN_REVIEWER = "SPAWN_REVIEWER"
    SPAWN_AUDIT = "SPAWN_AUDIT"
    WRITE_HANDOFF = "WRITE_HANDOFF"
    MARK_COMPLETE = "MARK_COMPLETE"
    RECOVER = "RECOVER"
    WAIT = "WAIT"


@dataclass
class SchedulerStep:
    """A single action the scheduler intends to take.

    Attributes:
        action: The action type.
        task_id: Relevant task ID (if applicable).
        agent_id: Relevant agent ID (if applicable).
        reviewer_id: Relevant reviewer ID (if applicable).
        payload: Additional data for the action.
    """

    action: SchedulerAction
    task_id: str | None = None
    agent_id: str | None = None
    reviewer_id: str | None = None
    payload: dict | None = None


# ── Pure scheduling logic ─────────────────────────────────────────────────────


def determine_next_actions(state: WalState, config: ArborConfig) -> list[SchedulerStep]:
    """Determine what the scheduler should do next, given the current WAL state.

    This is a PURE FUNCTION — no I/O, no LLM calls, no side effects.
    Input is WalState; output is a list of SchedulerStep objects.

    Rules applied in priority order:
        1. RUN_START with no TASK_PLANNED → PLAN_TASKS
        2. AGENT_SPAWNED without AGENT_STARTED → re-SPAWN_AGENT
        3. TASK_COMPLETED + MD_WRITTEN without REVIEW_STARTED → SPAWN_REVIEWER
        4. REVIEW_RESULT(fail, attempt < max) → re-ASSIGN_TASK with feedback
        5. REVIEW_RESULT(fail, attempt == max) → already TASK_FAILED (handled)
        6. REVIEW_RESULT(pass) + pending tasks → SPAWN_AGENT or ASSIGN_TASK
        7. N completions since last audit → SPAWN_AUDIT
        8. All tasks reviewed_pass → MARK_COMPLETE
        9. Nothing actionable → WAIT

    Args:
        state: Current WalState from WAL replay.
        config: ArborConfig for thresholds.

    Returns:
        List of SchedulerStep objects to execute.
    """
    steps: list[SchedulerStep] = []

    if state.is_complete:
        return steps

    # No run started yet
    if state.run_id is None:
        return steps

    # Rule 1: RUN_START but no tasks planned yet
    if not state.tasks:
        steps.append(SchedulerStep(action=SchedulerAction.PLAN_TASKS))
        return steps

    # Rule 2: agents spawned but never started → re-spawn
    for agent_id, agent in state.agents.items():
        if agent.status == "spawned":
            steps.append(
                SchedulerStep(
                    action=SchedulerAction.SPAWN_AGENT,
                    agent_id=agent_id,
                    payload={"retry": True},
                )
            )

    # Rule 3 & 4: task review state
    for task_id, task in state.tasks.items():
        if task.status == "completed" and task.md_path:
            # TASK_COMPLETED + MD_WRITTEN but no review started
            steps.append(
                SchedulerStep(
                    action=SchedulerAction.SPAWN_REVIEWER,
                    task_id=task_id,
                    agent_id=task.assigned_agent_id,
                )
            )
        elif task.status == "reviewed_fail":
            if task.review_attempts < config.max_review_attempts:
                # Retry — re-assign to same agent with feedback
                steps.append(
                    SchedulerStep(
                        action=SchedulerAction.ASSIGN_TASK,
                        task_id=task_id,
                        agent_id=task.assigned_agent_id,
                        payload={"retry": True, "attempt": task.review_attempts + 1},
                    )
                )
            # else: max attempts reached — TASK_FAILED already written by previous step

    # Rule 5: assign unassigned planned tasks to agents
    for task_id, task in state.tasks.items():
        if task.status == "planned":
            # Check if an existing active agent can absorb this task
            absorb_candidate = _find_absorb_candidate(task, state, config)
            if absorb_candidate:
                steps.append(
                    SchedulerStep(
                        action=SchedulerAction.ASSIGN_TASK,
                        task_id=task_id,
                        agent_id=absorb_candidate.agent_id,
                    )
                )
            else:
                steps.append(
                    SchedulerStep(
                        action=SchedulerAction.SPAWN_AGENT,
                        task_id=task_id,
                    )
                )

    # Rule 6: audit trigger
    since_last_audit = state.task_completion_count - state.last_audit_at_count
    if (
        since_last_audit >= config.audit_every_n_tasks
        and state.task_completion_count > 0
        and not _audit_already_running(state)
    ):
        steps.append(SchedulerStep(action=SchedulerAction.SPAWN_AUDIT))

    # Rule 7: all tasks done?
    if _all_tasks_complete(state):
        steps.append(SchedulerStep(action=SchedulerAction.MARK_COMPLETE))
        return steps

    # If no actionable steps found, wait
    if not steps:
        steps.append(SchedulerStep(action=SchedulerAction.WAIT))

    return steps


def _find_absorb_candidate(
    task: TaskState, state: WalState, config: ArborConfig
) -> AgentState | None:
    """Find an existing active agent that can absorb a task.

    Args:
        task: The task to absorb.
        state: Current WalState.
        config: ArborConfig for budget thresholds.

    Returns:
        An eligible AgentState, or None if no suitable agent exists.
    """
    budget_threshold = config.context_budget_per_agent * 0.6
    for agent in state.agents.values():
        if (
            agent.agent_type == task.task_type
            and agent.status in ("active", "started")
            and agent.tokens_used < budget_threshold
        ):
            return agent
    return None


def _audit_already_running(state: WalState) -> bool:
    """Check if an audit agent is already active.

    Args:
        state: Current WalState.

    Returns:
        True if any audit agent is in spawned/active status.
    """
    for agent in state.agents.values():
        if agent.agent_type == "audit" and agent.status in ("spawned", "active", "started"):
            return True
    return False


def _all_tasks_complete(state: WalState) -> bool:
    """Check if every task has passed review.

    Args:
        state: Current WalState.

    Returns:
        True if state has tasks and all are in reviewed_pass status.
    """
    if not state.tasks:
        return False
    return all(t.status == "reviewed_pass" for t in state.tasks.values())


# ── Scheduler class ───────────────────────────────────────────────────────────


class Scheduler:
    """Main scheduler for Arbor v2.

    Reads the WAL, determines next actions, writes WAL entries, and executes
    those actions by calling injected handler functions.

    Agent/orchestrator calls are injected via constructor for testability —
    pass stubs in tests, real implementations in production.

    Args:
        wal_path: Path to wal.ndjson.
        config: ArborConfig.
        orchestrator_fn: Async callable for task planning. Signature:
            (goal: str, state: WalState, writer: WalWriter) -> None
        agent_fn: Async callable to spawn an agent. Signature:
            (agent_id: str, task_id: str, state: WalState, writer: WalWriter) -> None
        reviewer_fn: Async callable to spawn a reviewer. Signature:
            (task_id: str, agent_id: str, state: WalState, writer: WalWriter) -> None
        audit_fn: Async callable to spawn an audit agent. Signature:
            (state: WalState, writer: WalWriter) -> None
    """

    def __init__(
        self,
        wal_path: Path,
        config: ArborConfig,
        orchestrator_fn: Callable | None = None,
        agent_fn: Callable | None = None,
        reviewer_fn: Callable | None = None,
        audit_fn: Callable | None = None,
    ) -> None:
        self._wal_path = wal_path
        self._config = config
        self._writer = WalWriter(wal_path)
        self._orchestrator_fn = orchestrator_fn or _noop_orchestrator
        self._agent_fn = agent_fn or _noop_agent
        self._reviewer_fn = reviewer_fn or _noop_reviewer
        self._audit_fn = audit_fn or _noop_audit
        self._run_id: str | None = None

    def _read_state(self) -> WalState:
        """Rebuild state from WAL.

        Returns:
            Current WalState.
        """
        entries = WalReader.read_all(self._wal_path)
        return build_state_from_wal(entries)

    def start_run(self, goal: str) -> str:
        """Write RUN_START to WAL and return the run_id.

        Args:
            goal: The user-submitted goal string.

        Returns:
            New run_id string.
        """
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        self._run_id = run_id
        self._writer.write(
            WalEventType.RUN_START,
            run_id=run_id,
            payload={"goal": goal},
        )
        logger.info("Run started: %s  goal=%r", run_id, goal)
        return run_id

    async def step(self) -> bool:
        """Execute one scheduler iteration.

        Reads WAL, determines next actions, writes WAL entries, and
        executes each action.

        Returns:
            False when the run is complete or no more actions are possible,
            True to continue looping.
        """
        state = self._read_state()

        if state.is_complete:
            logger.info("Run %s is complete", state.run_id)
            return False

        if state.run_id:
            self._run_id = state.run_id

        steps = determine_next_actions(state, self._config)

        if not steps:
            return False

        all_wait = all(s.action == SchedulerAction.WAIT for s in steps)
        if all_wait:
            logger.debug("Scheduler: no actionable steps — waiting")
            return True  # caller should sleep and retry

        for s in steps:
            await self._execute_step(s, state)

        # Re-read state to check if complete
        state = self._read_state()
        return not state.is_complete

    async def _execute_step(self, step: SchedulerStep, state: WalState) -> None:
        """Execute a single SchedulerStep.

        Args:
            step: The step to execute.
            state: Current WalState (used to pass context to handlers).
        """
        run_id = state.run_id or self._run_id or "unknown"

        if step.action == SchedulerAction.PLAN_TASKS:
            logger.info("Scheduler: planning tasks for goal %r", state.goal)
            await self._orchestrator_fn(state.goal, state, self._writer)

        elif step.action == SchedulerAction.SPAWN_AGENT:
            task_id = step.task_id
            agent_id = step.agent_id
            if agent_id is None and task_id:
                # Determine agent type from task
                task = state.tasks.get(task_id)
                agent_type = task.task_type if task else "dev"
                depth = 1  # default; orchestrator sets depth in real implementation
                seq = len([a for a in state.agents.values() if a.agent_type == agent_type]) + 1
                agent_id = f"agent-{agent_type}-{depth}-{seq:03d}"

            if agent_id:
                self._writer.write(
                    WalEventType.AGENT_SPAWNED,
                    run_id=run_id,
                    payload={
                        "agent_id": agent_id,
                        "agent_type": state.tasks[task_id].task_type if task_id and task_id in state.tasks else "dev",
                        "model": self._config.agent_model_default,
                        "depth": 1,
                        "initial_task_id": task_id,
                        "context_budget_tokens": self._config.context_budget_per_agent,
                        "status": "open",
                    },
                )
                logger.info("Scheduler: spawning agent %s for task %s", agent_id, task_id)
                await self._agent_fn(agent_id, task_id, state, self._writer)

        elif step.action == SchedulerAction.ASSIGN_TASK:
            task_id = step.task_id
            agent_id = step.agent_id
            payload = step.payload or {}
            self._writer.write(
                WalEventType.TASK_ASSIGNED,
                run_id=run_id,
                payload={
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "retry": payload.get("retry", False),
                    "attempt": payload.get("attempt", 1),
                    "retry_feedback": payload.get("retry_feedback"),
                },
            )
            logger.info("Scheduler: assigned task %s to agent %s", task_id, agent_id)
            if agent_id:
                await self._agent_fn(agent_id, task_id, state, self._writer)

        elif step.action == SchedulerAction.SPAWN_REVIEWER:
            task_id = step.task_id
            agent_id = step.agent_id
            reviewer_id = f"reviewer-{task_id}"
            self._writer.write(
                WalEventType.REVIEW_STARTED,
                run_id=run_id,
                payload={
                    "reviewer_id": reviewer_id,
                    "task_id": task_id,
                    "agent_id": agent_id,
                },
            )
            logger.info("Scheduler: spawning reviewer %s for task %s", reviewer_id, task_id)
            await self._reviewer_fn(task_id, agent_id, state, self._writer)

        elif step.action == SchedulerAction.SPAWN_AUDIT:
            audit_id = f"audit-{state.task_completion_count:03d}"
            self._writer.write(
                WalEventType.AUDIT_STARTED,
                run_id=run_id,
                payload={"audit_id": audit_id},
            )
            logger.info("Scheduler: spawning audit agent %s", audit_id)
            await self._audit_fn(state, self._writer)

        elif step.action == SchedulerAction.MARK_COMPLETE:
            self._writer.write(
                WalEventType.RUN_COMPLETE,
                run_id=run_id,
                payload={
                    "tasks_completed": len(
                        [t for t in state.tasks.values() if t.status == "reviewed_pass"]
                    ),
                    "total_tokens": sum(a.tokens_used for a in state.agents.values()),
                },
            )
            logger.info("Scheduler: run %s marked complete", run_id)

    async def run(self, goal: str, max_iterations: int = 1000) -> None:
        """Run the scheduler loop until completion or iteration limit.

        Args:
            goal: The user-submitted goal.
            max_iterations: Safety cap on loop iterations.
        """
        self.start_run(goal)
        for i in range(max_iterations):
            should_continue = await self.step()
            if not should_continue:
                break
            await asyncio.sleep(0.05)  # yield to event loop
        else:
            logger.warning("Scheduler: reached max_iterations=%d", max_iterations)


# ── No-op stubs (used when no handler is injected) ───────────────────────────


async def _noop_orchestrator(goal: str, state: WalState, writer: WalWriter) -> None:
    """Stub orchestrator — does nothing. Replace in production."""
    logger.debug("noop orchestrator called for goal %r", goal)


async def _noop_agent(
    agent_id: str, task_id: str | None, state: WalState, writer: WalWriter
) -> None:
    """Stub agent — does nothing. Replace in production."""
    logger.debug("noop agent called: agent=%s task=%s", agent_id, task_id)


async def _noop_reviewer(
    task_id: str | None, agent_id: str | None, state: WalState, writer: WalWriter
) -> None:
    """Stub reviewer — does nothing. Replace in production."""
    logger.debug("noop reviewer called: task=%s agent=%s", task_id, agent_id)


async def _noop_audit(state: WalState, writer: WalWriter) -> None:
    """Stub audit agent — does nothing. Replace in production."""
    logger.debug("noop audit called")
