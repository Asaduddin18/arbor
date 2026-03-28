"""Orchestrator prompt templates for Arbor v2."""

from __future__ import annotations


TASK_DECOMPOSITION_SYSTEM = """\
You are an orchestrator for a multi-agent system called Arbor.
Your job is to decompose a goal into tasks and assign them to agents.

Output ONLY valid JSON. No prose, no markdown code fences, no explanation.

Rules:
- Minimize the number of tasks. Prefer fewer, larger tasks over many small ones.
- Group dependent tasks into chains (A → B → C). Each chain will be assigned to one agent.
- Assign each task a type: dev | research | infra | qa
- Assign each task a complexity score 1-10 (1=trivial, 10=extremely complex)
- Identify cross-chain dependencies explicitly (when a task in one chain needs output from another chain)
- Prefer depth 1 agents (siblings) over depth 2+ agents (children)
- Do not create tasks for things that are already obviously done

Output format (strict):
{
  "tasks": [
    {
      "task_id": "short-kebab-case-id",
      "task_type": "dev",
      "goal": "human-readable description of what this task must produce",
      "complexity": 5,
      "chain_id": "chain-name-or-null",
      "dependencies": ["other-task-id-or-empty-list"]
    }
  ],
  "chains": [
    {
      "chain_id": "chain-name",
      "tasks": ["task-id-1", "task-id-2"],
      "agent_type": "dev",
      "estimated_tokens": 5000,
      "colocation": "single-agent"
    }
  ],
  "cross_chain_dependencies": [
    {
      "from_task": "task-id-in-chain-a",
      "to_task": "task-id-in-chain-b",
      "description": "why B needs A's output"
    }
  ]
}
"""


def build_decomposition_prompt(goal: str, active_agents: list[dict]) -> str:
    """Build the task decomposition user prompt.

    Args:
        goal: The user-submitted goal string.
        active_agents: List of active agent summaries from WAL state.

    Returns:
        User message string to send to the orchestrator LLM.
    """
    agents_text = ""
    if active_agents:
        lines = []
        for a in active_agents:
            budget_pct = 0
            if a.get("context_budget", 0) > 0:
                budget_pct = int(a.get("tokens_used", 0) / a["context_budget"] * 100)
            lines.append(
                f"  - {a['agent_id']} ({a['agent_type']}, depth={a['depth']}, "
                f"budget={budget_pct}% used, tasks={a.get('tasks', [])})"
            )
        agents_text = "EXISTING AGENTS (consider absorption before spawning new):\n" + "\n".join(lines)
    else:
        agents_text = "EXISTING AGENTS: none"

    return f"GOAL: {goal}\n\n{agents_text}"


ABSORPTION_CHECK_SYSTEM = """\
You are an orchestrator deciding whether a new task should be assigned to an
existing agent or requires spawning a new one.

Output ONLY valid JSON: {"absorb": true, "agent_id": "agent-xxx"} or {"absorb": false}

Rules for absorption (all must be true):
- Existing agent has the same type as required by the task
- Existing agent's context budget is less than 60% used
- The task is logically related to the agent's current work domain
- No write conflict exists (agent is not already writing to the same MD file)
"""


def build_absorption_prompt(task: dict, active_agents: list[dict]) -> str:
    """Build the absorption check user prompt.

    Args:
        task: Task dict with task_id, task_type, goal, complexity.
        active_agents: List of active agent state dicts.

    Returns:
        User message string.
    """
    agents_text = "\n".join(
        f"  - {a['agent_id']}: type={a['agent_type']}, "
        f"tokens_used={a.get('tokens_used', 0)}/{a.get('context_budget', 8000)}, "
        f"depth={a['depth']}, tasks={a.get('completed_tasks', [])}"
        for a in active_agents
    )
    return (
        f"NEW TASK:\n"
        f"  task_id: {task.get('task_id')}\n"
        f"  type: {task.get('task_type')}\n"
        f"  goal: {task.get('goal')}\n"
        f"  complexity: {task.get('complexity', 5)}\n\n"
        f"ACTIVE AGENTS:\n{agents_text or '  (none)'}"
    )
