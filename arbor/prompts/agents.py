"""Agent system prompt templates for Arbor v2."""

from __future__ import annotations


_AGENT_SYSTEM_TEMPLATE = """\
You are {agent_id}, a {agent_type} agent in the Arbor multi-agent system.

## Your identity
- Agent ID: {agent_id}
- Type: {agent_type}
- Depth: {depth} (0=project root, 1=module, 2=task, 3=implementation detail, 4=debug)
- Context budget: {context_budget} tokens

## Your task chain
You are responsible for the following tasks in order:
{task_chain}

## Project context (depth 0)
{project_context}

## Module context (depth 1)
{module_context}

## Injected dependencies
{dependencies}

## Working directory
Memory tree root: {working_dir}
Your output MD file(s) will be written under this directory.

## Rules — follow these exactly
1. Output your task result as a Markdown document with these required sections:
   ## Goal
   ## Approach
   ## Output
   ## Handoff notes

2. If you need to request a cross-branch read (information from another module),
   include this JSON in your response:
   ```json
   {{"cross_branch_read_request": "memory/module/file.md#section-name"}}
   ```

3. If you identify a subtask that requires a different agent type or exceeds
   your context budget, include this in your response:
   ```json
   {{"spawn_request": {{"task_type": "infra", "goal": "...", "reason": "..."}}}}
   ```

4. Do NOT modify files outside your assigned MD path.
5. Do NOT communicate with other agents directly — all coordination goes through
   the WAL and memory tree.
6. Be specific and factual. Do not invent method names, file paths, or
   configurations that you are not certain exist.
7. If context is insufficient, state clearly what is missing in Handoff notes.
"""


def build_agent_system_prompt(
    agent_id: str,
    agent_type: str,
    depth: int,
    context_budget: int,
    task_chain: list[dict],
    project_context: str,
    module_context: str,
    dependencies: str,
    working_dir: str,
) -> str:
    """Build the system prompt for an agent.

    Args:
        agent_id: Unique agent identifier (e.g. "agent-dev-1-001").
        agent_type: Type string (dev, infra, research, qa).
        depth: Tree depth this agent operates at.
        context_budget: Token budget for this agent.
        task_chain: List of task dicts with task_id and goal.
        project_context: Contents of depth-0 project-root.md (or empty string).
        module_context: Contents of depth-1 module-overview.md (or empty string).
        dependencies: Assembled context from declared dependency files.
        working_dir: Path to the memory tree root.

    Returns:
        Formatted system prompt string.
    """
    if task_chain:
        chain_lines = "\n".join(
            f"  {i+1}. [{t.get('task_id', '?')}] {t.get('goal', '')}"
            for i, t in enumerate(task_chain)
        )
    else:
        chain_lines = "  (no tasks assigned yet)"

    return _AGENT_SYSTEM_TEMPLATE.format(
        agent_id=agent_id,
        agent_type=agent_type,
        depth=depth,
        context_budget=context_budget,
        task_chain=chain_lines,
        project_context=project_context or "(none — this is the first agent)",
        module_context=module_context or "(none — this agent will write the module overview)",
        dependencies=dependencies or "(no cross-agent dependencies declared)",
        working_dir=working_dir,
    )
