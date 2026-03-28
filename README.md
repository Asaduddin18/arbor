# Arbor

Tree-structured multi-agent orchestration framework built on Claude. Arbor decomposes a goal into a task graph, assigns tasks to specialized agents, reviews each output, and maintains a versioned markdown memory tree — all driven by an append-only Write-Ahead Log (WAL) that enables full crash recovery.

---

## Features

- **WAL-first architecture** — every action is recorded before execution; replay the log to recover from any crash
- **Depth-minimized scheduling** — prefers assigning tasks to existing agents over spawning new ones
- **Chain colocation** — dependency chains (A→B→C) run on a single agent to preserve context
- **Reviewer system** — every completed task is reviewed by a type-matched reviewer (code/fact/infra/qa) before being marked done
- **Audit agent** — periodically scans memory files for hallucination patterns (contradictions, specificity creep); injects visible warning flags into suspect files
- **Prompt injection defense** — slicer strips `"you are a"`, `"ignore previous"`, `"forget"` patterns before feeding files to agents
- **Rich live dashboard** — real-time WAL stream, agent pool table with budget % color coding, cost estimates per model
- **Atomic file writes** — all memory writes use a temp-file-then-rename pattern; no partial writes

---

## Requirements

- Python 3.11 or higher
- An Anthropic API key

---

## Installation

### 1. Clone the repository

```bash
git clone <repo-url>
cd Arbor
```

### 2. Create a virtual environment

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

### 3. Install the package

```bash
# Production install
pip install -e .

# Include dev/test dependencies
pip install -e ".[dev]"
```

### 4. Set your Anthropic API key

```bash
# Windows (Command Prompt)
set ANTHROPIC_API_KEY=sk-ant-...

# Windows (PowerShell)
$env:ANTHROPIC_API_KEY = "sk-ant-..."

# macOS / Linux
export ANTHROPIC_API_KEY=sk-ant-...
```

---

## Configuration

Arbor reads `arbor.config` (TOML) from the current directory. If the file is absent, built-in defaults are used.

```toml
[arbor]
max_depth              = 4        # maximum agent tree depth
context_budget_per_agent = 8000   # token budget per agent before handoff
orchestrator_model     = "claude-opus-4-6"
agent_model_default    = "claude-sonnet-4-6"
reviewer_model         = "claude-haiku-4-5-20251001"
audit_every_n_tasks    = 10       # run an audit every N task completions
max_review_attempts    = 3        # reviewer strike limit before marking failed
log_level              = "INFO"
wal_dir                = "arbor-run"   # WAL and run artifacts go here
memory_dir             = "memory"      # markdown memory tree root
```

Copy the included `arbor.config` to your project root and adjust as needed.

---

## Project structure

```
arbor/
├── cli.py              # Typer CLI — 7 commands
├── config.py           # ArborConfig dataclass + TOML loader
├── wal.py              # WAL writer, reader, state reconstruction
├── scheduler.py        # Deterministic scheduler (no LLM calls)
├── orchestrator.py     # Goal decomposition + agent assignment (claude-opus-4-6)
├── recovery.py         # Crash detection and WAL replay
├── agents/
│   ├── base.py         # BaseAgent — run/handoff/context logic
│   ├── dev.py          # DevAgent
│   ├── research.py     # ResearchAgent
│   ├── infra.py        # InfraAgent
│   ├── qa.py           # QAAgent
│   └── audit.py        # AuditAgent — hallucination detection
├── reviewers/
│   ├── base.py         # BaseReviewer — review/retry/WAL logic
│   ├── code.py         # CodeReviewer (auto-fail on security=="fail")
│   ├── fact.py         # FactReviewer (consistency checks)
│   ├── infra.py        # InfraReviewer (secrets check)
│   └── qa.py           # QAReviewer
├── memory/
│   ├── versioner.py    # SHA-256 versioned markdown writes
│   ├── tree.py         # MemoryTree — depth-aware read up/down/sideways
│   ├── slicer.py       # Context slicing + prompt injection stripping
│   └── flag_injector.py # Audit flag injection/removal
└── prompts/
    ├── orchestrator.py
    ├── agents.py
    ├── reviewers.py
    └── audit.py

tests/
├── conftest.py
├── test_wal.py
├── test_scheduler.py
├── test_recovery.py
├── test_memory.py
├── test_orchestrator.py
├── test_colocation.py
├── test_depth.py
├── test_audit.py
├── test_reviewers.py
└── test_cli.py
```

---

## CLI reference

### `arbor run <goal>`

Start a new run with a live Rich dashboard. Shows the WAL event stream, agent pool, and cost estimate in real time.

```bash
arbor run "Build a REST API for a task management app"
arbor run "Refactor the authentication module to use JWT" --config arbor.config
```

If an incomplete run exists in `wal_dir`, Arbor refuses to start and directs you to `arbor resume`.

---

### `arbor plan <goal>`

Call the orchestrator and show the planned task graph without executing anything. Add `--confirm` to approve and run immediately.

```bash
# Show plan only
arbor plan "Build a payment integration"

# Show plan, then prompt for approval before running
arbor plan "Build a payment integration" --confirm
```

---

### `arbor dry-run <goal>`

Same as `plan` but makes it explicit that nothing will be executed and no WAL is written. Useful in CI or review workflows.

```bash
arbor dry-run "Migrate the database schema to v3"
```

Output groups tasks by chain (dependency sequences) and standalone tasks.

---

### `arbor status`

Print the current run state from the WAL: event stream, agent pool table, cost panel, and memory tree.

```bash
arbor status
arbor status --config /path/to/arbor.config
```

---

### `arbor resume`

Detect an incomplete run, write crash recovery entries to the WAL, then resume the scheduler from where it left off.

```bash
arbor resume
```

Arbor checks for stuck agents (started but never completed) and re-queues their tasks.

---

### `arbor replay --wal <path>`

Replay a WAL file as an animated table — useful for debugging or post-mortems.

```bash
arbor replay --wal arbor-run/wal.ndjson
arbor replay --wal arbor-run/wal.ndjson --delay 0.05
```

`--delay` controls the pause between entries in seconds (default: 0.1).

---

### `arbor audit-now`

Run an on-demand audit of the five most recently modified memory files.

```bash
arbor audit-now
```

Files scoring below 0.6 confidence get a visible `⚠ AUDIT FLAG` prepended and a `MD_FLAGGED` entry written to the WAL.

---

## How it works

### Write-Ahead Log (WAL)

Every action is appended to `arbor-run/wal.ndjson` as NDJSON before it executes. The WAL is the single source of truth. Arbor can reconstruct full system state by replaying it top-to-bottom — no database required.

```
w-0001  RUN_START        goal: "Build a REST API..."
w-0002  TASK_PLANNED     task=t-001  type=dev  goal="Implement auth"
w-0003  TASK_PLANNED     task=t-002  type=dev  goal="Build endpoints"
w-0004  AGENT_SPAWNED    agent=agent-dev-1-001  depth=1
w-0005  TASK_ASSIGNED    task=t-001 → agent=agent-dev-1-001
w-0006  AGENT_STARTED    agent=agent-dev-1-001
w-0007  TASK_COMPLETED   task=t-001  tokens=1842
w-0008  MD_WRITTEN       path=memory/auth/jwt-auth.md
w-0009  REVIEW_STARTED   reviewer=rev-code-001
w-0010  REVIEW_RESULT    ✓ task=t-001  attempt=1
...
```

### Memory tree

Completed tasks produce versioned markdown files under `memory/`. Depth encodes abstraction level:

| Depth | Content |
|-------|---------|
| 0 | Project root overview |
| 1 | Module overview |
| 2 | Task completion notes |
| 3 | Implementation detail |
| 4 | Debug trace / failure notes |

Each file has YAML frontmatter with a `content_hash` (SHA-256) for integrity verification.

### Scheduler rules (in priority order)

1. **PLAN_TASKS** — if no tasks exist yet, call the orchestrator to decompose the goal
2. **SPAWN_REVIEWER** — after every `TASK_COMPLETED + MD_WRITTEN`, spawn a reviewer
3. **ASSIGN_TASK** — if a task failed under the retry limit, retry it
4. **SPAWN_AUDIT** — after every N task completions (`audit_every_n_tasks`)
5. **ASSIGN_TASK** — assign unstarted tasks to existing agents (absorption) or spawn new ones
6. **MARK_COMPLETE** — when all tasks are `reviewed_pass`, write `RUN_COMPLETE`

### Agent absorption

Before spawning a new agent, the scheduler checks whether an existing active agent can absorb the task:
- Same agent type
- Context usage below 60% of budget
- Agent depth ≤ 1

This keeps the agent tree shallow and avoids unnecessary model calls.

### Audit flags

After every N completions, the audit agent reads a batch of memory files and checks for:
- Internal contradictions (numbers, names that change between sections)
- Cross-file contradictions
- Unverified references (methods/files not described anywhere in the batch)
- Specificity creep (claims that grow increasingly precise without grounding)

Files scoring below 0.6 get a warning block prepended:

```
> ⚠ AUDIT FLAG (audit-003, confidence: 0.54)
> This file contains claims that may be inconsistent. Treat as unverified context.
> Issues: Session TTL stated as 8h in Overview but 24h in Configuration
> Reviewed by audit agent on 2024-11-15. See audit/audit-003.md for full report.

---

# Session Manager
...
```

---

## Running the tests

```bash
# All tests
pytest

# Verbose
pytest -v

# Specific phase
pytest tests/test_wal.py
pytest tests/test_audit.py
pytest tests/test_cli.py
```

Expected output: **224 tests, 0 failures**.

---

## Cost estimate

Approximate USD per 1M tokens (input + output averaged):

| Model | Role | Cost |
|-------|------|------|
| claude-opus-4-6 | Orchestrator | $15.00 |
| claude-sonnet-4-6 | Dev / Research agents | $3.00 |
| claude-haiku-4-5-20251001 | Reviewers / Infra / QA | $0.25 |

The `arbor status` command shows a running cost estimate for the current run.

---

## Crash recovery

If a process is killed mid-run, re-run:

```bash
arbor resume
```

Arbor detects agents that were `started` but never completed, writes `CRASH_DETECTED` and `RECOVERY_REPLAY` entries, then hands off to the scheduler to continue from the last consistent state.

---

## License

MIT
