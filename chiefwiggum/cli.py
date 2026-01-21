"""ChiefWiggum CLI

Command-line interface for multi-Ralph coordination.
"""

import asyncio
import socket
import uuid
from pathlib import Path

import click

from chiefwiggum import (
    __version__,
    claim_task,
    complete_task,
    export_task_history_csv,
    get_database_path,
    init_db,
    list_active_instances,
    list_all_tasks,
    list_pending_tasks,
    register_ralph_instance,
    release_claim,
    reset_db,
    shutdown_instance,
    sync_tasks_from_fix_plan,
)
from chiefwiggum.coordination import cleanup_instance_files, list_stopped_instances


def run_async(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def generate_ralph_id(name: str | None = None) -> str:
    """Generate a Ralph ID from hostname and optional name."""
    hostname = socket.gethostname().split(".")[0]
    if name:
        return f"{hostname}-{name}"
    return f"{hostname}-{uuid.uuid4().hex[:6]}"


@click.group()
@click.version_option(version=__version__)
def main():
    """ChiefWiggum - Multi-Ralph Coordination System

    Orchestrates multiple Ralph (Claude Code) instances working on the same codebase.
    """
    pass


@main.command()
@click.option("--project", "-p", help="Filter by project name")
@click.option("--all", "-a", "show_all", is_flag=True, help="Show all tasks (not just pending)")
def status(project: str | None, show_all: bool):
    """Show status of instances and tasks."""
    from rich.console import Console
    from rich.table import Table

    console = Console()

    # Initialize DB if needed
    run_async(init_db())

    # Show instances
    instances = run_async(list_active_instances())
    if instances:
        table = Table(title="Active Ralph Instances")
        table.add_column("ID", style="cyan")
        table.add_column("Host", style="green")
        table.add_column("Task", style="yellow")
        table.add_column("Loops", justify="right")
        table.add_column("Status", style="magenta")

        for inst in instances:
            table.add_row(
                inst.ralph_id,
                inst.hostname or "-",
                inst.current_task_id or "-",
                str(inst.loop_count),
                inst.status.value,
            )
        console.print(table)
        console.print()
    else:
        console.print("[dim]No active Ralph instances[/dim]")
        console.print()

    # Show tasks
    if show_all:
        tasks = run_async(list_all_tasks(project))
        title = "All Tasks"
    else:
        tasks = run_async(list_pending_tasks(project))
        title = "Pending Tasks"

    if project:
        title += f" ({project})"

    if tasks:
        table = Table(title=title)
        table.add_column("Task ID", style="cyan", no_wrap=True)
        table.add_column("Priority", style="red")
        table.add_column("Task", style="white")
        table.add_column("Status", style="green")
        table.add_column("Claimed By", style="magenta")

        for task in tasks:
            status_style = {
                "pending": "yellow",
                "in_progress": "blue",
                "completed": "green",
                "failed": "red",
                "released": "dim",
                "retry_pending": "magenta",
            }.get(task.status.value, "white")

            table.add_row(
                task.task_id[:30] + ("..." if len(task.task_id) > 30 else ""),
                task.task_priority.value,
                task.task_title[:40] + ("..." if len(task.task_title) > 40 else ""),
                f"[{status_style}]{task.status.value}[/{status_style}]",
                task.claimed_by_ralph_id or "-",
            )
        console.print(table)
    else:
        console.print("[dim]No tasks found[/dim]")

    console.print(f"\n[dim]Database: {get_database_path()}[/dim]")


@main.command("list")
@click.option("--project", "-p", help="Filter by project name")
@click.option("--all", "-a", "show_all", is_flag=True, help="Include completed/failed tasks")
@click.option("--format", "-f", "output_format", type=click.Choice(["table", "json"]), default="table", help="Output format")
def list_tasks(project: str | None, show_all: bool, output_format: str):
    """List tasks with copyable task IDs."""
    import json

    from rich.console import Console
    from rich.table import Table

    console = Console()
    run_async(init_db())

    if show_all:
        tasks = run_async(list_all_tasks(project))
    else:
        tasks = run_async(list_pending_tasks(project))

    if output_format == "json":
        # JSON output for scripting
        task_list = [
            {
                "task_id": task.task_id,
                "priority": task.task_priority.value,
                "status": task.status.value,
                "title": task.task_title,
                "project": task.project,
                "claimed_by": task.claimed_by_ralph_id,
            }
            for task in tasks
        ]
        console.print(json.dumps(task_list, indent=2))
        return

    # Table output
    if not tasks:
        console.print("[dim]No tasks found[/dim]")
        return

    table = Table(title="Tasks" + (f" ({project})" if project else ""))
    table.add_column("TASK_ID", style="cyan", no_wrap=True)
    table.add_column("PRIORITY", style="red")
    table.add_column("STATUS", style="green")
    table.add_column("TITLE", style="white")

    for task in tasks:
        status_style = {
            "pending": "yellow",
            "in_progress": "blue",
            "completed": "green",
            "failed": "red",
            "released": "dim",
            "retry_pending": "magenta",
        }.get(task.status.value, "white")

        table.add_row(
            task.task_id,
            task.task_priority.value,
            f"[{status_style}]{task.status.value}[/{status_style}]",
            task.task_title[:50] + ("..." if len(task.task_title) > 50 else ""),
        )

    console.print(table)


@main.command()
def tui():
    """Launch live TUI dashboard."""
    from chiefwiggum.tui import run_tui

    run_async(init_db())
    run_tui()


@main.command()
@click.option("--name", "-n", help="Custom name for this Ralph instance")
@click.option("--project", "-p", help="Project being worked on")
@click.option("--session", "-s", help="Session file path")
def register(name: str | None, project: str | None, session: str | None):
    """Register this terminal as a Ralph instance."""
    from rich.console import Console

    console = Console()
    run_async(init_db())

    ralph_id = generate_ralph_id(name)
    run_async(register_ralph_instance(ralph_id, session_file=session, project=project))

    console.print(f"[green]Registered:[/green] {ralph_id}")
    if project:
        console.print(f"[dim]Project: {project}[/dim]")


@main.command()
@click.argument("project")
@click.option("--ralph-id", "-r", help="Ralph ID (auto-generated if not provided)")
@click.option("--fix-plan", "-f", help="Path to @fix_plan.md to sync first")
def claim(project: str, ralph_id: str | None, fix_plan: str | None):
    """Claim next available task from a project."""
    from rich.console import Console

    console = Console()
    run_async(init_db())

    if not ralph_id:
        ralph_id = generate_ralph_id()
        run_async(register_ralph_instance(ralph_id, project=project))

    result = run_async(claim_task(ralph_id, project=project, fix_plan_path=fix_plan))

    if result:
        console.print(f"[green]Claimed:[/green] {result['task_title']}")
        console.print(f"[dim]Task ID: {result['task_id']}[/dim]")
        console.print(f"[dim]Priority: {result['task_priority']}[/dim]")
        console.print(f"[dim]Expires: {result['expires_at']}[/dim]")
    else:
        console.print("[yellow]No tasks available to claim[/yellow]")


@main.command()
@click.argument("task_id")
@click.option("--ralph-id", "-r", required=True, help="Ralph ID that owns the claim")
@click.option("--message", "-m", help="Completion message")
@click.option("--commit", "-c", help="Git commit SHA")
def complete(task_id: str, ralph_id: str, message: str | None, commit: str | None):
    """Mark a task as complete."""
    from rich.console import Console

    console = Console()
    run_async(init_db())

    success = run_async(complete_task(ralph_id, task_id, commit_sha=commit, message=message))

    if success:
        console.print(f"[green]Completed:[/green] {task_id}")
    else:
        console.print(f"[red]Failed to complete:[/red] {task_id}")
        console.print("[dim]Task may not be claimed by this Ralph or is not in_progress[/dim]")


@main.command()
@click.argument("task_id")
@click.option("--ralph-id", "-r", required=True, help="Ralph ID that owns the claim")
def release(task_id: str, ralph_id: str):
    """Release a claim without completing the task."""
    from rich.console import Console

    console = Console()
    run_async(init_db())

    success = run_async(release_claim(ralph_id, task_id))

    if success:
        console.print(f"[green]Released:[/green] {task_id}")
    else:
        console.print(f"[red]Failed to release:[/red] {task_id}")


@main.command()
@click.argument("fix_plan", type=click.Path(exists=True))
@click.option("--project", "-p", help="Project name (auto-detected from path if not provided)")
def sync(fix_plan: str, project: str | None):
    """Sync tasks from a @fix_plan.md file."""
    from rich.console import Console

    console = Console()
    run_async(init_db())

    count = run_async(sync_tasks_from_fix_plan(fix_plan, project=project))

    if project is None:
        project = Path(fix_plan).parent.name

    console.print(f"[green]Synced {count} tasks[/green] from {fix_plan}")
    console.print(f"[dim]Project: {project}[/dim]")


@main.command()
@click.option("--ralph-id", "-r", required=True, help="Ralph ID to shutdown")
def shutdown(ralph_id: str):
    """Shutdown a Ralph instance (releases claims)."""
    from rich.console import Console

    console = Console()
    run_async(init_db())
    run_async(shutdown_instance(ralph_id))

    console.print(f"[green]Shutdown:[/green] {ralph_id}")


@main.command("init")
def init_command():
    """Initialize the database."""
    from rich.console import Console

    console = Console()
    run_async(init_db())
    console.print(f"[green]Database initialized:[/green] {get_database_path()}")


@main.command()
@click.confirmation_option(prompt="Are you sure you want to reset the database?")
def reset():
    """Reset the database (delete all data)."""
    from rich.console import Console

    console = Console()
    run_async(init_db())
    run_async(reset_db())
    console.print("[yellow]Database reset complete[/yellow]")


@main.command("export-history")
@click.argument("output", type=click.Path())
@click.option("--project", "-p", help="Filter by project name")
@click.option("--ralph-id", "-r", help="Filter by Ralph instance")
@click.option("--limit", "-l", default=1000, help="Maximum number of records (default: 1000)")
def export_history(output: str, project: str | None, ralph_id: str | None, limit: int):
    """Export task history to a CSV file."""
    from rich.console import Console

    console = Console()
    run_async(init_db())

    count = run_async(export_task_history_csv(
        output_path=output,
        project=project,
        ralph_id=ralph_id,
        limit=limit,
    ))

    if count > 0:
        console.print(f"[green]Exported {count} records[/green] to {output}")
    else:
        console.print("[yellow]No history records to export[/yellow]")


def _parse_duration(duration_str: str) -> float | None:
    """Parse duration string like '24h', '7d', '30m' to hours."""
    if not duration_str:
        return None

    duration_str = duration_str.strip().lower()
    try:
        if duration_str.endswith("h"):
            return float(duration_str[:-1])
        elif duration_str.endswith("d"):
            return float(duration_str[:-1]) * 24
        elif duration_str.endswith("m"):
            return float(duration_str[:-1]) / 60
        else:
            # Assume hours if no unit
            return float(duration_str)
    except ValueError:
        return None


@main.command()
@click.argument("ralph_id", required=False)
@click.option("--all", "-a", "clean_all", is_flag=True, help="Clean ALL stopped/crashed instance files")
@click.option("--dry-run", "-n", is_flag=True, help="Show what would be deleted without deleting")
@click.option("--older-than", "-o", help="Only instances older than duration (e.g., 24h, 7d)")
def cleanup(ralph_id: str | None, clean_all: bool, dry_run: bool, older_than: str | None):
    """Clean up files for stopped/crashed Ralph instances.

    Deletes session files, log files, and PID files.
    Does NOT delete database records (preserves history).

    Examples:

        wig cleanup ralph-123          # Clean specific instance

        wig cleanup --all              # Clean ALL stopped/crashed instances

        wig cleanup --all --dry-run    # Show what would be deleted

        wig cleanup --all --older-than 24h  # Only instances older than 24 hours
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()
    run_async(init_db())

    # Validate arguments
    if not ralph_id and not clean_all:
        console.print("[red]Error:[/red] Must specify either a ralph_id or --all")
        console.print("\nUsage:")
        console.print("  wig cleanup <ralph_id>              # Clean specific instance")
        console.print("  wig cleanup --all                   # Clean ALL stopped/crashed")
        console.print("  wig cleanup --all --dry-run         # Show what would be deleted")
        console.print("  wig cleanup --all --older-than 24h  # Only older than 24 hours")
        raise SystemExit(1)

    if ralph_id and clean_all:
        console.print("[red]Error:[/red] Cannot specify both ralph_id and --all")
        raise SystemExit(1)

    # Parse older_than if provided
    older_than_hours = _parse_duration(older_than) if older_than else None

    if clean_all:
        # Get all stopped/crashed instances
        instances = run_async(list_stopped_instances(older_than_hours))

        if not instances:
            if older_than:
                console.print(f"[dim]No stopped/crashed instances older than {older_than}[/dim]")
            else:
                console.print("[dim]No stopped/crashed instances to clean up[/dim]")
            return

        # Show what we'll clean
        if dry_run:
            console.print(f"[yellow]DRY RUN:[/yellow] Would clean {len(instances)} instance(s):\n")
        else:
            console.print(f"Cleaning {len(instances)} instance(s):\n")

        total_deleted = 0
        table = Table(title="Cleanup Results" if not dry_run else "Would Delete")
        table.add_column("Ralph ID", style="cyan")
        table.add_column("Status", style="yellow")
        table.add_column("Files", justify="right")

        for inst in instances:
            results = cleanup_instance_files(inst.ralph_id, dry_run=dry_run)
            deleted_count = sum(1 for deleted in results.values() if deleted)
            total_deleted += deleted_count

            table.add_row(
                inst.ralph_id,
                inst.status.value,
                str(deleted_count),
            )

        console.print(table)

        if dry_run:
            console.print(f"\n[yellow]Would delete {total_deleted} file(s)[/yellow]")
        else:
            console.print(f"\n[green]Deleted {total_deleted} file(s)[/green]")

    else:
        # Clean specific instance
        if dry_run:
            console.print(f"[yellow]DRY RUN:[/yellow] Would clean files for {ralph_id}:\n")
        else:
            console.print(f"Cleaning files for {ralph_id}:\n")

        results = cleanup_instance_files(ralph_id, dry_run=dry_run)

        for path, deleted in results.items():
            if deleted:
                if dry_run:
                    console.print(f"  [yellow]Would delete:[/yellow] {path}")
                else:
                    console.print(f"  [green]Deleted:[/green] {path}")
            else:
                console.print(f"  [dim]Not found:[/dim] {path}")

        deleted_count = sum(1 for deleted in results.values() if deleted)
        if dry_run:
            console.print(f"\n[yellow]Would delete {deleted_count} file(s)[/yellow]")
        else:
            console.print(f"\n[green]Deleted {deleted_count} file(s)[/green]")


@main.command()
@click.option("--dry-run", "-n", is_flag=True, help="Show what would be migrated without moving files")
@click.option("--status", "-s", is_flag=True, help="Show migration status only")
def migrate(dry_run: bool, status: bool):
    """Migrate from legacy ~/.chiefwiggum/ to XDG-compliant paths.

    This migrates configuration and data files to platform-appropriate locations:
    - Linux: ~/.config/chiefwiggum/, ~/.local/share/chiefwiggum/
    - macOS: ~/Library/Application Support/chiefwiggum/
    - Windows: %LOCALAPPDATA%/chiefwiggum/

    Examples:

        wig migrate --status     # Check migration status

        wig migrate --dry-run    # Preview what would be migrated

        wig migrate              # Perform the migration
    """
    from rich.console import Console
    from rich.table import Table

    from chiefwiggum.paths import get_migration_status, migrate_to_xdg

    console = Console()

    if status:
        # Show migration status
        migration_status = get_migration_status()

        table = Table(title="Migration Status")
        table.add_column("Property", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Using Legacy Paths", "Yes" if migration_status["using_legacy"] else "No")
        table.add_row("Legacy Dir Exists", "Yes" if migration_status["legacy_exists"] else "No")
        table.add_row("XDG Dir Exists", "Yes" if migration_status["xdg_exists"] else "No")
        table.add_row("Legacy Path", migration_status["legacy_path"])
        table.add_row("XDG Config Path", migration_status["xdg_config_path"])

        console.print(table)
        console.print(f"\n[dim]Recommended: {migration_status['recommended_action']}[/dim]")
        return

    # Perform migration
    console.print("[bold]Migrating to XDG-compliant paths...[/bold]\n")

    result = migrate_to_xdg(dry_run=dry_run)

    if result["migrated"]:
        if dry_run:
            console.print("[yellow]Would migrate:[/yellow]")
        else:
            console.print("[green]Migrated:[/green]")
        for src, dst in result["migrated"]:
            console.print(f"  {src} -> {dst}")

    if result["skipped"]:
        console.print("\n[dim]Skipped (destination exists):[/dim]")
        for src, dst in result["skipped"]:
            console.print(f"  {src}")

    if result["errors"]:
        console.print("\n[red]Errors:[/red]")
        for src, dst in result["errors"]:
            console.print(f"  Failed: {src}")

    if dry_run:
        console.print(f"\n[yellow]Would migrate {len(result['migrated'])} item(s)[/yellow]")
    elif result["migrated"]:
        console.print(f"\n[green]Migration complete: {len(result['migrated'])} item(s) migrated[/green]")
        console.print("[dim]The legacy directory can be removed after verifying everything works.[/dim]")
    else:
        console.print("\n[dim]Nothing to migrate[/dim]")


@main.command("paths")
def show_paths():
    """Show current path configuration.

    Displays where ChiefWiggum stores its configuration, data, and state files.
    """
    from rich.console import Console
    from rich.table import Table

    from chiefwiggum.paths import get_paths

    console = Console()
    paths = get_paths()

    table = Table(title="ChiefWiggum Paths")
    table.add_column("Purpose", style="cyan")
    table.add_column("Path", style="green")
    table.add_column("Exists", style="yellow")

    rows = [
        ("Config Directory", paths.config_dir),
        ("Config File", paths.config_path),
        ("Data Directory", paths.data_dir),
        ("Database", paths.database_path),
        ("State Directory", paths.state_dir),
        ("Ralphs Directory", paths.ralphs_dir),
        ("Logs Directory", paths.logs_dir),
    ]

    for purpose, path in rows:
        exists = "Yes" if path.exists() else "No"
        table.add_row(purpose, str(path), exists)

    console.print(table)

    if paths.using_legacy:
        console.print("\n[yellow]Note: Using legacy ~/.chiefwiggum/ paths[/yellow]")
        console.print("[dim]Run 'chiefwiggum migrate' to move to XDG-compliant locations[/dim]")


if __name__ == "__main__":
    main()
