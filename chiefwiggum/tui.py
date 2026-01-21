"""ChiefWiggum TUI Dashboard

Rich-based live dashboard for monitoring Ralph instances and tasks.
Implements the full HPC Job Scheduler mental model.
"""

import asyncio
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chiefwiggum import (
    get_system_stats,
    list_active_instances,
    list_all_instances,
    list_all_tasks,
    list_failed_tasks,
    list_in_progress_tasks,
    list_pending_tasks,
    list_task_history,
    mark_stale_instances_crashed,
    pause_all_instances,
    pause_instance,
    process_retry_tasks,
    release_claim,
    resume_all_instances,
    resume_instance,
    shutdown_instance,
    stop_all_instances,
    sync_tasks_from_fix_plan,
)
from chiefwiggum.keyboard import KeyboardListener
from chiefwiggum.models import (
    ClaudeModel,
    ErrorCategory,
    RalphConfig,
    RalphInstanceStatus,
    TargetingConfig,
    TaskCategory,
    TaskClaimStatus,
    TaskPriority,
    TaskSortOrder,
)
from chiefwiggum.spawner import (
    can_spawn_ralph,
    generate_ralph_id,
    get_running_ralphs,
    read_ralph_log,
    spawn_ralph_daemon,
    stop_all_ralph_daemons,
    stop_ralph_daemon,
)


def discover_fix_plan_projects() -> list[tuple[str, Path]]:
    """Discover projects by scanning for @fix_plan.md files.

    Scans ~/claudecode/*/ for @fix_plan.md files and also checks cwd.
    Returns list of (project_name, fix_plan_path) tuples.
    """
    projects = []
    claudecode_dir = Path.home() / "claudecode"
    if claudecode_dir.exists():
        for project_dir in claudecode_dir.iterdir():
            if project_dir.is_dir():
                fix_plan = project_dir / "@fix_plan.md"
                if fix_plan.exists():
                    projects.append((project_dir.name, fix_plan))
    # Also check cwd
    cwd_fix_plan = Path.cwd() / "@fix_plan.md"
    if cwd_fix_plan.exists():
        project_name = Path.cwd().name
        if not any(p[0] == project_name for p in projects):
            projects.append((project_name, cwd_fix_plan))
    return projects


class TUIMode(Enum):
    """TUI interaction modes."""

    NORMAL = auto()
    HELP = auto()
    PROJECT_FILTER = auto()
    SHUTDOWN = auto()
    RELEASE = auto()
    # Removed SYNC mode - 'y' now syncs immediately
    SETTINGS = auto()  # Settings/config view
    SPAWN_PROJECT = auto()  # US3: Spawn Ralph - project selection
    SPAWN_PRIORITY = auto()  # US3: Spawn Ralph - priority selection
    SPAWN_CATEGORY = auto()  # US4: Spawn Ralph - category selection
    SPAWN_MODEL = auto()  # US3: Spawn Ralph - model selection
    SPAWN_CONFIRM = auto()  # US3: Spawn Ralph - confirmation
    ERROR_DETAIL = auto()  # US5: Error details view
    STATS = auto()  # US11: Statistics view
    CONFIRM_BULK_STOP = auto()  # US10: Confirm stop all
    CONFIRM_BULK_PAUSE = auto()  # US10: Confirm pause all
    LOG_VIEW = auto()  # US8: Log viewer
    HISTORY = auto()  # US12: History view
    SEARCH = auto()  # Search tasks by title
    TASK_DETAIL = auto()  # Full task detail view
    BULK_SELECT = auto()  # Bulk task selection mode
    BULK_ACTION = auto()  # Bulk action menu
    LOG_STREAM = auto()  # Live log streaming view


class ViewFocus(Enum):
    """Which panel(s) to show."""

    BOTH = auto()  # Default: split view
    TASKS = auto()  # Tasks only (full width)
    INSTANCES = auto()  # Ralph instances only (full width)


@dataclass
class SpawnConfig:
    """Configuration being built for spawning a Ralph."""

    project: str = ""
    fix_plan_path: str = ""
    priority_min: TaskPriority | None = None
    categories: list[TaskCategory] = field(default_factory=list)
    model: ClaudeModel = ClaudeModel.SONNET
    no_continue: bool = False
    max_loops: int | None = None


@dataclass
class TUIState:
    """State for the TUI dashboard."""

    mode: TUIMode = TUIMode.NORMAL
    project_filter: Optional[str] = None
    show_all_tasks: bool = False  # Default to pending only
    show_all_instances: bool = False  # US2: False = show only active/idle
    status_message: str = ""
    status_message_time: float = 0
    projects: list[str] = field(default_factory=list)
    instances: list = field(default_factory=list)
    all_instances: list = field(default_factory=list)  # For visibility toggle
    in_progress_tasks: list = field(default_factory=list)
    failed_tasks: list = field(default_factory=list)
    history_tasks: list = field(default_factory=list)  # US12: History view
    selected_task_idx: int = 0  # For error details
    selected_instance_idx: int = 0  # For log view
    spawn_config: SpawnConfig = field(default_factory=SpawnConfig)
    log_content: str = ""
    # Pagination/scrolling
    task_scroll_offset: int = 0  # For scrolling through tasks
    instance_scroll_offset: int = 0  # For scrolling through instances
    tasks_per_page: int = 20  # Number of tasks visible
    # View focus
    view_focus: ViewFocus = ViewFocus.BOTH  # Which panel(s) to show
    # Category filter
    category_filter: Optional[TaskCategory] = None
    # Search functionality
    search_query: str = ""
    search_results: list = field(default_factory=list)
    # Task detail view
    selected_task: Optional[Any] = None  # TaskClaim for detail view
    # Sort order
    sort_order: TaskSortOrder = TaskSortOrder.PRIORITY
    # Bulk operations
    selected_task_ids: set = field(default_factory=set)
    bulk_mode_active: bool = False
    # All tasks for filtering/sorting
    all_tasks_cache: list = field(default_factory=list)


def create_instances_table(instances: list, show_all: bool = False) -> Table:
    """Create a table showing Ralph instances."""
    title = "Ralph Instances" + (" (All)" if show_all else " (Active)")
    table = Table(title=title, expand=True)
    table.add_column("#", style="dim", no_wrap=True, width=2)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Host", style="green")
    table.add_column("Project", style="blue")
    table.add_column("Current Task", style="yellow")
    table.add_column("Done", justify="right", style="green")
    table.add_column("Heartbeat", style="dim")
    table.add_column("Status", justify="center")

    now = datetime.now()

    for idx, inst in enumerate(instances, 1):
        # Calculate heartbeat age
        age_seconds = (now - inst.last_heartbeat).total_seconds()
        if age_seconds < 60:
            heartbeat_str = f"{int(age_seconds)}s ago"
        elif age_seconds < 3600:
            heartbeat_str = f"{int(age_seconds / 60)}m ago"
        else:
            heartbeat_str = f"{int(age_seconds / 3600)}h ago"

        # Status styling
        status_styles = {
            RalphInstanceStatus.ACTIVE: "[green]ACTIVE[/green]",
            RalphInstanceStatus.IDLE: "[yellow]IDLE[/yellow]",
            RalphInstanceStatus.PAUSED: "[blue]PAUSED[/blue]",
            RalphInstanceStatus.STOPPED: "[dim]STOPPED[/dim]",
            RalphInstanceStatus.CRASHED: "[red]CRASHED[/red]",
        }
        status_str = status_styles.get(inst.status, inst.status.value)

        # Heartbeat warning if stale
        if inst.status == RalphInstanceStatus.ACTIVE and age_seconds > 300:
            heartbeat_str = f"[yellow]{heartbeat_str}[/yellow]"

        # Show completed task count
        done_count = str(inst.tasks_completed) if inst.tasks_completed else "0"

        table.add_row(
            str(idx) if idx <= 9 else "",
            inst.ralph_id[:12],
            inst.hostname or "-",
            inst.project or "-",
            inst.current_task_id[:25] if inst.current_task_id else "-",
            done_count,
            heartbeat_str,
            status_str,
        )

    if not instances:
        msg = "[dim]No instances" + (" registered" if show_all else " active (press i to show all)") + "[/dim]"
        table.add_row("", msg, "", "", "", "", "", "")

    return table


def create_tasks_table(
    tasks: list,
    show_numbers: bool = False,
    highlight_failed: bool = False,
    offset: int = 0,
    limit: int = 20,
    show_category: bool = True,
    expanded: bool = False,
    bulk_mode: bool = False,
    selected_ids: set | None = None,
    show_time: bool = True,
) -> Table:
    """Create a table showing tasks with pagination support."""
    title = f"Task Queue ({offset + 1}-{min(offset + limit, len(tasks))} of {len(tasks)})" if tasks else "Task Queue"
    table = Table(title=title, expand=True)
    if bulk_mode:
        table.add_column("", style="dim", no_wrap=True, width=3)  # Selection checkbox
    if show_numbers:
        table.add_column("#", style="dim", no_wrap=True, width=2)
    table.add_column("Priority", style="bold", no_wrap=True, width=8)
    if show_category:
        table.add_column("Cat", style="magenta", no_wrap=True, width=5)
    # Wider task column when expanded
    task_width = None if expanded else 35
    table.add_column("Task", style="white", max_width=task_width)
    table.add_column("Project", style="blue", width=10)
    table.add_column("Status", justify="center", width=12)
    if show_time:
        table.add_column("Time", style="dim", width=8)
    table.add_column("Claimed By", style="cyan", width=12)

    priority_styles = {
        "HIGH": "[red]HIGH[/red]",
        "MEDIUM": "[yellow]MEDIUM[/yellow]",
        "LOWER": "[blue]LOWER[/blue]",
        "POLISH": "[dim]POLISH[/dim]",
    }

    status_styles = {
        TaskClaimStatus.PENDING: "[yellow]pending[/yellow]",
        TaskClaimStatus.IN_PROGRESS: "[blue]in_progress[/blue]",
        TaskClaimStatus.COMPLETED: "[green]completed[/green]",
        TaskClaimStatus.FAILED: "[red]FAILED[/red]",
        TaskClaimStatus.RELEASED: "[dim]released[/dim]",
        TaskClaimStatus.RETRY_PENDING: "[magenta]retry[/magenta]",
    }

    category_abbrev = {
        TaskCategory.UX: "UX",
        TaskCategory.API: "API",
        TaskCategory.TESTING: "TEST",
        TaskCategory.DATABASE: "DB",
        TaskCategory.INFRA: "INFRA",
    }

    selected_ids = selected_ids or set()
    now = datetime.now()

    # Show tasks from offset to offset+limit
    visible_tasks = tasks[offset : offset + limit]
    for idx, task in enumerate(visible_tasks, offset + 1):
        priority_str = priority_styles.get(task.task_priority.value, task.task_priority.value)
        status_str = status_styles.get(task.status, task.status.value)

        # Add error indicator for failed tasks
        max_title_len = 60 if expanded else 35
        task_title = task.task_title[:max_title_len]
        if task.status == TaskClaimStatus.FAILED and task.error_category:
            task_title += f" [red]({task.error_category.value})[/red]"

        # Category
        cat_str = "-"
        if hasattr(task, "category") and task.category:
            cat_str = category_abbrev.get(task.category, task.category.value[:5])

        # Time column - show elapsed/waiting time
        time_str = "-"
        if show_time:
            if task.status == TaskClaimStatus.IN_PROGRESS and task.started_at:
                elapsed = (now - task.started_at).total_seconds()
                if elapsed < 60:
                    time_str = f"{int(elapsed)}s"
                elif elapsed < 3600:
                    time_str = f"{int(elapsed // 60)}m"
                else:
                    time_str = f"{int(elapsed // 3600)}h{int((elapsed % 3600) // 60)}m"
            elif task.status == TaskClaimStatus.PENDING and task.created_at:
                wait = (now - task.created_at).total_seconds()
                if wait < 60:
                    time_str = f"{int(wait)}s wait"
                elif wait < 3600:
                    time_str = f"{int(wait // 60)}m wait"
                else:
                    time_str = f"{int(wait // 3600)}h wait"

        row = []
        if bulk_mode:
            is_selected = task.task_id in selected_ids
            row.append("[green][X][/green]" if is_selected else "[ ]")
        if show_numbers:
            row.append(str(idx) if idx <= 9 else "")
        row.append(priority_str)
        if show_category:
            row.append(cat_str)
        row.append(task_title)
        row.append(task.project or "-")
        row.append(status_str)
        if show_time:
            row.append(time_str)
        row.append(task.claimed_by_ralph_id[:10] if task.claimed_by_ralph_id else "-")
        table.add_row(*row)

    # Calculate number of empty columns for footer rows
    num_cols = 5  # Base: Priority, Task, Project, Status, Claimed By
    if bulk_mode:
        num_cols += 1
    if show_numbers:
        num_cols += 1
    if show_category:
        num_cols += 1
    if show_time:
        num_cols += 1

    # Show pagination hint if there are more
    if len(tasks) > offset + limit:
        remaining = len(tasks) - offset - limit
        row = [""] * (num_cols - 4) + [f"[dim]... {remaining} more (j/k to scroll)[/dim]", "", "", ""]
        table.add_row(*row)
    elif offset > 0:
        # At end, show hint to scroll up
        row = [""] * (num_cols - 4) + [f"[dim](j/k to scroll)[/dim]", "", "", ""]
        table.add_row(*row)

    if not tasks:
        row = [""] * (num_cols - 4) + ["[dim]No tasks synced (press 'y' to sync)[/dim]", "", "", ""]
        table.add_row(*row)

    return table


def create_stats_panel(instances: list, tasks: list, state: TUIState) -> Panel:
    """Create a stats summary panel with notification badges."""
    active_count = sum(1 for i in instances if i.status in (RalphInstanceStatus.ACTIVE, RalphInstanceStatus.IDLE))
    pending_count = sum(1 for t in tasks if t.status == TaskClaimStatus.PENDING)
    in_progress_count = sum(1 for t in tasks if t.status == TaskClaimStatus.IN_PROGRESS)
    completed_count = sum(1 for t in tasks if t.status == TaskClaimStatus.COMPLETED)
    failed_count = sum(1 for t in tasks if t.status == TaskClaimStatus.FAILED)

    # Check for stale instances (heartbeat > 5min old)
    now = datetime.now()
    stale_count = sum(
        1 for i in instances
        if i.status == RalphInstanceStatus.ACTIVE
        and (now - i.last_heartbeat).total_seconds() > 300
    )

    text = Text()

    # Notification badges at start
    if failed_count > 0:
        text.append(f" {failed_count} FAILED ", style="bold white on red")
        text.append("  ", style="dim")
    if stale_count > 0:
        text.append(f" {stale_count} STALE ", style="bold black on yellow")
        text.append("  ", style="dim")

    text.append("Instances: ", style="dim")
    text.append(f"{active_count}", style="green bold")
    text.append(" active", style="dim")
    text.append("  |  ", style="dim")
    text.append("Tasks: ", style="dim")
    text.append(f"{pending_count}", style="yellow bold")
    text.append(" pending, ", style="dim")
    text.append(f"{in_progress_count}", style="blue bold")
    text.append(" in progress, ", style="dim")
    text.append(f"{completed_count}", style="green bold")
    text.append(" done", style="dim")

    if failed_count > 0:
        text.append(", ", style="dim")
        text.append(f"{failed_count}", style="red bold")
        text.append(" failed", style="dim")

    # Add filter info
    if state.project_filter:
        text.append("  |  ", style="dim")
        text.append("Project: ", style="dim")
        text.append(state.project_filter, style="magenta bold")

    # Add sort order indicator
    if state.sort_order != TaskSortOrder.PRIORITY:
        text.append("  |  ", style="dim")
        text.append("Sort: ", style="dim")
        text.append(state.sort_order.value, style="cyan")

    # Add view mode indicators
    text.append("  |  ", style="dim")
    if state.show_all_instances:
        text.append("[I]", style="yellow")
    else:
        text.append("[i]", style="dim")
    text.append(" ", style="dim")
    text.append("All Tasks" if state.show_all_tasks else "Pending", style="cyan")

    # Add ViewFocus indicator (zoom state)
    text.append("  ", style="dim")
    if state.view_focus == ViewFocus.BOTH:
        text.append("[B]", style="dim")
    elif state.view_focus == ViewFocus.TASKS:
        text.append("[T]", style="yellow bold")
    elif state.view_focus == ViewFocus.INSTANCES:
        text.append("[R]", style="green bold")

    # Add category filter indicator
    if state.category_filter:
        text.append("  |  ", style="dim")
        text.append("Cat: ", style="dim")
        text.append(state.category_filter.value.upper(), style="magenta bold")

    # Bulk mode indicator
    if state.bulk_mode_active:
        text.append("  |  ", style="dim")
        text.append(f"[BULK: {len(state.selected_task_ids)} selected]", style="magenta bold")

    return Panel(text, title="Summary", border_style="blue")


def create_layout() -> Layout:
    """Create the dashboard layout."""
    layout = Layout()

    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="stats", size=3),
        Layout(name="main"),
        Layout(name="command_bar", size=3),
    )

    layout["main"].split_row(
        Layout(name="instances"),
        Layout(name="tasks"),
    )

    return layout


def create_help_panel() -> Panel:
    """Create the help overlay panel."""
    help_text = Text()
    help_text.append("Keyboard Commands\n\n", style="bold cyan")

    # Navigation & Views
    help_text.append("Navigation & Views\n", style="bold yellow")
    help_text.append("  h, ?", style="yellow")
    help_text.append("  Show this help\n")
    help_text.append("  j/k ", style="yellow")
    help_text.append("  Scroll tasks down/up\n")
    help_text.append("  z   ", style="yellow")
    help_text.append("  Cycle view (split/tasks only/instances only)\n")
    help_text.append("  p   ", style="yellow")
    help_text.append("  Filter by project\n")
    help_text.append("  c   ", style="yellow")
    help_text.append("  Cycle category filter\n")
    help_text.append("  a   ", style="yellow")
    help_text.append("  Toggle all tasks / pending only\n")
    help_text.append("  i   ", style="yellow")
    help_text.append("  Toggle instance visibility (active/all)\n")
    help_text.append("  t   ", style="yellow")
    help_text.append("  Show statistics view\n")
    help_text.append("  H   ", style="yellow")
    help_text.append("  Show task history (audit trail)\n")
    help_text.append("  S   ", style="yellow")
    help_text.append("  Settings (API key, permissions, config)\n")

    # Task Operations
    help_text.append("\nTask Operations\n", style="bold yellow")
    help_text.append("  y   ", style="yellow")
    help_text.append("  Sync tasks from @fix_plan.md\n")
    help_text.append("  r   ", style="yellow")
    help_text.append("  Release task claim\n")
    help_text.append("  e   ", style="yellow")
    help_text.append("  View error details for failed task\n")

    # Instance Operations
    help_text.append("\nInstance Operations\n", style="bold yellow")
    help_text.append("  n   ", style="yellow")
    help_text.append("  Spawn new Ralph daemon\n")
    help_text.append("  s   ", style="yellow")
    help_text.append("  Shutdown instance\n")
    help_text.append("  l   ", style="yellow")
    help_text.append("  View logs for instance\n")

    # Bulk Operations
    help_text.append("\nBulk Operations\n", style="bold yellow")
    help_text.append("  ^S  ", style="yellow")
    help_text.append("  Stop ALL Ralphs (emergency)\n")
    help_text.append("  ^P  ", style="yellow")
    help_text.append("  Pause ALL Ralphs\n")
    help_text.append("  ^R  ", style="yellow")
    help_text.append("  Resume ALL paused Ralphs\n")

    # Exit
    help_text.append("\n  q   ", style="yellow")
    help_text.append("  Quit dashboard\n")
    help_text.append("  Esc ", style="yellow")
    help_text.append("  Cancel / return to normal mode\n")

    return Panel(help_text, title="Help", border_style="cyan", padding=(1, 2))


def create_stats_view_panel(state: TUIState) -> Panel:
    """Create detailed statistics view panel (US11)."""
    text = Text()
    text.append("System Statistics\n\n", style="bold cyan")

    # We'll populate this with actual stats in update_dashboard
    text.append("Press any key to close", style="dim")

    return Panel(text, title="Statistics (t)", border_style="cyan", padding=(1, 2))


def create_error_detail_panel(task, state: TUIState) -> Panel:
    """Create error detail panel for a failed task (US5)."""
    text = Text()

    if not task:
        text.append("No failed task selected", style="dim")
        return Panel(text, title="Error Details", border_style="red")

    text.append(f"Task: ", style="bold")
    text.append(f"{task.task_title}\n\n", style="white")

    text.append(f"ID: ", style="dim")
    text.append(f"{task.task_id}\n", style="cyan")

    text.append(f"Status: ", style="dim")
    text.append(f"{task.status.value}\n", style="red bold")

    if task.error_category:
        text.append(f"Error Category: ", style="dim")
        category_styles = {
            ErrorCategory.TRANSIENT: "yellow",
            ErrorCategory.CODE_ERROR: "red",
            ErrorCategory.PERMISSION: "magenta",
            ErrorCategory.CONFLICT: "blue",
            ErrorCategory.TIMEOUT: "yellow",
            ErrorCategory.UNKNOWN: "dim",
        }
        style = category_styles.get(task.error_category, "white")
        text.append(f"{task.error_category.value}\n", style=style)

    text.append(f"Retry Count: ", style="dim")
    text.append(f"{task.retry_count}/{task.max_retries}\n", style="white")

    if task.next_retry_at:
        text.append(f"Next Retry: ", style="dim")
        text.append(f"{task.next_retry_at.strftime('%H:%M:%S')}\n", style="yellow")

    if task.error_message:
        text.append(f"\nError Message:\n", style="bold red")
        # Truncate long error messages
        error_msg = task.error_message
        if len(error_msg) > 500:
            error_msg = error_msg[:500] + "..."
        text.append(error_msg, style="white")

    # Suggested action
    text.append("\n\nSuggested Action: ", style="bold")
    if task.error_category == ErrorCategory.TRANSIENT:
        text.append("Will auto-retry", style="green")
    elif task.error_category == ErrorCategory.TIMEOUT:
        text.append("Increase timeout and retry", style="yellow")
    elif task.error_category == ErrorCategory.CONFLICT:
        text.append("Resolve git conflict manually", style="blue")
    elif task.error_category == ErrorCategory.PERMISSION:
        text.append("Check access permissions", style="magenta")
    else:
        text.append("Manual fix required", style="red")

    text.append("\n\nPress any key to close", style="dim")

    return Panel(text, title="Error Details (e)", border_style="red", padding=(1, 2))


def create_spawn_panel(state: TUIState) -> Panel:
    """Create spawn configuration panel (US3)."""
    text = Text()
    config = state.spawn_config

    text.append("Spawn New Ralph\n\n", style="bold cyan")

    if state.mode == TUIMode.SPAWN_PROJECT:
        text.append("Select Project:\n", style="bold yellow")
        for idx, project in enumerate(state.projects[:9], 1):
            text.append(f"  {idx}", style="yellow bold")
            text.append(f" = {project}\n", style="white")
        text.append("\n  Esc", style="yellow bold")
        text.append(" = cancel\n", style="dim")

    elif state.mode == TUIMode.SPAWN_PRIORITY:
        text.append(f"Project: ", style="dim")
        text.append(f"{config.project}\n\n", style="cyan")
        text.append("Select Minimum Priority:\n", style="bold yellow")
        text.append("  1", style="yellow bold")
        text.append(" = HIGH only\n", style="white")
        text.append("  2", style="yellow bold")
        text.append(" = MEDIUM and above\n", style="white")
        text.append("  3", style="yellow bold")
        text.append(" = LOWER and above\n", style="white")
        text.append("  4", style="yellow bold")
        text.append(" = All priorities\n", style="white")

    elif state.mode == TUIMode.SPAWN_CATEGORY:
        text.append(f"Project: ", style="dim")
        text.append(f"{config.project}\n", style="cyan")
        text.append(f"Priority: ", style="dim")
        text.append(f"{config.priority_min.value if config.priority_min else 'All'}\n\n", style="cyan")
        text.append("Select Category (optional):\n", style="bold yellow")
        text.append("  1", style="yellow bold")
        text.append(" = UX (components, templates)\n", style="white")
        text.append("  2", style="yellow bold")
        text.append(" = API (routes, endpoints)\n", style="white")
        text.append("  3", style="yellow bold")
        text.append(" = Testing (tests, specs)\n", style="white")
        text.append("  4", style="yellow bold")
        text.append(" = Database (models, migrations)\n", style="white")
        text.append("  5", style="yellow bold")
        text.append(" = Infra (scripts, docker)\n", style="white")
        text.append("  0", style="yellow bold")
        text.append(" = All categories (skip)\n", style="white")

    elif state.mode == TUIMode.SPAWN_MODEL:
        text.append(f"Project: ", style="dim")
        text.append(f"{config.project}\n", style="cyan")
        text.append(f"Priority: ", style="dim")
        text.append(f"{config.priority_min.value if config.priority_min else 'All'}\n", style="cyan")
        if config.categories:
            text.append(f"Categories: ", style="dim")
            text.append(f"{', '.join(c.value for c in config.categories)}\n\n", style="cyan")
        else:
            text.append(f"Categories: ", style="dim")
            text.append("All\n\n", style="cyan")
        text.append("Select Model:\n", style="bold yellow")
        text.append("  1", style="yellow bold")
        text.append(" = Sonnet (recommended)\n", style="white")
        text.append("  2", style="yellow bold")
        text.append(" = Opus\n", style="white")
        text.append("  3", style="yellow bold")
        text.append(" = Haiku\n", style="white")

    elif state.mode == TUIMode.SPAWN_CONFIRM:
        text.append("Configuration Summary:\n\n", style="bold yellow")
        text.append(f"  Project: ", style="dim")
        text.append(f"{config.project}\n", style="cyan")
        text.append(f"  Priority: ", style="dim")
        text.append(f"{config.priority_min.value if config.priority_min else 'All'}\n", style="cyan")
        if config.categories:
            text.append(f"  Categories: ", style="dim")
            text.append(f"{', '.join(c.value for c in config.categories)}\n", style="cyan")
        text.append(f"  Model: ", style="dim")
        text.append(f"{config.model.value}\n", style="cyan")
        text.append(f"  Fix Plan: ", style="dim")
        text.append(f"{config.fix_plan_path}\n\n", style="cyan")
        text.append("  Enter", style="green bold")
        text.append(" = Spawn\n", style="white")
        text.append("  Esc", style="yellow bold")
        text.append(" = Cancel\n", style="dim")

    return Panel(text, title="Spawn Ralph (n)", border_style="green", padding=(1, 2))


def create_log_view_panel(state: TUIState) -> Panel:
    """Create log view panel (US8)."""
    text = Text()

    if not state.log_content:
        text.append("No log content available", style="dim")
    else:
        # Show last portion of logs
        lines = state.log_content.split("\n")
        for line in lines[-30:]:
            if "error" in line.lower() or "Error" in line:
                text.append(line + "\n", style="red")
            elif "warning" in line.lower() or "Warning" in line:
                text.append(line + "\n", style="yellow")
            else:
                text.append(line + "\n", style="white")

    text.append("\nPress any key to close", style="dim")

    return Panel(text, title="Ralph Logs (l)", border_style="blue", padding=(1, 2))


def create_history_panel(state: TUIState) -> Panel:
    """Create task history panel (US12)."""
    text = Text()
    text.append("Task History\n\n", style="bold cyan")

    if not state.history_tasks:
        text.append("No completed tasks yet", style="dim")
    else:
        # Show last 15 completed tasks
        for task in state.history_tasks[:15]:
            status_style = "green" if task.status.value == "completed" else "red"
            text.append(f"  {task.task_title[:30]:<30}", style="white")
            text.append(f"  [{task.status.value}]", style=status_style)
            text.append(f"  {task.ralph_id[:10]}", style="cyan")

            # Duration
            if task.duration_seconds < 60:
                dur_str = f"{task.duration_seconds:.0f}s"
            elif task.duration_seconds < 3600:
                dur_str = f"{task.duration_seconds/60:.1f}m"
            else:
                dur_str = f"{task.duration_seconds/3600:.1f}h"
            text.append(f"  {dur_str}", style="dim")

            # Commit SHA if available
            if task.commit_sha:
                text.append(f"  {task.commit_sha[:7]}", style="yellow")

            text.append("\n")

        if len(state.history_tasks) > 15:
            text.append(f"\n  ... and {len(state.history_tasks) - 15} more\n", style="dim")

    text.append("\n\nPress any key to close", style="dim")

    return Panel(text, title="History (H)", border_style="magenta", padding=(1, 2))


def create_settings_panel(state: TUIState) -> Panel:
    """Create settings/configuration panel."""
    text = Text()
    text.append("Settings & Configuration\n\n", style="bold cyan")

    # API Key status
    text.append("API Configuration\n", style="bold yellow")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        masked = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
        text.append(f"  ANTHROPIC_API_KEY: ", style="dim")
        text.append(f"{masked}\n", style="green")
    else:
        text.append(f"  ANTHROPIC_API_KEY: ", style="dim")
        text.append("Not set\n", style="red")
        text.append("    Set via: export ANTHROPIC_API_KEY=sk-...\n", style="dim")

    # Ralph permissions (from ralph_loop defaults)
    text.append("\nRalph Permissions (hardcoded defaults)\n", style="bold yellow")
    text.append("  Allowed prompts:\n", style="dim")
    text.append("    - run tests\n", style="white")
    text.append("    - install dependencies\n", style="white")
    text.append("    - build the project\n", style="white")
    text.append("    - run type checker\n", style="white")
    text.append("    - run linter\n", style="white")
    text.append("    - run formatter\n", style="white")
    text.append("\n  [Future: configure via ~/.chiefwiggum/config.yaml]\n", style="dim")

    # Current view state
    text.append("\nCurrent View State\n", style="bold yellow")
    text.append(f"  Show all tasks:    ", style="dim")
    text.append(f"{'Yes' if state.show_all_tasks else 'No (pending only)'}\n", style="white")
    text.append(f"  Show all instances:", style="dim")
    text.append(f"{'Yes' if state.show_all_instances else 'No (active only)'}\n", style="white")
    text.append(f"  View focus:        ", style="dim")
    text.append(f"{state.view_focus.name}\n", style="white")
    if state.category_filter:
        text.append(f"  Category filter:   ", style="dim")
        text.append(f"{state.category_filter.value}\n", style="magenta")
    if state.project_filter:
        text.append(f"  Project filter:    ", style="dim")
        text.append(f"{state.project_filter}\n", style="cyan")

    # Key bindings reference
    text.append("\nQuick Reference\n", style="bold yellow")
    text.append("  j/k  - Scroll tasks\n", style="dim")
    text.append("  z    - Toggle view (split/tasks/instances)\n", style="dim")
    text.append("  c    - Cycle category filter\n", style="dim")
    text.append("  a    - Toggle all/pending tasks\n", style="dim")
    text.append("  i    - Toggle all/active instances\n", style="dim")

    text.append("\n\nPress any key to close", style="dim")

    return Panel(text, title="Settings (S)", border_style="cyan", padding=(1, 2))


def create_search_panel(state: TUIState) -> Panel:
    """Create search input panel."""
    text = Text()
    text.append("Search Tasks\n\n", style="bold cyan")
    text.append("Type to search by task title (case-insensitive)\n\n", style="dim")
    text.append("Query: ", style="yellow bold")
    text.append(state.search_query or "_", style="white")
    text.append("\n\n", style="dim")

    if state.search_results:
        text.append(f"Found {len(state.search_results)} matching tasks:\n", style="green")
        for idx, task in enumerate(state.search_results[:10], 1):
            status_style = "green" if task.status == TaskClaimStatus.COMPLETED else (
                "red" if task.status == TaskClaimStatus.FAILED else "yellow"
            )
            text.append(f"  {idx}. ", style="dim")
            text.append(f"[{task.task_priority.value[:1]}]", style="cyan")
            text.append(f" {task.task_title[:50]}", style="white")
            text.append(f" [{task.status.value}]", style=status_style)
            text.append("\n")
        if len(state.search_results) > 10:
            text.append(f"\n  ... and {len(state.search_results) - 10} more\n", style="dim")
    elif state.search_query:
        text.append("No matching tasks found\n", style="dim")

    text.append("\nEnter", style="yellow bold")
    text.append(" = search  ", style="dim")
    text.append("Esc", style="yellow bold")
    text.append(" = cancel", style="dim")

    return Panel(text, title="Search (/)", border_style="cyan", padding=(1, 2))


def create_task_detail_panel(task, state: TUIState) -> Panel:
    """Create detailed task view panel."""
    text = Text()

    if not task:
        text.append("No task selected", style="dim")
        return Panel(text, title="Task Detail", border_style="cyan")

    # Title and ID
    text.append(f"{task.task_title}\n", style="bold white")
    text.append(f"ID: {task.task_id}\n\n", style="dim cyan")

    # Status and Priority
    status_styles = {
        TaskClaimStatus.PENDING: "yellow",
        TaskClaimStatus.IN_PROGRESS: "blue",
        TaskClaimStatus.COMPLETED: "green",
        TaskClaimStatus.FAILED: "red",
        TaskClaimStatus.RELEASED: "dim",
        TaskClaimStatus.RETRY_PENDING: "magenta",
    }
    text.append("Status: ", style="dim")
    text.append(f"{task.status.value}\n", style=status_styles.get(task.status, "white"))

    priority_styles = {"HIGH": "red", "MEDIUM": "yellow", "LOWER": "blue", "POLISH": "dim"}
    text.append("Priority: ", style="dim")
    text.append(f"{task.task_priority.value}\n", style=priority_styles.get(task.task_priority.value, "white"))

    # Category and Project
    if hasattr(task, "category") and task.category:
        text.append("Category: ", style="dim")
        text.append(f"{task.category.value}\n", style="magenta")
    if task.project:
        text.append("Project: ", style="dim")
        text.append(f"{task.project}\n", style="blue")

    text.append("\n")

    # Assignment info
    if task.claimed_by_ralph_id:
        text.append("Claimed By: ", style="dim")
        text.append(f"{task.claimed_by_ralph_id}\n", style="cyan")
    if task.claimed_at:
        text.append("Claimed At: ", style="dim")
        text.append(f"{task.claimed_at.strftime('%Y-%m-%d %H:%M:%S')}\n", style="white")

    # Timestamps
    if task.started_at:
        text.append("Started: ", style="dim")
        text.append(f"{task.started_at.strftime('%Y-%m-%d %H:%M:%S')}\n", style="white")
        # Calculate running time for in-progress tasks
        if task.status == TaskClaimStatus.IN_PROGRESS:
            elapsed = (datetime.now() - task.started_at).total_seconds()
            if elapsed < 60:
                elapsed_str = f"{int(elapsed)}s"
            elif elapsed < 3600:
                elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
            else:
                elapsed_str = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m"
            text.append("Running Time: ", style="dim")
            text.append(f"{elapsed_str}\n", style="yellow")

    if task.completed_at:
        text.append("Completed: ", style="dim")
        text.append(f"{task.completed_at.strftime('%Y-%m-%d %H:%M:%S')}\n", style="green")

    # Git commit
    if task.git_commit_sha:
        text.append("\nCommit: ", style="dim")
        text.append(f"{task.git_commit_sha[:12]}\n", style="yellow")

    # Error info (full, not truncated)
    if task.error_message:
        text.append("\nError Message:\n", style="bold red")
        text.append(f"{task.error_message}\n", style="white")

    if task.error_category:
        text.append("Error Category: ", style="dim")
        text.append(f"{task.error_category.value}\n", style="red")

    # Retry info
    if task.retry_count > 0:
        text.append(f"\nRetry Count: {task.retry_count}/{task.max_retries}\n", style="yellow")
    if task.next_retry_at:
        text.append(f"Next Retry: {task.next_retry_at.strftime('%H:%M:%S')}\n", style="yellow")

    text.append("\n\nPress any key to close", style="dim")

    return Panel(text, title="Task Detail (d)", border_style="cyan", padding=(1, 2))


def create_bulk_action_panel(state: TUIState) -> Panel:
    """Create bulk action menu panel."""
    text = Text()
    count = len(state.selected_task_ids)
    text.append(f"Bulk Actions - {count} task(s) selected\n\n", style="bold cyan")

    text.append("Select action:\n\n", style="dim")
    text.append("  r", style="yellow bold")
    text.append(" = Release claims (return to pending)\n", style="white")
    text.append("  p", style="yellow bold")
    text.append(" = Mark as pending (retry failed tasks)\n", style="white")

    text.append("\n\nEsc", style="yellow bold")
    text.append(" = cancel", style="dim")

    return Panel(text, title="Bulk Actions (m)", border_style="magenta", padding=(1, 2))


def create_log_stream_panel(state: TUIState) -> Panel:
    """Create log streaming panel with auto-refresh."""
    text = Text()

    if not state.log_content:
        text.append("No log content available\n", style="dim")
        text.append("Select an instance first (l key)", style="dim")
    else:
        lines = state.log_content.split("\n")
        for line in lines[-40:]:
            line_lower = line.lower()
            if "error" in line_lower or "failed" in line_lower or "exception" in line_lower:
                text.append(line + "\n", style="red")
            elif "warning" in line_lower or "warn" in line_lower:
                text.append(line + "\n", style="yellow")
            elif "success" in line_lower or "completed" in line_lower or "done" in line_lower:
                text.append(line + "\n", style="green")
            else:
                text.append(line + "\n", style="white")

    text.append("\n[Auto-refreshing every 2s] ", style="dim")
    text.append("Press any key to close", style="dim")

    return Panel(text, title="Log Stream (v)", border_style="blue", padding=(1, 2))


def create_confirm_panel(action: str, count: int) -> Panel:
    """Create confirmation panel for bulk operations."""
    text = Text()
    text.append(f"Confirm: {action}\n\n", style="bold red")
    text.append(f"This will affect ", style="white")
    text.append(f"{count}", style="yellow bold")
    text.append(f" Ralph instance(s).\n\n", style="white")
    text.append("  y", style="green bold")
    text.append(" = Confirm\n", style="white")
    text.append("  n, Esc", style="yellow bold")
    text.append(" = Cancel\n", style="white")

    return Panel(text, title="Confirm Action", border_style="red", padding=(1, 2))


def create_command_bar(state: TUIState) -> Panel:
    """Create the command bar based on current mode."""
    text = Text()

    if state.mode == TUIMode.NORMAL:
        text.append("  h", style="yellow bold")
        text.append(" Help  ", style="dim")
        text.append("y", style="yellow bold")
        text.append(" Sync  ", style="dim")
        text.append("j/k", style="yellow bold")
        text.append(" Scroll  ", style="dim")
        # Show zoom state
        text.append("z", style="yellow bold")
        if state.view_focus == ViewFocus.BOTH:
            text.append(" [Both]  ", style="dim")
        elif state.view_focus == ViewFocus.TASKS:
            text.append(" [Tasks]  ", style="cyan")
        else:
            text.append(" [Ralphs]  ", style="green")
        # Show category state
        text.append("c", style="yellow bold")
        if state.category_filter:
            text.append(f" [{state.category_filter.value}]  ", style="magenta")
        else:
            text.append(" [All]  ", style="dim")
        text.append("S", style="yellow bold")
        text.append(" Settings  ", style="dim")
        text.append("q", style="yellow bold")
        text.append(" Quit", style="dim")

        # Show status message if recent (< 5 seconds)
        if state.status_message and (time.time() - state.status_message_time) < 5:
            text.append("  |  ", style="dim")
            # Color based on content
            if "error" in state.status_message.lower() or "failed" in state.status_message.lower():
                text.append(state.status_message, style="red")
            else:
                text.append(state.status_message, style="green")

    elif state.mode == TUIMode.HELP:
        text.append("  Press any key to close help", style="cyan")

    elif state.mode == TUIMode.PROJECT_FILTER:
        text.append("  Select project: ", style="cyan bold")
        for idx, project in enumerate(state.projects[:9], 1):
            text.append(f" {idx}", style="yellow bold")
            text.append(f"={project}", style="dim")
        text.append("  0", style="yellow bold")
        text.append("=clear", style="dim")
        text.append("  Esc", style="yellow bold")
        text.append("=cancel", style="dim")

    elif state.mode == TUIMode.SHUTDOWN:
        text.append("  Select instance to shutdown: ", style="red bold")
        for idx, inst in enumerate(state.instances[:9], 1):
            text.append(f" {idx}", style="yellow bold")
            text.append(f"={inst.ralph_id[:8]}", style="dim")
        text.append("  Esc", style="yellow bold")
        text.append("=cancel", style="dim")

    elif state.mode == TUIMode.RELEASE:
        text.append("  Select task to release: ", style="blue bold")
        for idx, task in enumerate(state.in_progress_tasks[:9], 1):
            text.append(f" {idx}", style="yellow bold")
            text.append(f"={task.task_title[:12]}", style="dim")
        text.append("  Esc", style="yellow bold")
        text.append("=cancel", style="dim")

    elif state.mode in (TUIMode.SPAWN_PROJECT, TUIMode.SPAWN_PRIORITY, TUIMode.SPAWN_CATEGORY, TUIMode.SPAWN_MODEL, TUIMode.SPAWN_CONFIRM):
        text.append("  Spawning Ralph - follow prompts above  ", style="green bold")
        text.append("Esc", style="yellow bold")
        text.append("=cancel", style="dim")

    elif state.mode == TUIMode.HISTORY:
        text.append("  Viewing task history  ", style="magenta bold")
        text.append("Press any key to close", style="dim")

    elif state.mode == TUIMode.ERROR_DETAIL:
        text.append("  Viewing error details  ", style="red bold")
        text.append("Press any key to close", style="dim")

    elif state.mode == TUIMode.STATS:
        text.append("  Viewing statistics  ", style="cyan bold")
        text.append("Press any key to close", style="dim")

    elif state.mode == TUIMode.LOG_VIEW:
        text.append("  Viewing logs  ", style="blue bold")
        text.append("Press any key to close", style="dim")

    elif state.mode == TUIMode.SETTINGS:
        text.append("  Viewing settings  ", style="cyan bold")
        text.append("Press any key to close", style="dim")

    elif state.mode in (TUIMode.CONFIRM_BULK_STOP, TUIMode.CONFIRM_BULK_PAUSE):
        text.append("  y", style="green bold")
        text.append("=confirm  ", style="dim")
        text.append("n/Esc", style="yellow bold")
        text.append("=cancel", style="dim")

    return Panel(text, border_style="dim")


async def update_dashboard(layout: Layout, state: TUIState) -> None:
    """Update all dashboard components."""
    # Mark stale instances and process retries
    await mark_stale_instances_crashed()
    await process_retry_tasks()

    # Get data
    state.all_instances = await list_all_instances()
    if state.show_all_instances:
        instances = state.all_instances
    else:
        instances = await list_active_instances()

    if state.show_all_tasks:
        tasks = await list_all_tasks()
    else:
        tasks = await list_pending_tasks()

    # Apply project filter
    if state.project_filter:
        tasks = [t for t in tasks if t.project == state.project_filter]
        instances = [i for i in instances if i.project == state.project_filter]

    # Apply sort order
    priority_order = {TaskPriority.HIGH: 0, TaskPriority.MEDIUM: 1, TaskPriority.LOWER: 2, TaskPriority.POLISH: 3}
    status_order = {
        TaskClaimStatus.PENDING: 0, TaskClaimStatus.IN_PROGRESS: 1,
        TaskClaimStatus.RETRY_PENDING: 2, TaskClaimStatus.FAILED: 3,
        TaskClaimStatus.COMPLETED: 4, TaskClaimStatus.RELEASED: 5
    }

    if state.sort_order == TaskSortOrder.PRIORITY:
        tasks.sort(key=lambda t: priority_order.get(t.task_priority, 99))
    elif state.sort_order == TaskSortOrder.STATUS:
        tasks.sort(key=lambda t: status_order.get(t.status, 99))
    elif state.sort_order == TaskSortOrder.AGE_NEWEST:
        tasks.sort(key=lambda t: t.created_at, reverse=True)
    elif state.sort_order == TaskSortOrder.AGE_OLDEST:
        tasks.sort(key=lambda t: t.created_at)
    elif state.sort_order == TaskSortOrder.PROJECT:
        tasks.sort(key=lambda t: t.project or "")

    # Store tasks for bulk mode and detail view
    state.all_tasks_cache = tasks

    # Update state with current data for command modes
    state.projects = list(set(t.project for t in await list_all_tasks() if t.project))
    state.instances = await list_active_instances()
    state.in_progress_tasks = await list_in_progress_tasks()
    state.failed_tasks = await list_failed_tasks()

    # Update header
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    running_count = len(get_running_ralphs())
    header_text = f"ChiefWiggum Dashboard  |  {now}  |  {running_count} daemon(s)"
    header = Panel(
        Text(header_text, justify="center", style="bold white"),
        style="blue",
    )
    layout["header"].update(header)

    # Update stats
    all_tasks = await list_all_tasks()
    layout["stats"].update(create_stats_panel(state.all_instances, all_tasks, state))

    # Check for overlay modes - must unsplit first to clear child layouts
    if state.mode == TUIMode.HELP:
        layout["main"].unsplit()
        layout["main"].update(create_help_panel())
    elif state.mode == TUIMode.STATS:
        layout["main"].unsplit()
        # Create detailed stats panel
        stats = await get_system_stats()
        text = Text()
        text.append("System Statistics\n\n", style="bold cyan")

        text.append("Tasks\n", style="bold yellow")
        text.append(f"  Total:       {stats.total_tasks}\n")
        text.append(f"  Pending:     {stats.pending_tasks}\n", style="yellow")
        text.append(f"  In Progress: {stats.in_progress_tasks}\n", style="blue")
        text.append(f"  Completed:   {stats.completed_tasks}\n", style="green")
        text.append(f"  Failed:      {stats.failed_tasks}\n", style="red")

        text.append("\nPerformance\n", style="bold yellow")
        text.append(f"  Tasks/Hour:  {stats.tasks_per_hour:.1f}\n")
        if stats.eta_minutes:
            if stats.eta_minutes < 60:
                text.append(f"  ETA:         {stats.eta_minutes:.0f} minutes\n", style="green")
            else:
                text.append(f"  ETA:         {stats.eta_minutes/60:.1f} hours\n", style="yellow")
        else:
            text.append(f"  ETA:         Unknown\n", style="dim")

        text.append("\nInstances\n", style="bold yellow")
        text.append(f"  Active:      {stats.active_instances}\n", style="green")
        text.append(f"  Idle/Paused: {stats.idle_instances}\n", style="yellow")

        if stats.session_start:
            duration = datetime.now() - stats.session_start
            hours = int(duration.total_seconds() // 3600)
            minutes = int((duration.total_seconds() % 3600) // 60)
            text.append(f"\nSession:       {hours}h {minutes}m\n", style="dim")

        text.append("\n\nPress any key to close", style="dim")
        layout["main"].update(Panel(text, title="Statistics", border_style="cyan", padding=(1, 2)))

    elif state.mode == TUIMode.ERROR_DETAIL:
        layout["main"].unsplit()
        task = state.failed_tasks[state.selected_task_idx] if state.failed_tasks else None
        layout["main"].update(create_error_detail_panel(task, state))

    elif state.mode in (TUIMode.SPAWN_PROJECT, TUIMode.SPAWN_PRIORITY, TUIMode.SPAWN_CATEGORY, TUIMode.SPAWN_MODEL, TUIMode.SPAWN_CONFIRM):
        layout["main"].unsplit()
        layout["main"].update(create_spawn_panel(state))

    elif state.mode == TUIMode.LOG_VIEW:
        layout["main"].unsplit()
        layout["main"].update(create_log_view_panel(state))

    elif state.mode == TUIMode.HISTORY:
        layout["main"].unsplit()
        # Load history data
        state.history_tasks = await list_task_history(project=state.project_filter, limit=50)
        layout["main"].update(create_history_panel(state))

    elif state.mode == TUIMode.CONFIRM_BULK_STOP:
        layout["main"].unsplit()
        count = len(state.instances)
        layout["main"].update(create_confirm_panel("STOP ALL Ralphs", count))

    elif state.mode == TUIMode.CONFIRM_BULK_PAUSE:
        layout["main"].unsplit()
        count = len([i for i in state.instances if i.status == RalphInstanceStatus.ACTIVE])
        layout["main"].update(create_confirm_panel("PAUSE ALL Ralphs", count))

    elif state.mode == TUIMode.SETTINGS:
        layout["main"].unsplit()
        layout["main"].update(create_settings_panel(state))

    elif state.mode == TUIMode.SEARCH:
        layout["main"].unsplit()
        layout["main"].update(create_search_panel(state))

    elif state.mode == TUIMode.TASK_DETAIL:
        layout["main"].unsplit()
        layout["main"].update(create_task_detail_panel(state.selected_task, state))

    elif state.mode == TUIMode.BULK_ACTION:
        layout["main"].unsplit()
        layout["main"].update(create_bulk_action_panel(state))

    elif state.mode == TUIMode.LOG_STREAM:
        layout["main"].unsplit()
        # Refresh log content for streaming
        if state.instances and state.selected_instance_idx < len(state.instances):
            ralph_id = state.instances[state.selected_instance_idx].ralph_id
            state.log_content = read_ralph_log(ralph_id, 100)
        layout["main"].update(create_log_stream_panel(state))

    else:
        # Apply category filter
        if state.category_filter:
            tasks = [t for t in tasks if hasattr(t, "category") and t.category == state.category_filter]

        # Store filtered task count for scrolling bounds
        state._current_tasks_count = len(tasks)

        # Clamp scroll offset to valid range
        if state.task_scroll_offset >= len(tasks):
            state.task_scroll_offset = max(0, len(tasks) - state.tasks_per_page)

        # Normal view - handle view focus
        show_task_numbers = state.mode == TUIMode.RELEASE
        expanded = state.view_focus != ViewFocus.BOTH

        if state.view_focus == ViewFocus.TASKS:
            # Tasks only - full width (must unsplit first)
            layout["main"].unsplit()
            layout["main"].update(
                Panel(
                    create_tasks_table(
                        tasks,
                        show_numbers=show_task_numbers,
                        offset=state.task_scroll_offset,
                        limit=state.tasks_per_page,
                        expanded=True,
                        bulk_mode=state.bulk_mode_active,
                        selected_ids=state.selected_task_ids,
                    ),
                    border_style="yellow",
                )
            )
        elif state.view_focus == ViewFocus.INSTANCES:
            # Instances only - full width (must unsplit first)
            layout["main"].unsplit()
            layout["main"].update(
                Panel(create_instances_table(instances, state.show_all_instances), border_style="green")
            )
        else:
            # Both - split view (default)
            layout["main"].split_row(
                Layout(name="instances"),
                Layout(name="tasks"),
            )
            layout["main"]["instances"].update(Panel(create_instances_table(instances, state.show_all_instances), border_style="green"))
            layout["main"]["tasks"].update(
                Panel(
                    create_tasks_table(
                        tasks,
                        show_numbers=show_task_numbers,
                        offset=state.task_scroll_offset,
                        limit=state.tasks_per_page,
                        bulk_mode=state.bulk_mode_active,
                        selected_ids=state.selected_task_ids,
                    ),
                    border_style="yellow",
                )
            )

    # Update command bar
    layout["command_bar"].update(create_command_bar(state))


def handle_normal_mode(key: str, state: TUIState, tasks_count: int = 0) -> bool:
    """Handle key press in normal mode. Returns True if should quit."""
    if key in ("h", "?"):
        state.mode = TUIMode.HELP
    elif key == "p":
        if state.projects:
            state.mode = TUIMode.PROJECT_FILTER
        else:
            state.status_message = "No projects available"
            state.status_message_time = time.time()
    elif key == "a":
        state.show_all_tasks = not state.show_all_tasks
        state.task_scroll_offset = 0  # Reset scroll when toggling
        state.status_message = "Showing all tasks" if state.show_all_tasks else "Showing pending only"
        state.status_message_time = time.time()
    elif key == "i":  # US2: Toggle instance visibility
        state.show_all_instances = not state.show_all_instances
        state.instance_scroll_offset = 0  # Reset scroll
        state.status_message = "Showing all instances" if state.show_all_instances else "Showing active only"
        state.status_message_time = time.time()
    elif key == "j":  # Scroll down tasks
        if state.task_scroll_offset + state.tasks_per_page < tasks_count:
            state.task_scroll_offset += 5  # Scroll by 5
    elif key == "k":  # Scroll up tasks
        if state.task_scroll_offset > 0:
            state.task_scroll_offset = max(0, state.task_scroll_offset - 5)
    elif key == "z":  # Cycle view focus: BOTH -> TASKS -> INSTANCES -> BOTH
        if state.view_focus == ViewFocus.BOTH:
            state.view_focus = ViewFocus.TASKS
            state.status_message = "Zoom: Tasks only (z to cycle)"
        elif state.view_focus == ViewFocus.TASKS:
            state.view_focus = ViewFocus.INSTANCES
            state.status_message = "Zoom: Ralphs only (z to cycle)"
        else:
            state.view_focus = ViewFocus.BOTH
            state.status_message = "Zoom: Split view (z to cycle)"
        state.status_message_time = time.time()
    elif key == "c":  # Cycle category filter
        categories = [None, TaskCategory.UX, TaskCategory.API, TaskCategory.TESTING, TaskCategory.DATABASE, TaskCategory.INFRA]
        current_idx = 0
        if state.category_filter:
            try:
                current_idx = categories.index(state.category_filter)
            except ValueError:
                pass
        next_idx = (current_idx + 1) % len(categories)
        state.category_filter = categories[next_idx]
        state.task_scroll_offset = 0  # Reset scroll
        if state.category_filter:
            state.status_message = f"Category: {state.category_filter.value} (c to cycle)"
        else:
            state.status_message = "Category: All (c to cycle)"
        state.status_message_time = time.time()
    elif key == "S":  # Settings
        state.mode = TUIMode.SETTINGS
    elif key == "s":
        if state.instances:
            state.mode = TUIMode.SHUTDOWN
        else:
            state.status_message = "No active instances"
            state.status_message_time = time.time()
    elif key == "r":
        if state.in_progress_tasks:
            state.mode = TUIMode.RELEASE
        else:
            state.status_message = "No in-progress tasks"
            state.status_message_time = time.time()
    elif key == "y":  # US1: Sync tasks immediately
        # Show syncing message before starting
        state.status_message = "Syncing tasks..."
        state.status_message_time = time.time()
        # Note: actual sync happens in handle_command since we need async
    elif key == "n":  # US3: Spawn Ralph
        if state.projects:
            state.spawn_config = SpawnConfig()
            state.mode = TUIMode.SPAWN_PROJECT
        else:
            state.status_message = "No projects available - sync tasks first"
            state.status_message_time = time.time()
    elif key == "e":  # US5: Error details
        if state.failed_tasks:
            state.selected_task_idx = 0
            state.mode = TUIMode.ERROR_DETAIL
        else:
            state.status_message = "No failed tasks"
            state.status_message_time = time.time()
    elif key == "t":  # US11: Statistics
        state.mode = TUIMode.STATS
    elif key == "l":  # US8: Log view
        if state.instances:
            state.selected_instance_idx = 0
            ralph_id = state.instances[0].ralph_id
            state.log_content = read_ralph_log(ralph_id, 100)
            state.mode = TUIMode.LOG_VIEW
        else:
            state.status_message = "No instances to view logs"
            state.status_message_time = time.time()
    elif key == "H":  # US12: History view
        state.mode = TUIMode.HISTORY
    elif key == "\x13":  # Ctrl+S - Stop all
        if state.instances:
            state.mode = TUIMode.CONFIRM_BULK_STOP
        else:
            state.status_message = "No instances to stop"
            state.status_message_time = time.time()
    elif key == "\x10":  # Ctrl+P - Pause all
        active = [i for i in state.instances if i.status == RalphInstanceStatus.ACTIVE]
        if active:
            state.mode = TUIMode.CONFIRM_BULK_PAUSE
        else:
            state.status_message = "No active instances to pause"
            state.status_message_time = time.time()
    elif key == "\x12":  # Ctrl+R - Resume all
        return False  # Will be handled in handle_command
    # New key bindings
    elif key == "/":  # Search tasks
        state.search_query = ""
        state.search_results = []
        state.mode = TUIMode.SEARCH
    elif key == "d":  # Task detail view
        # Select first task from current view
        if state.all_tasks_cache:
            state.selected_task = state.all_tasks_cache[state.task_scroll_offset] if state.task_scroll_offset < len(state.all_tasks_cache) else None
            if state.selected_task:
                state.mode = TUIMode.TASK_DETAIL
            else:
                state.status_message = "No task to view"
                state.status_message_time = time.time()
        else:
            state.status_message = "No tasks available"
            state.status_message_time = time.time()
    elif key == "o":  # Cycle sort order
        sort_orders = list(TaskSortOrder)
        current_idx = sort_orders.index(state.sort_order)
        next_idx = (current_idx + 1) % len(sort_orders)
        state.sort_order = sort_orders[next_idx]
        state.task_scroll_offset = 0  # Reset scroll
        state.status_message = f"Sort: {state.sort_order.value}"
        state.status_message_time = time.time()
    elif key == "x":  # Toggle bulk select mode
        state.bulk_mode_active = not state.bulk_mode_active
        if not state.bulk_mode_active:
            state.selected_task_ids = set()  # Clear selection when exiting
        state.status_message = "Bulk select ON" if state.bulk_mode_active else "Bulk select OFF"
        state.status_message_time = time.time()
    elif key == " " and state.bulk_mode_active:  # Space to toggle selection in bulk mode
        if state.all_tasks_cache and state.task_scroll_offset < len(state.all_tasks_cache):
            task = state.all_tasks_cache[state.task_scroll_offset]
            if task.task_id in state.selected_task_ids:
                state.selected_task_ids.remove(task.task_id)
            else:
                state.selected_task_ids.add(task.task_id)
    elif key == "m" and state.bulk_mode_active:  # Bulk action menu
        if state.selected_task_ids:
            state.mode = TUIMode.BULK_ACTION
        else:
            state.status_message = "No tasks selected"
            state.status_message_time = time.time()
    elif key == "w":  # JSON export
        return False  # Will be handled in handle_command (async)
    elif key == "v":  # Log streaming view
        if state.instances:
            state.selected_instance_idx = 0
            ralph_id = state.instances[0].ralph_id
            state.log_content = read_ralph_log(ralph_id, 100)
            state.mode = TUIMode.LOG_STREAM
        else:
            state.status_message = "No instances for log streaming"
            state.status_message_time = time.time()
    elif key == "q":
        return True
    return False


async def handle_project_filter(key: str, state: TUIState) -> None:
    """Handle key press in project filter mode."""
    if key == "ESCAPE":
        state.mode = TUIMode.NORMAL
    elif key == "0":
        state.project_filter = None
        state.status_message = "Filter cleared"
        state.status_message_time = time.time()
        state.mode = TUIMode.NORMAL
    elif key.isdigit() and 1 <= int(key) <= min(9, len(state.projects)):
        idx = int(key) - 1
        state.project_filter = state.projects[idx]
        state.status_message = f"Filtering by: {state.project_filter}"
        state.status_message_time = time.time()
        state.mode = TUIMode.NORMAL


async def handle_shutdown(key: str, state: TUIState) -> None:
    """Handle key press in shutdown mode."""
    if key == "ESCAPE":
        state.mode = TUIMode.NORMAL
    elif key.isdigit() and 1 <= int(key) <= min(9, len(state.instances)):
        idx = int(key) - 1
        instance = state.instances[idx]
        try:
            await shutdown_instance(instance.ralph_id)
            # Also try to stop the daemon process
            stop_ralph_daemon(instance.ralph_id)
            state.status_message = f"Shutdown: {instance.ralph_id}"
            state.status_message_time = time.time()
        except Exception as e:
            state.status_message = f"Error: {e}"
            state.status_message_time = time.time()
        state.mode = TUIMode.NORMAL


async def handle_release(key: str, state: TUIState) -> None:
    """Handle key press in release mode."""
    if key == "ESCAPE":
        state.mode = TUIMode.NORMAL
    elif key.isdigit() and 1 <= int(key) <= min(9, len(state.in_progress_tasks)):
        idx = int(key) - 1
        task = state.in_progress_tasks[idx]
        try:
            await release_claim(task.claimed_by_ralph_id, task.task_id)
            state.status_message = f"Released: {task.task_title[:20]}"
            state.status_message_time = time.time()
        except Exception as e:
            state.status_message = f"Error: {e}"
            state.status_message_time = time.time()
        state.mode = TUIMode.NORMAL


async def handle_spawn(key: str, state: TUIState) -> None:
    """Handle spawn mode navigation (US3)."""
    config = state.spawn_config

    if key == "ESCAPE":
        state.mode = TUIMode.NORMAL
        return

    if state.mode == TUIMode.SPAWN_PROJECT:
        if key.isdigit() and 1 <= int(key) <= min(9, len(state.projects)):
            idx = int(key) - 1
            config.project = state.projects[idx]
            # Find fix plan path
            possible_paths = [
                Path.home() / "claudecode" / config.project / "@fix_plan.md",
            ]
            for path in possible_paths:
                if path.exists():
                    config.fix_plan_path = str(path)
                    break
            state.mode = TUIMode.SPAWN_PRIORITY

    elif state.mode == TUIMode.SPAWN_PRIORITY:
        if key == "1":
            config.priority_min = TaskPriority.HIGH
            state.mode = TUIMode.SPAWN_CATEGORY
        elif key == "2":
            config.priority_min = TaskPriority.MEDIUM
            state.mode = TUIMode.SPAWN_CATEGORY
        elif key == "3":
            config.priority_min = TaskPriority.LOWER
            state.mode = TUIMode.SPAWN_CATEGORY
        elif key == "4":
            config.priority_min = None  # All priorities
            state.mode = TUIMode.SPAWN_CATEGORY

    elif state.mode == TUIMode.SPAWN_CATEGORY:
        if key == "0":
            config.categories = []  # All categories
            state.mode = TUIMode.SPAWN_MODEL
        elif key == "1":
            config.categories = [TaskCategory.UX]
            state.mode = TUIMode.SPAWN_MODEL
        elif key == "2":
            config.categories = [TaskCategory.API]
            state.mode = TUIMode.SPAWN_MODEL
        elif key == "3":
            config.categories = [TaskCategory.TESTING]
            state.mode = TUIMode.SPAWN_MODEL
        elif key == "4":
            config.categories = [TaskCategory.DATABASE]
            state.mode = TUIMode.SPAWN_MODEL
        elif key == "5":
            config.categories = [TaskCategory.INFRA]
            state.mode = TUIMode.SPAWN_MODEL

    elif state.mode == TUIMode.SPAWN_MODEL:
        if key == "1":
            config.model = ClaudeModel.SONNET
            state.mode = TUIMode.SPAWN_CONFIRM
        elif key == "2":
            config.model = ClaudeModel.OPUS
            state.mode = TUIMode.SPAWN_CONFIRM
        elif key == "3":
            config.model = ClaudeModel.HAIKU
            state.mode = TUIMode.SPAWN_CONFIRM

    elif state.mode == TUIMode.SPAWN_CONFIRM:
        if key in ("\r", "\n"):  # Enter
            # Actually spawn the Ralph
            can_spawn, reason = await can_spawn_ralph()
            if not can_spawn:
                state.status_message = reason
                state.status_message_time = time.time()
                state.mode = TUIMode.NORMAL
                return

            ralph_id = generate_ralph_id(config.project[:8])
            ralph_config = RalphConfig(model=config.model)
            targeting = TargetingConfig(
                project=config.project,
                priority_min=config.priority_min,
                categories=config.categories,
            )

            success, message = spawn_ralph_daemon(
                ralph_id=ralph_id,
                project=config.project,
                fix_plan_path=config.fix_plan_path,
                config=ralph_config,
                targeting=targeting,
            )

            state.status_message = message
            state.status_message_time = time.time()
            state.mode = TUIMode.NORMAL


async def handle_bulk_operations(key: str, state: TUIState) -> None:
    """Handle bulk operation confirmations (US10)."""
    if key == "ESCAPE" or key == "n":
        state.mode = TUIMode.NORMAL
        return

    if key == "y":
        if state.mode == TUIMode.CONFIRM_BULK_STOP:
            # Stop all
            count = await stop_all_instances()
            # Also stop daemon processes
            stop_all_ralph_daemons()
            state.status_message = f"Stopped {count} instances"
            state.status_message_time = time.time()

        elif state.mode == TUIMode.CONFIRM_BULK_PAUSE:
            # Pause all
            count = await pause_all_instances()
            state.status_message = f"Paused {count} instances"
            state.status_message_time = time.time()

        state.mode = TUIMode.NORMAL


async def handle_search(key: str, state: TUIState) -> None:
    """Handle search mode input."""
    from chiefwiggum import list_all_tasks

    if key == "ESCAPE":
        state.mode = TUIMode.NORMAL
        state.search_query = ""
        state.search_results = []
    elif key in ("\r", "\n"):  # Enter - execute search
        if state.search_query:
            all_tasks = await list_all_tasks()
            query_lower = state.search_query.lower()
            state.search_results = [
                t for t in all_tasks
                if query_lower in t.task_title.lower()
            ]
            if not state.search_results:
                state.status_message = "No matching tasks found"
                state.status_message_time = time.time()
        state.mode = TUIMode.NORMAL
    elif key == "\x7f" or key == "BACKSPACE":  # Backspace
        state.search_query = state.search_query[:-1]
        # Live search as user types
        if state.search_query:
            all_tasks = await list_all_tasks()
            query_lower = state.search_query.lower()
            state.search_results = [
                t for t in all_tasks
                if query_lower in t.task_title.lower()
            ]
    elif len(key) == 1 and key.isprintable():  # Regular character
        state.search_query += key
        # Live search as user types
        all_tasks = await list_all_tasks()
        query_lower = state.search_query.lower()
        state.search_results = [
            t for t in all_tasks
            if query_lower in t.task_title.lower()
        ]


async def handle_bulk_task_action(key: str, state: TUIState) -> None:
    """Handle bulk action menu."""
    from chiefwiggum import release_claim, list_all_tasks
    from chiefwiggum.database import get_connection

    if key == "ESCAPE":
        state.mode = TUIMode.NORMAL
        return

    if key == "r":  # Release claims
        released = 0
        for task_id in state.selected_task_ids:
            all_tasks = await list_all_tasks()
            task = next((t for t in all_tasks if t.task_id == task_id), None)
            if task and task.claimed_by_ralph_id:
                try:
                    await release_claim(task.claimed_by_ralph_id, task_id)
                    released += 1
                except Exception:
                    pass
        state.status_message = f"Released {released} task(s)"
        state.status_message_time = time.time()
        state.selected_task_ids = set()
        state.bulk_mode_active = False
        state.mode = TUIMode.NORMAL

    elif key == "p":  # Mark as pending (retry)
        conn = await get_connection()
        try:
            updated = 0
            for task_id in state.selected_task_ids:
                await conn.execute(
                    """UPDATE task_claims
                       SET status = ?, claimed_by_ralph_id = NULL, error_message = NULL,
                           error_category = NULL, updated_at = ?
                       WHERE task_id = ?""",
                    (TaskClaimStatus.PENDING.value, datetime.now(), task_id)
                )
                updated += 1
            await conn.commit()
            state.status_message = f"Reset {updated} task(s) to pending"
            state.status_message_time = time.time()
        finally:
            await conn.close()
        state.selected_task_ids = set()
        state.bulk_mode_active = False
        state.mode = TUIMode.NORMAL


async def handle_command(key: str, state: TUIState) -> bool:
    """Handle a key command. Returns True if should quit."""
    if state.mode == TUIMode.NORMAL:
        # Handle Ctrl+R for resume here since it's async
        if key == "\x12":  # Ctrl+R
            count = await resume_all_instances()
            state.status_message = f"Resumed {count} instances"
            state.status_message_time = time.time()
            return False
        # Handle 'y' for sync here since it's async
        if key == "y":
            state.status_message = "Syncing tasks..."
            state.status_message_time = time.time()
            synced = 0
            # Use discovery to find projects instead of relying on state.projects
            # This fixes the chicken-and-egg problem where empty DB = empty projects
            discovered = discover_fix_plan_projects()
            if not discovered:
                state.status_message = "No @fix_plan.md files found"
                state.status_message_time = time.time()
                return False
            for project_name, fix_plan_path in discovered:
                count = await sync_tasks_from_fix_plan(str(fix_plan_path), project_name)
                synced += count
            state.status_message = f"Synced {synced} tasks from {len(discovered)} project(s)"
            state.status_message_time = time.time()
            return False
        # Handle 'w' for JSON export here since it's async
        if key == "w":
            from chiefwiggum.coordination import export_tasks_json
            try:
                filepath = await export_tasks_json()
                state.status_message = f"Exported to {filepath}"
                state.status_message_time = time.time()
            except Exception as e:
                state.status_message = f"Export failed: {e}"
                state.status_message_time = time.time()
            return False
        tasks_count = getattr(state, "_current_tasks_count", 0)
        return handle_normal_mode(key, state, tasks_count)

    elif state.mode == TUIMode.HELP:
        state.mode = TUIMode.NORMAL

    elif state.mode == TUIMode.STATS:
        state.mode = TUIMode.NORMAL

    elif state.mode == TUIMode.ERROR_DETAIL:
        state.mode = TUIMode.NORMAL

    elif state.mode == TUIMode.LOG_VIEW:
        state.mode = TUIMode.NORMAL

    elif state.mode == TUIMode.LOG_STREAM:
        state.mode = TUIMode.NORMAL

    elif state.mode == TUIMode.HISTORY:
        state.mode = TUIMode.NORMAL

    elif state.mode == TUIMode.SETTINGS:
        state.mode = TUIMode.NORMAL

    elif state.mode == TUIMode.TASK_DETAIL:
        state.mode = TUIMode.NORMAL

    elif state.mode == TUIMode.PROJECT_FILTER:
        await handle_project_filter(key, state)

    elif state.mode == TUIMode.SHUTDOWN:
        await handle_shutdown(key, state)

    elif state.mode == TUIMode.RELEASE:
        await handle_release(key, state)

    elif state.mode == TUIMode.SEARCH:
        await handle_search(key, state)

    elif state.mode == TUIMode.BULK_ACTION:
        await handle_bulk_task_action(key, state)

    elif state.mode in (TUIMode.SPAWN_PROJECT, TUIMode.SPAWN_PRIORITY, TUIMode.SPAWN_CATEGORY, TUIMode.SPAWN_MODEL, TUIMode.SPAWN_CONFIRM):
        await handle_spawn(key, state)

    elif state.mode in (TUIMode.CONFIRM_BULK_STOP, TUIMode.CONFIRM_BULK_PAUSE):
        await handle_bulk_operations(key, state)

    return False


def run_tui(debug: bool = False):
    """Run the TUI dashboard."""
    console = Console()
    layout = create_layout()
    state = TUIState()
    keyboard = KeyboardListener()

    # Debug logging
    debug_file = None
    if debug or os.environ.get("CHIEFWIGGUM_DEBUG"):
        debug_file = open("/tmp/chiefwiggum_debug.log", "a")
        debug_file.write(f"\n=== TUI started at {datetime.now()} ===\n")

    try:
        keyboard.start()

        with Live(layout, console=console, refresh_per_second=10, screen=True) as live:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # Initial update
            loop.run_until_complete(update_dashboard(layout, state))

            last_data_refresh = time.time()
            should_quit = False

            while not should_quit:
                # Check for keyboard input
                key = keyboard.get_key()
                if key:
                    if debug_file:
                        debug_file.write(f"Key: {repr(key)} | Mode: {state.mode.name} | Focus: {state.view_focus.name} | Cat: {state.category_filter}\n")
                        debug_file.flush()
                    should_quit = loop.run_until_complete(handle_command(key, state))
                    if debug_file:
                        debug_file.write(f"  -> Mode: {state.mode.name} | Focus: {state.view_focus.name} | Cat: {state.category_filter} | Quit: {should_quit}\n")
                        debug_file.flush()
                    if should_quit:
                        break
                    # Refresh display after command
                    loop.run_until_complete(update_dashboard(layout, state))
                    live.refresh()

                # Refresh data every 2 seconds
                current_time = time.time()
                if current_time - last_data_refresh >= 2:
                    loop.run_until_complete(update_dashboard(layout, state))
                    live.refresh()
                    last_data_refresh = current_time

                # Small sleep to prevent busy-waiting
                time.sleep(0.05)

            # Check if there are running daemons on exit
            running = get_running_ralphs()
            if running:
                console.print(f"\n[yellow]Note: {len(running)} Ralph daemon(s) still running[/yellow]")
                console.print("[dim]Use 'chiefwiggum tui' to manage them, or kill manually[/dim]")

    except KeyboardInterrupt:
        pass
    finally:
        keyboard.stop()
        if debug_file:
            debug_file.write(f"=== TUI closed at {datetime.now()} ===\n")
            debug_file.close()
        console.print("\n[dim]Dashboard closed[/dim]")
