"""CLI for Arbor v2 — full implementation with rich dashboard.

Commands:
    arbor run <goal>               — start a new run with live dashboard
    arbor plan --confirm <goal>    — show task graph, wait for approval
    arbor audit --now              — on-demand audit of most recent files
    arbor resume                   — crash recovery + resume scheduler
    arbor replay --wal <path>      — replay WAL as animated table
    arbor dry-run <goal>           — show planned task graph (no execution)
    arbor status                   — current run state from WAL
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.tree import Tree
from rich.text import Text
from rich.panel import Panel
from rich.columns import Columns
from rich.layout import Layout
from rich import box

from arbor.config import ArborConfig, load_config, get_default_config
from arbor.wal import WalReader, WalState, WalEventType, WalEntry, build_state_from_wal
from arbor.recovery import is_recovery_needed, recover
from arbor.scheduler import Scheduler

app = typer.Typer(
    name="arbor",
    help="Arbor v2 — tree-structured multi-agent orchestration.",
    add_completion=False,
)
console = Console()

_DEFAULT_CONFIG_PATH = Path("arbor.config")

# Approximate token pricing (USD per 1M tokens, input/output average)
_MODEL_COST_PER_1M = {
    "claude-opus-4-6": 15.0,
    "claude-sonnet-4-6": 3.0,
    "claude-haiku-4-5-20251001": 0.25,
}


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_cfg(config_path: Path) -> ArborConfig:
    if config_path.exists():
        cfg = load_config(config_path)
    else:
        console.print(
            f"[yellow]Config not found at {config_path} — using defaults.[/yellow]"
        )
        cfg = get_default_config()
    return cfg


def _wal_path(cfg: ArborConfig) -> Path:
    return Path(cfg.wal_dir) / "wal.ndjson"


# ── Colour map ────────────────────────────────────────────────────────────────

_EVENT_COLORS = {
    "RUN_START": "bold green",
    "TASK_PLANNED": "cyan",
    "AGENT_SPAWNED": "blue",
    "TASK_ASSIGNED": "blue",
    "AGENT_STARTED": "bright_blue",
    "TASK_COMPLETED": "green",
    "MD_WRITTEN": "bright_green",
    "REVIEW_STARTED": "yellow",
    "REVIEW_RESULT": "yellow",
    "TASK_FAILED": "bold red",
    "AUDIT_STARTED": "magenta",
    "AUDIT_RESULT": "bright_magenta",
    "MD_FLAGGED": "bold red",
    "HANDOFF_WRITTEN": "orange3",
    "RUN_COMPLETE": "bold green",
    "CRASH_DETECTED": "bold red",
    "RECOVERY_REPLAY": "orange3",
}


def _entry_summary(entry: WalEntry) -> str:
    p = entry.payload
    ev = entry.event.value
    if ev == "RUN_START":
        return f"goal: {str(p.get('goal', ''))[:60]}"
    if ev == "TASK_PLANNED":
        return f"task={p.get('task_id')}  type={p.get('task_type')}  goal={str(p.get('goal',''))[:40]}"
    if ev in ("AGENT_SPAWNED",):
        return f"agent={p.get('agent_id')}  type={p.get('agent_type')}  depth={p.get('depth')}"
    if ev == "AGENT_STARTED":
        return f"agent={p.get('agent_id')} confirmed start"
    if ev == "TASK_ASSIGNED":
        return f"task={p.get('task_id')} → agent={p.get('agent_id')}  retry={p.get('retry', False)}"
    if ev == "TASK_COMPLETED":
        return f"task={p.get('task_id')}  tokens={p.get('tokens_used')}  md={p.get('md_path')}"
    if ev == "MD_WRITTEN":
        return f"path={p.get('md_path')}"
    if ev == "REVIEW_STARTED":
        return f"reviewer={p.get('reviewer_id')}  task={p.get('task_id')}"
    if ev == "REVIEW_RESULT":
        result_icon = "✓" if p.get("result") == "pass" else "✗"
        return f"{result_icon} task={p.get('task_id')}  attempt={p.get('attempt')}"
    if ev == "TASK_FAILED":
        return f"task={p.get('task_id')}  after={p.get('review_attempts')} attempts"
    if ev in ("AUDIT_STARTED",):
        return f"audit={p.get('audit_id')}"
    if ev == "AUDIT_RESULT":
        flagged = sum(1 for r in p.get("results", []) if r.get("flagged"))
        return f"audit={p.get('audit_id')}  files={len(p.get('files_audited', []))}  flagged={flagged}"
    if ev == "MD_FLAGGED":
        return f"path={p.get('md_path')}  confidence={p.get('confidence_score')}"
    if ev == "HANDOFF_WRITTEN":
        return f"agent={p.get('agent_id')}  → {p.get('handoff_path')}"
    if ev == "RUN_COMPLETE":
        return f"tasks={p.get('tasks_completed')}  total_tokens={p.get('total_tokens'):,}" if p.get("total_tokens") else f"tasks={p.get('tasks_completed')}"
    if ev == "CRASH_DETECTED":
        return f"entries_replayed={p.get('entries_replayed')}  agents={p.get('agents_found')}"
    if ev == "RECOVERY_REPLAY":
        return f"action={p.get('action_type')}  task={p.get('task_id')}"
    return str(p)[:80]


def _build_wal_table(entries: list, max_rows: int = 40, title: str = "WAL Event Stream") -> Table:
    table = Table(
        title=title,
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold white",
        expand=True,
    )
    table.add_column("ID", style="dim", width=7)
    table.add_column("Event", width=18)
    table.add_column("Time", width=10)
    table.add_column("Summary", overflow="fold")

    for entry in entries[-max_rows:]:
        color = _EVENT_COLORS.get(entry.event.value, "white")
        table.add_row(
            entry.wal_id,
            Text(entry.event.value, style=color),
            entry.timestamp[11:19] if len(entry.timestamp) > 19 else entry.timestamp,
            _entry_summary(entry),
        )
    return table


def _build_agent_table(state: WalState) -> Table:
    table = Table(
        title="Agent Pool",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold white",
        expand=True,
    )
    table.add_column("Agent", style="cyan", no_wrap=True)
    table.add_column("Type", width=9)
    table.add_column("Status", width=10)
    table.add_column("Done", width=5)
    table.add_column("Tokens", width=8)
    table.add_column("Budget%", width=8)
    table.add_column("Cost$", width=7)

    for agent in state.agents.values():
        budget_pct = (
            agent.tokens_used / agent.context_budget * 100
            if agent.context_budget
            else 0.0
        )
        budget_color = "red" if budget_pct > 80 else ("yellow" if budget_pct > 60 else "green")
        status_color = {
            "active": "green", "started": "green",
            "spawned": "yellow", "handoff": "orange3",
            "complete": "dim", "failed": "red",
        }.get(agent.status, "white")

        # Estimate cost
        cost = _estimate_cost(agent.tokens_used, agent.model)

        table.add_row(
            agent.agent_id,
            agent.agent_type,
            Text(agent.status, style=status_color),
            str(len(agent.completed_tasks)),
            f"{agent.tokens_used:,}",
            Text(f"{budget_pct:.0f}%", style=budget_color),
            f"${cost:.4f}",
        )
    return table


def _build_cost_panel(state: WalState) -> Panel:
    total_tokens = sum(a.tokens_used for a in state.agents.values())
    total_cost = sum(_estimate_cost(a.tokens_used, a.model) for a in state.agents.values())
    tasks_done = sum(1 for t in state.tasks.values() if t.status == "reviewed_pass")
    total_tasks = len(state.tasks)

    content = (
        f"[bold]Tasks:[/bold] [green]{tasks_done}[/green]/{total_tasks}\n"
        f"[bold]Total tokens:[/bold] [cyan]{total_tokens:,}[/cyan]\n"
        f"[bold]Estimated cost:[/bold] [yellow]${total_cost:.4f}[/yellow]\n"
        f"[bold]Agents:[/bold] {len(state.agents)}\n"
        f"[bold]Run:[/bold] [dim]{state.run_id or 'none'}[/dim]\n"
        f"[bold]Complete:[/bold] {'[bold green]YES[/bold green]' if state.is_complete else '[yellow]no[/yellow]'}"
    )
    return Panel(content, title="Run Summary", expand=True)


def _build_tree_panel(state: WalState) -> Panel:
    tree = Tree("[bold]memory/[/bold]")
    modules: dict[str, list] = {}
    for md_path in state.md_files:
        parts = md_path.replace("memory/", "").split("/")
        if len(parts) >= 1:
            module = parts[0]
            modules.setdefault(module, []).append(md_path)

    for module, files in sorted(modules.items()):
        branch = tree.add(f"[cyan]{module}/[/cyan]")
        for f in files[:5]:
            fname = f.split("/")[-1]
            branch.add(f"[dim]{fname}[/dim]")
        if len(files) > 5:
            branch.add(f"[dim]... +{len(files) - 5} more[/dim]")

    return Panel(tree, title="Memory Tree", expand=True)


def _estimate_cost(tokens: int, model: str) -> float:
    rate = _MODEL_COST_PER_1M.get(model, 3.0)
    return tokens / 1_000_000 * rate


# ── Commands ──────────────────────────────────────────────────────────────────


@app.command()
def run(
    goal: str = typer.Argument(..., help="Goal for the agent run."),
    config: Path = typer.Option(_DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Start a new Arbor run for the given goal."""
    cfg = _load_cfg(config)
    _setup_logging(cfg.log_level)
    wal = _wal_path(cfg)

    if is_recovery_needed(wal):
        console.print(
            "[bold yellow]Warning:[/bold yellow] An incomplete run exists. "
            "Use [bold]arbor resume[/bold] to continue or delete the WAL directory."
        )
        raise typer.Exit(1)

    wal.parent.mkdir(parents=True, exist_ok=True)
    scheduler = Scheduler(wal_path=wal, config=cfg)

    console.print(f"\n[bold green]Starting Arbor run[/bold green]")
    console.print(f"Goal: [cyan]{goal}[/cyan]")
    console.print(f"WAL:  [dim]{wal}[/dim]\n")

    async def _run_with_live() -> None:
        run_task = asyncio.create_task(scheduler.run(goal))
        try:
            with Live(console=console, refresh_per_second=2) as live:
                while not run_task.done():
                    entries = WalReader.read_all(wal)
                    state = build_state_from_wal(entries)
                    layout = Layout()
                    layout.split_row(
                        Layout(_build_wal_table(entries, max_rows=20), name="left", ratio=2),
                        Layout(name="right", ratio=1),
                    )
                    layout["right"].split_column(
                        Layout(_build_agent_table(state), name="agents"),
                        Layout(_build_cost_panel(state), name="cost"),
                    )
                    live.update(layout)
                    await asyncio.sleep(0.5)
        finally:
            await run_task

    asyncio.run(_run_with_live())

    entries = WalReader.read_all(wal)
    state = build_state_from_wal(entries)
    console.print(_build_wal_table(entries, max_rows=30))
    console.print(_build_agent_table(state))
    console.print(_build_cost_panel(state))
    if state.is_complete:
        console.print("\n[bold green]Run complete![/bold green]")
    else:
        console.print("\n[yellow]Run did not complete. Use 'arbor status' to inspect.[/yellow]")


@app.command()
def plan(
    goal: str = typer.Argument(..., help="Goal to plan."),
    confirm: bool = typer.Option(False, "--confirm", help="Wait for human approval before running."),
    config: Path = typer.Option(_DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Show the planned task graph. With --confirm, prompt before executing."""
    cfg = _load_cfg(config)
    _setup_logging(cfg.log_level)

    console.print(f"\n[bold]Planning:[/bold] [cyan]{goal}[/cyan]\n")
    console.print("[yellow]Calling orchestrator for task decomposition...[/yellow]")

    # Use a temp WAL just for planning
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_wal = Path(tmpdir) / "plan.ndjson"
        from arbor.wal import WalWriter, WalEventType, WalState
        from arbor.orchestrator import decompose_goal
        import anthropic

        async def _plan_only() -> WalState:
            writer = WalWriter(tmp_wal)
            run_id = "plan-preview"
            writer.write(WalEventType.RUN_START, run_id, {"goal": goal})
            state = WalState(run_id=run_id, goal=goal)
            client = anthropic.AsyncAnthropic()
            await decompose_goal(goal, state, writer, cfg, client=client)
            from arbor.wal import WalReader, build_state_from_wal
            entries = WalReader.read_all(tmp_wal)
            return build_state_from_wal(entries)

        state = asyncio.run(_plan_only())

    # Display task graph
    tree = Tree(f"[bold cyan]{goal}[/bold cyan]")
    for task_id, task in state.tasks.items():
        chain = f" [dim](chain: {task.chain_id})[/dim]" if task.chain_id else ""
        task_node = tree.add(
            f"[green]{task_id}[/green]  [{task.task_type}] complexity={task.complexity}{chain}"
        )
        if task.dependencies:
            task_node.add(f"[dim]depends on: {', '.join(task.dependencies)}[/dim]")
    console.print(Panel(tree, title="Planned Task Graph", expand=False))
    console.print(f"\n[bold]Total tasks:[/bold] {len(state.tasks)}")

    if confirm:
        proceed = typer.confirm("\nProceed with execution?")
        if proceed:
            wal = _wal_path(cfg)
            wal.parent.mkdir(parents=True, exist_ok=True)
            scheduler = Scheduler(wal_path=wal, config=cfg)
            asyncio.run(scheduler.run(goal))
            console.print("[bold green]Run complete![/bold green]")
        else:
            console.print("[yellow]Execution cancelled.[/yellow]")


@app.command()
def audit_now(
    config: Path = typer.Option(_DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Run an on-demand audit of the most recent memory files."""
    cfg = _load_cfg(config)
    _setup_logging(cfg.log_level)
    wal = _wal_path(cfg)

    if not wal.exists():
        console.print("[yellow]No WAL found — nothing to audit.[/yellow]")
        raise typer.Exit(0)

    entries = WalReader.read_all(wal)
    state = build_state_from_wal(entries)
    memory_dir = Path(cfg.memory_dir)

    # Collect most recent N MD files
    all_md = sorted(memory_dir.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    files_to_audit = all_md[:5]  # audit most recent 5 files

    if not files_to_audit:
        console.print("[yellow]No memory files found to audit.[/yellow]")
        raise typer.Exit(0)

    from arbor.agents.audit import AuditAgent
    from arbor.wal import WalWriter
    import anthropic

    audit_id = f"audit-demand-{state.task_completion_count:03d}"
    writer = WalWriter(wal)
    writer.write(WalEventType.AUDIT_STARTED, run_id=state.run_id or "manual", payload={"audit_id": audit_id})

    async def _audit() -> None:
        agent = AuditAgent(audit_id, cfg, client=anthropic.AsyncAnthropic())
        result = await agent.run_and_record(files_to_audit, writer, state.run_id or "manual", memory_base=memory_dir)

        for r in result.results:
            flag_text = "[bold red]FLAGGED[/bold red]" if r.flagged else "[green]CLEAN[/green]"
            console.print(f"  {flag_text}  {r.md_path}  confidence={r.confidence_score:.2f}")
            for issue in r.issues:
                console.print(f"    [yellow]•[/yellow] {issue}")

    asyncio.run(_audit())


@app.command()
def status(
    config: Path = typer.Option(_DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Show the current run state from the WAL."""
    cfg = _load_cfg(config)
    _setup_logging(cfg.log_level)
    wal = _wal_path(cfg)

    if not wal.exists():
        console.print("[yellow]No WAL found. Run 'arbor run <goal>' to start.[/yellow]")
        raise typer.Exit(0)

    entries = WalReader.read_all(wal)
    state = build_state_from_wal(entries)

    console.print(_build_wal_table(entries, max_rows=20))
    console.print()
    if state.agents:
        console.print(_build_agent_table(state))
    console.print()
    console.print(_build_cost_panel(state))
    if state.md_files:
        console.print(_build_tree_panel(state))


@app.command()
def resume(
    config: Path = typer.Option(_DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Resume a crashed or incomplete run via WAL replay."""
    cfg = _load_cfg(config)
    _setup_logging(cfg.log_level)
    wal = _wal_path(cfg)

    if not wal.exists():
        console.print("[yellow]No WAL found — nothing to resume.[/yellow]")
        raise typer.Exit(0)

    if not is_recovery_needed(wal):
        console.print("[green]WAL shows a completed run — nothing to resume.[/green]")
        raise typer.Exit(0)

    console.print("[bold yellow]Recovering from WAL...[/bold yellow]")
    state, actions = recover(wal, cfg)
    console.print(f"  Run:     [cyan]{state.run_id}[/cyan]")
    console.print(f"  Goal:    [cyan]{state.goal}[/cyan]")
    console.print(f"  Actions: [yellow]{len(actions)}[/yellow]")
    for a in actions:
        console.print(f"    • {a.action_type.value}  {a.reason}")

    console.print("\n[bold green]Resuming scheduler...[/bold green]")
    scheduler = Scheduler(wal_path=wal, config=cfg)

    async def _resume() -> None:
        for _ in range(1000):
            should_continue = await scheduler.step()
            if not should_continue:
                break
            await asyncio.sleep(0.05)

    asyncio.run(_resume())

    entries = WalReader.read_all(wal)
    state = build_state_from_wal(entries)
    if state.is_complete:
        console.print("[bold green]Run completed successfully after recovery.[/bold green]")
    else:
        console.print("[yellow]Scheduler stopped — run not yet complete.[/yellow]")
    console.print(_build_cost_panel(state))


@app.command()
def replay(
    wal_file: Path = typer.Option(..., "--wal", help="Path to the WAL file to replay."),
    delay: float = typer.Option(0.1, "--delay", help="Delay between entries in seconds."),
) -> None:
    """Replay a WAL file as an animated table for debugging."""
    if not wal_file.exists():
        console.print(f"[red]WAL file not found: {wal_file}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Replaying WAL:[/bold] [dim]{wal_file}[/dim]\n")
    replayed: list = []

    with Live(console=console, refresh_per_second=10) as live:
        for entry in WalReader.replay(wal_file):
            replayed.append(entry)
            live.update(_build_wal_table(replayed, title=f"WAL Replay ({wal_file.name})"))
            import time
            time.sleep(delay)

    console.print(f"\n[green]Replay complete — {len(replayed)} entries.[/green]")


@app.command(name="dry-run")
def dry_run(
    goal: str = typer.Argument(..., help="Goal to plan without executing."),
    config: Path = typer.Option(_DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Show the planned task graph without writing to WAL or executing anything."""
    cfg = _load_cfg(config)
    _setup_logging(cfg.log_level)

    console.print(f"\n[bold]Dry run:[/bold] [cyan]{goal}[/cyan]\n")
    console.print("[dim]No WAL writes. No agent execution. Planning only.[/dim]\n")

    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_wal = Path(tmpdir) / "dryrun.ndjson"
        from arbor.wal import WalWriter, WalEventType, WalState
        from arbor.orchestrator import decompose_goal
        import anthropic

        async def _dry_run() -> WalState:
            writer = WalWriter(tmp_wal)
            run_id = "dry-run"
            writer.write(WalEventType.RUN_START, run_id, {"goal": goal})
            state = WalState(run_id=run_id, goal=goal)
            client = anthropic.AsyncAnthropic()
            await decompose_goal(goal, state, writer, cfg, client=client)
            from arbor.wal import WalReader, build_state_from_wal
            entries = WalReader.read_all(tmp_wal)
            return build_state_from_wal(entries)

        state = asyncio.run(_dry_run())

    tree = Tree(f"[bold cyan]{goal}[/bold cyan]")
    chains: dict[str, list] = {}
    standalone = []
    for task_id, task in state.tasks.items():
        if task.chain_id:
            chains.setdefault(task.chain_id, []).append((task_id, task))
        else:
            standalone.append((task_id, task))

    for chain_id, tasks in chains.items():
        chain_node = tree.add(f"[magenta]chain: {chain_id}[/magenta]")
        for task_id, task in tasks:
            chain_node.add(
                f"[green]{task_id}[/green] [{task.task_type}] c={task.complexity}"
            )

    for task_id, task in standalone:
        tree.add(f"[green]{task_id}[/green] [{task.task_type}] c={task.complexity}")

    console.print(Panel(tree, title="Dry Run Task Graph (not executed)", expand=False))
    console.print(f"\n[bold]Tasks:[/bold] {len(state.tasks)}  |  [bold]Chains:[/bold] {len(chains)}")


def main() -> None:
    """Entry point for the arbor CLI."""
    app()


if __name__ == "__main__":
    main()
