"""`loom` CLI — thin wrapper over the same core the dashboard uses."""

from __future__ import annotations

import contextlib
import os
import subprocess
import webbrowser

import typer
from rich.console import Console
from rich.table import Table

from loom.core import claude_session
from loom.core import doctor as doctor_mod
from loom.core import manager, registry
from loom.core.config import LOOM_API_HOST, LOOM_API_PORT, load_repo_config
from loom.core.tests import build_test_run, serialize_lock

app = typer.Typer(no_args_is_help=True, add_completion=False, help="parallel git-worktree dev orchestrator")
console = Console()


@app.command()
def doctor() -> None:
    """Preflight: check loom's prerequisites (git/uv/node/claude, optional bun/tmux/gh/docker)."""
    checks = doctor_mod.run_checks()
    table = Table("check", "status", "hint")
    for c in checks:
        table.add_row(c["name"], "[green]ok[/]" if c["ok"] else "[red]MISSING[/]", c["hint"])
    console.print(table)
    raise typer.Exit(0 if doctor_mod.all_ok(checks) else 1)


@app.command()
def serve(
    host: str = LOOM_API_HOST,
    port: int = LOOM_API_PORT,
    reload: bool = False,
    open_browser: bool = True,
) -> None:
    """Run the dashboard + API."""
    import uvicorn

    url = f"http://{host}:{port}"
    console.print(f"[bold magenta]loom[/] → {url}")
    if open_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(url)
    uvicorn.run("loom.server.app:app", host=host, port=port, reload=reload)


@app.command("repo-add")
def repo_add(root: str) -> None:
    """Register a repo (must contain a .loom.yaml)."""
    from loom.core import repos

    info = repos.register(root)
    console.print(f"registered [bold]{info['name']}[/] → {info['root']}")


@app.command()
def new(
    branch: str,
    repo: str = typer.Option(".", "--repo", "-r", help="path to the target repo"),
    base: str | None = typer.Option(None, "--base", help="base branch (default from .loom.yaml)"),
) -> None:
    """Create a worktree + isolated env for a branch."""
    cfg = load_repo_config(repo)
    task = manager.create_task(cfg, branch, base)
    if task.state.value == "error":
        console.print(f"[red]error:[/] {task.note}")
        raise typer.Exit(1)
    console.print(f"[green]created[/] [bold]{task.id}[/] @ {task.worktree_path}")
    if task.ports:
        console.print(f"  backend :{task.ports.backend}   frontend :{task.ports.frontend}")
    console.print(f"  cd {task.worktree_path}")


@app.command("ls")
def ls() -> None:
    """List all tasks."""
    tasks = registry.list_tasks()
    if not tasks:
        console.print("no tasks yet — [dim]loom new <branch> -r <repo>[/]")
        return
    table = Table("task", "repo", "state", "backend", "frontend", "worktree")
    for t in tasks:
        table.add_row(
            t.id, t.repo, t.state.value,
            str(t.ports.backend) if t.ports else "-",
            str(t.ports.frontend) if t.ports else "-",
            t.worktree_path,
        )
    console.print(table)


@app.command()
def rm(task_id: str, force: bool = typer.Option(False, "--force", "-f")) -> None:
    """Remove a task and its worktree."""
    manager.remove_task(task_id, force=force)
    console.print(f"removed {task_id}")


@app.command()
def test(task_id: str, pytest_args: str = typer.Argument("")) -> None:
    """Run the isolated test suite for a task (foreground)."""
    task = registry.get_task(task_id)
    if not task:
        console.print("[red]unknown task[/]")
        raise typer.Exit(1)
    cfg = load_repo_config(task.repo_root)
    command, cwd, env = build_test_run(task, cfg, pytest_args)
    console.print(f"[dim]$ {command}[/]  (cwd={cwd})")
    ctx = serialize_lock() if cfg.test.isolation == "serialize" else contextlib.nullcontext()
    with ctx:
        rc = subprocess.run(command, shell=True, cwd=cwd, env={**os.environ, **env}).returncode
    raise typer.Exit(rc)


@app.command()
def start(task_id: str) -> None:
    """Start the task's dev services (Phase 2)."""
    task = registry.get_task(task_id)
    if not task:
        console.print("[red]unknown task[/]")
        raise typer.Exit(1)
    manager.start_task(load_repo_config(task.repo_root), task_id)
    console.print(f"started services for {task_id}")


@app.command()
def stop(task_id: str) -> None:
    """Stop the task's dev services (keep the worktree)."""
    manager.stop_task(task_id)
    console.print(f"stopped {task_id}")


@app.command()
def claude(task_id: str, prompt: str = typer.Argument(None)) -> None:
    """Open a Claude Code session in the task's worktree (optionally seed a /skill)."""
    task = registry.get_task(task_id)
    if not task:
        console.print("[red]unknown task[/]")
        raise typer.Exit(1)
    console.print(claude_session.open_session(task.worktree_path, prompt))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
