"""Write-Ahead Log (WAL) for Arbor v2.

The WAL is an append-only NDJSON file. Every action is recorded before it is
executed. The WAL is the source of truth for system state — it is never
modified or deleted, only appended to.

Entry format (one JSON object per line):
    {
        "wal_id": "w-0042",
        "event": "AGENT_SPAWNED",
        "timestamp": "2025-03-22T14:32:11.442Z",
        "run_id": "run-abc123",
        "payload": { ... }
    }
"""

import json
import logging
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)


class WalEventType(str, Enum):
    """All valid WAL event types."""

    RUN_START = "RUN_START"
    TASK_PLANNED = "TASK_PLANNED"
    AGENT_SPAWNED = "AGENT_SPAWNED"
    TASK_ASSIGNED = "TASK_ASSIGNED"
    AGENT_STARTED = "AGENT_STARTED"
    TASK_COMPLETED = "TASK_COMPLETED"
    MD_WRITTEN = "MD_WRITTEN"
    REVIEW_STARTED = "REVIEW_STARTED"
    REVIEW_RESULT = "REVIEW_RESULT"
    TASK_FAILED = "TASK_FAILED"
    AUDIT_STARTED = "AUDIT_STARTED"
    AUDIT_RESULT = "AUDIT_RESULT"
    MD_FLAGGED = "MD_FLAGGED"
    HANDOFF_WRITTEN = "HANDOFF_WRITTEN"
    RUN_COMPLETE = "RUN_COMPLETE"
    CRASH_DETECTED = "CRASH_DETECTED"
    RECOVERY_REPLAY = "RECOVERY_REPLAY"


class WalCorruptError(Exception):
    """Raised when a WAL line cannot be parsed."""


@dataclass
class WalEntry:
    """A single WAL entry.

    Attributes:
        wal_id: Monotonically increasing identifier, format "w-NNNN".
        event: The event type.
        timestamp: ISO 8601 UTC timestamp string.
        run_id: The run this entry belongs to.
        payload: Event-specific data dictionary.
    """

    wal_id: str
    event: WalEventType
    timestamp: str
    run_id: str
    payload: dict

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON encoding.

        Returns:
            Dictionary with all fields; event is the string value.
        """
        return {
            "wal_id": self.wal_id,
            "event": self.event.value,
            "timestamp": self.timestamp,
            "run_id": self.run_id,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WalEntry":
        """Deserialize from a plain dict.

        Args:
            data: Dictionary from JSON parsing.

        Returns:
            WalEntry instance.

        Raises:
            KeyError: If a required field is missing.
            ValueError: If the event type is unknown.
        """
        return cls(
            wal_id=data["wal_id"],
            event=WalEventType(data["event"]),
            timestamp=data["timestamp"],
            run_id=data["run_id"],
            payload=data.get("payload", {}),
        )


# ── State dataclasses reconstructed from WAL ─────────────────────────────────


@dataclass
class AgentState:
    """State of a single agent as reconstructed from the WAL.

    Attributes:
        agent_id: Unique agent identifier.
        agent_type: Type string (dev, infra, research, qa, audit).
        model: LLM model ID used by this agent.
        depth: Tree depth at which this agent operates.
        status: Current lifecycle status.
        parent_agent_id: ID of parent agent, or None for top-level.
        context_budget: Token budget for this agent.
        tokens_used: Tokens consumed so far (summed from TASK_COMPLETED entries).
        tasks: Ordered list of task IDs assigned to this agent.
        completed_tasks: Task IDs that have been completed.
    """

    agent_id: str
    agent_type: str
    model: str
    depth: int
    status: str = "spawned"  # spawned | started | active | complete | failed
    parent_agent_id: str | None = None
    context_budget: int = 8000
    tokens_used: int = 0
    tasks: list = field(default_factory=list)
    completed_tasks: list = field(default_factory=list)


@dataclass
class TaskState:
    """State of a single task as reconstructed from the WAL.

    Attributes:
        task_id: Unique task identifier.
        task_type: Agent type required (dev, infra, research, qa).
        goal: Human-readable task description.
        status: Current status string.
        assigned_agent_id: Agent handling this task, or None.
        review_attempts: Number of review attempts made.
        review_result: "pass", "fail", or None.
        md_path: Path to the task output MD file, or None.
        md_hash: SHA-256 hash of the MD file, or None.
        chain_id: Chain this task belongs to, or None.
        dependencies: Task IDs this task depends on.
        complexity: 1–10 complexity score assigned at planning time.
    """

    task_id: str
    task_type: str
    goal: str
    status: str = "planned"  # planned | assigned | in_progress | completed | failed | waiting
    assigned_agent_id: str | None = None
    review_attempts: int = 0
    review_result: str | None = None
    md_path: str | None = None
    md_hash: str | None = None
    chain_id: str | None = None
    dependencies: list = field(default_factory=list)
    complexity: int = 5


@dataclass
class WalState:
    """Full system state reconstructed from a WAL replay.

    Attributes:
        run_id: Active run identifier, or None if no run started.
        goal: The original goal submitted for this run.
        agents: Map from agent_id to AgentState.
        tasks: Map from task_id to TaskState.
        md_files: Map from md_path to the wal_id of the MD_WRITTEN entry.
        task_completion_count: Total completed tasks (drives audit trigger).
        last_audit_at_count: task_completion_count when last audit was triggered.
        is_complete: True when RUN_COMPLETE has been written.
        chains: Map from chain_id to list of task_ids.
        reviewer_states: Map from reviewer_id to status string.
    """

    run_id: str | None = None
    goal: str = ""
    agents: dict = field(default_factory=dict)
    tasks: dict = field(default_factory=dict)
    md_files: dict = field(default_factory=dict)
    task_completion_count: int = 0
    last_audit_at_count: int = 0
    is_complete: bool = False
    chains: dict = field(default_factory=dict)
    reviewer_states: dict = field(default_factory=dict)


# ── WAL Writer ────────────────────────────────────────────────────────────────


class WalWriter:
    """Append-only writer for the WAL NDJSON file.

    Guarantees monotonically increasing wal_ids and immediate flush after
    every write. Raises on any I/O error — WAL writes must never fail silently.

    Args:
        path: Path to the wal.ndjson file. Created if it does not exist.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._counter = self._load_max_id()
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug("WalWriter initialized at %s (next id=%d)", path, self._counter + 1)

    def _load_max_id(self) -> int:
        """Scan the existing WAL to find the highest wal_id.

        Returns:
            The numeric part of the highest wal_id found, or 0 if file is empty/absent.
        """
        if not self._path.exists():
            return 0
        max_id = 0
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        raw_id = data.get("wal_id", "w-0")
                        num = int(raw_id.split("-")[1])
                        if num > max_id:
                            max_id = num
                    except (json.JSONDecodeError, IndexError, ValueError):
                        pass
        except OSError:
            pass
        return max_id

    def _next_id(self) -> str:
        """Return the next wal_id string and increment the counter.

        Returns:
            String in format "w-NNNN" (zero-padded to 4 digits minimum).
        """
        self._counter += 1
        return f"w-{self._counter:04d}"

    def write(self, event: WalEventType, run_id: str, payload: dict) -> WalEntry:
        """Append one entry to the WAL and flush immediately.

        Args:
            event: The event type.
            run_id: The run this entry belongs to.
            payload: Event-specific data.

        Returns:
            The WalEntry that was written.

        Raises:
            OSError: If the write or flush fails. Never silently swallowed.
        """
        with self._lock:
            wal_id = self._next_id()
            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            entry = WalEntry(
                wal_id=wal_id,
                event=event,
                timestamp=timestamp,
                run_id=run_id,
                payload=payload,
            )
            line = json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
            logger.info("WAL %s  %s  run=%s", wal_id, event.value, run_id)
            return entry


# ── WAL Reader ────────────────────────────────────────────────────────────────


class WalReader:
    """Reader for the WAL NDJSON file."""

    @staticmethod
    def read_all(path: Path) -> list[WalEntry]:
        """Read every entry from the WAL file.

        Args:
            path: Path to the wal.ndjson file.

        Returns:
            Ordered list of WalEntry objects.

        Raises:
            WalCorruptError: If any line is not valid JSON or is missing required fields.
        """
        if not path.exists():
            return []
        entries: list[WalEntry] = []
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise WalCorruptError(
                        f"WAL line {lineno} is not valid JSON: {exc}"
                    ) from exc
                try:
                    entry = WalEntry.from_dict(data)
                except (KeyError, ValueError) as exc:
                    raise WalCorruptError(
                        f"WAL line {lineno} missing required field: {exc}"
                    ) from exc
                entries.append(entry)
        logger.debug("WalReader.read_all: loaded %d entries from %s", len(entries), path)
        return entries

    @staticmethod
    def replay(path: Path) -> Generator[WalEntry, None, None]:
        """Yield WAL entries one at a time for streaming replay.

        Args:
            path: Path to the wal.ndjson file.

        Yields:
            WalEntry objects in file order.

        Raises:
            WalCorruptError: If any line is malformed.
        """
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise WalCorruptError(
                        f"WAL line {lineno} is not valid JSON: {exc}"
                    ) from exc
                try:
                    yield WalEntry.from_dict(data)
                except (KeyError, ValueError) as exc:
                    raise WalCorruptError(
                        f"WAL line {lineno} missing required field: {exc}"
                    ) from exc


# ── State reconstruction ──────────────────────────────────────────────────────


def build_state_from_wal(entries: list[WalEntry]) -> WalState:
    """Reconstruct full system state by replaying WAL entries top-to-bottom.

    This is the canonical state reconstruction function. It is deterministic:
    given the same list of entries, it always returns the same state.

    Args:
        entries: Ordered list of WalEntry objects (from WalReader.read_all).

    Returns:
        WalState reflecting the cumulative effect of all entries.
    """
    state = WalState()

    for entry in entries:
        event = entry.event
        p = entry.payload

        if event == WalEventType.RUN_START:
            state.run_id = entry.run_id
            state.goal = p.get("goal", "")

        elif event == WalEventType.TASK_PLANNED:
            task_id = p["task_id"]
            state.tasks[task_id] = TaskState(
                task_id=task_id,
                task_type=p.get("task_type", "dev"),
                goal=p.get("goal", ""),
                status="planned",
                chain_id=p.get("chain_id"),
                dependencies=p.get("dependencies", []),
                complexity=p.get("complexity", 5),
            )
            # Register chain membership
            chain_id = p.get("chain_id")
            if chain_id:
                state.chains.setdefault(chain_id, []).append(task_id)

        elif event == WalEventType.AGENT_SPAWNED:
            agent_id = p["agent_id"]
            state.agents[agent_id] = AgentState(
                agent_id=agent_id,
                agent_type=p.get("agent_type", "dev"),
                model=p.get("model", ""),
                depth=p.get("depth", 1),
                status="spawned",
                parent_agent_id=p.get("parent_agent_id"),
                context_budget=p.get("context_budget_tokens", 8000),
            )
            # Mark initial task as assigned
            initial_task_id = p.get("initial_task_id")
            if initial_task_id and initial_task_id in state.tasks:
                state.tasks[initial_task_id].assigned_agent_id = agent_id
                state.tasks[initial_task_id].status = "assigned"
                state.agents[agent_id].tasks.append(initial_task_id)

        elif event == WalEventType.TASK_ASSIGNED:
            task_id = p.get("task_id")
            agent_id = p.get("agent_id")
            if task_id and task_id in state.tasks:
                state.tasks[task_id].assigned_agent_id = agent_id
                state.tasks[task_id].status = "assigned"
                if p.get("retry_feedback"):
                    state.tasks[task_id].status = "retry"
            if agent_id and agent_id in state.agents:
                if task_id not in state.agents[agent_id].tasks:
                    state.agents[agent_id].tasks.append(task_id)

        elif event == WalEventType.AGENT_STARTED:
            agent_id = p.get("agent_id")
            if agent_id and agent_id in state.agents:
                state.agents[agent_id].status = "active"

        elif event == WalEventType.TASK_COMPLETED:
            task_id = p.get("task_id")
            agent_id = p.get("agent_id")
            tokens = p.get("tokens_used", 0)
            if task_id and task_id in state.tasks:
                state.tasks[task_id].status = "completed"
                state.tasks[task_id].md_path = p.get("md_path")
                state.tasks[task_id].md_hash = p.get("md_hash")
            if agent_id and agent_id in state.agents:
                state.agents[agent_id].tokens_used += tokens
                if task_id and task_id not in state.agents[agent_id].completed_tasks:
                    state.agents[agent_id].completed_tasks.append(task_id)
            state.task_completion_count += 1

        elif event == WalEventType.MD_WRITTEN:
            md_path = p.get("md_path")
            if md_path:
                state.md_files[md_path] = entry.wal_id

        elif event == WalEventType.REVIEW_STARTED:
            reviewer_id = p.get("reviewer_id")
            if reviewer_id:
                state.reviewer_states[reviewer_id] = "started"
            task_id = p.get("task_id")
            if task_id and task_id in state.tasks:
                state.tasks[task_id].status = "under_review"

        elif event == WalEventType.REVIEW_RESULT:
            reviewer_id = p.get("reviewer_id")
            if reviewer_id:
                state.reviewer_states[reviewer_id] = p.get("result", "unknown")
            task_id = p.get("task_id")
            if task_id and task_id in state.tasks:
                result = p.get("result")
                attempt = p.get("attempt", 1)
                state.tasks[task_id].review_result = result
                state.tasks[task_id].review_attempts = attempt
                if result == "pass":
                    state.tasks[task_id].status = "reviewed_pass"
                else:
                    state.tasks[task_id].status = "reviewed_fail"

        elif event == WalEventType.TASK_FAILED:
            task_id = p.get("task_id")
            if task_id and task_id in state.tasks:
                state.tasks[task_id].status = "failed"

        elif event == WalEventType.AUDIT_STARTED:
            state.last_audit_at_count = state.task_completion_count

        elif event == WalEventType.MD_FLAGGED:
            md_path = p.get("md_path")
            if md_path and md_path in state.md_files:
                # Keep a note in md_files that this path is flagged
                state.md_files[md_path] = entry.wal_id  # update to latest entry

        elif event == WalEventType.HANDOFF_WRITTEN:
            agent_id = p.get("agent_id")
            if agent_id and agent_id in state.agents:
                state.agents[agent_id].status = "handoff"

        elif event == WalEventType.RUN_COMPLETE:
            state.is_complete = True

    return state
