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
    archive_task,
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
                "archived": "dim",
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
            "archived": "dim",
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
    """Mark a task as complete.

    This command is used by the system to record task completions. For manual
    marking of tasks that were completed but not detected, use 'mark-complete'.

    Examples:
        wig complete task-1 --ralph-id ralph-123 --commit abc1234
        wig complete task-1 -r ralph-123 -m "Manually completed"
    """
    from rich.console import Console

    console = Console()
    run_async(init_db())

    success = run_async(complete_task(ralph_id, task_id, commit_sha=commit, message=message))

    if success:
        console.print(f"[green]Completed:[/green] {task_id}")
        if commit:
            console.print(f"[dim]Commit: {commit}[/dim]")
    else:
        console.print(f"[red]Failed to complete:[/red] {task_id}")
        console.print("[dim]Task may not be claimed by this Ralph or is not in_progress[/dim]")
        # Exit non-zero so ralph_loop's `wig complete` shell check sees the
        # failure and does NOT proceed to self-chain on a task that wasn't
        # actually marked complete. Without this, the worker logs
        # "✅ marked complete" and forges ahead into incorrect chaining.
        raise SystemExit(1)


@main.command("mark-complete")
@click.argument("task_id")
@click.option("--commit", "-c", help="Git commit SHA (or use 'HEAD' to extract from last commit)")
@click.option("--ralph-id", "-r", help="Ralph ID (auto-detected from current directory if not provided)")
@click.option("--message", "-m", help="Completion message")
def mark_complete_command(task_id: str, commit: str | None, ralph_id: str | None, message: str | None):
    """Manually mark a task as complete (when auto-detection failed).

    Use this command when:
    - A task was completed (code committed, tests passing)
    - But the system didn't mark it as complete in the database
    - The TUI shows the task as "pending" despite being done

    The commit SHA can be:
    - An explicit SHA: --commit abc1234567890abcdef
    - HEAD: --commit HEAD (extracts from last commit)
    - Omitted: will try to find recent commit related to the task

    Examples:
        # Mark complete with explicit commit
        wig mark-complete task-1 --commit abc1234567890abcdef

        # Mark complete with last commit (HEAD)
        wig mark-complete task-1 --commit HEAD

        # Mark complete and let it find the commit
        wig mark-complete task-1

        # Specify Ralph ID explicitly
        wig mark-complete task-1 --ralph-id ralph-123 --commit HEAD
    """
    import subprocess

    from rich.console import Console

    console = Console()
    run_async(init_db())

    # Extract commit SHA if HEAD or empty
    if commit == "HEAD" or commit is None:
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--pretty=%H"],
                capture_output=True,
                text=True,
                check=True
            )
            extracted_commit = result.stdout.strip()
            if extracted_commit:
                commit = extracted_commit
                console.print(f"[dim]Extracted commit: {commit}[/dim]")
            else:
                console.print("[yellow]Warning: Could not extract commit SHA from git[/yellow]")
        except (subprocess.CalledProcessError, FileNotFoundError):
            console.print("[yellow]Warning: git not available or not in a repository[/yellow]")

    # Auto-detect Ralph ID from database if not provided
    if not ralph_id:
        # Try to get the Ralph that claimed this task
        from chiefwiggum.coordination import get_task_claim
        task_claim = run_async(get_task_claim(task_id))
        if task_claim and task_claim.claimed_by_ralph_id:
            ralph_id = task_claim.claimed_by_ralph_id
            console.print(f"[dim]Auto-detected Ralph ID: {ralph_id}[/dim]")
        else:
            console.print("[red]Error:[/red] Task not claimed by any Ralph")
            console.print("[dim]Use --ralph-id to specify the Ralph ID explicitly[/dim]")
            raise SystemExit(1)

    # Set default message if not provided
    if not message:
        message = "Manually marked complete via mark-complete command"

    # Mark the task complete
    success = run_async(complete_task(ralph_id, task_id, commit_sha=commit, message=message))

    if success:
        console.print(f"[green]✅ Task {task_id} marked complete[/green]")
        if commit:
            console.print(f"[dim]Commit: {commit}[/dim]")
        console.print("[dim]The task status and @fix_plan.md have been updated[/dim]")
    else:
        console.print(f"[red]❌ Failed to mark task {task_id} complete[/red]")
        console.print("[dim]Possible reasons:[/dim]")
        console.print("  - Task doesn't exist")
        console.print("  - Task is not in 'in_progress' status")
        console.print("  - Task is not claimed by the specified Ralph")
        console.print("\n[dim]Check task status with: wig list --all[/dim]")


@main.command("archive-task")
@click.argument("task_id")
def archive_task_command(task_id: str):
    """Archive a completed task that's no longer in @fix_plan.md.

    Use this command to mark completed tasks as archived when they've been
    removed from @fix_plan.md. Archived tasks won't appear in reconciliation
    and won't cause reconciliation failures.

    Examples:
        wig archive-task task-21-google-drive-integration
        wig archive-task task-35-pf-1-timezone-to-pacific
    """
    from rich.console import Console

    console = Console()
    run_async(init_db())

    success = run_async(archive_task(task_id))

    if success:
        console.print(f"[green]✅ Task {task_id} archived[/green]")
        console.print("[dim]Task will no longer appear in reconciliation[/dim]")
    else:
        console.print(f"[red]❌ Failed to archive task {task_id}[/red]")
        console.print("[dim]Task must exist and be in 'completed' status[/dim]")
        console.print("\n[dim]Check task status with: wig list --all[/dim]")


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
@click.option("--with-grading", is_flag=True, help="Generate and grade task-specific prompts (NEW)")
def sync(fix_plan: str, project: str | None, with_grading: bool):
    """Sync tasks from a @fix_plan.md file.

    By default, syncs to the legacy task_claims table.
    Use --with-grading to also generate and grade task-specific prompts.
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()
    run_async(init_db())

    # Always sync to old system for backward compatibility
    count = run_async(sync_tasks_from_fix_plan(fix_plan, project=project))

    if project is None:
        project = Path(fix_plan).parent.name

    console.print(f"[green]Synced {count} tasks[/green] from {fix_plan}")
    console.print(f"[dim]Project: {project}[/dim]")

    # Optionally sync to new graded task queue
    if with_grading:
        from chiefwiggum.coordination import sync_tasks_with_grading

        console.print("\n[cyan]Generating and grading task-specific prompts...[/cyan]")
        counts = run_async(sync_tasks_with_grading(fix_plan, project=project))

        # Display grade distribution
        table = Table(title="Task Grades")
        table.add_column("Grade", style="cyan")
        table.add_column("Count", justify="right", style="green")
        table.add_column("Spawnable", style="yellow")

        grade_data = [
            ("A (90-100)", counts['grade_a'], "✓ Auto-spawn"),
            ("B (70-89)", counts['grade_b'], "✓ Auto-spawn"),
            ("C (50-69)", counts['grade_c'], "⚠ Review required"),
            ("F (<50)", counts['grade_f'], "✗ Blocked"),
        ]

        for grade, count, spawnable in grade_data:
            table.add_row(grade, str(count), spawnable)

        console.print(table)
        console.print(f"\n[green]Total tasks graded: {counts['total']}[/green]")
        console.print("[dim]Use 'wig tui' to view task queue with grades[/dim]")


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


@main.group()
def config():
    """Manage ChiefWiggum configuration.

    Settings are stored in a single user-level config file and apply
    to all projects on this machine. No per-project configuration needed.
    """


@config.command("set-key")
@click.argument("api_key")
def config_set_key(api_key: str):
    """Set the Anthropic API key (stored once, used across all projects).

    Example:
        chiefwiggum config set-key sk-ant-...
    """
    from rich.console import Console

    from chiefwiggum.config import get_config_path, set_api_key, validate_api_key

    console = Console()

    if not api_key.startswith("sk-ant-"):
        console.print("[red]Error:[/red] Key must start with sk-ant-")
        raise SystemExit(1)

    ok, msg = validate_api_key(api_key)
    if not ok and "format" not in msg.lower():
        console.print(f"[yellow]Warning:[/yellow] {msg}")

    set_api_key(api_key)
    console.print(f"[green]API key saved[/green] → {get_config_path()}")
    console.print("[dim]All future ralph spawns on this machine will use this key.[/dim]")


@config.command("show")
def config_show():
    """Show current configuration (secrets redacted)."""
    from rich.console import Console
    from rich.table import Table

    from chiefwiggum.config import get_api_key, get_config_path, load_config

    console = Console()
    cfg = load_config()
    api_key = get_api_key()

    table = Table(title=f"Config: {get_config_path()}")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")

    # Show API key status without revealing it
    if api_key and api_key.startswith("sk-ant-"):
        key_display = f"{api_key[:12]}...{api_key[-4:]} ✓"
    elif api_key:
        key_display = "[red]set but invalid format[/red]"
    else:
        key_display = "[red]not set — run: chiefwiggum config set-key sk-ant-...[/red]"

    table.add_row("anthropic_api_key", key_display)

    skip = {"anthropic_api_key", "view_state", "category_assignments"}
    for k, v in sorted(cfg.items()):
        if k not in skip:
            table.add_row(k, str(v))

    console.print(table)


@main.command()
def verify():
    """Verify ChiefWiggum installation and dependencies."""
    import importlib.util
    import shutil
    import sys

    from rich.console import Console

    console = Console()
    console.print("🔍 Verifying ChiefWiggum installation...\n")

    checks = []

    # Check version
    try:
        checks.append(("Version", f"✅ {__version__}", True))
    except Exception as e:
        checks.append(("Version", f"❌ Failed: {e}", False))

    # Check core modules
    modules = ["coordination", "database", "spawner", "worktree_manager", "git_merge"]
    for mod in modules:
        try:
            importlib.import_module(f"chiefwiggum.{mod}")
            checks.append((f"Module: {mod}", "✅", True))
        except Exception as e:
            checks.append((f"Module: {mod}", f"❌ {e}", False))

    # Check CLI tools
    cli_tools = ["claude", "git"]
    for tool in cli_tools:
        if shutil.which(tool):
            checks.append((f"CLI Tool: {tool}", "✅", True))
        else:
            checks.append((f"CLI Tool: {tool}", "⚠️  Not found", False))

    # Check shell scripts
    script_path = Path(__file__).parent / "scripts" / "ralph_loop.sh"
    if script_path.exists():
        checks.append(("Ralph Loop Script", "✅", True))
    else:
        checks.append(("Ralph Loop Script", "❌ Not found", False))

    # Check database path
    try:
        db_path = get_database_path()
        checks.append(("Database Path", f"✅ {db_path}", True))
    except Exception as e:
        checks.append(("Database Path", f"❌ {e}", False))

    # Print results
    for name, status, success in checks:
        console.print(f"{name:.<40} {status}")

    # Summary
    all_critical_passed = all(c[2] for c in checks if not c[0].startswith("CLI Tool"))
    console.print()
    if all_critical_passed:
        console.print("✅ All critical checks passed!")
        sys.exit(0)
    else:
        console.print("❌ Some checks failed. Please reinstall.")
        sys.exit(1)


@main.command()
@click.option("--check", is_flag=True, help="Check for updates without installing")
def update(check: bool):
    """Update ChiefWiggum to the latest version.

    Detects how ChiefWiggum is installed and updates accordingly:
    - Editable install (development): git pull + reinstall
    - pipx install: pipx upgrade
    - PyPI install: pip install --upgrade

    Examples:
        wig update              # Update to latest version
        wig update --check      # Check if update available
    """
    import os
    import shutil
    import subprocess
    import sys

    from rich.console import Console

    console = Console()

    # Detect installation type
    package_path = Path(__file__).parent.parent
    is_editable = (package_path / ".git").exists()
    is_pipx = "pipx" in sys.prefix or "pipx" in str(Path(sys.executable).parent)

    if check:
        # Just check for updates
        console.print("🔍 Checking for updates...")

        if is_editable:
            # Check git for updates
            try:
                os.chdir(package_path)
                subprocess.run(["git", "fetch"], check=True, capture_output=True)
                result = subprocess.run(
                    ["git", "rev-list", "--count", "HEAD..origin/main"],
                    capture_output=True,
                    text=True,
                    check=True
                )
                commits_behind = int(result.stdout.strip())

                if commits_behind > 0:
                    console.print(f"[yellow]Updates available:[/yellow] {commits_behind} commit(s) behind origin/main")
                    console.print("Run [cyan]wig update[/cyan] to update")
                else:
                    console.print("[green]Already up to date[/green]")
            except Exception as e:
                console.print(f"[red]Error checking for updates:[/red] {e}")
        else:
            console.print("[dim]Update checking not available for this installation type[/dim]")
            console.print("Run [cyan]wig update[/cyan] to upgrade to latest version")
        return

    # Perform update
    console.print("🔄 Updating ChiefWiggum...\n")

    if is_editable:
        console.print("[cyan]Detected:[/cyan] Editable install (development mode)")
        console.print()

        # Check for uncommitted changes
        has_uncommitted = False
        try:
            os.chdir(package_path)
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True
            )
            has_uncommitted = bool(result.stdout.strip())
        except Exception:
            pass

        if has_uncommitted:
            console.print("[yellow]⚠ You have uncommitted local changes[/yellow]")
            console.print()
            console.print("Update options:")
            console.print("  1. [cyan]--check[/cyan] - Just reinstall dependencies (recommended)")
            console.print("  2. Stash changes first: [cyan]git stash && wig update && git stash pop[/cyan]")
            console.print("  3. Commit changes first: [cyan]git add . && git commit[/cyan]")
            console.print()
            console.print("[dim]For editable installs, code changes are live immediately.[/dim]")
            console.print("[dim]Only reinstall is needed if dependencies changed.[/dim]")
            console.print()

            # Just reinstall dependencies without git pull
            console.print("📦 Reinstalling to pick up any dependency changes...")
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-e", ".[dev]"],
                    check=True,
                    capture_output=True
                )
                console.print("[green]✓[/green] Reinstall complete\n")
                console.print("[green]✅ Dependencies updated![/green]")
                console.print("[dim]Your local code changes are preserved.[/dim]")
                sys.exit(0)
            except subprocess.CalledProcessError as e:
                console.print(f"[red]✗ Reinstall failed:[/red] {e}")
                console.print("[dim]Try running manually: pip install -e '.[dev]'[/dim]")
                sys.exit(1)

        # No uncommitted changes - proceed with full update
        console.print("📥 Pulling latest changes from git...")
        try:
            subprocess.run(["git", "pull"], check=True)
            console.print("[green]✓[/green] Git pull complete\n")
        except subprocess.CalledProcessError as e:
            console.print(f"[red]✗ Git pull failed:[/red] {e}")
            console.print("[dim]Try running manually: git pull[/dim]")
            sys.exit(1)
        except FileNotFoundError:
            console.print("[red]✗ Git not found[/red]")
            sys.exit(1)

        # Reinstall to pick up any dependency changes
        console.print("📦 Reinstalling...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-e", ".[dev]"],
                check=True,
                capture_output=True
            )
            console.print("[green]✓[/green] Reinstall complete\n")
        except subprocess.CalledProcessError as e:
            console.print(f"[red]✗ Reinstall failed:[/red] {e}")
            console.print("[dim]Try running manually: pip install -e '.[dev]'[/dim]")
            sys.exit(1)

        # Verify
        console.print("🔍 Verifying installation...")
        try:
            import importlib

            import chiefwiggum
            importlib.reload(chiefwiggum)
            console.print(f"[green]✓[/green] Updated to version {chiefwiggum.__version__}\n")
        except Exception as e:
            console.print(f"[yellow]⚠[/yellow] Could not verify version: {e}\n")

        console.print("[green]✅ Update complete![/green]")

    elif is_pipx:
        console.print("[cyan]Detected:[/cyan] pipx installation")
        console.print()

        if not shutil.which("pipx"):
            console.print("[red]✗ pipx not found[/red]")
            console.print("Install pipx first: brew install pipx")
            sys.exit(1)

        console.print("📦 Running pipx upgrade...")
        try:
            subprocess.run(["pipx", "upgrade", "chiefwiggum"], check=True)
            console.print("\n[green]✅ Update complete![/green]")
        except subprocess.CalledProcessError as e:
            console.print(f"\n[red]✗ Update failed:[/red] {e}")
            console.print("[dim]Try running manually: pipx upgrade chiefwiggum[/dim]")
            sys.exit(1)

    else:
        console.print("[cyan]Detected:[/cyan] Standard pip installation")
        console.print()

        console.print("📦 Running pip upgrade...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", "chiefwiggum"],
                check=True
            )
            console.print("\n[green]✅ Update complete![/green]")
        except subprocess.CalledProcessError as e:
            console.print(f"\n[red]✗ Update failed:[/red] {e}")
            console.print("[dim]Try running manually: pip install --upgrade chiefwiggum[/dim]")
            sys.exit(1)

    console.print("\n[dim]Run 'wig verify' to verify the installation[/dim]")


@main.group()
def daemon():
    """Background reconciler that spawns/cancels ralphs from intent queue.

    The daemon is what makes chiefwiggum keep working after you close the TUI.
    The TUI writes spawn_requests rows; the daemon consumes them and actually
    spawns ralph_loop processes. Run under launchd (see `wig service install`)
    so it survives crashes and reboots.
    """
    pass


@daemon.command("start")
@click.option("--foreground", is_flag=True, help="Run in foreground (don't detach). Required when launchd runs us.")
@click.option("--tick", type=int, default=15, show_default=True, help="Reconcile interval in seconds.")
def daemon_start(foreground: bool, tick: int):
    """Start the chiefwiggum daemon."""
    import sys

    from chiefwiggum.daemon import start_daemon

    run_async(init_db())
    sys.exit(start_daemon(foreground=foreground, tick_seconds=tick))


@daemon.command("stop")
@click.option("--timeout", type=float, default=10.0, show_default=True, help="Seconds to wait for graceful exit.")
def daemon_stop(timeout: float):
    """Stop the chiefwiggum daemon (SIGTERM, SIGKILL on timeout)."""
    from rich.console import Console

    from chiefwiggum.daemon import stop_daemon

    console = Console()
    ok, msg = stop_daemon(timeout_seconds=timeout)
    if ok:
        console.print(f"[green]✓[/green] {msg}")
    else:
        console.print(f"[yellow]![/yellow] {msg}")


@main.command("spawn")
@click.argument("project")
@click.option("--fix-plan", "-f", help="Path to @fix_plan.md (defaults to auto-detect in <project-dir>).")
@click.option("--task-id", "-t", help="Specific task ID to spawn on (default: daemon claims next).")
@click.option("--priority", "-p", type=int, default=0, show_default=True, help="Queue priority; higher goes first.")
@click.option("--wait", is_flag=True, help="Block until the daemon consumes this request.")
@click.option("--timeout", type=float, default=30.0, show_default=True, help="Seconds to wait in --wait mode.")
def spawn_cmd(project: str, fix_plan: str | None, task_id: str | None, priority: int, wait: bool, timeout: float):
    """Enqueue a ralph spawn request for the chiefwiggum daemon to execute.

    PROJECT can be either a project name (e.g. `tian`) — in which case we look
    for `~/claudecode/<name>/@fix_plan.md` — or a path to the project directory.

    The CLI writes a row to the `spawn_requests` table; the daemon picks it up
    on its next reconcile tick and actually spawns the ralph process. This
    means you can `wig spawn` from any shell, close the terminal, and the
    spawn still happens. If the daemon isn't running, the request will sit
    pending until it starts — use `wig daemon start` or `wig service install`
    first for unattended operation.
    """
    import time as _time

    from rich.console import Console

    from chiefwiggum.coordination import (
        enqueue_spawn_request,
        fetch_pending_spawn_requests,
    )
    from chiefwiggum.daemon import is_daemon_running

    console = Console()
    run_async(init_db())

    # Resolve project input to (project_name, project_dir).
    # - If the input exists as a directory, use that directory; project name = basename.
    # - Else treat it as a bare project name and look under ~/claudecode/<name>/.
    raw = Path(project).expanduser()
    if raw.exists() and raw.is_dir():
        project_dir = raw.resolve()
        project_name = project_dir.name
    else:
        project_name = project
        project_dir = (Path.home() / "claudecode" / project_name).resolve()
        if not project_dir.is_dir():
            console.print(
                f"[red]✗ project directory not found:[/red] {project_dir}\n"
                f"[dim]Pass an explicit path, e.g. `wig spawn /path/to/{project_name}`[/dim]"
            )
            return

    if fix_plan:
        fix_plan_path = str(Path(fix_plan).expanduser().resolve())
    else:
        # Ralph convention is `@fix_plan.md`; some projects use bare `fix_plan.md`.
        for candidate in ("@fix_plan.md", "fix_plan.md"):
            candidate_path = project_dir / candidate
            if candidate_path.exists():
                fix_plan_path = str(candidate_path)
                break
        else:
            console.print(
                f"[red]✗ no fix plan found in {project_dir} "
                "(looked for @fix_plan.md and fix_plan.md)[/red]"
            )
            return

    req_id = run_async(enqueue_spawn_request(
        project_path=project_name,
        fix_plan_path=fix_plan_path,
        task_id=task_id,
        priority=priority,
        requested_by="cli",
    ))

    running, pid = is_daemon_running()
    if running:
        console.print(f"[green]✓ spawn_request enqueued[/green] (id={req_id}, daemon pid={pid})")
    else:
        console.print(
            f"[yellow]! spawn_request enqueued[/yellow] (id={req_id}) — "
            "daemon is NOT running; start it with [cyan]wig daemon start[/cyan] "
            "or install as a service with [cyan]wig service install[/cyan]."
        )

    if not wait:
        return

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        pending_ids = {r["id"] for r in run_async(fetch_pending_spawn_requests(limit=100))}
        if req_id not in pending_ids:
            console.print(f"[green]✓ consumed by daemon[/green] (request {req_id})")
            return
        _time.sleep(0.5)

    console.print(
        f"[yellow]! timeout waiting for daemon to consume request {req_id}[/yellow]"
    )


@daemon.command("status")
@click.option("--format", "-f", "output_format", type=click.Choice(["text", "json"]), default="text", show_default=True)
def daemon_status_cmd(output_format: str):
    """Show daemon running state and queue depth."""
    import json

    from chiefwiggum.daemon import daemon_status

    run_async(init_db())
    info = run_async(daemon_status())

    if output_format == "json":
        # Use click.echo (no line wrapping) since paths may contain spaces.
        click.echo(json.dumps(info, indent=2, default=str))
        return

    from rich.console import Console
    console = Console()
    running = info["running"]
    pid = info["pid"]
    if running:
        console.print(f"[green]● running[/green] (pid={pid})")
    else:
        console.print("[red]○ not running[/red]")
    console.print(f"  pid file: {info['pid_file']}")
    console.print(f"  log file: {info['log_file']}")
    console.print(f"  pending spawn_requests:  {info['pending_spawn_requests']}")
    console.print(f"  pending cancel_requests: {info['pending_cancel_requests']}")


@main.group()
def service():
    """Install chiefwiggum daemon as a launchd user agent (macOS).

    `wig service install` is the one-command setup for walk-away reliability:
    it renders a launchd plist referencing your current `chiefwiggum` binary,
    loads it with launchctl, and launchd takes over from there — starting the
    daemon at login and auto-restarting it if it crashes. No terminal needed.
    """
    pass


@service.command("install")
def service_install():
    """Install and load the chiefwiggum launchd user agent."""
    from rich.console import Console

    from chiefwiggum.service import install, is_supported

    console = Console()
    if not is_supported():
        console.print(
            "[yellow]! wig service is macOS-only today[/yellow]\n"
            "On Linux, run `wig daemon start` under a systemd user-unit "
            "(template support is planned)."
        )
        return

    result = install()
    if result.installed:
        console.print(f"[green]✓ {result.message}[/green]")
        console.print(f"[dim]plist: {result.plist_path}[/dim]")
        console.print("[dim]The daemon will start now and on every login.[/dim]")
    else:
        console.print(f"[red]✗ {result.message}[/red]")


@service.command("uninstall")
def service_uninstall():
    """Unload the agent and remove the plist."""
    from rich.console import Console

    from chiefwiggum.service import is_supported, uninstall

    console = Console()
    if not is_supported():
        console.print("[yellow]! wig service is macOS-only today[/yellow]")
        return

    result = uninstall()
    color = "green" if "Unloaded" in result.message else "yellow"
    console.print(f"[{color}]{result.message}[/{color}]")


@service.command("status")
@click.option("--format", "-f", "output_format", type=click.Choice(["text", "json"]), default="text", show_default=True)
def service_status(output_format: str):
    """Show launchd agent state + daemon state."""
    import json

    from rich.console import Console

    from chiefwiggum.service import is_supported, status

    if not is_supported():
        msg = "wig service is macOS-only today"
        if output_format == "json":
            click.echo(json.dumps({"supported": False, "message": msg}, indent=2))
        else:
            Console().print(f"[yellow]! {msg}[/yellow]")
        return

    info = status()
    if output_format == "json":
        click.echo(json.dumps(info, indent=2, default=str))
        return

    console = Console()
    plist_ok = info["plist_installed"]
    loaded = info["launchd_loaded"]
    running = info["daemon_running"]
    pid = info["daemon_pid"]

    console.print(f"[bold]{info['label']}[/bold]")
    console.print(f"  plist installed: {'[green]yes[/green]' if plist_ok else '[red]no[/red]'}")
    console.print(f"  launchd loaded:  {'[green]yes[/green]' if loaded else '[red]no[/red]'}")
    if running:
        console.print(f"  daemon process:  [green]running[/green] (pid={pid})")
    else:
        console.print("  daemon process:  [red]not running[/red]")
    console.print(f"[dim]plist: {info['plist_path']}[/dim]")


@service.command("restart")
def service_restart():
    """Ask launchd to restart the daemon (kickstart -k)."""
    from rich.console import Console

    from chiefwiggum.service import is_supported, restart

    console = Console()
    if not is_supported():
        console.print("[yellow]! wig service is macOS-only today[/yellow]")
        return
    result = restart()
    color = "green" if result.installed and "failed" not in result.message else "yellow"
    console.print(f"[{color}]{result.message}[/{color}]")


if __name__ == "__main__":
    main()
