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

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chiefwiggum import (
    check_ralph_completions,
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
    register_ralph_instance,
    release_claim,
    resume_all_instances,
    resume_instance,
    shutdown_instance,
    stop_all_instances,
    sync_tasks_from_fix_plan,
)
from chiefwiggum.config import (
    get_api_key_source,
    validate_api_key,
    get_api_key,
    get_auto_scaling_config,
    get_default_model,
    get_default_timeout,
    get_max_ralphs,
    get_quickstart_defaults,
    get_ralph_loop_settings,
    get_ralph_permissions,
    get_view_state,
    load_config_on_startup,
    save_view_state,
    set_api_key,
    set_auto_scaling_config,
    set_default_model,
    set_max_ralphs,
    set_ralph_loop_setting,
    set_ralph_permission,
    get_config_value,
    set_config_value,
    get_task_assignment_strategy,
    set_task_assignment_strategy,
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
    spawn_ralph_with_task_claim,
    stop_all_ralph_daemons,
    stop_ralph_daemon,
)
from chiefwiggum.icons import (
    ICON_ACTIVE,
    ICON_CRASHED,
    ICON_DAEMON,
    ICON_DONE,
    ICON_FAILED,
    ICON_HIGH,
    ICON_IDLE,
    ICON_LOWER,
    ICON_MEDIUM,
    ICON_PAUSED,
    ICON_PENDING,
    ICON_POLISH,
    ICON_RELEASED,
    ICON_RETRY,
    ICON_SELECTED,
    ICON_STALE,
    ICON_STOPPED,
    ICON_WORKING,
    SEP_VERTICAL,
    SPINNER,
)


def discover_fix_plan_projects(project: str | None = None) -> list[tuple[str, Path]]:
    """Discover projects by scanning for @fix_plan.md files.

    If project is specified, only returns that project's fix_plan.
    Otherwise scans ~/claudecode/*/ for @fix_plan.md files and also checks cwd.

    Args:
        project: Optional specific project to look for

    Returns:
        List of (project_name, fix_plan_path) tuples.
    """
    projects = []
    claudecode_dir = Path.home() / "claudecode"

    # If a specific project is requested, only look for that one
    if project is not None:
        fix_plan = claudecode_dir / project / "@fix_plan.md"
        if fix_plan.exists():
            return [(project, fix_plan)]
        # Also check cwd if it matches the project name
        if Path.cwd().name == project:
            cwd_fix_plan = Path.cwd() / "@fix_plan.md"
            if cwd_fix_plan.exists():
                return [(project, cwd_fix_plan)]
        return []

    # Otherwise, scan for all projects
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


def auto_save_view_state(state: "TUIState") -> None:
    """Auto-save view state if persistence is enabled."""
    if get_config_value("persist_view_state", True):
        save_view_state({
            "show_all_tasks": state.show_all_tasks,
            "show_all_instances": state.show_all_instances,
            "view_focus": state.view_focus.name,
            "category_filter": state.category_filter.value if state.category_filter else None,
            "project_filter": state.project_filter,
            "sort_order": state.sort_order.value,
        })


def get_current_project(state: "TUIState") -> str | None:
    """Determine the current project context.

    Priority:
    1. If project_filter is set in TUI state -> use that
    2. If cwd is under ~/claudecode/{project}/ -> use that project
    3. Else -> return None

    Args:
        state: Current TUI state

    Returns:
        Project name or None if unknown
    """
    # 1. Check project filter
    if state.project_filter:
        return state.project_filter

    # 2. Check if cwd is under claudecode
    cwd = Path.cwd()
    claudecode_dir = Path.home() / "claudecode"
    try:
        if claudecode_dir in cwd.parents or cwd.parent == claudecode_dir:
            if cwd.parent == claudecode_dir:
                return cwd.name
            else:
                return cwd.relative_to(claudecode_dir).parts[0]
    except ValueError:
        pass

    return None


class TUIMode(Enum):
    """TUI interaction modes."""

    NORMAL = auto()
    HELP = auto()
    PROJECT_FILTER = auto()
    SHUTDOWN = auto()
    RELEASE = auto()
    # Removed SYNC mode - 'y' now syncs immediately
    SETTINGS = auto()  # Settings/config view
    SETTINGS_EDIT_API_KEY = auto()  # Edit API key input
    SETTINGS_EDIT_MAX_RALPHS = auto()  # Edit max concurrent ralphs
    SETTINGS_EDIT_MODEL = auto()  # Edit default model
    SETTINGS_EDIT_TIMEOUT = auto()  # Edit default timeout
    SETTINGS_EDIT_PERMISSIONS = auto()  # Edit ralph permissions
    SETTINGS_EDIT_STRATEGY = auto()  # Edit task assignment strategy
    SETTINGS_EDIT_AUTO_SPAWN = auto()  # Edit auto-spawn settings
    SETTINGS_EDIT_RALPH_LOOP = auto()  # Edit ralph loop settings
    SPAWN_PROJECT = auto()  # US3: Spawn Ralph - project selection
    SPAWN_PRIORITY = auto()  # US3: Spawn Ralph - priority selection
    SPAWN_CATEGORY = auto()  # US4: Spawn Ralph - category selection
    SPAWN_MODEL = auto()  # US3: Spawn Ralph - model selection
    SPAWN_SESSION = auto()  # Spawn Ralph - session settings
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
    INSTANCE_DETAIL = auto()  # Instance detail drill-down view
    INSTANCE_ERROR_DETAIL = auto()  # Full error message overlay
    CLEANUP_CONFIRM = auto()  # Confirm cleanup of idle ralphs


class InstanceDetailTab(Enum):
    """Instance detail view tabs."""

    DASHBOARD = 0
    HISTORY = 1
    ERRORS = 2
    LOGS = 3


class SettingsSection(Enum):
    """Settings panel sections for navigation."""

    API_CONFIG = 0
    TASK_BEHAVIOR = 1
    RALPH_PERMISSIONS = 2
    INSTANCE_SPECIALIZATION = 3
    AUTO_SCALING = 4
    VIEW_STATE = 5


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
    no_continue: bool = True  # Stop after one task by default
    max_loops: int | None = None
    # Session settings (from config defaults, adjustable in SPAWN_SESSION step)
    session_continuity: bool = True  # Continue sessions (opposite of no_continue)
    session_expiry_hours: int = 24  # Session expiry in hours


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
    # Text input buffer for settings edit modes
    input_buffer: str = ""
    # Settings navigation cursor (0 = API Key, 1 = Max Ralphs)
    settings_cursor: int = 0
    # Settings section navigation
    settings_section: int = 0  # SettingsSection index
    # Permission editing cursor (for navigating permissions list)
    permission_cursor: int = 0
    # List of permission keys for editing
    permission_keys: list = field(default_factory=lambda: [
        "run_tests", "install_dependencies", "build_project",
        "run_type_checker", "run_linter", "run_formatter"
    ])
    # Instance detail view state
    instance_detail_tab: int = 0  # 0=Dashboard, 1=History, 2=Errors, 3=Logs
    instance_task_history: list = field(default_factory=list)  # Tasks for this instance
    instance_failed_tasks: list = field(default_factory=list)  # Failed tasks for this instance
    instance_history_scroll: int = 0  # Scroll position in history tab
    instance_error_scroll: int = 0  # Scroll position in errors tab
    instance_selected_error_idx: int = 0  # Selected error for full view
    instance_history_filter: str = "all"  # all, completed, failed
    instance_current_task_started: Optional[datetime] = None  # For time-on-task
    instance_status_message: str = ""  # Activity message from status file
    instance_failure_streak: int = 0  # Consecutive failures (warn if >= 3)
    instance_log_content: str = ""  # Log content for instance detail view
    instance_log_needs_refresh: bool = True  # Flag to control log reloading
    instance_detail_needs_refresh: bool = True  # Flag to control instance detail data reloading
    # Help panel scrolling
    help_scroll_offset: int = 0  # For scrolling through help content
    # Console dimensions for responsive layout
    console_width: int = 80  # Updated dynamically in run_tui


def create_instances_table(instances: list, show_all: bool = False, selected_idx: int | None = None) -> Table:
    """Create a table showing Ralph instances."""
    title = "Ralph Instances" + (" (All)" if show_all else " (Active)")
    table = Table(title=title, expand=True)
    table.add_column("#", style="dim", no_wrap=True, width=2)
    table.add_column("ID", no_wrap=True)
    table.add_column("Host")
    table.add_column("Project")
    table.add_column("Current Task")
    table.add_column("Done", justify="right", style="dim")
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

        # Status styling with icons
        # Check for stale ACTIVE instances (heartbeat > 5min old)
        if inst.status == RalphInstanceStatus.ACTIVE and age_seconds > 300:
            # Stale ACTIVE - show warning instead of green
            status_str = f"[bold yellow]{ICON_STALE} STALE[/bold yellow]"
            heartbeat_str = f"[yellow]{heartbeat_str}[/yellow]"
        else:
            status_styles = {
                RalphInstanceStatus.ACTIVE: f"[bold green]{ICON_ACTIVE} ACTIVE[/bold green]",
                RalphInstanceStatus.IDLE: f"[yellow]{ICON_IDLE} IDLE[/yellow]",
                RalphInstanceStatus.PAUSED: f"[blue]{ICON_PAUSED} PAUSED[/blue]",
                RalphInstanceStatus.STOPPED: f"[dim]{ICON_STOPPED} STOP[/dim]",
                RalphInstanceStatus.CRASHED: f"[bold red]{ICON_CRASHED} CRASH[/bold red]",
            }
            status_str = status_styles.get(inst.status, inst.status.value)

        # Show completed task count
        done_count = str(inst.tasks_completed) if inst.tasks_completed else "0"

        # Highlight selected row
        is_selected = selected_idx is not None and (idx - 1) == selected_idx
        # Show only the suffix of ralph_id (without hostname prefix) for readability
        id_display = inst.ralph_id
        if inst.hostname and inst.ralph_id.startswith(inst.hostname + "-"):
            id_display = inst.ralph_id[len(inst.hostname) + 1:]
        # Add selection indicator to index column
        idx_display = f"{ICON_SELECTED} {idx}" if is_selected and idx <= 9 else (str(idx) if idx <= 9 else "")
        row_args = (
            idx_display,
            id_display[:12],
            inst.hostname or "-",
            inst.project or "-",
            inst.current_task_id[:25] if inst.current_task_id else "-",
            done_count,
            heartbeat_str,
            status_str,
        )
        if is_selected:
            table.add_row(*row_args, style="bold white on grey23")
        else:
            table.add_row(*row_args)

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
    selected_idx: int | None = None,
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
        table.add_column("Cat", style="dim", no_wrap=True, width=5)
    # Wider task column when expanded
    task_width = None if expanded else 35
    table.add_column("Task", max_width=task_width)
    table.add_column("Project", width=10)
    table.add_column("Status", justify="center", width=12)
    table.add_column("Claimed By", style="dim", width=12)

    priority_styles = {
        "HIGH": f"[bold red]{ICON_HIGH} HIGH[/bold red]",
        "MEDIUM": f"[yellow]{ICON_MEDIUM} MEDIUM[/yellow]",
        "LOWER": f"[blue]{ICON_LOWER} LOWER[/blue]",
        "POLISH": f"[dim]{ICON_POLISH} POLISH[/dim]",
    }

    status_styles = {
        TaskClaimStatus.PENDING: f"[yellow]{ICON_PENDING} pending[/yellow]",
        TaskClaimStatus.IN_PROGRESS: f"[bold blue]{ICON_WORKING} active[/bold blue]",
        TaskClaimStatus.COMPLETED: f"[green]{ICON_DONE} done[/green]",
        TaskClaimStatus.FAILED: f"[bold red]{ICON_FAILED} FAILED[/bold red]",
        TaskClaimStatus.RELEASED: f"[dim]{ICON_RELEASED} released[/dim]",
        TaskClaimStatus.RETRY_PENDING: f"[magenta]{ICON_RETRY} retry[/magenta]",
    }

    category_abbrev = {
        TaskCategory.UX: "UX",
        TaskCategory.API: "API",
        TaskCategory.TESTING: "TEST",
        TaskCategory.DATABASE: "DB",
        TaskCategory.INFRA: "INFRA",
    }

    selected_ids = selected_ids or set()

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

        # Check if this row is highlighted
        is_row_selected = selected_idx is not None and (idx - 1) == selected_idx

        row = []
        if bulk_mode:
            is_bulk_selected = task.task_id in selected_ids
            row.append("[green][X][/green]" if is_bulk_selected else "[ ]")
        if show_numbers:
            # Add selection indicator to index column
            idx_display = f"{ICON_SELECTED} {idx}" if is_row_selected and idx <= 9 else (str(idx) if idx <= 9 else "")
            row.append(idx_display)
        row.append(priority_str)
        if show_category:
            row.append(cat_str)
        row.append(task_title)
        row.append(task.project or "-")
        row.append(status_str)
        # Show just the suffix (project-unique) of ralph_id for readability
        if task.claimed_by_ralph_id:
            parts = task.claimed_by_ralph_id.split("-")
            claimed_display = "-".join(parts[-2:]) if len(parts) >= 2 else task.claimed_by_ralph_id
            row.append(claimed_display[:12])
        else:
            row.append("-")

        # Highlight selected row with softer style
        if is_row_selected:
            table.add_row(*row, style="bold white on grey23")
        else:
            table.add_row(*row)

    # Calculate number of empty columns for footer rows
    num_cols = 5  # Base: Priority, Task, Project, Status, Claimed By
    if bulk_mode:
        num_cols += 1
    if show_numbers:
        num_cols += 1
    if show_category:
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

    # Notification badges at start with icons
    if failed_count > 0:
        text.append(f" {ICON_FAILED} {failed_count} FAILED ", style="bold white on red")
        text.append(f"  {SEP_VERTICAL}  ", style="dim")
    if stale_count > 0:
        text.append(f" {ICON_STALE} {stale_count} STALE ", style="bold black on yellow")
        text.append(f"  {SEP_VERTICAL}  ", style="dim")

    # Instances section
    text.append("Instances ", style="dim")
    text.append(f"{active_count}", style="bold green")
    text.append("/", style="dim")
    total_instances = len(instances)
    text.append(f"{total_instances}", style="dim")
    text.append(f"  {SEP_VERTICAL}  ", style="dim")

    # Tasks section
    text.append("Tasks ", style="dim")
    text.append(f"{pending_count}", style="yellow bold")
    text.append(f" {ICON_PENDING}", style="yellow")
    text.append("  ", style="dim")
    text.append(f"{in_progress_count}", style="blue bold")
    text.append(f" {ICON_WORKING}", style="blue")
    text.append("  ", style="dim")
    text.append(f"{completed_count}", style="green bold")
    text.append(f" {ICON_DONE}", style="green")
    # Note: Failed count is shown in the notification badge at start, not duplicated here

    # Add filter info
    if state.project_filter:
        text.append(f"  {SEP_VERTICAL}  ", style="dim")
        text.append("Prj: ", style="dim")
        text.append(state.project_filter, style="magenta bold")

    # Add sort order indicator
    if state.sort_order != TaskSortOrder.PRIORITY:
        text.append(f"  {SEP_VERTICAL}  ", style="dim")
        text.append("Sort: ", style="dim")
        text.append(state.sort_order.value, style="cyan")

    # Add view mode indicators
    text.append(f"  {SEP_VERTICAL}  ", style="dim")
    if state.show_all_instances:
        text.append("[I]", style="yellow")
    else:
        text.append("[i]", style="dim")
    text.append(" ", style="dim")
    text.append("All Tasks" if state.show_all_tasks else "Active", style="cyan")

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


def get_help_lines() -> list[tuple[str, str]]:
    """Get help content as a list of (text, style) tuples for each line.

    Returns:
        List of (text, style) tuples representing each line of help content.
    """
    lines = []

    # Header
    lines.append(("Keyboard Commands", "bold cyan"))
    lines.append(("", ""))

    # Navigation & Views
    lines.append(("Navigation & Views", "bold yellow"))
    lines.append(("  h, ?   Show this help", ""))
    lines.append(("  j/k    Scroll down/up (in Help: scroll help)", ""))
    lines.append(("  z      Cycle view (split/tasks only/instances only)", ""))
    lines.append(("  p      Filter by project", ""))
    lines.append(("  c      Cycle category filter", ""))
    lines.append(("  a      Toggle all tasks / pending only", ""))
    lines.append(("  i      Toggle instance visibility (active/all)", ""))
    lines.append(("  t      Show statistics view", ""))
    lines.append(("  H      Show task history (audit trail)", ""))
    lines.append(("  S      Settings (API key, permissions, config)", ""))
    lines.append(("", ""))

    # Task Operations
    lines.append(("Task Operations", "bold yellow"))
    lines.append(("  y      Sync current project from @fix_plan.md", ""))
    lines.append(("  Y      Sync ALL projects from @fix_plan.md", ""))
    lines.append(("  r      Release task claim", ""))
    lines.append(("  e      View error details for failed task", ""))
    lines.append(("", ""))

    # Instance Operations
    lines.append(("Instance Operations", "bold yellow"))
    lines.append(("  n      Spawn new Ralph (6-step workflow)", ""))
    lines.append(("  N      Quickstart spawn with defaults (Shift+N)", ""))
    lines.append(("  s      Shutdown instance", ""))
    lines.append(("  l      View logs for instance", ""))
    lines.append(("  C      Cleanup idle ralphs (Shift+C)", ""))
    lines.append(("  Ret    Instance detail (in Instances view)", ""))
    lines.append(("", ""))

    # Search & Viewing
    lines.append(("Search & Viewing", "bold yellow"))
    lines.append(("  /      Search tasks by title", ""))
    lines.append(("  d      View task details", ""))
    lines.append(("  o      Cycle sort order", ""))
    lines.append(("  w      Export tasks to JSON", ""))
    lines.append(("  v      Live log streaming", ""))
    lines.append(("", ""))

    # Bulk Task Operations
    lines.append(("Bulk Task Operations", "bold yellow"))
    lines.append(("  x      Toggle bulk select mode", ""))
    lines.append(("  SPC    Select/deselect task (in bulk mode)", ""))
    lines.append(("  m      Open bulk action menu", ""))
    lines.append(("", ""))

    # Bulk Instance Operations
    lines.append(("Bulk Instance Operations", "bold yellow"))
    lines.append(("  ^S     Stop ALL Ralphs (emergency)", ""))
    lines.append(("  ^P     Pause ALL Ralphs", ""))
    lines.append(("  ^R     Resume ALL paused Ralphs", ""))
    lines.append(("", ""))

    # Exit
    lines.append(("  q      Quit dashboard", ""))
    lines.append(("  Esc    Cancel / return to normal mode", ""))

    return lines


def create_help_panel(offset: int = 0, visible_lines: int = 30) -> Panel:
    """Create the help overlay panel with scrolling support.

    Args:
        offset: Number of lines to skip from the top (scroll position)
        visible_lines: Maximum number of lines to display

    Returns:
        Panel containing the help text with scroll indicators
    """
    all_lines = get_help_lines()
    total_lines = len(all_lines)

    # Clamp offset
    max_offset = max(0, total_lines - visible_lines)
    offset = max(0, min(offset, max_offset))

    # Get visible portion
    visible = all_lines[offset:offset + visible_lines]

    help_text = Text()

    # Show scroll indicator at top if scrolled
    if offset > 0:
        help_text.append(f"↑ {offset} more lines above\n", style="dim cyan")

    # Render visible lines
    for line_text, style in visible:
        if style:
            help_text.append(line_text + "\n", style=style)
        else:
            # Parse inline styles for command keys
            if line_text.startswith("  ") and len(line_text) > 7:
                # Command line format: "  key  description"
                key_part = line_text[:7]
                desc_part = line_text[7:]
                help_text.append(key_part, style="yellow")
                help_text.append(desc_part + "\n")
            else:
                help_text.append(line_text + "\n")

    # Show scroll indicator at bottom if more content
    remaining = total_lines - offset - visible_lines
    if remaining > 0:
        help_text.append(f"↓ {remaining} more lines below", style="dim cyan")

    # Navigation hint
    help_text.append("\n\n")
    help_text.append("j/k", style="yellow bold")
    help_text.append(" Scroll  ", style="dim")
    help_text.append("q/Esc", style="yellow bold")
    help_text.append(" Close", style="dim")

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

    # Step indicator for spawn workflow (6 steps)
    step_map = {
        TUIMode.SPAWN_PROJECT: (1, "Select Project"),
        TUIMode.SPAWN_PRIORITY: (2, "Select Priority"),
        TUIMode.SPAWN_CATEGORY: (3, "Select Category"),
        TUIMode.SPAWN_MODEL: (4, "Select Model"),
        TUIMode.SPAWN_SESSION: (5, "Session Settings"),
        TUIMode.SPAWN_CONFIRM: (6, "Confirm"),
    }
    step_info = step_map.get(state.mode)

    text.append("Spawn New Ralph\n", style="bold cyan")
    if step_info:
        step, step_name = step_info
        text.append(f"Step {step}/6: {step_name}\n\n", style="bold dim")
    else:
        text.append("\n", style="dim")

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

    elif state.mode == TUIMode.SPAWN_SESSION:
        text.append(f"Project: ", style="dim")
        text.append(f"{config.project}\n", style="cyan")
        text.append(f"Model: ", style="dim")
        text.append(f"{config.model.value}\n\n", style="cyan")
        text.append("Session Settings:\n", style="bold yellow")
        # Session continuity toggle
        continuity_status = "ON" if config.session_continuity else "OFF"
        continuity_style = "green" if config.session_continuity else "red"
        text.append("  c", style="yellow bold")
        text.append(" = Session Continuity: ", style="white")
        text.append(f"{continuity_status}\n", style=continuity_style)
        text.append("      (ON = resume previous session, OFF = fresh start)\n\n", style="dim")
        # Session expiry
        text.append("  e", style="yellow bold")
        text.append(" = Session Expiry: ", style="white")
        text.append(f"{config.session_expiry_hours} hours\n", style="cyan")
        text.append("      (How long before session expires)\n\n", style="dim")
        text.append("\n  Enter", style="green bold")
        text.append(" = Continue  ", style="white")
        text.append("Esc", style="yellow bold")
        text.append(" = Back\n", style="dim")

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
        # Session settings summary
        continuity_str = "Continue" if config.session_continuity else "Fresh"
        text.append(f"  Session: ", style="dim")
        text.append(f"{continuity_str}, expires in {config.session_expiry_hours}h\n", style="cyan")
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
    """Create settings/configuration panel with expanded sections."""
    text = Text()

    # Handle edit modes
    if state.mode == TUIMode.SETTINGS_EDIT_API_KEY:
        text.append("Edit API Key\n\n", style="bold cyan")
        text.append("Enter your Anthropic API key:\n\n", style="dim")
        text.append("  ", style="dim")
        if state.input_buffer:
            display = state.input_buffer[:4] + "*" * (len(state.input_buffer) - 4) if len(state.input_buffer) > 4 else state.input_buffer
            text.append(display, style="green")
        text.append("_", style="bold white")
        text.append("\n\n")
        text.append("Enter", style="yellow bold")
        text.append(" = save  ", style="dim")
        text.append("Esc", style="yellow bold")
        text.append(" = cancel", style="dim")
        return Panel(text, title="Settings - Edit API Key", border_style="cyan", padding=(1, 2))

    elif state.mode == TUIMode.SETTINGS_EDIT_MAX_RALPHS:
        text.append("Edit Max Concurrent Ralphs\n\n", style="bold cyan")
        text.append("Enter maximum number (1-20):\n\n", style="dim")
        text.append("  ", style="dim")
        text.append(state.input_buffer or "", style="green")
        text.append("_", style="bold white")
        text.append("\n\n")
        text.append("Enter", style="yellow bold")
        text.append(" = save  ", style="dim")
        text.append("Esc", style="yellow bold")
        text.append(" = cancel", style="dim")
        return Panel(text, title="Settings - Edit Max Ralphs", border_style="cyan", padding=(1, 2))

    elif state.mode == TUIMode.SETTINGS_EDIT_MODEL:
        text.append("Select Default Model\n\n", style="bold cyan")
        current = get_default_model()
        models = ["sonnet", "opus", "haiku"]
        for i, model in enumerate(models, 1):
            marker = "[X]" if model == current else "[ ]"
            text.append(f"  {i}. {marker} {model.capitalize()}\n", style="green" if model == current else "white")
        text.append("\n")
        text.append("1/2/3", style="yellow bold")
        text.append(" = select  ", style="dim")
        text.append("Esc", style="yellow bold")
        text.append(" = cancel", style="dim")
        return Panel(text, title="Settings - Default Model", border_style="cyan", padding=(1, 2))

    elif state.mode == TUIMode.SETTINGS_EDIT_TIMEOUT:
        text.append("Edit Default Timeout (minutes)\n\n", style="bold cyan")
        text.append("Enter timeout in minutes (5-120):\n\n", style="dim")
        text.append("  ", style="dim")
        text.append(state.input_buffer or "", style="green")
        text.append("_", style="bold white")
        text.append("\n\n")
        text.append("Enter", style="yellow bold")
        text.append(" = save  ", style="dim")
        text.append("Esc", style="yellow bold")
        text.append(" = cancel", style="dim")
        return Panel(text, title="Settings - Default Timeout", border_style="cyan", padding=(1, 2))

    elif state.mode == TUIMode.SETTINGS_EDIT_PERMISSIONS:
        text.append("Ralph Permissions\n\n", style="bold cyan")
        text.append("Toggle permissions with Space, j/k to navigate:\n\n", style="dim")
        permissions = get_ralph_permissions()
        perm_labels = {
            "run_tests": "Run Tests",
            "install_dependencies": "Install Dependencies",
            "build_project": "Build Project",
            "run_type_checker": "Run Type Checker",
            "run_linter": "Run Linter",
            "run_formatter": "Run Formatter",
        }
        for i, key in enumerate(state.permission_keys):
            enabled = permissions.get(key, True)
            marker = "[X]" if enabled else "[ ]"
            cursor = ">" if i == state.permission_cursor else " "
            style = "cyan bold" if i == state.permission_cursor else ("green" if enabled else "red")
            text.append(f"  {cursor} {marker} {perm_labels.get(key, key)}\n", style=style)
        text.append("\n")
        text.append("Space", style="yellow bold")
        text.append("=toggle  ", style="dim")
        text.append("j/k", style="yellow bold")
        text.append("=nav  ", style="dim")
        text.append("Esc", style="yellow bold")
        text.append("=done", style="dim")
        return Panel(text, title="Settings - Permissions", border_style="cyan", padding=(1, 2))

    elif state.mode == TUIMode.SETTINGS_EDIT_STRATEGY:
        text.append("Task Assignment Strategy\n\n", style="bold cyan")
        current = get_task_assignment_strategy()
        strategies = [
            ("priority", "Priority - Highest priority unclaimed task"),
            ("round_robin", "Round Robin - Distribute evenly across ralphs"),
            ("specialized", "Specialized - Match ralph to task category"),
        ]
        for i, (key, desc) in enumerate(strategies, 1):
            marker = "[X]" if key == current else "[ ]"
            text.append(f"  {i}. {marker} {desc}\n", style="green" if key == current else "white")
        text.append("\n")
        text.append("1/2/3", style="yellow bold")
        text.append(" = select  ", style="dim")
        text.append("Esc", style="yellow bold")
        text.append(" = cancel", style="dim")
        return Panel(text, title="Settings - Assignment Strategy", border_style="cyan", padding=(1, 2))

    elif state.mode == TUIMode.SETTINGS_EDIT_AUTO_SPAWN:
        text.append("Auto-Scaling Settings\n\n", style="bold cyan")
        config = get_auto_scaling_config()
        text.append("Toggle settings with number keys:\n\n", style="dim")
        text.append(f"  1. Auto-Spawn Enabled: ", style="dim")
        text.append(f"{'Yes' if config['auto_spawn_enabled'] else 'No'}\n", style="green" if config['auto_spawn_enabled'] else "red")
        text.append(f"  2. Spawn Threshold:    ", style="dim")
        text.append(f"{config['auto_spawn_threshold']} pending tasks\n", style="white")
        text.append(f"  3. Auto-Cleanup:       ", style="dim")
        text.append(f"{'Yes' if config['auto_cleanup_enabled'] else 'No'}\n", style="green" if config['auto_cleanup_enabled'] else "red")
        text.append(f"  4. Idle Timeout:       ", style="dim")
        text.append(f"{config['auto_cleanup_idle_minutes']} minutes\n", style="white")
        text.append("\n")
        text.append("1-4", style="yellow bold")
        text.append(" = toggle/edit  ", style="dim")
        text.append("Esc", style="yellow bold")
        text.append(" = done", style="dim")
        return Panel(text, title="Settings - Auto-Scaling", border_style="cyan", padding=(1, 2))

    elif state.mode == TUIMode.SETTINGS_EDIT_RALPH_LOOP:
        text.append("Ralph Loop Settings\n\n", style="bold cyan")
        loop_settings = get_ralph_loop_settings()
        text.append("Configure settings passed to ralph_loop.sh:\n\n", style="dim")
        # Session continuity
        continuity = loop_settings.get("session_continuity", True)
        text.append(f"  1. Session Continuity: ", style="dim")
        text.append(f"{'Yes (continue)' if continuity else 'No (fresh start)'}\n",
                   style="green" if continuity else "red")
        # Session expiry
        text.append(f"  2. Session Expiry:     ", style="dim")
        text.append(f"{loop_settings.get('session_expiry_hours', 24)} hours\n", style="white")
        # Output format
        output_fmt = loop_settings.get("output_format", "json")
        text.append(f"  3. Output Format:      ", style="dim")
        text.append(f"{output_fmt}\n", style="white")
        # Max calls per hour
        text.append(f"  4. Max Calls/Hour:     ", style="dim")
        text.append(f"{loop_settings.get('max_calls_per_hour', 100)}\n", style="white")
        text.append("\n")
        text.append("1-4", style="yellow bold")
        text.append(" = toggle/edit  ", style="dim")
        text.append("Esc", style="yellow bold")
        text.append(" = done", style="dim")
        return Panel(text, title="Settings - Ralph Loop", border_style="cyan", padding=(1, 2))

    # Main settings view with sections
    text.append("Settings & Configuration\n\n", style="bold cyan")

    # Settings items with cursor navigation
    settings_items = [
        ("API Key", 0),
        ("Max Concurrent Ralphs", 1),
        ("Default Model", 2),
        ("Default Timeout", 3),
        ("Ralph Permissions", 4),
        ("Task Assignment Strategy", 5),
        ("Auto-Scaling", 6),
    ]

    # Section 1: API Configuration
    text.append("API Configuration\n", style="bold yellow")
    api_key = get_api_key()
    api_source = get_api_key_source()  # ENV, CONFIG, or NONE
    if api_key:
        masked = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
        api_status = f"{masked} [{api_source}]"
        api_style = "green"
    else:
        api_status = "Not set"
        api_style = "red"

    cursor = ">" if state.settings_cursor == 0 else " "
    style = "cyan bold" if state.settings_cursor == 0 else "dim"
    text.append(f"  {cursor} API Key: ", style=style)
    text.append(f"{api_status}\n", style=api_style)

    cursor = ">" if state.settings_cursor == 1 else " "
    style = "cyan bold" if state.settings_cursor == 1 else "dim"
    text.append(f"  {cursor} Max Concurrent: ", style=style)
    text.append(f"{get_max_ralphs()} Ralphs\n", style="white")

    cursor = ">" if state.settings_cursor == 2 else " "
    style = "cyan bold" if state.settings_cursor == 2 else "dim"
    text.append(f"  {cursor} Default Model: ", style=style)
    text.append(f"{get_default_model().capitalize()}\n", style="white")

    cursor = ">" if state.settings_cursor == 3 else " "
    style = "cyan bold" if state.settings_cursor == 3 else "dim"
    text.append(f"  {cursor} Default Timeout: ", style=style)
    text.append(f"{get_default_timeout()} minutes\n", style="white")

    # Section 2: Ralph Permissions
    text.append("\nRalph Permissions\n", style="bold yellow")
    permissions = get_ralph_permissions()
    perm_summary = sum(1 for v in permissions.values() if v)
    cursor = ">" if state.settings_cursor == 4 else " "
    style = "cyan bold" if state.settings_cursor == 4 else "dim"
    text.append(f"  {cursor} Permissions: ", style=style)
    text.append(f"{perm_summary}/6 enabled\n", style="green" if perm_summary == 6 else "yellow")

    # Section 3: Instance Specialization
    text.append("\nInstance Specialization\n", style="bold yellow")
    cursor = ">" if state.settings_cursor == 5 else " "
    style = "cyan bold" if state.settings_cursor == 5 else "dim"
    strategy = get_task_assignment_strategy()
    text.append(f"  {cursor} Assignment Strategy: ", style=style)
    text.append(f"{strategy.capitalize()}\n", style="white")

    # Section 4: Auto-Scaling
    text.append("\nAuto-Scaling\n", style="bold yellow")
    auto_config = get_auto_scaling_config()
    cursor = ">" if state.settings_cursor == 6 else " "
    style = "cyan bold" if state.settings_cursor == 6 else "dim"
    auto_status = "Enabled" if auto_config["auto_spawn_enabled"] else "Disabled"
    text.append(f"  {cursor} Auto-Spawn: ", style=style)
    text.append(f"{auto_status}\n", style="green" if auto_config["auto_spawn_enabled"] else "dim")

    # Section 5: Ralph Loop Settings
    text.append("\nRalph Loop Settings\n", style="bold yellow")
    loop_settings = get_ralph_loop_settings()
    cursor = ">" if state.settings_cursor == 7 else " "
    style = "cyan bold" if state.settings_cursor == 7 else "dim"
    continuity_str = "Continue" if loop_settings.get("session_continuity", True) else "Fresh"
    text.append(f"  {cursor} Session: ", style=style)
    text.append(f"{continuity_str}, {loop_settings.get('session_expiry_hours', 24)}h expiry\n", style="white")

    # Section 6: Current View State (read-only)
    text.append("\nView State (auto-saved)\n", style="bold yellow")
    text.append(f"    Tasks:     ", style="dim")
    text.append(f"{'All' if state.show_all_tasks else 'Active only'}\n", style="white")
    text.append(f"    Instances: ", style="dim")
    text.append(f"{'All' if state.show_all_instances else 'Active only'}\n", style="white")
    text.append(f"    Focus:     ", style="dim")
    text.append(f"{state.view_focus.name}\n", style="white")

    text.append("\n")
    text.append("j/k", style="yellow bold")
    text.append("=select  ", style="dim")
    text.append("Enter", style="yellow bold")
    text.append("=edit  ", style="dim")
    text.append("Esc", style="yellow bold")
    text.append("=close", style="dim")

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


def create_instance_tab_bar(active_tab: int) -> Text:
    """Create tab bar for instance detail view."""
    text = Text()
    tabs = ["1 Dashboard", "2 History", "3 Errors", "4 Logs"]
    for idx, tab in enumerate(tabs):
        if idx == active_tab:
            text.append(f" [{tab}] ", style="bold white on blue")
        else:
            text.append(f" {tab} ", style="dim")
        if idx < len(tabs) - 1:
            text.append(" ", style="dim")
    return text


def create_instance_dashboard_content(instance, state: TUIState, current_task, process_health: dict | None = None, status_staleness: dict | None = None, skip_health_checks: bool = False) -> Text:
    """Create dashboard content for instance detail view."""
    from chiefwiggum.spawner import get_process_health, get_status_staleness

    text = Text()

    # Skip expensive health checks for dead instances
    if skip_health_checks:
        process_health = {"healthy": False, "state": "dead", "pid": None, "elapsed": None}
        status_staleness = {"stale": True, "exists": False, "message": "Instance is crashed/stopped"}
    else:
        # Get health/staleness if not provided
        if process_health is None:
            process_health = get_process_health(instance.ralph_id)
        if status_staleness is None:
            status_staleness = get_status_staleness(instance.ralph_id)

    # Status with color and icons
    status_styles = {
        RalphInstanceStatus.ACTIVE: (f"{ICON_ACTIVE} ACTIVE", "green bold"),
        RalphInstanceStatus.IDLE: (f"{ICON_IDLE} IDLE", "yellow"),
        RalphInstanceStatus.PAUSED: (f"{ICON_PAUSED} PAUSED", "blue"),
        RalphInstanceStatus.STOPPED: (f"{ICON_STOPPED} STOPPED", "dim"),
        RalphInstanceStatus.CRASHED: (f"{ICON_CRASHED} CRASHED", "red bold"),
    }
    status_str, status_style = status_styles.get(instance.status, (instance.status.value, "white"))
    text.append("Status: ", style="dim")
    text.append(f"{status_str}", style=status_style)

    # Add process health indicator
    if process_health["state"] == "zombie":
        text.append(f"  {ICON_STALE} ZOMBIE", style="red bold blink")
    elif process_health["state"] == "dead" and instance.status == RalphInstanceStatus.ACTIVE:
        text.append(f"  {ICON_STALE} DEAD", style="red bold")
    elif process_health["state"] == "stopped":
        text.append("  (suspended)", style="yellow")
    elif process_health["healthy"]:
        text.append(f"  {ICON_DONE}", style="green")
    text.append("\n")

    # Process Health section (skip for dead instances)
    if not skip_health_checks:
        text.append("\nProcess Health\n", style="bold yellow")
        text.append("  Process: ", style="dim")
        if process_health["healthy"]:
            text.append(f"{process_health['state']}", style="green")
        elif process_health["state"] == "zombie":
            text.append("ZOMBIE (defunct)", style="red bold")
        elif process_health["state"] == "dead":
            text.append("NOT RUNNING", style="red")
        else:
            text.append(f"{process_health['state']}", style="yellow")

        if process_health["pid"]:
            text.append(f" (PID: {process_health['pid']})", style="dim")
        if process_health["elapsed"]:
            text.append(f" [{process_health['elapsed']}]", style="dim")
        text.append("\n")

        # Status file staleness
        text.append("  Status File: ", style="dim")
        if status_staleness["stale"]:
            text.append(f"{status_staleness['message']}", style="yellow")
        elif status_staleness["exists"]:
            text.append(f"{status_staleness['message']}", style="green")
        else:
            text.append("No status file", style="red")
        text.append("\n")

    # Check if stuck and show diagnosis (skip for dead instances)
    if skip_health_checks:
        is_stuck = False
        stuck_reason = ""
        activity = {"log_age_seconds": None, "is_responsive": False}
    else:
        from chiefwiggum.spawner import is_ralph_stuck, get_ralph_activity
        timeout_mins = instance.config.timeout_minutes if instance.config else 30
        is_stuck, stuck_reason = is_ralph_stuck(instance.ralph_id, timeout_mins)
        activity = get_ralph_activity(instance.ralph_id)

    # Log activity indicator (only show for alive instances or if data exists)
    if not skip_health_checks:
        text.append("  Log Activity: ", style="dim")
        if activity["log_age_seconds"] is not None:
            age = activity["log_age_seconds"]
            if age < 60:
                text.append(f"Updated {age:.0f}s ago", style="green")
            elif age < 300:
                text.append(f"Updated {age/60:.1f}m ago", style="cyan")
            else:
                text.append(f"⚠ No updates for {age/60:.1f}m", style="yellow bold")
        else:
            text.append("No log file", style="dim")
        text.append("\n")

        # Diagnosis
        text.append("  Diagnosis: ", style="dim")
        if is_stuck:
            text.append(f"⚠ STUCK - {stuck_reason}", style="red bold")
            text.append(" (press K to kill)", style="dim")
        elif not activity["is_responsive"]:
            text.append("⚠ Unresponsive", style="yellow")
        elif not process_health["healthy"]:
            text.append(f"⚠ Process issue: {process_health['state']}", style="yellow")
        else:
            text.append("OK", style="green")
        text.append("\n")

    # Current Task + Time on Task with Progress Bar
    text.append("\nCurrent Task: ", style="dim")
    if instance.current_task_id and current_task:
        text.append(f"{current_task.task_title[:40]}\n", style="white")
        # Calculate time on task
        if current_task.started_at:
            elapsed = (datetime.now() - current_task.started_at).total_seconds()
            if elapsed < 60:
                elapsed_str = f"{int(elapsed)}s"
            elif elapsed < 3600:
                elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
            else:
                elapsed_str = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m"
            # Warning if time > timeout/2 (default 15min)
            timeout_minutes = instance.config.timeout_minutes if instance.config else 30
            timeout_seconds = timeout_minutes * 60
            warning_threshold = timeout_seconds / 2

            # Progress bar
            progress = min(elapsed / timeout_seconds, 1.0)
            bar_width = 10
            filled = int(progress * bar_width)
            bar_style = "green" if progress < 0.5 else ("yellow" if progress < 0.75 else "red")
            text.append("Progress: ", style="dim")
            text.append("\u2588" * filled, style=bar_style)  # █ filled
            text.append("\u2591" * (bar_width - filled), style="dim")  # ░ empty
            text.append(f" {int(progress * 100)}%\n", style=bar_style)

            text.append("Time on Task: ", style="dim")
            if elapsed > warning_threshold:
                text.append(f"{ICON_STALE} {elapsed_str}", style="yellow bold")
            else:
                text.append(f"{elapsed_str}", style="cyan")
            text.append("\n")
    else:
        text.append("Idle\n", style="dim")

    # Loop Count
    text.append("Loop Count: ", style="dim")
    text.append(f"#{instance.loop_count}\n", style="cyan")

    # Activity message from status file
    if state.instance_status_message:
        text.append("Activity: ", style="dim")
        text.append(f"{state.instance_status_message[:50]}\n", style="white")

    text.append("\n")

    # Health Indicators
    text.append("Health\n", style="bold yellow")
    total_tasks = instance.tasks_completed + instance.tasks_failed
    if total_tasks > 0:
        success_rate = (instance.tasks_completed / total_tasks) * 100
        text.append("  Success Rate: ", style="dim")
        rate_style = "green" if success_rate >= 80 else ("yellow" if success_rate >= 50 else "red")
        text.append(f"{success_rate:.0f}% ({instance.tasks_completed}/{total_tasks})\n", style=rate_style)
    else:
        text.append("  Success Rate: ", style="dim")
        text.append("N/A\n", style="dim")

    # Tasks/hour
    if instance.started_at:
        hours_running = max(0.1, (datetime.now() - instance.started_at).total_seconds() / 3600)
        tasks_per_hour = instance.tasks_completed / hours_running
        text.append("  Tasks/Hour: ", style="dim")
        text.append(f"{tasks_per_hour:.1f}\n", style="cyan")

    # Failure streak warning
    if state.instance_failure_streak >= 3:
        text.append("  Streak: ", style="dim")
        text.append(f"⚠ {state.instance_failure_streak} consecutive failures\n", style="red bold")
    elif state.instance_failure_streak > 0:
        text.append("  Streak: ", style="dim")
        text.append(f"{state.instance_failure_streak} failure(s)\n", style="yellow")

    text.append("\n")

    # Stats
    text.append("Statistics\n", style="bold yellow")
    text.append("  Tasks Completed: ", style="dim")
    text.append(f"{instance.tasks_completed}\n", style="green")
    text.append("  Tasks Failed: ", style="dim")
    text.append(f"{instance.tasks_failed}\n", style="red" if instance.tasks_failed > 0 else "dim")
    if instance.total_work_seconds > 0:
        hours = int(instance.total_work_seconds // 3600)
        minutes = int((instance.total_work_seconds % 3600) // 60)
        text.append("  Total Work Time: ", style="dim")
        text.append(f"{hours}h {minutes}m\n", style="white")

    text.append("\n")

    # Config summary
    text.append("Configuration\n", style="bold yellow")
    if instance.config:
        text.append("  Model: ", style="dim")
        text.append(f"{instance.config.model.value}\n", style="cyan")
        text.append("  Timeout: ", style="dim")
        text.append(f"{instance.config.timeout_minutes}m\n", style="white")
    if instance.targeting:
        if instance.targeting.project:
            text.append("  Project: ", style="dim")
            text.append(f"{instance.targeting.project}\n", style="magenta")
        if instance.targeting.priority_min:
            text.append("  Priority: ", style="dim")
            text.append(f">= {instance.targeting.priority_min.value}\n", style="white")
        if instance.targeting.categories:
            text.append("  Categories: ", style="dim")
            text.append(f"{', '.join(c.value for c in instance.targeting.categories)}\n", style="white")

    return text


def create_instance_history_content(state: TUIState) -> Text:
    """Create history tab content for instance detail view."""
    text = Text()

    # Filter toggle
    text.append(f"Filter: [{state.instance_history_filter}] ", style="dim")
    text.append("(f to toggle)\n\n", style="dim")

    if not state.instance_task_history:
        text.append("No task history for this instance", style="dim")
        return text

    # Filter history based on current filter
    history = state.instance_task_history
    if state.instance_history_filter == "completed":
        history = [t for t in history if t.status == TaskClaimStatus.COMPLETED]
    elif state.instance_history_filter == "failed":
        history = [t for t in history if t.status == TaskClaimStatus.FAILED]

    # Scrollable table
    visible_start = state.instance_history_scroll
    visible_end = visible_start + 15
    visible_history = history[visible_start:visible_end]

    # Header
    text.append(f"{'Task':<35} {'Status':<10} {'Duration':<10} {'Commit':<8}\n", style="bold")
    text.append("-" * 70 + "\n", style="dim")

    for task in visible_history:
        # Task title (truncated)
        title = task.task_title[:33] + ".." if len(task.task_title) > 35 else task.task_title
        text.append(f"{title:<35} ", style="white")

        # Status
        status_style = "green" if task.status == TaskClaimStatus.COMPLETED else "red"
        text.append(f"{task.status.value:<10} ", style=status_style)

        # Duration
        if task.duration_seconds < 60:
            dur_str = f"{task.duration_seconds:.0f}s"
        elif task.duration_seconds < 3600:
            dur_str = f"{task.duration_seconds/60:.1f}m"
        else:
            dur_str = f"{task.duration_seconds/3600:.1f}h"
        text.append(f"{dur_str:<10} ", style="dim")

        # Commit SHA
        if task.commit_sha:
            text.append(f"{task.commit_sha[:7]}\n", style="yellow")
        else:
            text.append("-\n", style="dim")

    # Scroll hint
    if len(history) > 15:
        text.append(f"\n... {len(history) - visible_end} more (j/k to scroll)\n", style="dim")

    return text


def create_instance_errors_content(state: TUIState) -> Text:
    """Create errors tab content for instance detail view."""
    text = Text()

    if not state.instance_failed_tasks:
        text.append("No failed tasks for this instance", style="dim")
        return text

    text.append("Failed tasks (Enter for full error)\n\n", style="dim")

    # Scrollable list with selection
    visible_start = state.instance_error_scroll
    visible_end = visible_start + 12
    visible_errors = state.instance_failed_tasks[visible_start:visible_end]

    for idx, task in enumerate(visible_errors):
        global_idx = visible_start + idx
        is_selected = global_idx == state.instance_selected_error_idx

        # Selection indicator
        prefix = ">" if is_selected else " "
        style = "reverse" if is_selected else "white"

        # Task title
        title = task.task_title[:40] + ".." if len(task.task_title) > 42 else task.task_title
        text.append(f"{prefix} {title}\n", style=style)

        # Error category and retry count
        if task.error_category:
            cat_styles = {
                ErrorCategory.TRANSIENT: "yellow",
                ErrorCategory.CODE_ERROR: "red",
                ErrorCategory.PERMISSION: "magenta",
                ErrorCategory.CONFLICT: "blue",
                ErrorCategory.TIMEOUT: "yellow",
                ErrorCategory.UNKNOWN: "dim",
            }
            cat_style = cat_styles.get(task.error_category, "white")
            text.append(f"    [{task.error_category.value}]", style=cat_style)
            text.append(f" retry {task.retry_count}/{task.max_retries}\n", style="dim")

    # Scroll hint
    if len(state.instance_failed_tasks) > 12:
        remaining = len(state.instance_failed_tasks) - visible_end
        if remaining > 0:
            text.append(f"\n... {remaining} more (j/k to scroll)\n", style="dim")

    return text


def create_instance_error_detail_overlay(state: TUIState) -> Panel:
    """Create full error message overlay for selected error."""
    text = Text()

    if not state.instance_failed_tasks or state.instance_selected_error_idx >= len(state.instance_failed_tasks):
        text.append("No error selected", style="dim")
        return Panel(text, title="Error Detail", border_style="red")

    task = state.instance_failed_tasks[state.instance_selected_error_idx]

    text.append(f"Task: {task.task_title}\n\n", style="bold white")

    text.append("Task ID: ", style="dim")
    text.append(f"{task.task_id}\n", style="cyan")

    if task.error_category:
        text.append("Error Category: ", style="dim")
        text.append(f"{task.error_category.value}\n", style="red")

    text.append("Retry Count: ", style="dim")
    text.append(f"{task.retry_count}/{task.max_retries}\n", style="yellow")

    if task.error_message:
        text.append("\nFull Error Message:\n", style="bold red")
        text.append("-" * 60 + "\n", style="dim")
        text.append(task.error_message, style="white")

    text.append("\n\nPress Esc to close", style="dim")

    return Panel(text, title="Error Detail", border_style="red", padding=(1, 2))


def create_instance_logs_content(state: TUIState) -> Text:
    """Create logs tab content for instance detail view."""
    text = Text()

    if not state.instance_log_content:
        text.append("No log content available", style="dim")
        return text

    text.append("[Auto-refreshing every 2s]\n\n", style="dim")

    lines = state.instance_log_content.split("\n")
    for line in lines[-35:]:  # Show last 35 lines
        line_lower = line.lower()
        if "error" in line_lower or "failed" in line_lower or "exception" in line_lower:
            text.append(line + "\n", style="red")
        elif "warning" in line_lower or "warn" in line_lower:
            text.append(line + "\n", style="yellow")
        elif "success" in line_lower or "completed" in line_lower or "done" in line_lower:
            text.append(line + "\n", style="green")
        else:
            text.append(line + "\n", style="white")

    return text


def create_instance_detail_panel(instance, state: TUIState, current_task=None, skip_health_checks: bool = False) -> Panel:
    """Create main instance detail panel with tabs."""
    from rich.console import Group

    # Tab bar
    tab_bar = create_instance_tab_bar(state.instance_detail_tab)

    # Tab content
    if state.instance_detail_tab == InstanceDetailTab.DASHBOARD.value:
        content = create_instance_dashboard_content(instance, state, current_task, skip_health_checks=skip_health_checks)
    elif state.instance_detail_tab == InstanceDetailTab.HISTORY.value:
        content = create_instance_history_content(state)
    elif state.instance_detail_tab == InstanceDetailTab.ERRORS.value:
        content = create_instance_errors_content(state)
    elif state.instance_detail_tab == InstanceDetailTab.LOGS.value:
        content = create_instance_logs_content(state)
    else:
        content = Text("Unknown tab", style="red")

    # Show instance ID in title
    id_display = instance.ralph_id
    if instance.hostname and instance.ralph_id.startswith(instance.hostname + "-"):
        id_display = instance.ralph_id[len(instance.hostname) + 1:]

    # Combine tab bar and content
    full_content = Text()
    full_content.append_text(tab_bar)
    full_content.append("\n\n")
    full_content.append_text(content)

    return Panel(full_content, title=f"Instance: {id_display[:20]}", border_style="green", padding=(1, 2))


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


async def create_cleanup_panel() -> Panel:
    """Create cleanup confirmation panel showing what will be cleaned up."""
    from chiefwiggum.coordination import get_idle_ralphs
    from chiefwiggum.config import get_auto_scaling_config

    config = get_auto_scaling_config()
    idle_timeout = config.get("auto_cleanup_idle_minutes", 30)

    text = Text()
    text.append("Cleanup Idle Ralphs\n\n", style="bold cyan")

    # Get idle ralphs
    idle_ralphs = await get_idle_ralphs(idle_timeout)

    if idle_ralphs:
        text.append("The following idle Ralphs will be stopped:\n\n", style="white")
        for ralph in idle_ralphs[:10]:  # Show max 10
            text.append(f"  • {ralph.ralph_id[:20]}", style="yellow")
            text.append(f" ({ralph.project or 'no project'})\n", style="dim")
        if len(idle_ralphs) > 10:
            text.append(f"  ... and {len(idle_ralphs) - 10} more\n", style="dim")
        text.append(f"\nTotal: ", style="white")
        text.append(f"{len(idle_ralphs)}", style="yellow bold")
        text.append(" idle Ralph(s)\n", style="white")
    else:
        text.append("No idle Ralphs to clean up.\n", style="green")
        text.append(f"(Idle threshold: {idle_timeout} minutes)\n\n", style="dim")

    text.append("\n")
    text.append("  y", style="green bold")
    text.append(" = Confirm cleanup\n", style="white")
    text.append("  n, Esc", style="yellow bold")
    text.append(" = Cancel\n", style="white")

    return Panel(text, title="Cleanup Confirmation (C)", border_style="cyan", padding=(1, 2))


def create_command_bar(state: TUIState, console_width: int = 80) -> Panel:
    """Create the command bar based on current mode.

    Args:
        state: Current TUI state
        console_width: Width of the console for responsive layout
    """
    text = Text()

    if state.mode == TUIMode.NORMAL:
        # Priority-based command display for narrow terminals
        # Color hierarchy: primary=yellow, secondary=cyan, shift=magenta, ctrl=red
        # Essential commands always shown (~30 chars): h Help, n New, q Quit
        # Secondary if space (~30 more): y Sync, p Project, j/k Scroll
        # Tertiary if more space (~40 more): z Zoom, c Category, S Settings

        # Calculate available width (subtract panel borders ~4 chars)
        available = console_width - 4

        # Tier 1: Compact (<80) - Essential commands always shown
        text.append("  h", style="bold yellow")
        text.append(" Help  ", style="dim")
        text.append("n", style="cyan")
        text.append(" New  ", style="dim")
        text.append("q", style="bold yellow")
        text.append(" Quit", style="dim")
        used = 28

        # Tier 2: Medium (80-120) - Add navigation
        if available >= 80:
            text.append("  y", style="bold yellow")
            text.append(" Sync  ", style="dim")
            text.append("j/k", style="bold yellow")
            text.append(" \u2195", style="dim")  # ↕
            used += 18

        # Tier 3: Wide (>120) - Add view controls and settings
        if available >= 120:
            text.append("  z", style="cyan")
            if state.view_focus == ViewFocus.BOTH:
                text.append(" [B]", style="dim")
            elif state.view_focus == ViewFocus.TASKS:
                text.append(" [T]", style="cyan")
            else:
                text.append(" [R]", style="green")
            text.append("  S", style="bold magenta")
            text.append(" Set", style="dim")
            used += 18

        # Show 'x Del' when stopped/crashed instance is selected in INSTANCES view
        if state.view_focus == ViewFocus.INSTANCES and state.instances:
            if state.selected_instance_idx < len(state.instances):
                inst = state.instances[state.selected_instance_idx]
                if inst.status in (RalphInstanceStatus.STOPPED, RalphInstanceStatus.CRASHED):
                    text.append("  x", style="red bold")
                    text.append(" Del", style="dim")
                    used += 7

        # Show status message if recent (< 8 seconds) and space permits
        if state.status_message and (time.time() - state.status_message_time) < 8:
            remaining = available - used - 5  # 5 for separator
            if remaining > 10:
                text.append(f"  {SEP_VERTICAL}  ", style="dim")
                # Truncate status message to fit
                msg = state.status_message
                if len(msg) > remaining:
                    msg = msg[:remaining - 3] + "..."
                # Color based on content
                if "error" in msg.lower() or "failed" in msg.lower():
                    text.append(msg, style="red")
                else:
                    text.append(msg, style="green")

    elif state.mode == TUIMode.HELP:
        text.append("  j/k", style="yellow bold")
        text.append(" Scroll  ", style="dim")
        text.append("h/q/Esc", style="yellow bold")
        text.append(" Close", style="dim")

    elif state.mode == TUIMode.PROJECT_FILTER:
        text.append("  Select project: ", style="cyan bold")
        # Calculate how many projects can fit
        available = console_width - 40  # Reserve space for header and controls
        max_name_len = 8 if available < 60 else (12 if available < 100 else 20)
        projects_shown = 0
        for idx, project in enumerate(state.projects[:9], 1):
            proj_display = project[:max_name_len]
            if len(project) > max_name_len:
                proj_display = proj_display[:-1] + "…"
            text.append(f" {idx}", style="yellow bold")
            text.append(f"={proj_display}", style="dim")
            projects_shown += 1
            # Check if we've used too much space
            if projects_shown * (max_name_len + 3) > available:
                remaining = len(state.projects) - projects_shown
                if remaining > 0:
                    text.append(f" +{remaining}", style="dim")
                break
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

    elif state.mode in (TUIMode.SPAWN_PROJECT, TUIMode.SPAWN_PRIORITY, TUIMode.SPAWN_CATEGORY, TUIMode.SPAWN_MODEL, TUIMode.SPAWN_SESSION, TUIMode.SPAWN_CONFIRM):
        text.append("  Spawning Ralph - follow prompts above  ", style="green bold")
        text.append("Esc", style="yellow bold")
        text.append("=cancel", style="dim")

    elif state.mode == TUIMode.HISTORY:
        text.append("  Viewing task history  ", style="magenta bold")
        text.append("H/q/Esc", style="yellow bold")
        text.append(" Close", style="dim")

    elif state.mode == TUIMode.ERROR_DETAIL:
        text.append("  Viewing error details  ", style="red bold")
        text.append("e/q/Esc", style="yellow bold")
        text.append(" Close", style="dim")

    elif state.mode == TUIMode.STATS:
        text.append("  Viewing statistics  ", style="cyan bold")
        text.append("t/q/Esc", style="yellow bold")
        text.append(" Close", style="dim")

    elif state.mode == TUIMode.LOG_VIEW:
        text.append("  Viewing logs  ", style="blue bold")
        text.append("l/q/Esc", style="yellow bold")
        text.append(" Close", style="dim")

    elif state.mode == TUIMode.LOG_STREAM:
        text.append("  Live log streaming  ", style="blue bold")
        text.append("v/q/Esc", style="yellow bold")
        text.append(" Close", style="dim")

    elif state.mode == TUIMode.TASK_DETAIL:
        text.append("  Viewing task details  ", style="cyan bold")
        text.append("d/q/Esc", style="yellow bold")
        text.append(" Close", style="dim")

    elif state.mode == TUIMode.SETTINGS:
        text.append("  j/k", style="yellow bold")
        text.append("=select  ", style="dim")
        text.append("Enter", style="yellow bold")
        text.append("=edit  ", style="dim")
        text.append("Esc", style="yellow bold")
        text.append("=close", style="dim")

    elif state.mode in (TUIMode.SETTINGS_EDIT_API_KEY, TUIMode.SETTINGS_EDIT_MAX_RALPHS,
                        TUIMode.SETTINGS_EDIT_TIMEOUT):
        text.append("  Type value  ", style="cyan")
        text.append("Enter", style="yellow bold")
        text.append("=save  ", style="dim")
        text.append("Esc", style="yellow bold")
        text.append("=cancel", style="dim")

    elif state.mode in (TUIMode.SETTINGS_EDIT_MODEL, TUIMode.SETTINGS_EDIT_STRATEGY):
        text.append("  1/2/3", style="yellow bold")
        text.append("=select  ", style="dim")
        text.append("Esc", style="yellow bold")
        text.append("=cancel", style="dim")

    elif state.mode == TUIMode.SETTINGS_EDIT_PERMISSIONS:
        text.append("  j/k", style="yellow bold")
        text.append("=nav  ", style="dim")
        text.append("Space", style="yellow bold")
        text.append("=toggle  ", style="dim")
        text.append("Esc", style="yellow bold")
        text.append("=done", style="dim")

    elif state.mode == TUIMode.SETTINGS_EDIT_AUTO_SPAWN:
        text.append("  1-4", style="yellow bold")
        text.append("=toggle/cycle  ", style="dim")
        text.append("Esc", style="yellow bold")
        text.append("=done", style="dim")

    elif state.mode in (TUIMode.CONFIRM_BULK_STOP, TUIMode.CONFIRM_BULK_PAUSE):
        text.append("  y", style="green bold")
        text.append("=confirm  ", style="dim")
        text.append("n/Esc", style="yellow bold")
        text.append("=cancel", style="dim")

    elif state.mode == TUIMode.CLEANUP_CONFIRM:
        text.append("  y", style="green bold")
        text.append("=confirm cleanup  ", style="dim")
        text.append("n/Esc", style="yellow bold")
        text.append("=cancel", style="dim")

    elif state.mode == TUIMode.INSTANCE_DETAIL:
        # Get current instance for conditional display
        instance = None
        if state.instances and state.selected_instance_idx < len(state.instances):
            instance = state.instances[state.selected_instance_idx]
        text.append("  1-4", style="yellow bold")
        text.append(" Tabs  ", style="dim")
        text.append("j/k", style="yellow bold")
        text.append(" Prev/Next  ", style="dim")
        text.append("f", style="yellow bold")
        text.append(" Filter  ", style="dim")
        # Only show 'r' release if instance has a current task
        if instance and instance.current_task_id:
            text.append("r", style="yellow bold")
            text.append(" Release  ", style="dim")
        text.append("P", style="yellow bold")
        text.append(" Pause  ", style="dim")
        text.append("s", style="yellow bold")
        text.append(" Stop  ", style="dim")
        text.append("K", style="bold magenta")
        text.append(" Kill  ", style="dim")
        # Show 'x' delete option for stopped/crashed instances
        if instance and instance.status in (RalphInstanceStatus.STOPPED, RalphInstanceStatus.CRASHED):
            text.append("x", style="red bold")
            text.append(" Del  ", style="dim")
        text.append("Esc", style="yellow bold")
        text.append(" Back", style="dim")

    elif state.mode == TUIMode.INSTANCE_ERROR_DETAIL:
        text.append("  Viewing full error message  ", style="red bold")
        text.append("Esc", style="yellow bold")
        text.append("=close", style="dim")

    return Panel(text, border_style="dim")


async def update_dashboard(layout: Layout, state: TUIState) -> None:
    """Update all dashboard components."""
    # Clean up dead/zombie Ralph processes
    from chiefwiggum.spawner import cleanup_dead_ralphs
    cleaned = cleanup_dead_ralphs()
    if cleaned:
        state.status_message = f"Cleaned up {len(cleaned)} dead Ralph(s): {', '.join(r[:12] for r in cleaned)}"
        state.status_message_time = time.time()

    # Mark stale instances and process retries
    await mark_stale_instances_crashed()
    await process_retry_tasks()

    # Check for task completions from Ralph logs
    completion_events = await check_ralph_completions()
    for event in completion_events:
        # Update status message with completion info
        if event["status"] == "completed":
            state.status_message = f"Task {event['task_id']} completed by {event['ralph_id']}"
        elif event["status"] == "failed":
            state.status_message = f"Task {event['task_id']} failed: {event['message'][:50]}"
        elif event["status"] == "released":
            state.status_message = f"Task {event['task_id']} released (Ralph died)"
        state.status_message_time = time.time()

    # Fetch master data in parallel (3 queries instead of 6, run concurrently)
    all_instances, all_tasks = await asyncio.gather(
        list_all_instances(),
        list_all_tasks(),
    )

    # Derive filtered lists from master data (no DB roundtrip)
    active_instances = [i for i in all_instances
                        if i.status in (RalphInstanceStatus.ACTIVE, RalphInstanceStatus.IDLE)]
    pending_tasks = [t for t in all_tasks if t.status == TaskClaimStatus.PENDING]
    in_progress_tasks = [t for t in all_tasks if t.status == TaskClaimStatus.IN_PROGRESS]
    failed_tasks = [t for t in all_tasks if t.status == TaskClaimStatus.FAILED]

    # Store in state for reuse - respect show_all_instances flag
    state.all_instances = all_instances
    state.instances = all_instances if state.show_all_instances else active_instances
    state.in_progress_tasks = in_progress_tasks
    state.failed_tasks = failed_tasks
    state.projects = list(set(t.project for t in all_tasks if t.project))

    # Get display data based on filters (use state.instances which respects flag)
    instances = state.instances

    if state.show_all_tasks:
        tasks = all_tasks
    else:
        # Show both pending and in_progress tasks (active work)
        tasks = in_progress_tasks + pending_tasks  # in_progress first

    # Apply project filter
    if state.project_filter:
        tasks = [t for t in tasks if t.project == state.project_filter]
        instances = [i for i in instances if i.project == state.project_filter]
        state.instances = instances  # Update state to match display for selection

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

    # Update header with branding and spinner
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    running_count = len(get_running_ralphs())

    # Build header text with branding and version
    from chiefwiggum._version import __version__
    header_text = Text()
    header_text.append("  CHIEF", style="bold cyan")
    header_text.append("WIGGUM", style="bold white")
    header_text.append(f" v{__version__}", style="dim cyan")
    header_text.append(f"    {now}  ", style="dim")

    # Daemon count with icon and optional spinner for active instances
    if running_count > 0:
        # Add spinner when instances are active
        spinner_idx = int(time.time() * 4) % len(SPINNER)
        header_text.append(f" {SPINNER[spinner_idx]} ", style="cyan")
        header_text.append(f"{ICON_DAEMON} {running_count}", style="bold green")
    else:
        header_text.append(f"{ICON_DAEMON} 0", style="dim")
    header_text.append(" daemons", style="dim")

    header = Panel(
        header_text,
        style="blue",
        box=box.ROUNDED,
    )
    layout["header"].update(header)

    # Update stats (reuse all_tasks fetched at start)
    layout["stats"].update(create_stats_panel(state.all_instances, all_tasks, state))

    # Check for overlay modes - must unsplit first to clear child layouts
    if state.mode == TUIMode.HELP:
        layout["main"].unsplit()
        # Calculate visible lines (main area ~height - header - stats - command_bar - padding)
        visible_lines = max(10, state.console_width // 4)  # Rough estimate based on width
        layout["main"].update(create_help_panel(state.help_scroll_offset, visible_lines))
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

    elif state.mode in (TUIMode.SPAWN_PROJECT, TUIMode.SPAWN_PRIORITY, TUIMode.SPAWN_CATEGORY, TUIMode.SPAWN_MODEL, TUIMode.SPAWN_SESSION, TUIMode.SPAWN_CONFIRM):
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

    elif state.mode == TUIMode.CLEANUP_CONFIRM:
        layout["main"].unsplit()
        layout["main"].update(await create_cleanup_panel())

    elif state.mode in (TUIMode.SETTINGS, TUIMode.SETTINGS_EDIT_API_KEY, TUIMode.SETTINGS_EDIT_MAX_RALPHS,
                        TUIMode.SETTINGS_EDIT_MODEL, TUIMode.SETTINGS_EDIT_TIMEOUT,
                        TUIMode.SETTINGS_EDIT_PERMISSIONS, TUIMode.SETTINGS_EDIT_STRATEGY,
                        TUIMode.SETTINGS_EDIT_AUTO_SPAWN, TUIMode.SETTINGS_EDIT_RALPH_LOOP):
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

    elif state.mode == TUIMode.INSTANCE_DETAIL:
        layout["main"].unsplit()
        # Load instance detail data only when first entering or explicitly refreshed
        if state.instances and state.selected_instance_idx < len(state.instances):
            instance = state.instances[state.selected_instance_idx]
            ralph_id = instance.ralph_id

            # Only reload data if refresh flag is set (set when entering detail view)
            if getattr(state, 'instance_detail_needs_refresh', True):
                # Load task history for this instance
                state.instance_task_history = await list_task_history(ralph_id=ralph_id, limit=100)

                # Use cached failed_tasks instead of re-querying
                state.instance_failed_tasks = [t for t in failed_tasks if t.claimed_by_ralph_id == ralph_id]

                # Calculate failure streak from history
                state.instance_failure_streak = 0
                for task in state.instance_task_history:
                    if task.status == TaskClaimStatus.FAILED:
                        state.instance_failure_streak += 1
                    else:
                        break

                # Load status message from status file (skip for dead instances)
                if instance.status not in (RalphInstanceStatus.CRASHED, RalphInstanceStatus.STOPPED):
                    from chiefwiggum.spawner import read_ralph_status
                    status_data = read_ralph_status(ralph_id)
                    if status_data and "message" in status_data:
                        state.instance_status_message = status_data["message"]
                    else:
                        state.instance_status_message = ""
                else:
                    state.instance_status_message = ""

                state.instance_detail_needs_refresh = False

            # Load logs only if on logs tab AND refresh flag is set
            if state.instance_detail_tab == InstanceDetailTab.LOGS.value:
                if state.instance_log_needs_refresh:
                    state.instance_log_content = read_ralph_log(ralph_id, 100)
                    state.instance_log_needs_refresh = False

            # Get current task for dashboard (reuse cached all_tasks)
            current_task = None
            if instance.current_task_id:
                current_task = next((t for t in all_tasks if t.task_id == instance.current_task_id), None)

            # Skip expensive health checks for dead instances
            skip_health_checks = instance.status in (RalphInstanceStatus.CRASHED, RalphInstanceStatus.STOPPED)
            layout["main"].update(create_instance_detail_panel(instance, state, current_task, skip_health_checks=skip_health_checks))
        else:
            # No instance selected, go back to normal
            state.mode = TUIMode.NORMAL

    elif state.mode == TUIMode.INSTANCE_ERROR_DETAIL:
        layout["main"].unsplit()
        layout["main"].update(create_instance_error_detail_overlay(state))

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
                        selected_idx=state.selected_task_idx,
                    ),
                    border_style="yellow",
                )
            )
        elif state.view_focus == ViewFocus.INSTANCES:
            # Instances only - full width (must unsplit first)
            layout["main"].unsplit()
            layout["main"].update(
                Panel(create_instances_table(instances, state.show_all_instances, selected_idx=state.selected_instance_idx), border_style="green")
            )
        else:
            # Both - split view (default)
            layout["main"].split_row(
                Layout(name="instances"),
                Layout(name="tasks"),
            )
            layout["main"]["instances"].update(Panel(create_instances_table(instances, state.show_all_instances, selected_idx=state.selected_instance_idx), border_style="green"))
            layout["main"]["tasks"].update(
                Panel(
                    create_tasks_table(
                        tasks,
                        show_numbers=show_task_numbers,
                        offset=state.task_scroll_offset,
                        limit=state.tasks_per_page,
                        bulk_mode=state.bulk_mode_active,
                        selected_ids=state.selected_task_ids,
                        selected_idx=state.selected_task_idx,
                    ),
                    border_style="yellow",
                )
            )

    # Update command bar
    layout["command_bar"].update(create_command_bar(state, state.console_width))


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
        state.status_message = "Showing all tasks" if state.show_all_tasks else "Showing active tasks"
        state.status_message_time = time.time()
        auto_save_view_state(state)
    elif key == "i":  # US2: Toggle instance visibility
        state.show_all_instances = not state.show_all_instances
        state.instance_scroll_offset = 0  # Reset scroll
        state.status_message = "Showing all instances" if state.show_all_instances else "Showing active only"
        state.status_message_time = time.time()
        auto_save_view_state(state)
    elif key == "j":  # Move selection down
        if state.view_focus == ViewFocus.INSTANCES:
            # Move instance selection
            if state.selected_instance_idx < len(state.instances) - 1:
                state.selected_instance_idx += 1
                # Auto-scroll to keep selection visible
                if state.selected_instance_idx >= state.instance_scroll_offset + 10:
                    state.instance_scroll_offset = state.selected_instance_idx - 9
        else:
            # Move task selection (TASKS or BOTH view)
            if state.selected_task_idx < tasks_count - 1:
                state.selected_task_idx += 1
                # Auto-scroll to keep selection visible
                if state.selected_task_idx >= state.task_scroll_offset + state.tasks_per_page:
                    state.task_scroll_offset = state.selected_task_idx - state.tasks_per_page + 1
    elif key == "k":  # Move selection up
        if state.view_focus == ViewFocus.INSTANCES:
            # Move instance selection
            if state.selected_instance_idx > 0:
                state.selected_instance_idx -= 1
                # Auto-scroll to keep selection visible
                if state.selected_instance_idx < state.instance_scroll_offset:
                    state.instance_scroll_offset = state.selected_instance_idx
        else:
            # Move task selection (TASKS or BOTH view)
            if state.selected_task_idx > 0:
                state.selected_task_idx -= 1
                # Auto-scroll to keep selection visible
                if state.selected_task_idx < state.task_scroll_offset:
                    state.task_scroll_offset = state.selected_task_idx
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
        auto_save_view_state(state)
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
        auto_save_view_state(state)
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
    elif key == "N":  # Shift+N: Quickstart spawn with defaults
        # Skip the 5-step workflow, use quickstart defaults
        return False  # Will be handled in handle_command (async)
    elif key == "Y":  # Shift+Y: Sync ALL projects
        return False  # Will be handled in handle_command (async)
    elif key == "n":  # US3: Spawn Ralph
        if state.projects:
            # Initialize with defaults from ralph_loop_settings
            loop_settings = get_ralph_loop_settings()
            state.spawn_config = SpawnConfig(
                session_continuity=loop_settings.get("session_continuity", True),
                session_expiry_hours=loop_settings.get("session_expiry_hours", 24),
            )
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
    elif key == "C":  # Cleanup idle ralphs
        state.mode = TUIMode.CLEANUP_CONFIRM
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
        auto_save_view_state(state)
    elif key == "x":  # Delete stopped/crashed instance (INSTANCES view) or toggle bulk mode (TASKS view)
        if state.view_focus == ViewFocus.INSTANCES:
            # In INSTANCES view: delete stopped/crashed instance (handled in handle_command)
            if state.instances and state.selected_instance_idx < len(state.instances):
                inst = state.instances[state.selected_instance_idx]
                if inst.status in (RalphInstanceStatus.STOPPED, RalphInstanceStatus.CRASHED):
                    return False  # Will be handled in handle_command (async)
                else:
                    state.status_message = f"Can only delete stopped/crashed (current: {inst.status.value})"
                    state.status_message_time = time.time()
        else:
            # In TASKS view: toggle bulk select mode (existing behavior)
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
    elif key in ("\r", "\n"):  # Enter - instance detail (when in INSTANCES view)
        if state.view_focus == ViewFocus.INSTANCES:
            if state.instances and state.selected_instance_idx < len(state.instances):
                # Reset instance detail state
                state.instance_detail_tab = 0
                state.instance_history_scroll = 0
                state.instance_error_scroll = 0
                state.instance_selected_error_idx = 0
                state.instance_history_filter = "all"
                state.instance_log_needs_refresh = True  # Refresh logs when entering detail view
                state.instance_detail_needs_refresh = True  # Refresh data when entering detail view
                state.mode = TUIMode.INSTANCE_DETAIL
            else:
                state.status_message = "No instances to view (press 'i' to show all)"
                state.status_message_time = time.time()
        elif state.view_focus == ViewFocus.TASKS and state.all_tasks_cache:
            # In TASKS view, Enter opens task detail
            if state.selected_task_idx < len(state.all_tasks_cache):
                state.selected_task = state.all_tasks_cache[state.selected_task_idx]
                state.mode = TUIMode.TASK_DETAIL
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
            state.mode = TUIMode.SPAWN_SESSION
        elif key == "2":
            config.model = ClaudeModel.OPUS
            state.mode = TUIMode.SPAWN_SESSION
        elif key == "3":
            config.model = ClaudeModel.HAIKU
            state.mode = TUIMode.SPAWN_SESSION

    elif state.mode == TUIMode.SPAWN_SESSION:
        if key == "c":
            # Toggle session continuity
            config.session_continuity = not config.session_continuity
        elif key == "e":
            # Cycle through expiry options (12h, 24h, 48h, 72h)
            expiry_options = [12, 24, 48, 72]
            try:
                current_idx = expiry_options.index(config.session_expiry_hours)
                next_idx = (current_idx + 1) % len(expiry_options)
            except ValueError:
                next_idx = 1  # Default to 24h if current value not in options
            config.session_expiry_hours = expiry_options[next_idx]
        elif key in ("\r", "\n"):  # Enter - continue to confirm
            state.mode = TUIMode.SPAWN_CONFIRM
        elif key == "ESCAPE":
            state.mode = TUIMode.SPAWN_MODEL

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
            ralph_config = RalphConfig(
                model=config.model,
                no_continue=not config.session_continuity,  # Invert for internal use
                session_expiry_hours=config.session_expiry_hours,
            )
            targeting = TargetingConfig(
                project=config.project,
                priority_min=config.priority_min,
                categories=config.categories,
            )

            # Use task-aware spawning - claims a task and spawns with focused prompt
            success, message, task_id = await spawn_ralph_with_task_claim(
                ralph_id=ralph_id,
                project=config.project,
                fix_plan_path=config.fix_plan_path,
                config=ralph_config,
                targeting=targeting,
            )

            # spawn_ralph_with_task_claim already registers in database

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


async def handle_settings(key: str, state: TUIState) -> None:
    """Handle settings mode input."""
    if state.mode == TUIMode.SETTINGS:
        # j/k navigation, Enter to edit
        if key in ("ESCAPE", "S"):  # Esc or Shift+S to close
            # Save view state on exit if persistence is enabled
            if get_config_value("persist_view_state", True):
                save_view_state({
                    "show_all_tasks": state.show_all_tasks,
                    "show_all_instances": state.show_all_instances,
                    "view_focus": state.view_focus.name,
                    "category_filter": state.category_filter.value if state.category_filter else None,
                    "project_filter": state.project_filter,
                    "sort_order": state.sort_order.value,
                })
            state.mode = TUIMode.NORMAL
        elif key in ("k", "UP"):
            state.settings_cursor = max(0, state.settings_cursor - 1)
        elif key in ("j", "DOWN"):
            state.settings_cursor = min(7, state.settings_cursor + 1)  # 8 items total (0-7)
        elif key in ("\r", "\n"):  # Enter to edit selected item
            if state.settings_cursor == 0:
                state.input_buffer = ""
                state.mode = TUIMode.SETTINGS_EDIT_API_KEY
            elif state.settings_cursor == 1:
                state.input_buffer = str(get_max_ralphs())
                state.mode = TUIMode.SETTINGS_EDIT_MAX_RALPHS
            elif state.settings_cursor == 2:
                state.mode = TUIMode.SETTINGS_EDIT_MODEL
            elif state.settings_cursor == 3:
                state.input_buffer = str(get_default_timeout())
                state.mode = TUIMode.SETTINGS_EDIT_TIMEOUT
            elif state.settings_cursor == 4:
                state.permission_cursor = 0
                state.mode = TUIMode.SETTINGS_EDIT_PERMISSIONS
            elif state.settings_cursor == 5:
                state.mode = TUIMode.SETTINGS_EDIT_STRATEGY
            elif state.settings_cursor == 6:
                state.mode = TUIMode.SETTINGS_EDIT_AUTO_SPAWN
            elif state.settings_cursor == 7:
                state.mode = TUIMode.SETTINGS_EDIT_RALPH_LOOP

    elif state.mode == TUIMode.SETTINGS_EDIT_API_KEY:
        if key == "ESCAPE":
            state.input_buffer = ""
            state.mode = TUIMode.SETTINGS
        elif key in ("\r", "\n"):  # Enter - save
            if state.input_buffer:
                # Validate format before saving
                if not state.input_buffer.startswith("sk-ant-"):
                    state.status_message = "Invalid format (should start with sk-ant-)"
                elif set_api_key(state.input_buffer):
                    state.status_message = f"API key saved [{get_api_key_source()}]"
                else:
                    state.status_message = "Failed to save API key"
                state.status_message_time = time.time()
            state.input_buffer = ""
            state.mode = TUIMode.SETTINGS
        elif key == "\x7f" or key == "BACKSPACE":  # Backspace
            state.input_buffer = state.input_buffer[:-1]
        elif len(key) == 1 and key.isprintable():  # Regular character
            state.input_buffer += key

    elif state.mode == TUIMode.SETTINGS_EDIT_MAX_RALPHS:
        if key == "ESCAPE":
            state.input_buffer = ""
            state.mode = TUIMode.SETTINGS
        elif key in ("\r", "\n"):  # Enter - save
            if state.input_buffer:
                try:
                    value = int(state.input_buffer)
                    if 1 <= value <= 20:
                        if set_max_ralphs(value):
                            state.status_message = f"Max Ralphs set to {value}"
                        else:
                            state.status_message = "Failed to save setting"
                    else:
                        state.status_message = "Value must be 1-20"
                except ValueError:
                    state.status_message = "Invalid number"
                state.status_message_time = time.time()
            state.input_buffer = ""
            state.mode = TUIMode.SETTINGS
        elif key == "\x7f" or key == "BACKSPACE":  # Backspace
            state.input_buffer = state.input_buffer[:-1]
        elif key.isdigit():  # Only allow digits
            state.input_buffer += key

    elif state.mode == TUIMode.SETTINGS_EDIT_MODEL:
        if key == "ESCAPE":
            state.mode = TUIMode.SETTINGS
        elif key == "1":
            set_default_model("sonnet")
            state.status_message = "Default model set to Sonnet"
            state.status_message_time = time.time()
            state.mode = TUIMode.SETTINGS
        elif key == "2":
            set_default_model("opus")
            state.status_message = "Default model set to Opus"
            state.status_message_time = time.time()
            state.mode = TUIMode.SETTINGS
        elif key == "3":
            set_default_model("haiku")
            state.status_message = "Default model set to Haiku"
            state.status_message_time = time.time()
            state.mode = TUIMode.SETTINGS

    elif state.mode == TUIMode.SETTINGS_EDIT_TIMEOUT:
        if key == "ESCAPE":
            state.input_buffer = ""
            state.mode = TUIMode.SETTINGS
        elif key in ("\r", "\n"):  # Enter - save
            if state.input_buffer:
                try:
                    value = int(state.input_buffer)
                    if 5 <= value <= 120:
                        set_config_value("default_timeout_minutes", value)
                        state.status_message = f"Default timeout set to {value} minutes"
                    else:
                        state.status_message = "Value must be 5-120"
                except ValueError:
                    state.status_message = "Invalid number"
                state.status_message_time = time.time()
            state.input_buffer = ""
            state.mode = TUIMode.SETTINGS
        elif key == "\x7f" or key == "BACKSPACE":
            state.input_buffer = state.input_buffer[:-1]
        elif key.isdigit():
            state.input_buffer += key

    elif state.mode == TUIMode.SETTINGS_EDIT_PERMISSIONS:
        if key == "ESCAPE":
            state.mode = TUIMode.SETTINGS
        elif key in ("k", "UP"):
            state.permission_cursor = max(0, state.permission_cursor - 1)
        elif key in ("j", "DOWN"):
            state.permission_cursor = min(len(state.permission_keys) - 1, state.permission_cursor + 1)
        elif key == " ":  # Space to toggle
            perm_key = state.permission_keys[state.permission_cursor]
            current = get_ralph_permissions().get(perm_key, True)
            set_ralph_permission(perm_key, not current)
            state.status_message = f"{perm_key}: {'enabled' if not current else 'disabled'}"
            state.status_message_time = time.time()

    elif state.mode == TUIMode.SETTINGS_EDIT_STRATEGY:
        if key == "ESCAPE":
            state.mode = TUIMode.SETTINGS
        elif key == "1":
            set_task_assignment_strategy("priority")
            state.status_message = "Strategy set to Priority"
            state.status_message_time = time.time()
            state.mode = TUIMode.SETTINGS
        elif key == "2":
            set_task_assignment_strategy("round_robin")
            state.status_message = "Strategy set to Round Robin"
            state.status_message_time = time.time()
            state.mode = TUIMode.SETTINGS
        elif key == "3":
            set_task_assignment_strategy("specialized")
            state.status_message = "Strategy set to Specialized"
            state.status_message_time = time.time()
            state.mode = TUIMode.SETTINGS

    elif state.mode == TUIMode.SETTINGS_EDIT_AUTO_SPAWN:
        config = get_auto_scaling_config()
        if key == "ESCAPE":
            state.mode = TUIMode.SETTINGS
        elif key == "1":  # Toggle auto-spawn
            new_val = not config["auto_spawn_enabled"]
            set_auto_scaling_config({"auto_spawn_enabled": new_val})
            state.status_message = f"Auto-spawn {'enabled' if new_val else 'disabled'}"
            state.status_message_time = time.time()
        elif key == "2":  # Edit threshold - cycle through values
            new_val = 5 if config["auto_spawn_threshold"] >= 10 else 10 if config["auto_spawn_threshold"] >= 5 else 15
            set_auto_scaling_config({"auto_spawn_threshold": new_val})
            state.status_message = f"Spawn threshold set to {new_val}"
            state.status_message_time = time.time()
        elif key == "3":  # Toggle auto-cleanup
            new_val = not config["auto_cleanup_enabled"]
            set_auto_scaling_config({"auto_cleanup_enabled": new_val})
            state.status_message = f"Auto-cleanup {'enabled' if new_val else 'disabled'}"
            state.status_message_time = time.time()
        elif key == "4":  # Edit idle timeout - cycle through 15, 30, 60, 120
            current = config["auto_cleanup_idle_minutes"]
            new_val = 30 if current == 15 else 60 if current == 30 else 120 if current == 60 else 15
            set_auto_scaling_config({"auto_cleanup_idle_minutes": new_val})
            state.status_message = f"Idle timeout set to {new_val} minutes"
            state.status_message_time = time.time()

    elif state.mode == TUIMode.SETTINGS_EDIT_RALPH_LOOP:
        loop_settings = get_ralph_loop_settings()
        if key == "ESCAPE":
            state.mode = TUIMode.SETTINGS
        elif key == "1":  # Toggle session continuity
            new_val = not loop_settings.get("session_continuity", True)
            set_ralph_loop_setting("session_continuity", new_val)
            state.status_message = f"Session continuity {'enabled' if new_val else 'disabled'}"
            state.status_message_time = time.time()
        elif key == "2":  # Cycle session expiry: 12, 24, 48, 72
            current = loop_settings.get("session_expiry_hours", 24)
            expiry_options = [12, 24, 48, 72]
            try:
                current_idx = expiry_options.index(current)
                next_idx = (current_idx + 1) % len(expiry_options)
            except ValueError:
                next_idx = 1  # Default to 24h
            new_val = expiry_options[next_idx]
            set_ralph_loop_setting("session_expiry_hours", new_val)
            state.status_message = f"Session expiry set to {new_val} hours"
            state.status_message_time = time.time()
        elif key == "3":  # Toggle output format: json/text
            current = loop_settings.get("output_format", "json")
            new_val = "text" if current == "json" else "json"
            set_ralph_loop_setting("output_format", new_val)
            state.status_message = f"Output format set to {new_val}"
            state.status_message_time = time.time()
        elif key == "4":  # Cycle max calls/hour: 50, 100, 200, 500
            current = loop_settings.get("max_calls_per_hour", 100)
            rate_options = [50, 100, 200, 500]
            try:
                current_idx = rate_options.index(current)
                next_idx = (current_idx + 1) % len(rate_options)
            except ValueError:
                next_idx = 1  # Default to 100
            new_val = rate_options[next_idx]
            set_ralph_loop_setting("max_calls_per_hour", new_val)
            state.status_message = f"Max calls/hour set to {new_val}"
            state.status_message_time = time.time()


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


async def handle_instance_detail(key: str, state: TUIState) -> None:
    """Handle instance detail mode navigation and actions."""
    from chiefwiggum.spawner import read_ralph_status

    # Get current instance
    if not state.instances or state.selected_instance_idx >= len(state.instances):
        state.mode = TUIMode.NORMAL
        return

    instance = state.instances[state.selected_instance_idx]

    if key == "ESCAPE":
        if state.mode == TUIMode.INSTANCE_ERROR_DETAIL:
            state.mode = TUIMode.INSTANCE_DETAIL
        else:
            state.mode = TUIMode.NORMAL
        return

    # Tab navigation
    if key == "1":
        state.instance_detail_tab = InstanceDetailTab.DASHBOARD.value
    elif key == "2":
        state.instance_detail_tab = InstanceDetailTab.HISTORY.value
    elif key == "3":
        state.instance_detail_tab = InstanceDetailTab.ERRORS.value
    elif key == "4":
        state.instance_detail_tab = InstanceDetailTab.LOGS.value
        state.instance_log_needs_refresh = True  # Refresh logs when switching to LOGS tab
    elif key == "\t":  # Tab - cycle tabs
        state.instance_detail_tab = (state.instance_detail_tab + 1) % 4
        # Refresh logs if cycling to LOGS tab
        if state.instance_detail_tab == InstanceDetailTab.LOGS.value:
            state.instance_log_needs_refresh = True

    # j/k cycle through instances (not scrolling)
    elif key == "j":  # Next instance
        if state.selected_instance_idx < len(state.instances) - 1:
            state.selected_instance_idx += 1
            state.instance_log_needs_refresh = True
            state.instance_detail_needs_refresh = True
            state.instance_history_scroll = 0
            state.instance_error_scroll = 0
            state.instance_selected_error_idx = 0
    elif key == "k":  # Previous instance
        if state.selected_instance_idx > 0:
            state.selected_instance_idx -= 1
            state.instance_log_needs_refresh = True
            state.instance_detail_needs_refresh = True
            state.instance_history_scroll = 0
            state.instance_error_scroll = 0
            state.instance_selected_error_idx = 0

    # f - toggle history filter
    elif key == "f" and state.instance_detail_tab == InstanceDetailTab.HISTORY.value:
        filters = ["all", "completed", "failed"]
        current_idx = filters.index(state.instance_history_filter) if state.instance_history_filter in filters else 0
        state.instance_history_filter = filters[(current_idx + 1) % len(filters)]
        state.instance_history_scroll = 0  # Reset scroll

    # Enter - view full error (in Errors tab)
    elif key in ("\r", "\n"):
        if state.instance_detail_tab == InstanceDetailTab.ERRORS.value and state.instance_failed_tasks:
            state.mode = TUIMode.INSTANCE_ERROR_DETAIL
        elif state.instance_detail_tab == InstanceDetailTab.DASHBOARD.value and instance.current_task_id:
            # Jump to task detail for current task
            all_tasks = await list_all_tasks()
            current_task = next((t for t in all_tasks if t.task_id == instance.current_task_id), None)
            if current_task:
                state.selected_task = current_task
                state.mode = TUIMode.TASK_DETAIL

    # Quick actions
    elif key == "r":  # Release current task
        if instance.current_task_id:
            try:
                await release_claim(instance.ralph_id, instance.current_task_id)
                state.status_message = f"Released task from {instance.ralph_id[:12]}"
                state.status_message_time = time.time()
            except Exception as e:
                state.status_message = f"Error: {e}"
                state.status_message_time = time.time()
        else:
            state.status_message = "No current task to release"
            state.status_message_time = time.time()

    elif key == "P":  # Pause/resume this instance (capital P to avoid conflict)
        if instance.status == RalphInstanceStatus.ACTIVE:
            await pause_instance(instance.ralph_id)
            state.status_message = f"Paused {instance.ralph_id[:12]}"
        elif instance.status == RalphInstanceStatus.PAUSED:
            await resume_instance(instance.ralph_id)
            state.status_message = f"Resumed {instance.ralph_id[:12]}"
        else:
            state.status_message = f"Cannot pause/resume instance in {instance.status.value} state"
        state.status_message_time = time.time()

    elif key == "s":  # Stop this instance
        try:
            await shutdown_instance(instance.ralph_id)
            stop_ralph_daemon(instance.ralph_id)
            state.status_message = f"Stopped {instance.ralph_id[:12]}"
            state.status_message_time = time.time()
            state.mode = TUIMode.NORMAL  # Go back after stopping
        except Exception as e:
            state.status_message = f"Error: {e}"
            state.status_message_time = time.time()

    elif key == "K":  # Kill stuck instance & offer restart (uppercase K to avoid j/k scroll conflict)
        from chiefwiggum.spawner import is_ralph_stuck, handle_stuck_ralph

        # Check if instance is actually stuck
        is_stuck, reason = is_ralph_stuck(instance.ralph_id)

        if is_stuck:
            state.status_message = f"Killing stuck Ralph: {reason}"
            state.status_message_time = time.time()

            try:
                result = await handle_stuck_ralph(instance.ralph_id, reason)
                if result["terminated"]:
                    state.status_message = f"Killed {instance.ralph_id[:12]}: {reason}"
                else:
                    state.status_message = f"Handled {instance.ralph_id[:12]} (not running)"
                state.status_message_time = time.time()
            except Exception as e:
                state.status_message = f"Error killing Ralph: {e}"
                state.status_message_time = time.time()
        else:
            # Force kill even if not detected as stuck (user override)
            try:
                stop_ralph_daemon(instance.ralph_id, force=True)
                await shutdown_instance(instance.ralph_id)
                state.status_message = f"Force killed {instance.ralph_id[:12]}"
                state.status_message_time = time.time()
            except Exception as e:
                state.status_message = f"Error: {e}"
                state.status_message_time = time.time()

    elif key == "x":  # Delete stopped/crashed instance (cattle not pets)
        if instance.status in (RalphInstanceStatus.STOPPED, RalphInstanceStatus.CRASHED):
            from chiefwiggum.coordination import delete_instance
            try:
                deleted = await delete_instance(instance.ralph_id)
                if deleted:
                    state.status_message = f"Removed {instance.ralph_id[:12]}"
                else:
                    state.status_message = "Instance not found"
                state.mode = TUIMode.NORMAL  # Go back to list
            except Exception as e:
                state.status_message = f"Error: {e}"
            state.status_message_time = time.time()
        else:
            state.status_message = f"Can only delete stopped/crashed instances (current: {instance.status.value})"
            state.status_message_time = time.time()


async def handle_command(key: str, state: TUIState) -> bool:
    """Handle a key command. Returns True if should quit."""
    if state.mode == TUIMode.NORMAL:
        # Handle Ctrl+R for resume here since it's async
        if key == "\x12":  # Ctrl+R
            count = await resume_all_instances()
            state.status_message = f"Resumed {count} instances"
            state.status_message_time = time.time()
            return False
        # Handle 'y' for sync here since it's async - NOW PROJECT-SCOPED
        if key == "y":
            # Sync current project only (use project filter or detect from cwd)
            project = get_current_project(state)
            if project is None:
                state.status_message = "Set project filter (p) before syncing"
                state.status_message_time = time.time()
                return False

            state.status_message = f"Syncing {project}..."
            state.status_message_time = time.time()
            discovered = discover_fix_plan_projects(project)
            if not discovered:
                state.status_message = f"No @fix_plan.md found for {project}"
                state.status_message_time = time.time()
                return False

            synced = 0
            for project_name, fix_plan_path in discovered:
                count = await sync_tasks_from_fix_plan(str(fix_plan_path), project_name)
                synced += count
            state.status_message = f"Synced {synced} tasks from {project}"
            state.status_message_time = time.time()
            return False

        # Handle 'Y' (Shift+Y) for sync ALL projects
        if key == "Y":
            state.status_message = "Syncing ALL projects..."
            state.status_message_time = time.time()
            synced = 0
            discovered = discover_fix_plan_projects()  # No project filter = all
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

        # Handle 'N' (Shift+N) for quickstart spawn
        if key == "N":
            can_spawn, reason = await can_spawn_ralph()
            if not can_spawn:
                state.status_message = reason
                state.status_message_time = time.time()
                return False

            # Get quickstart defaults from config
            defaults = get_quickstart_defaults()
            project = get_current_project(state)

            if project is None:
                # Try to use first discovered project
                discovered = discover_fix_plan_projects()
                if discovered:
                    project = discovered[0][0]
                else:
                    state.status_message = "No project available for quickstart"
                    state.status_message_time = time.time()
                    return False

            # Find fix_plan path
            fix_plan_path = Path.home() / "claudecode" / project / "@fix_plan.md"
            if not fix_plan_path.exists():
                state.status_message = f"No @fix_plan.md for {project}"
                state.status_message_time = time.time()
                return False

            # Build config from defaults
            model_str = defaults.get("model", "sonnet")
            model = ClaudeModel(model_str)
            timeout = defaults.get("timeout_minutes", 30)

            ralph_id = generate_ralph_id(project[:8])
            ralph_config = RalphConfig(model=model, timeout_minutes=timeout)
            targeting = TargetingConfig(
                project=project,
                priority_min=None,  # All priorities
                categories=[],  # All categories
            )

            # Use task-aware spawning that claims a task and generates focused prompt
            success, message, task_id = await spawn_ralph_with_task_claim(
                ralph_id=ralph_id,
                project=project,
                fix_plan_path=str(fix_plan_path),
                config=ralph_config,
                targeting=targeting,
            )

            if success:
                if task_id:
                    state.status_message = f"Quickstart: {message} (Task: {task_id})"
                else:
                    state.status_message = f"Quickstart: {message}"
            else:
                state.status_message = f"Quickstart failed: {message}"
            state.status_message_time = time.time()
            return False

        # Handle 'x' for deleting stopped/crashed instance in INSTANCES view
        if key == "x" and state.view_focus == ViewFocus.INSTANCES:
            if state.instances and state.selected_instance_idx < len(state.instances):
                inst = state.instances[state.selected_instance_idx]
                if inst.status in (RalphInstanceStatus.STOPPED, RalphInstanceStatus.CRASHED):
                    from chiefwiggum.coordination import delete_instance
                    try:
                        deleted = await delete_instance(inst.ralph_id)
                        if deleted:
                            state.status_message = f"Removed {inst.ralph_id[:12]}"
                            # Adjust selection index if needed
                            if state.selected_instance_idx >= len(state.instances) - 1:
                                state.selected_instance_idx = max(0, state.selected_instance_idx - 1)
                        else:
                            state.status_message = "Instance not found"
                    except Exception as e:
                        state.status_message = f"Error: {e}"
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
        total_lines = len(get_help_lines())
        if key == "j":
            # Scroll down
            state.help_scroll_offset = min(state.help_scroll_offset + 1, max(0, total_lines - 10))
        elif key == "k":
            # Scroll up
            state.help_scroll_offset = max(0, state.help_scroll_offset - 1)
        elif key in ("h", "?", "q", "ESCAPE"):  # h, ?, q or Esc
            # Close help and reset scroll
            state.help_scroll_offset = 0
            state.mode = TUIMode.NORMAL
        # Other keys ignored

    elif state.mode == TUIMode.STATS:
        if key in ("t", "q", "ESCAPE"):  # t toggle, q, or Esc
            state.mode = TUIMode.NORMAL
        # Other keys ignored

    elif state.mode == TUIMode.ERROR_DETAIL:
        if key in ("e", "q", "ESCAPE"):  # e toggle, q, or Esc
            state.mode = TUIMode.NORMAL
        # Other keys ignored

    elif state.mode == TUIMode.LOG_VIEW:
        if key in ("l", "q", "ESCAPE"):  # l toggle, q, or Esc
            state.mode = TUIMode.NORMAL
        # Other keys ignored

    elif state.mode == TUIMode.LOG_STREAM:
        if key in ("v", "q", "ESCAPE"):  # v toggle, q, or Esc
            state.mode = TUIMode.NORMAL
        # Other keys ignored

    elif state.mode == TUIMode.HISTORY:
        if key in ("H", "q", "ESCAPE"):  # H toggle, q, or Esc
            state.mode = TUIMode.NORMAL
        # Other keys ignored

    elif state.mode in (TUIMode.SETTINGS, TUIMode.SETTINGS_EDIT_API_KEY, TUIMode.SETTINGS_EDIT_MAX_RALPHS,
                        TUIMode.SETTINGS_EDIT_MODEL, TUIMode.SETTINGS_EDIT_TIMEOUT,
                        TUIMode.SETTINGS_EDIT_PERMISSIONS, TUIMode.SETTINGS_EDIT_STRATEGY,
                        TUIMode.SETTINGS_EDIT_AUTO_SPAWN, TUIMode.SETTINGS_EDIT_RALPH_LOOP):
        await handle_settings(key, state)

    elif state.mode == TUIMode.TASK_DETAIL:
        if key in ("d", "q", "ESCAPE"):  # d toggle, q, or Esc
            state.mode = TUIMode.NORMAL
        # Other keys ignored

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

    elif state.mode in (TUIMode.INSTANCE_DETAIL, TUIMode.INSTANCE_ERROR_DETAIL):
        await handle_instance_detail(key, state)

    elif state.mode in (TUIMode.SPAWN_PROJECT, TUIMode.SPAWN_PRIORITY, TUIMode.SPAWN_CATEGORY, TUIMode.SPAWN_MODEL, TUIMode.SPAWN_SESSION, TUIMode.SPAWN_CONFIRM):
        await handle_spawn(key, state)

    elif state.mode in (TUIMode.CONFIRM_BULK_STOP, TUIMode.CONFIRM_BULK_PAUSE):
        await handle_bulk_operations(key, state)

    elif state.mode == TUIMode.CLEANUP_CONFIRM:
        if key == "y":
            # Perform cleanup
            from chiefwiggum.coordination import cleanup_idle_ralphs
            cleaned = await cleanup_idle_ralphs()
            if cleaned > 0:
                state.status_message = f"Cleaned up {cleaned} idle Ralph(s)"
            else:
                state.status_message = "No idle Ralphs to clean up"
            state.status_message_time = time.time()
            state.mode = TUIMode.NORMAL
        elif key in ("n", "ESCAPE"):  # n or Esc
            state.mode = TUIMode.NORMAL

    return False


def run_tui(debug: bool = False):
    """Run the TUI dashboard."""
    # Load saved configuration (API key, etc.) on startup
    load_config_on_startup()

    # Check for orphaned tmux sessions and auto-cleanup
    from chiefwiggum.spawner import find_orphaned_tmux_sessions, cleanup_orphaned_tmux_sessions
    orphans = find_orphaned_tmux_sessions()
    if orphans:
        cleanup_orphaned_tmux_sessions()

    console = Console()
    layout = create_layout()
    state = TUIState()

    # Initialize console width
    state.console_width = console.width

    # Load saved view state if persistence is enabled
    if get_config_value("persist_view_state", True):
        saved_state = get_view_state()
        state.show_all_tasks = saved_state.get("show_all_tasks", False)
        state.show_all_instances = saved_state.get("show_all_instances", False)
        view_focus_name = saved_state.get("view_focus", "BOTH")
        try:
            state.view_focus = ViewFocus[view_focus_name]
        except KeyError:
            state.view_focus = ViewFocus.BOTH
        cat_filter = saved_state.get("category_filter")
        if cat_filter:
            try:
                state.category_filter = TaskCategory(cat_filter)
            except ValueError:
                state.category_filter = None
        state.project_filter = saved_state.get("project_filter")
        sort_order_name = saved_state.get("sort_order", "priority")
        try:
            state.sort_order = TaskSortOrder(sort_order_name)
        except ValueError:
            state.sort_order = TaskSortOrder.PRIORITY

    # Show orphan cleanup message after state is ready
    if orphans:
        state.status_message = f"Cleaned up {len(orphans)} orphaned session(s)"
        state.status_message_time = time.time()
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
                    # Update console width for responsive layout (handles terminal resize)
                    state.console_width = console.width
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
