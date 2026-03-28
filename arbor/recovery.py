"""Crash recovery via WAL replay for Arbor v2.

On startup, if the WAL exists and has open (incomplete) entries, the recovery
module reconstructs system state and emits the actions needed to resume.

All recovery actions are idempotent — calling recover() twice on the same WAL
produces the same result.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from arbor.wal import (
    WalEventType,
    WalReader,
    WalState,
    WalWriter,
    build_state_from_wal,
)
from arbor.config import ArborConfig

logger = logging.getLogger(__name__)


class RecoveryActionType(str, Enum):
    """Type of recovery action needed."""

    RESPAWN_AGENT = "RESPAWN_AGENT"
    SPAWN_REVIEWER = "SPAWN_REVIEWER"
    RESPAWN_REVIEWER = "RESPAWN_REVIEWER"
    RESPAWN_AUDIT = "RESPAWN_AUDIT"
    RESUME_TASK = "RESUME_TASK"


@dataclass
class RecoveryAction:
    """A single recovery action to be executed after WAL replay.

    Attributes:
        action_type: What kind of recovery to perform.
        agent_id: Relevant agent ID (if applicable).
        task_id: Relevant task ID (if applicable).
        reviewer_id: Relevant reviewer ID (if applicable).
        reason: Human-readable explanation of why this action is needed.
    """

    action_type: RecoveryActionType
    agent_id: str | None = None
    task_id: str | None = None
    reviewer_id: str | None = None
    reason: str = ""


def detect_incomplete_entries(state: WalState) -> list[RecoveryAction]:
    """Identify WAL entries that started but never completed.

    Examines the reconstructed WalState to find:
    - Agents that were spawned but never confirmed start → re-spawn
    - Tasks that completed but were never sent to review → spawn reviewer
    - Reviews that started but never returned a result → re-spawn reviewer
    - Audit agents that started but never returned results → re-spawn audit

    Args:
        state: WalState reconstructed from full WAL replay.

    Returns:
        Ordered list of RecoveryAction objects describing what must be done.
    """
    actions: list[RecoveryAction] = []

    # Check each agent
    for agent_id, agent in state.agents.items():
        if agent.status == "spawned":
            # AGENT_SPAWNED written but AGENT_STARTED never arrived
            actions.append(
                RecoveryAction(
                    action_type=RecoveryActionType.RESPAWN_AGENT,
                    agent_id=agent_id,
                    reason=f"Agent {agent_id} was spawned but never confirmed start",
                )
            )
            logger.warning("Recovery: agent %s needs re-spawn (never started)", agent_id)

    # Check each task for review gaps
    for task_id, task in state.tasks.items():
        if task.status == "completed" and task.md_path:
            # TASK_COMPLETED + MD_WRITTEN written but no REVIEW_STARTED
            actions.append(
                RecoveryAction(
                    action_type=RecoveryActionType.SPAWN_REVIEWER,
                    task_id=task_id,
                    agent_id=task.assigned_agent_id,
                    reason=f"Task {task_id} completed but review never started",
                )
            )
            logger.warning(
                "Recovery: task %s needs reviewer (completed but not reviewed)", task_id
            )

    # Check for reviews that started but produced no result
    for reviewer_id, status in state.reviewer_states.items():
        if status == "started":
            # REVIEW_STARTED written but no REVIEW_RESULT
            actions.append(
                RecoveryAction(
                    action_type=RecoveryActionType.RESPAWN_REVIEWER,
                    reviewer_id=reviewer_id,
                    reason=f"Reviewer {reviewer_id} started but never returned a result",
                )
            )
            logger.warning(
                "Recovery: reviewer %s needs re-spawn (started but no result)", reviewer_id
            )

    logger.info("Recovery analysis complete: %d actions needed", len(actions))
    return actions


def is_recovery_needed(wal_path: Path) -> bool:
    """Check whether a WAL file exists and contains incomplete entries.

    Args:
        wal_path: Path to wal.ndjson.

    Returns:
        True if the WAL exists, has a RUN_START, and is missing RUN_COMPLETE
        or has incomplete agent/review entries.
    """
    if not wal_path.exists():
        return False

    try:
        entries = WalReader.read_all(wal_path)
    except Exception:
        # Corrupted WAL — recovery definitely needed
        return True

    if not entries:
        return False

    state = build_state_from_wal(entries)

    # No run started
    if state.run_id is None:
        return False

    # Run already completed cleanly
    if state.is_complete:
        return False

    # Run started but not complete — recovery needed
    return True


def recover(wal_path: Path, config: ArborConfig) -> tuple[WalState, list[RecoveryAction]]:
    """Perform WAL replay and determine recovery actions.

    Reads the entire WAL, writes a CRASH_DETECTED entry, reconstructs state,
    detects incomplete entries, and writes a RECOVERY_REPLAY entry for each.

    This function does NOT execute recovery actions — it returns them for the
    scheduler to execute. This keeps recover() pure and testable.

    Args:
        wal_path: Path to wal.ndjson.
        config: ArborConfig (used for context budgets etc.).

    Returns:
        Tuple of (reconstructed WalState, list of RecoveryAction).

    Raises:
        WalCorruptError: If the WAL file cannot be parsed.
    """
    logger.info("Starting WAL recovery from %s", wal_path)

    entries = WalReader.read_all(wal_path)
    state = build_state_from_wal(entries)

    if state.run_id is None:
        logger.info("Recovery: no active run found in WAL")
        return state, []

    writer = WalWriter(wal_path)

    # Write CRASH_DETECTED before doing anything else
    writer.write(
        WalEventType.CRASH_DETECTED,
        run_id=state.run_id,
        payload={
            "entries_replayed": len(entries),
            "agents_found": len(state.agents),
            "tasks_found": len(state.tasks),
            "is_complete": state.is_complete,
        },
    )
    logger.info("CRASH_DETECTED written for run %s", state.run_id)

    if state.is_complete:
        logger.info("Recovery: run %s was already complete — nothing to do", state.run_id)
        return state, []

    actions = detect_incomplete_entries(state)

    # Write a RECOVERY_REPLAY entry for each action
    for action in actions:
        writer.write(
            WalEventType.RECOVERY_REPLAY,
            run_id=state.run_id,
            payload={
                "action_type": action.action_type.value,
                "agent_id": action.agent_id,
                "task_id": action.task_id,
                "reviewer_id": action.reviewer_id,
                "reason": action.reason,
            },
        )

    logger.info(
        "Recovery complete: %d actions to execute for run %s",
        len(actions),
        state.run_id,
    )
    return state, actions
