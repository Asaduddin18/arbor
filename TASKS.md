# Arbor v2 — Step-by-Step Build Tasks

> Strictly follow the 6 phases in order. Complete and test each phase before starting the next.
> Each task is atomic — implement, test, commit before moving on.

---

## PHASE 1 — WAL + Scheduler Backbone (no LLMs)

### Setup

- [ ] **1.0** Create `pyproject.toml` with all dependencies:
  - `anthropic`, `rich`, `mistletoe`, `tomli` (Python <3.11 fallback), `pytest`, `pytest-asyncio`
  - Entry point: `arbor = "arbor.cli:main"`
  - Python `>=3.11`

- [ ] **1.1** Create `arbor.config` (default TOML):
  ```toml
  max_depth = 4
  context_budget_per_agent = 8000
  reviewer_model = "claude-haiku-4-5-20251001"
  orchestrator_model = "claude-opus-4-6"
  audit_every_n_tasks = 10
  max_review_attempts = 3
  log_level = "INFO"
  ```

- [ ] **1.2** Create `arbor/__init__.py` — package marker, expose version string.

- [ ] **1.3** Create `tests/__init__.py` and `tests/fixtures/` directory.

---

### 1.A — `arbor/config.py`

- [ ] **1.A.1** Define `ArborConfig` dataclass with all config fields.
- [ ] **1.A.2** Implement `load_config(path: Path) -> ArborConfig` using `tomllib` (3.11+) or `tomli`.
- [ ] **1.A.3** Implement `get_default_config() -> ArborConfig` returning hardcoded defaults.
- [ ] **1.A.4** Add validation: `max_depth` 1–10, `context_budget_per_agent` > 1000, `max_review_attempts` 1–10.

---

### 1.B — `arbor/wal.py`

- [ ] **1.B.1** Define `WalEventType` enum with all 17 event types:
  `RUN_START, TASK_PLANNED, AGENT_SPAWNED, TASK_ASSIGNED, AGENT_STARTED, TASK_COMPLETED, MD_WRITTEN, REVIEW_STARTED, REVIEW_RESULT, TASK_FAILED, AUDIT_STARTED, AUDIT_RESULT, MD_FLAGGED, HANDOFF_WRITTEN, RUN_COMPLETE, CRASH_DETECTED, RECOVERY_REPLAY`

- [ ] **1.B.2** Define `WalEntry` dataclass:
  ```python
  wal_id: str        # "w-0042" monotonically increasing
  event: WalEventType
  timestamp: str     # ISO 8601 UTC
  run_id: str
  payload: dict
  ```

- [ ] **1.B.3** Implement `WalWriter`:
  - `__init__(self, path: Path)` — opens file in append mode; creates if not exists.
  - `write(self, event, run_id, payload) -> WalEntry` — assigns next `wal_id`, writes JSON line, flushes immediately. **Never silently fails — raises on any IO error.**
  - `_next_id(self) -> str` — reads current max id from in-memory counter; thread-safe.

- [ ] **1.B.4** Implement `WalReader`:
  - `read_all(self, path: Path) -> list[WalEntry]` — reads NDJSON file, parses each line, validates schema.
  - `replay(self, path: Path) -> Generator[WalEntry, None, None]` — yields entries one at a time for streaming replay.
  - Skips blank lines, raises `WalCorruptError` on malformed JSON with line number context.

- [ ] **1.B.5** Implement `build_state_from_wal(entries: list[WalEntry]) -> WalState` returning:
  ```python
  @dataclass
  class WalState:
      run_id: str | None
      agents: dict[str, AgentState]   # agent_id → state
      tasks: dict[str, TaskState]     # task_id → state
      md_files: dict[str, str]        # md_path → wal_commit_id
      task_completion_count: int
      is_complete: bool
  ```

- [ ] **1.B.6** Add `append_only` guard: if file modification timestamp is newer than last-read timestamp, reload before writing (detects external tampering).

---

### 1.C — `arbor/scheduler.py`

- [ ] **1.C.1** Define `SchedulerAction` enum:
  `PLAN_TASKS, SPAWN_AGENT, ASSIGN_TASK, SPAWN_REVIEWER, SPAWN_AUDIT, WRITE_HANDOFF, MARK_COMPLETE, RECOVER`

- [ ] **1.C.2** Implement `determine_next_actions(state: WalState) -> list[SchedulerAction]`:
  - Pure function. No I/O. No LLM calls.
  - Rules:
    - `RUN_START` with no `TASK_PLANNED` → `PLAN_TASKS`
    - `AGENT_SPAWNED` with no `AGENT_STARTED` → re-`SPAWN_AGENT`
    - `TASK_COMPLETED` + `MD_WRITTEN` with no `REVIEW_STARTED` → `SPAWN_REVIEWER`
    - `REVIEW_RESULT(pass)` + pending tasks → `ASSIGN_TASK` or `SPAWN_AGENT`
    - `REVIEW_RESULT(fail, attempt<3)` → re-assign task to same agent with feedback
    - `REVIEW_RESULT(fail, attempt=3)` → `TASK_FAILED`, escalate to orchestrator
    - N task completions since last audit → `SPAWN_AUDIT`
    - All tasks complete + all reviews pass → `MARK_COMPLETE`

- [ ] **1.C.3** Implement `Scheduler` class:
  - `__init__(self, wal_path: Path, config: ArborConfig)`
  - `run(self, goal: str)` — main event loop: read WAL → determine actions → write WAL entries → execute (stub agents for now).
  - `step(self) -> bool` — single iteration; returns `False` when complete.
  - All agent/orchestrator calls are injected via dependency injection (stub-able for tests).

- [ ] **1.C.4** Implement context budget monitor:
  - Track `tokens_used` from `TASK_COMPLETED` WAL entries per agent.
  - Emit `HANDOFF_WRITTEN` trigger when agent crosses 60% of `context_budget_per_agent`.

---

### 1.D — `arbor/recovery.py`

- [ ] **1.D.1** Implement `detect_incomplete_entries(state: WalState) -> list[RecoveryAction]`:
  - `AGENT_SPAWNED` without `AGENT_STARTED` → re-spawn
  - `TASK_COMPLETED` without `REVIEW_STARTED` → spawn reviewer
  - `REVIEW_STARTED` without `REVIEW_RESULT` → re-spawn reviewer
  - `AUDIT_STARTED` without `AUDIT_RESULT` → re-spawn audit agent
  - Returns ordered list of `RecoveryAction` objects.

- [ ] **1.D.2** Implement `recover(wal_path: Path, config: ArborConfig) -> WalState`:
  - Read entire WAL.
  - Write `CRASH_DETECTED` entry.
  - Call `detect_incomplete_entries`.
  - Write `RECOVERY_REPLAY` entry for each incomplete entry being re-processed.
  - Return reconstructed state.
  - All actions idempotent — safe to call multiple times.

- [ ] **1.D.3** Add `is_recovery_needed(wal_path: Path) -> bool` — checks if WAL exists and has open (incomplete) entries without a `RUN_COMPLETE` entry.

---

### 1.E — `arbor/cli.py` (minimal Phase 1 version)

- [ ] **1.E.1** Set up `typer` or `argparse`-based CLI (prefer `typer` for rich integration):
  - `arbor run <goal>` — create WAL dir, write `RUN_START`, start scheduler loop.
  - `arbor status` — read WAL, print summary table via `rich`.
  - `arbor resume` — call `recover()`, then resume scheduler loop.

- [ ] **1.E.2** Implement live WAL event stream display using `rich.live` + `rich.table`:
  - Columns: `wal_id`, `event`, `timestamp`, `summary`.
  - Auto-updates as new entries are written.

- [ ] **1.E.3** Add `arbor-run/` directory creation on first run, with WAL path `arbor-run/wal.ndjson`.

---

### 1.F — Phase 1 Tests

- [ ] **1.F.1** `tests/test_wal.py`:
  - Test `WalWriter` writes valid NDJSON.
  - Test `wal_id` is monotonically increasing (no gaps, no duplicates).
  - Test append-only: reading back after multiple writes returns all entries in order.
  - Test `WalReader.read_all` parses all event types correctly.
  - Test `WalCorruptError` is raised on malformed lines.
  - Test `build_state_from_wal` correctly reconstructs `agents`, `tasks`, `md_files` dicts.

- [ ] **1.F.2** `tests/test_scheduler.py`:
  - Test `determine_next_actions` returns `PLAN_TASKS` for fresh `RUN_START` state.
  - Test `determine_next_actions` returns `SPAWN_REVIEWER` after `TASK_COMPLETED` + `MD_WRITTEN`.
  - Test `determine_next_actions` returns `SPAWN_AUDIT` after N completions.
  - Test `determine_next_actions` returns `MARK_COMPLETE` when all tasks reviewed/passed.
  - Test `Scheduler.step()` writes correct WAL entries for each action type (with stubbed agents).

- [ ] **1.F.3** `tests/test_recovery.py`:
  - Fixture: WAL with `AGENT_SPAWNED` but no `AGENT_STARTED`.
  - Fixture: WAL with `TASK_COMPLETED` but no `REVIEW_STARTED`.
  - Fixture: WAL with `REVIEW_STARTED` but no `REVIEW_RESULT`.
  - Test `detect_incomplete_entries` identifies all three correctly.
  - Test `recover()` writes `CRASH_DETECTED` + `RECOVERY_REPLAY` entries.
  - Test `is_recovery_needed()` returns `True` for incomplete WAL, `False` for complete WAL.

- [ ] **1.F.4** `tests/conftest.py`:
  - `tmp_wal_path` fixture — temp directory with empty WAL.
  - `sample_wal_state` fixture — pre-built `WalState` for use in tests.

**Phase 1 milestone:** Run `pytest tests/test_wal.py tests/test_scheduler.py tests/test_recovery.py`. All pass. Then: `arbor run "test goal"` → WAL written → kill process → `arbor resume` → state reconstructed.

---

## PHASE 2 — Orchestrator + Single Agent Type

### 2.A — `arbor/memory/` package

- [ ] **2.A.1** `arbor/memory/__init__.py` — package marker.

- [ ] **2.A.2** `arbor/memory/versioner.py`:
  - `hash_content(content: str) -> str` — returns `sha256:{hex}`.
  - `write_versioned_md(path: Path, content: str, wal_commit_id: str) -> str`:
    - Prepends YAML frontmatter: `wal_commit_id`, `created_at`, `content_hash`.
    - Writes file. Returns hash.
  - `read_versioned_md(path: Path) -> tuple[dict, str]` — returns `(frontmatter, body)`.
  - `verify_integrity(path: Path, expected_hash: str) -> bool`.

- [ ] **2.A.3** `arbor/memory/tree.py`:
  - `MemoryTree` class with `base_path: Path`.
  - `resolve_path(depth: int, module: str, filename: str) -> Path`.
  - `read_up(current_path: Path) -> list[tuple[int, Path]]` — returns files from depth 0 to current depth.
  - `read_sideways(current_path: Path, declared_siblings: list[str]) -> list[Path]`.
  - `read_down(current_path: Path, depth_limit: int = 1) -> list[Path]`.
  - `list_branch(module: str) -> list[Path]` — all files in a module subtree.
  - Depth semantics: 0=project-root.md, 1=module-overview.md, 2=task files, 3=implementation details, 4=debug traces.

- [ ] **2.A.4** `arbor/memory/slicer.py`:
  - `extract_section(content: str, anchor: str) -> str | None` — extracts `## section-name` block.
  - `strip_injection_patterns(content: str) -> str` — removes patterns: `"you are a"`, `"ignore previous"`, `"your new goal"`, `"forget"` (case-insensitive regex).
  - `slice_to_budget(content: str, token_budget: int) -> str` — truncates to token budget (approx 4 chars/token).
  - `build_context_slice(files: list[tuple[Path, str | None]], budget: int) -> str` — assembles multi-file context slice respecting total budget.

---

### 2.B — `arbor/prompts/` package

- [ ] **2.B.1** `arbor/prompts/__init__.py` — package marker.

- [ ] **2.B.2** `arbor/prompts/orchestrator.py`:
  - `TASK_DECOMPOSITION_SYSTEM` — system prompt for task decomposition (JSON-only output, rules, format).
  - `build_decomposition_prompt(goal: str, active_agents: list[dict]) -> str` — fills in goal and agent summary.
  - `ABSORPTION_CHECK_CONTEXT` — context template for absorption decision.
  - `build_absorption_prompt(task: dict, active_agents: list[dict]) -> str`.

- [ ] **2.B.3** `arbor/prompts/agents.py`:
  - `build_agent_system_prompt(agent_id, agent_type, task_chain, project_context, module_context, dependencies, working_dir) -> str`.
  - Template includes: identity, task chain, depth-0 context, depth-1 context, injected deps, working dir, rules (never spawn directly, communicate via WAL only, output JSON or MD).

---

### 2.C — `arbor/orchestrator.py`

- [ ] **2.C.1** Define `OrchestratorInput` dataclass: `wal_state: WalState`, `event: WalEntry`, `config: ArborConfig`.

- [ ] **2.C.2** Define `OrchestratorOutput` dataclass: `wal_entries: list[WalEntry]` (entries to append — orchestrator never writes directly).

- [ ] **2.C.3** Implement `decompose_goal(goal: str, state: WalState, config: ArborConfig) -> OrchestratorOutput`:
  - Call `claude-opus-4-6` with decomposition prompt.
  - Parse JSON response; validate schema (tasks, chains, cross_chain_dependencies).
  - Retry up to 3 times on invalid JSON with error feedback in prompt.
  - Return list of `TASK_PLANNED` WAL entries (one per task).

- [ ] **2.C.4** Implement `assign_next_task(state: WalState, completed_task_id: str, config: ArborConfig) -> OrchestratorOutput`:
  - Check for chain continuation (next task in same chain → `TASK_ASSIGNED` to same agent).
  - Run absorption check (`should_absorb`).
  - If no absorption → determine depth → return `AGENT_SPAWNED` entry.
  - Returns list of WAL entries (never executes them).

- [ ] **2.C.5** Implement `should_absorb(task: dict, active_agents: list[AgentState]) -> AgentState | None`:
  - Type match, context budget < 60%, depth ≤ target depth, no conflict domains.

- [ ] **2.C.6** Implement `handle_task_failure(state: WalState, task_id: str, all_feedbacks: list[dict]) -> OrchestratorOutput`:
  - Generate bug MD path at depth 4.
  - Return `MD_WRITTEN` (bug file) + re-planning entries.

---

### 2.D — `arbor/agents/` package

- [ ] **2.D.1** `arbor/agents/__init__.py` — package marker.

- [ ] **2.D.2** `arbor/agents/base.py` — `BaseAgent`:
  - Fields: `agent_id`, `agent_type`, `depth`, `sequence`, `model`, `context_budget`, `tokens_used`, `task_queue: list[dict]`, `status`.
  - `agent_id` format: `agent-{type}-{depth}-{sequence:03d}`.
  - `async def run(self, wal_writer: WalWriter, memory_tree: MemoryTree) -> None` — main lifecycle loop.
  - Write `AGENT_STARTED` WAL entry on begin.
  - `async def execute_task(self, task: dict) -> AgentTaskResult` — abstract, implemented by subclasses.
  - Write `TASK_COMPLETED` + `MD_WRITTEN` WAL entries after each task.
  - Check context budget after each task: if >60% → trigger handoff.
  - `generate_handoff_md(self) -> str` — captures completed tasks, decisions, active state, what receiver must do.
  - Write `HANDOFF_WRITTEN` WAL entry when handoff triggered.

- [ ] **2.D.3** `arbor/agents/dev.py` — `DevAgent(BaseAgent)`:
  - `agent_type = "dev"`, default model `claude-sonnet-4-6`.
  - `async def execute_task(self, task: dict) -> AgentTaskResult`:
    - Build system prompt via `build_agent_system_prompt`.
    - Build context slice from memory tree (up + declared deps).
    - Call LLM. Parse response for: MD content, spawn requests, cross-branch read requests.
    - Write MD file via `versioner.write_versioned_md`.
    - Return result with `tokens_used`, `md_path`, `md_hash`, `chain_continues`, `next_task_id`.
  - `_write_depth2_md(self, task, result) -> Path` — writes at correct depth-2 path.

---

### 2.E — Phase 2 Tests

- [ ] **2.E.1** `tests/test_orchestrator.py`:
  - Mock Anthropic client to return fixed JSON responses.
  - Test `decompose_goal` produces valid `TASK_PLANNED` WAL entries.
  - Test `decompose_goal` retries on malformed JSON and succeeds on second attempt.
  - Test `should_absorb` returns agent when all conditions met.
  - Test `should_absorb` returns `None` when context budget > 60%.
  - Test depth decision tree: sub-problem → child; independent → sibling.

- [ ] **2.E.2** `tests/test_memory.py`:
  - Test `tree.resolve_path` returns correct filesystem paths for each depth.
  - Test `tree.read_up` returns files depth-0 through depth-N.
  - Test `slicer.extract_section` extracts correct markdown block.
  - Test `slicer.strip_injection_patterns` removes prompt injection strings.
  - Test `slicer.slice_to_budget` truncates at token count.
  - Test `versioner.hash_content` is deterministic.
  - Test `versioner.write_versioned_md` creates file with correct YAML frontmatter.
  - Test `versioner.verify_integrity` returns `True` for unmodified file.

**Phase 2 milestone:** Mock orchestrator decomposes "Build a REST API with JWT auth" into tasks. DevAgent executes one task (LLM call live or mocked), writes depth-2 MD with correct YAML frontmatter. WAL records full lifecycle. All pytest tests pass.

---

## PHASE 3 — Reviewer System

### 3.A — `arbor/reviewers/` package

- [ ] **3.A.1** `arbor/reviewers/__init__.py` — package marker.

- [ ] **3.A.2** `arbor/reviewers/base.py` — `BaseReviewer`:
  - Fields: `reviewer_id`, `reviewer_type`, `model`, `task_id`, `agent_id`, `attempt`.
  - `async def review(self, task_goal: str, md_content: str) -> ReviewResult`.
  - `ReviewResult` dataclass: `result (pass/fail)`, `scores: dict`, `feedback: list[dict]`, `hallucination_candidates: list[str]`.
  - Output: JSON only. Retry up to 2 times on malformed JSON.
  - Write `REVIEW_RESULT` WAL entry after completion.

- [ ] **3.A.3** `arbor/reviewers/code.py` — `CodeReviewer(BaseReviewer)`:
  - Rubric: `goal_achievement (1-5)`, `code_correctness (1-5)`, `security (pass/fail — auto-fail)`, `error_handling (1-5)`, `documentation_quality (1-5)`.
  - Pass threshold: all numeric ≥ 3, `security = pass`.
  - Model: `claude-haiku-4-5-20251001` (upgrade to sonnet if complexity > 7).

- [ ] **3.A.4** `arbor/reviewers/fact.py` — `FactReviewer(BaseReviewer)`:
  - Rubric: `source_support (1-5)`, `internal_consistency (pass/fail)`, `cross_file_consistency (pass/fail)`, `actionability (1-5)`.
  - Pass threshold: all numeric ≥ 3, both pass/fail = pass.

- [ ] **3.A.5** `arbor/reviewers/infra.py` — `InfraReviewer(BaseReviewer)`:
  - Rubric: `reproducibility (1-5)`, `secrets_check (pass/fail — auto-fail)`, `compatibility (1-5)`, `idempotency (1-5)`.

- [ ] **3.A.6** `arbor/reviewers/qa.py` — `QAReviewer(BaseReviewer)`:
  - Rubric: `test_coverage (1-5)`, `edge_case_handling (1-5)`, `assertion_quality (1-5)`.

---

### 3.B — `arbor/prompts/reviewers.py`

- [ ] **3.B.1** `REVIEWER_SYSTEM` — base reviewer system prompt (JSON-only output, rubric format).
- [ ] **3.B.2** `build_reviewer_prompt(reviewer_type, task_goal, md_content, rubric) -> str`.
- [ ] **3.B.3** `build_feedback_injection(feedbacks: list[dict], attempt: int) -> str` — formats structured feedback for worker retry prompt. Precise, localized, capped. Explicit "fix only these issues" instruction.

---

### 3.C — Retry loop integration

- [ ] **3.C.1** In `arbor/scheduler.py`: after `REVIEW_RESULT(fail)`:
  - If `attempt < max_review_attempts`: re-assign task to same agent with feedback injected.
  - Write `TASK_ASSIGNED` WAL entry with `retry_feedback` field.
  - If `attempt == max_review_attempts`: write `TASK_FAILED` entry, call orchestrator for escalation.

- [ ] **3.C.2** In `arbor/agents/base.py`: check for `retry_feedback` in task assignment.
  - If present: append feedback block to system prompt before next LLM call.
  - `"Fix only the listed issues. Do not rewrite passing sections."`

- [ ] **3.C.3** On 3-strike failure: scheduler calls `orchestrator.handle_task_failure()`.
  - Orchestrator writes bug MD at depth 4 with all 3 reviewer feedbacks.
  - Bug MD includes: oscillation pattern analysis, what kept failing, all three feedback payloads.
  - Write `MD_WRITTEN` WAL entry for bug file.

---

### 3.D — Phase 3 Tests

- [ ] **3.D.1** `tests/test_reviewers.py`:
  - Mock LLM to return failing review JSON. Test `CodeReviewer` returns `result: fail`.
  - Mock LLM to return passing review JSON. Test `CodeReviewer` returns `result: pass`.
  - Test auto-fail: `security: fail` → overall result is `fail` regardless of other scores.
  - Test `build_feedback_injection` formats structured feedback correctly.
  - Test 3-strike path: simulate 3 `REVIEW_RESULT(fail)` entries → `TASK_FAILED` written → bug MD generated.
  - Test bug MD structure: contains all 3 feedback payloads, oscillation analysis section.
  - Test `hallucination_candidates` list is forwarded correctly.

**Phase 3 milestone:** DevAgent produces intentionally low-quality output (mock LLM). Reviewer fails it. Feedback injected (verify format). Agent retries. Second attempt passes. All in WAL. Also test: 3 failures → bug MD auto-generated with correct structure. All pytest tests pass.

---

## PHASE 4 — Depth Minimization + Chain Colocation

### 4.A — Orchestrator depth minimization (update `arbor/orchestrator.py`)

- [ ] **4.A.1** Implement full `should_absorb` with all 4 conditions (type, budget, depth, conflict).
- [ ] **4.A.2** Implement depth decision tree as explicit function `decide_spawn_depth(task, state) -> int`.
- [ ] **4.A.3** Implement chain colocation: for chains ≤ 3 tasks with total estimated tokens within budget → assign all to single agent in `AGENT_SPAWNED` payload.
- [ ] **4.A.4** Add `cross_chain_dependency_check`: before assigning task B that depends on task A, verify A's MD is committed in WAL (has `MD_WRITTEN` entry). If not → block B with `WAITING_ON` status.

---

### 4.B — Handoff MD generation (update `arbor/agents/base.py`)

- [ ] **4.B.1** Implement `generate_handoff_md(self) -> str`:
  - Sections: `## Completed Tasks` (list + summary), `## Key Decisions` (design choices made), `## Active State` (variables, data structures holding current state), `## What Receiving Agent Must Do` (explicit next steps), `## Files to Read` (list of MD paths with section anchors).
  - Format: valid Markdown with YAML frontmatter.
- [ ] **4.B.2** Implement handoff trigger check after each task: context > 60% OR chain > 3 OR type mismatch → call `generate_handoff_md`, write to `memory/{module}/handoff-{task_id}.md`, write `HANDOFF_WRITTEN` WAL entry.
- [ ] **4.B.3** In scheduler: on `HANDOFF_WRITTEN` → spawn new agent with handoff MD path injected as first context file.

---

### 4.C — Additional agent types

- [ ] **4.C.1** `arbor/agents/research.py` — `ResearchAgent(BaseAgent)`:
  - `agent_type = "research"`, model `claude-sonnet-4-6`.
  - Focuses on information gathering and synthesis.
  - Reviewer pair: `FactReviewer`.

- [ ] **4.C.2** `arbor/agents/infra.py` — `InfraAgent(BaseAgent)`:
  - `agent_type = "infra"`, model `claude-haiku-4-5-20251001`.
  - Focuses on environment, config, deployment tasks.
  - Reviewer pair: `InfraReviewer`.

- [ ] **4.C.3** `arbor/agents/qa.py` — `QAAgent(BaseAgent)`:
  - `agent_type = "qa"`, model `claude-haiku-4-5-20251001`.
  - Focuses on test writing and coverage.
  - Reviewer pair: `QAReviewer`.

---

### 4.D — Parallel execution

- [ ] **4.D.1** In `arbor/scheduler.py`: identify independent tasks (no shared dependencies).
- [ ] **4.D.2** Use `asyncio.gather(*[agent.run() for agent in independent_agents])` for parallel execution.
- [ ] **4.D.3** WAL writes from parallel agents must be serialized: use `asyncio.Lock` around `WalWriter.write()`.

---

### 4.E — Cross-branch reads

- [ ] **4.E.1** In `arbor/agents/base.py`: parse `cross_branch_read_request` from agent LLM output.
- [ ] **4.E.2** In `arbor/scheduler.py`: on receiving `cross_branch_read_request`:
  - Validate request against known MD files in WAL state.
  - If valid: inject section into agent's next prompt, log in WAL (custom payload on `TASK_ASSIGNED`).
  - If invalid: inject error message, log rejection.

---

### 4.F — Phase 4 Tests

- [ ] **4.F.1** `tests/test_colocation.py`:
  - Test `decompose_goal` with chain A→B→C assigns all to single agent.
  - Test handoff triggered when context > 60% (mock token counts in WAL).
  - Test handoff triggered when chain > 3 tasks.
  - Test handoff MD contains all required sections.
  - Test new agent receives handoff MD path in spawn payload.

- [ ] **4.F.2** `tests/test_depth.py`:
  - Test `should_absorb` returns agent for eligible task.
  - Test `should_absorb` returns `None` when budget > 60%.
  - Test `decide_spawn_depth` returns parent depth+1 for sub-problems.
  - Test `decide_spawn_depth` returns same depth for independent tasks.
  - Test depth does not increase for non-justified tasks.

**Phase 4 milestone:** Run a 10-task mock goal. Assert ≥ 40% tasks absorbed (not spawned new). Assert chains ≤ 3 tasks stay on one agent. Assert independent tasks run in parallel (via `asyncio.gather`). All pytest tests pass.

---

## PHASE 5 — Audit Agent

### 5.A — `arbor/agents/audit.py`

- [ ] **5.A.1** `AuditAgent(BaseAgent)`:
  - `agent_type = "audit"`, model `claude-sonnet-4-6`.
  - Fields: `audit_id`, `files_to_audit: list[Path]`.

- [ ] **5.A.2** Implement `async def run_audit(self, files: list[Path]) -> AuditResult`:
  - Read each MD file's content (strip frontmatter).
  - Build audit prompt with all file contents.
  - Call LLM. Parse JSON response with per-file confidence scores and issues.

- [ ] **5.A.3** `AuditResult` dataclass:
  ```python
  audit_id: str
  files_audited: list[str]
  results: list[FileAuditResult]
  ```
  `FileAuditResult`: `md_path, confidence_score (0-1), flagged (bool), claims_checked (int), issues (list[str])`.

- [ ] **5.A.4** Write `AUDIT_RESULT` WAL entry after completion.
- [ ] **5.A.5** For files with `confidence_score < 0.6`: emit list to scheduler for flagging.

---

### 5.B — `arbor/memory/flag_injector.py`

- [ ] **5.B.1** `inject_audit_flag(md_path: Path, audit_id: str, confidence: float, issues: list[str]) -> None`:
  - Reads existing MD file.
  - Prepends `⚠ AUDIT FLAG` block above existing content (after YAML frontmatter).
  - Block format per spec (audit ID, confidence, issues, recommendation, date).
  - Writes back atomically (write to temp file, rename).

- [ ] **5.B.2** `has_audit_flag(md_path: Path) -> bool` — checks if file already has flag prepended.
- [ ] **5.B.3** `remove_audit_flag(md_path: Path) -> None` — removes flag if file re-passes audit.

---

### 5.C — `arbor/prompts/audit.py`

- [ ] **5.C.1** `AUDIT_SYSTEM` — system prompt for audit agent (JSON-only, 4 check types, format spec).
- [ ] **5.C.2** `build_audit_prompt(files: list[tuple[str, str]]) -> str` — assembles multi-file audit prompt.

---

### 5.D — Audit triggers in scheduler

- [ ] **5.D.1** Periodic trigger: increment task completion counter in state; when counter % `audit_every_n_tasks == 0` → write `AUDIT_STARTED`, spawn audit agent.
- [ ] **5.D.2** Pre-critical trigger: before scheduling a task that has 3+ dependent tasks → check if any of its context files are unaudited (no `AUDIT_RESULT` covering them) → if yes, spawn audit first.
- [ ] **5.D.3** Post-failure trigger: on `TASK_FAILED` (3-strike) → spawn audit for all files in same branch.
- [ ] **5.D.4** On `AUDIT_RESULT`: for each flagged file → write `MD_FLAGGED` WAL entry → call `flag_injector.inject_audit_flag`.

---

### 5.E — Phase 5 Tests

- [ ] **5.E.1** `tests/test_audit.py`:
  - Create MD file with TTL contradiction ("TTL is 8h" and "TTL is 24h").
  - Mock LLM to return `confidence_score: 0.54` with correct issues list.
  - Test `AuditAgent.run_audit` returns `AuditResult` with file flagged.
  - Test `inject_audit_flag` prepends correct block to MD file.
  - Test `has_audit_flag` returns `True` after injection.
  - Test `remove_audit_flag` removes the block correctly.
  - Test periodic trigger fires after N completions.
  - Test post-failure trigger fires after `TASK_FAILED` entry.
  - Test that next agent spawned in flagged branch has warning visible in context.

**Phase 5 milestone:** Inject TTL contradiction into MD file manually. Run audit (mock LLM detects it). WAL processor injects `⚠ AUDIT FLAG`. Spawn new agent in same branch — verify flag is in its context. All pytest tests pass.

---

## PHASE 6 — Polish and Observability

### 6.A — Full CLI (update `arbor/cli.py`)

- [ ] **6.A.1** `arbor run <goal>` — full run with live dashboard.
- [ ] **6.A.2** `arbor plan --confirm <goal>`:
  - Call orchestrator decomposition (no agents spawned yet).
  - Display task graph as `rich.tree` visualization.
  - Prompt user: "Proceed? [y/n]"
  - On yes: write `RUN_START`, begin scheduler.

- [ ] **6.A.3** `arbor audit --now`:
  - Write `AUDIT_STARTED` to existing WAL.
  - Spawn audit agent on most recent N files.
  - Display results.

- [ ] **6.A.4** `arbor resume` — detect WAL, run recovery, resume scheduler.
- [ ] **6.A.5** `arbor replay --wal <path>` — replay WAL entries as animated table for debugging.
- [ ] **6.A.6** `arbor dry-run <goal>` — call orchestrator, show planned task graph, write nothing to WAL.
- [ ] **6.A.7** `arbor status` — show current run state from WAL as rich table.

---

### 6.B — Rich dashboard

- [ ] **6.B.1** WAL event stream panel: live-updating `rich.table`, last 20 entries, auto-scroll.
- [ ] **6.B.2** Agent pool status table: `agent_id`, `type`, `status`, `tasks_completed`, `tokens_used`, `context_budget_%`.
- [ ] **6.B.3** Tree depth visualization: `rich.tree` showing current memory tree structure with file counts per depth.
- [ ] **6.B.4** Token cost tracker: per-agent cost in USD (use Anthropic pricing constants), total run cost.
- [ ] **6.B.5** Layout: use `rich.layout` with 3 panels — event stream (left), agent pool (top right), cost summary (bottom right).

---

### 6.C — `pyproject.toml` finalization

- [ ] **6.C.1** All dependencies pinned with minimum versions.
- [ ] **6.C.2** `[project.scripts]` entry: `arbor = "arbor.cli:main"`.
- [ ] **6.C.3** `[tool.pytest.ini_options]` with `asyncio_mode = "auto"`.
- [ ] **6.C.4** Dev dependencies: `pytest`, `pytest-asyncio`, `pytest-mock`.

---

### 6.D — `README.md`

- [ ] **6.D.1** Installation section (`pip install -e .`).
- [ ] **6.D.2** Quickstart (5-step: install, create config, run, view status, resume).
- [ ] **6.D.3** Architecture overview (WAL, memory tree, scheduler, orchestrator roles).
- [ ] **6.D.4** CLI reference table (all commands, flags, descriptions).
- [ ] **6.D.5** Config reference table (all `arbor.config` keys, defaults, descriptions).

---

### 6.E — Phase 6 Tests (end-to-end)

- [ ] **6.E.1** End-to-end test with fully mocked LLM: submit goal, agents complete, reviewers pass, run completes.
- [ ] **6.E.2** Crash recovery test: submit goal, write partial WAL (simulate mid-run crash), run `recover()`, assert system resumes from correct state.
- [ ] **6.E.3** Token cost tracking test: assert `tokens_used` sums correctly across all agents.
- [ ] **6.E.4** CLI smoke test: all 7 CLI commands run without exception on mock WAL.

**Phase 6 milestone:** Full end-to-end run with mocked LLMs. Simulate crash. `arbor resume` recovers. Dashboard shows correct data. All 7 CLI commands work. `pytest` full suite passes.

---

## Cross-cutting tasks (do as needed per phase)

- [ ] **X.1** Add `logging` calls to every WAL write, agent spawn, review result, audit flag — at appropriate log levels.
- [ ] **X.2** Ensure no exception is ever silently swallowed — all `except` blocks re-raise or log+raise.
- [ ] **X.3** Add type hints to all function signatures throughout.
- [ ] **X.4** Add Google-style docstrings to all public functions and classes.
- [ ] **X.5** Add `tests/fixtures/` golden WAL snapshots for replay testing (add alongside Phase 1 tests).

---

## Build order summary

| Phase | Focus | Key deliverable |
|---|---|---|
| 1 | WAL + Scheduler | `wal.py`, `scheduler.py`, `recovery.py`, `config.py`, `cli.py` (minimal) |
| 2 | Orchestrator + DevAgent | `orchestrator.py`, `agents/dev.py`, `memory/` package, `prompts/` package |
| 3 | Reviewer system | `reviewers/` package, retry loop, 3-strike escalation |
| 4 | Depth minimization + chains | Absorption check, chain colocation, handoff MD, parallel execution |
| 5 | Audit agent | `agents/audit.py`, `memory/flag_injector.py`, audit triggers |
| 6 | Polish + observability | Full CLI, rich dashboard, token cost tracking, README |
