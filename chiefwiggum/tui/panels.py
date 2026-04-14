"""Panel render functions extracted from chiefwiggum.tui.

These functions create Rich renderable objects (Table, Panel, Layout, Text)
for the TUI dashboard. They are pure rendering functions with no side effects
beyond reading configuration and system state.
"""

import time
from datetime import datetime

from rich import box
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chiefwiggum.config import (
    get_api_key,
    get_api_key_source,
    get_auto_scaling_config,
    get_default_model,
    get_default_timeout,
    get_max_ralphs,
    get_ralph_loop_settings,
    get_ralph_permissions,
    get_task_assignment_strategy,
)
from chiefwiggum.icons import (
    BORDER_ALERTS,
    BORDER_ERROR,
    # Background colors
    BORDER_OVERLAY,
    BORDER_SPAWN,
    BORDER_STATS,
    COLOR_ALERT_CRITICAL,
    COLOR_ALERT_WARNING,
    COLOR_ERROR,
    COLOR_OVERDUE,
    COLOR_WARNING,
    # Icons
    ICON_ACTIVE,
    # Error icons
    ICON_ALERT_CRITICAL,
    ICON_ALERT_WARNING,
    ICON_CRASHED,
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
    ICON_STALL,
    ICON_STOPPED,
    ICON_WORKING,
    # Progress/capacity chars
    SEP_VERTICAL,
    # Styles
    STYLE_ACTIVE,
    STYLE_HIGHLIGHT,
    STYLE_IDLE,
    STYLE_STALE,
    STYLE_TABLE_ROW_EVEN,
)
from chiefwiggum.models import (
    ErrorCategory,
    RalphInstanceStatus,
    TaskCategory,
    TaskClaimStatus,
    TaskSortOrder,
)
from chiefwiggum.spawner import (
    get_process_health_cached,
    read_ralph_log,
)
from chiefwiggum.tui.helpers import _get_error_indicator_cached, create_progress_bar, format_age
from chiefwiggum.tui.state import (
    Alert,
    AlertType,
    TUIMode,
    TUIState,
    ViewFocus,
)


def create_instances_table(instances: list, show_all: bool = False, selected_idx: int | None = None, progress_data: dict | None = None) -> Table:
    """Create a table showing Ralph instances.

    Args:
        instances: List of RalphInstance objects
        show_all: If True, show all instances; if False, show only active
        selected_idx: Index of selected row for highlighting
        progress_data: Dict mapping ralph_id -> {percent: int, last_update: datetime}
    """
    title = "Ralph Instances" + (" (All)" if show_all else " (Active)")
    table = Table(title=title, expand=True)
    table.add_column("#", style="dim", no_wrap=True, width=2)
    table.add_column("ID", no_wrap=True, width=10)
    table.add_column("Current Task", width=25)  # Merged with progress
    table.add_column("Done", justify="right", style="dim", width=4)
    table.add_column("Cost", justify="right", style="cyan", width=7)
    table.add_column("Err", justify="center", width=3)  # Error indicator column
    table.add_column("Elapsed", style="dim", width=7)  # Changed from Heartbeat to Elapsed
    table.add_column("Status", justify="center", width=10)

    now = datetime.now()
    progress_data = progress_data or {}

    for idx, inst in enumerate(instances, 1):
        # Calculate heartbeat age (still used for stale detection)
        heartbeat_age = (now - inst.last_heartbeat).total_seconds()

        # Calculate elapsed time (time since task started)
        elapsed_str = "-"
        if inst.current_task_id and inst.status == RalphInstanceStatus.ACTIVE:
            # Use started_at if available, otherwise use last_heartbeat as proxy
            started = getattr(inst, 'started_at', None) or inst.last_heartbeat
            elapsed_seconds = (now - started).total_seconds()
            elapsed_str = format_age(elapsed_seconds)

        # Get error indicator for this instance
        error_indicator = _get_error_indicator_cached(inst.ralph_id)

        # Status styling with icons (using semantic colors)
        # First, check actual process health if status shows ACTIVE
        process_is_dead = False
        if inst.status == RalphInstanceStatus.ACTIVE:
            health = get_process_health_cached(inst.ralph_id)
            process_is_dead = not health.get("healthy", True)

        # Check for stalled task (no log updates in 30s while active)
        is_stalled = False
        progress_info = progress_data.get(inst.ralph_id, {})
        last_log_update = progress_info.get("last_update")
        if last_log_update and inst.status == RalphInstanceStatus.ACTIVE and inst.current_task_id:
            log_age = (now - last_log_update).total_seconds()
            if log_age > 30:
                is_stalled = True

        # Show DEAD if process is dead but database still shows ACTIVE
        if process_is_dead:
            status_str = f"[bold {COLOR_ERROR}]{ICON_CRASHED} DEAD[/bold {COLOR_ERROR}]"
        # Check for stale ACTIVE instances (heartbeat > 5min old)
        elif inst.status == RalphInstanceStatus.ACTIVE and heartbeat_age > 300:
            # Stale ACTIVE - show warning instead of green
            status_str = f"[{STYLE_STALE}]{ICON_STALE} STALE[/{STYLE_STALE}]"
        else:
            status_styles = {
                RalphInstanceStatus.ACTIVE: f"[{STYLE_ACTIVE}]{ICON_ACTIVE} ACTIVE[/{STYLE_ACTIVE}]",
                RalphInstanceStatus.IDLE: f"[{STYLE_IDLE}]{ICON_IDLE} IDLE[/{STYLE_IDLE}]",
                RalphInstanceStatus.PAUSED: f"[blue]{ICON_PAUSED} PAUSED[/blue]",
                RalphInstanceStatus.STOPPED: f"[dim]{ICON_STOPPED} STOP[/dim]",
                RalphInstanceStatus.CRASHED: f"[bold {COLOR_ERROR}]{ICON_CRASHED} CRASH[/bold {COLOR_ERROR}]",
            }
            status_str = status_styles.get(inst.status, inst.status.value)

        # Show completed task count
        done_count = str(inst.tasks_completed) if inst.tasks_completed else "0"

        # Show cost
        cost_display = f"${inst.total_cost_usd:.2f}" if inst.total_cost_usd else "$0.00"

        # Build current task display with progress bar
        task_display = "-"
        if inst.current_task_id:
            task_name = inst.current_task_id[:15]
            progress_percent = progress_info.get("percent", -1)

            if progress_percent >= 0:
                # Show progress bar with percentage
                progress_bar = create_progress_bar(progress_percent, 5)
                task_display = f"{task_name} {progress_bar}"
            else:
                task_display = task_name

            # Add stall indicator
            if is_stalled:
                task_display += f" [{COLOR_WARNING}]{ICON_STALL}[/{COLOR_WARNING}]"

        # Highlight selected row and zebra striping
        is_selected = selected_idx is not None and (idx - 1) == selected_idx
        # Show only the suffix of ralph_id (without hostname prefix) for readability
        id_display = inst.ralph_id
        if inst.hostname and inst.ralph_id.startswith(inst.hostname + "-"):
            id_display = inst.ralph_id[len(inst.hostname) + 1:]
        # Add selection indicator to index column
        idx_display = f"{ICON_SELECTED} {idx}" if is_selected and idx <= 9 else (str(idx) if idx <= 9 else "")
        row_args = (
            idx_display,
            id_display[:10],
            task_display,
            done_count,
            cost_display,
            error_indicator or "-",
            elapsed_str,
            status_str,
        )
        if is_selected:
            table.add_row(*row_args, style=STYLE_HIGHLIGHT)
        else:
            # Zebra striping for non-selected rows
            row_style = STYLE_TABLE_ROW_EVEN if (idx % 2 == 0) else ""
            table.add_row(*row_args, style=row_style)

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
    table.add_column("Age", style="dim", no_wrap=True, width=5, justify="right")  # New Age column
    if show_category:
        table.add_column("Cat", style="dim", no_wrap=True, width=5)
    # Wider task column when expanded
    task_width = None if expanded else 30  # Slightly smaller to make room for Age
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
    now = datetime.now()

    # Show tasks from offset to offset+limit
    visible_tasks = tasks[offset : offset + limit]
    for idx, task in enumerate(visible_tasks, offset + 1):
        priority_str = priority_styles.get(task.task_priority.value, task.task_priority.value)
        status_str = status_styles.get(task.status, task.status.value)

        # Calculate task age
        age_seconds = (now - task.created_at).total_seconds()
        age_str = format_age(age_seconds)
        # Highlight overdue tasks (>30min)
        is_overdue = age_seconds > 1800 and task.status == TaskClaimStatus.PENDING
        if is_overdue:
            age_str = f"[bold {COLOR_OVERDUE}]{age_str}[/bold {COLOR_OVERDUE}]"

        # Add error indicator for failed tasks
        max_title_len = 55 if expanded else 30  # Adjusted for Age column
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
        row.append(age_str)  # Add age column
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

        # Highlight selected row with zebra striping
        if is_row_selected:
            table.add_row(*row, style=STYLE_HIGHLIGHT)
        else:
            # Zebra striping for non-selected rows
            row_style = STYLE_TABLE_ROW_EVEN if (idx % 2 == 0) else ""
            table.add_row(*row, style=row_style)

    # Calculate number of empty columns for footer rows
    num_cols = 6  # Base: Priority, Age, Task, Project, Status, Claimed By
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
        row = [""] * (num_cols - 4) + ["[dim](j/k to scroll)[/dim]", "", "", ""]
        table.add_row(*row)

    if not tasks:
        row = [""] * (num_cols - 4) + ["[dim]No tasks synced (press 'y' to sync)[/dim]", "", "", ""]
        table.add_row(*row)

    return table


def create_graded_tasks_table(
    tasks: list,
    offset: int = 0,
    limit: int = 20,
    selected_idx: int | None = None,
    expanded: bool = False,
) -> Table:
    """Create a table showing graded tasks from the Ralph Loop Alignment queue.

    Args:
        tasks: List of task dicts from the graded tasks table
        offset: Pagination offset
        limit: Max tasks to show
        selected_idx: Currently selected task index
        expanded: Whether to expand task column width
    """
    from chiefwiggum.prompt_grader import get_grade_letter

    title = f"Graded Task Queue ({offset + 1}-{min(offset + limit, len(tasks))} of {len(tasks)})" if tasks else "Graded Task Queue (Empty)"
    table = Table(title=title, expand=True, box=box.ROUNDED)

    table.add_column("#", style="dim", no_wrap=True, width=3)
    table.add_column("Grade", no_wrap=True, width=7, justify="center")
    table.add_column("ID", style="cyan", no_wrap=True, width=15)
    task_width = None if expanded else 35
    table.add_column("Title", max_width=task_width)
    table.add_column("Status", justify="center", width=12)
    table.add_column("Claimed By", style="dim", width=12)

    status_icons = {
        "pending": f"[yellow]{ICON_PENDING} pending[/yellow]",
        "active": f"[bold blue]{ICON_WORKING} active[/bold blue]",
        "completed": f"[green]{ICON_DONE} done[/green]",
        "blocked": f"[red]{ICON_STALL} blocked[/red]",
        "needs_review": f"[magenta]{ICON_ALERT_WARNING} review[/magenta]",
    }

    # Show tasks from offset to offset+limit
    visible_tasks = tasks[offset : offset + limit]
    for idx, task in enumerate(visible_tasks, offset + 1):
        # Get grade info
        grade = task.get("grade")
        grade_letter = get_grade_letter(grade) if grade is not None else "?"

        # Color-code grades
        if grade_letter == "A":
            grade_display = f"[bold green]A {grade}[/bold green]"
        elif grade_letter == "B":
            grade_display = f"[yellow]B {grade}[/yellow]"
        elif grade_letter == "C":
            grade_display = f"[bold {COLOR_WARNING}]C {grade}[/bold {COLOR_WARNING}]"
        elif grade_letter == "F":
            grade_display = f"[bold red]F {grade}[/bold red]"
        else:
            grade_display = "[dim]? --[/dim]"

        # Task info
        task_id = task.get("id", "")
        title = task.get("title", "")[:50]
        status = task.get("status", "pending")
        claimed_by = task.get("claimed_by_ralph_id", "")

        # Status display
        status_str = status_icons.get(status, status)

        # Claimed by display (shortened)
        if claimed_by:
            parts = claimed_by.split("-")
            claimed_display = "-".join(parts[-2:]) if len(parts) >= 2 else claimed_by
            claimed_display = claimed_display[:12]
        else:
            claimed_display = "-"

        # Check if this row is selected
        is_row_selected = selected_idx is not None and (idx - 1) == selected_idx

        # Build row
        idx_display = f"{ICON_SELECTED} {idx}" if is_row_selected and idx <= 9 else str(idx)
        row = [idx_display, grade_display, task_id[:15], title, status_str, claimed_display]

        # Highlight selected row
        if is_row_selected:
            table.add_row(*row, style=STYLE_HIGHLIGHT)
        else:
            # Zebra striping
            row_style = STYLE_TABLE_ROW_EVEN if (idx % 2 == 0) else ""
            table.add_row(*row, style=row_style)

    # Pagination hints
    if len(tasks) > offset + limit:
        remaining = len(tasks) - offset - limit
        table.add_row("", "", "", f"[dim]... {remaining} more (j/k to scroll)[/dim]", "", "")
    elif offset > 0:
        table.add_row("", "", "", "[dim](j/k to scroll)[/dim]", "", "")

    if not tasks:
        table.add_row("", "", "", "[dim]No graded tasks (run 'wig sync --with-grading')[/dim]", "", "")

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

    # Notification badges at start with icons (softer colors)
    if failed_count > 0:
        text.append(f" {ICON_FAILED} {failed_count} FAILED ", style=f"bold white on {COLOR_ERROR}")
        text.append(f"  {SEP_VERTICAL}  ", style="dim")
    if stale_count > 0:
        text.append(f" {ICON_STALE} {stale_count} STALE ", style="bold grey11 on yellow3")
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

    return Panel(text, title="Summary", border_style=BORDER_STATS)


def generate_alerts(state: TUIState) -> list[Alert]:
    """Generate current alerts from system state.

    Scans for failed tasks, down instances, stale instances, and overdue queued tasks.
    """
    alerts = []
    now = datetime.now()
    current_time = time.time()

    # 1. Failed tasks
    for task in state.failed_tasks:
        alerts.append(Alert(
            alert_type=AlertType.TASK_FAILED,
            message=f"{task.task_id[:25]} (FAILED) - {task.error_category.value if task.error_category else 'unknown'}",
            created_at=current_time,
            critical=True,
            source_id=f"task-{task.task_id}",
        ))

    # 2. Down/crashed instances
    for inst in state.all_instances:
        if inst.status == RalphInstanceStatus.CRASHED:
            alerts.append(Alert(
                alert_type=AlertType.INSTANCE_DOWN,
                message=f"{inst.ralph_id[:15]} (DOWN) - Crashed",
                created_at=current_time,
                critical=True,
                source_id=f"inst-{inst.ralph_id}",
            ))

    # 3. Stale active instances (heartbeat > 5min old)
    for inst in state.all_instances:
        if inst.status == RalphInstanceStatus.ACTIVE:
            age_seconds = (now - inst.last_heartbeat).total_seconds()
            if age_seconds > 300:  # 5 minutes
                age_str = format_age(age_seconds)
                alerts.append(Alert(
                    alert_type=AlertType.INSTANCE_STALE,
                    message=f"{inst.ralph_id[:15]} (STALE) - Heartbeat lost {age_str}",
                    created_at=current_time,
                    critical=False,
                    source_id=f"stale-{inst.ralph_id}",
                ))

    # 4. Overdue tasks (queued >30min)
    overdue_count = 0
    for task in state.all_tasks_cache:
        if task.status == TaskClaimStatus.PENDING:
            age_seconds = (now - task.created_at).total_seconds()
            if age_seconds > 1800:  # 30 minutes
                overdue_count += 1

    if overdue_count > 0:
        alerts.append(Alert(
            alert_type=AlertType.QUEUE_OVERDUE,
            message=f"{overdue_count} task(s) queued >30min",
            created_at=current_time,
            critical=False,
            source_id="overdue-tasks",
        ))

    return alerts


def create_alerts_panel(state: TUIState) -> Panel | None:
    """Create the alerts panel showing critical issues.

    Returns None if there are no alerts to show.
    """
    if not state.alerts:
        return None

    text = Text()

    # Show up to 3 alerts (most recent first for critical, then warnings)
    critical_alerts = [a for a in state.alerts if a.critical]
    warning_alerts = [a for a in state.alerts if not a.critical]

    displayed_alerts = critical_alerts[:2] + warning_alerts[:1] if critical_alerts else warning_alerts[:3]

    for i, alert in enumerate(displayed_alerts):
        if alert.critical:
            icon = ICON_ALERT_CRITICAL
            style = f"bold {COLOR_ALERT_CRITICAL}"
        else:
            icon = ICON_ALERT_WARNING
            style = COLOR_ALERT_WARNING

        text.append(f" {icon} ", style=style)
        text.append(alert.message, style=style)

        if i < len(displayed_alerts) - 1:
            text.append("  │  ", style="dim")

    # Show count if more alerts exist
    remaining = len(state.alerts) - len(displayed_alerts)
    if remaining > 0:
        text.append(f"  (+{remaining} more)", style="dim")

    return Panel(text, title="ALERTS", border_style=BORDER_ALERTS, height=3)


def create_layout(has_alerts: bool = False) -> Layout:
    """Create the dashboard layout."""
    layout = Layout()

    if has_alerts:
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="alerts", size=3),
            Layout(name="stats", size=3),
            Layout(name="main"),
            Layout(name="command_bar", size=3),
        )
    else:
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
    lines.append(("  g      Graded task queue (Ralph Loop Alignment)", ""))
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
    lines.append(("  R      Reconcile completed tasks with @fix_plan.md", ""))
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

    return Panel(help_text, title="Help", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))


def create_stats_view_panel(state: TUIState) -> Panel:
    """Create detailed statistics view panel (US11)."""
    text = Text()
    text.append("System Statistics\n\n", style="bold cyan")

    # We'll populate this with actual stats in update_dashboard
    text.append("Press any key to close", style="dim")

    return Panel(text, title="Statistics (t)", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))


def create_error_detail_panel(task, state: TUIState) -> Panel:
    """Create error detail panel for a failed task (US5)."""
    text = Text()

    if not task:
        text.append("No failed task selected", style="dim")
        return Panel(text, title="Error Details", border_style=BORDER_ERROR)

    text.append("Task: ", style="bold")
    text.append(f"{task.task_title}\n\n", style="white")

    text.append("ID: ", style="dim")
    text.append(f"{task.task_id}\n", style="cyan")

    text.append("Status: ", style="dim")
    text.append(f"{task.status.value}\n", style="red bold")

    if task.error_category:
        text.append("Error Category: ", style="dim")
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

    text.append("Retry Count: ", style="dim")
    text.append(f"{task.retry_count}/{task.max_retries}\n", style="white")

    if task.next_retry_at:
        text.append("Next Retry: ", style="dim")
        text.append(f"{task.next_retry_at.strftime('%H:%M:%S')}\n", style="yellow")

    if task.error_message:
        text.append("\nError Message:\n", style="bold red")
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

    return Panel(text, title="Error Details (e)", border_style=BORDER_ERROR, box=box.DOUBLE, padding=(1, 2))


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
        text.append("Project: ", style="dim")
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
        text.append("Project: ", style="dim")
        text.append(f"{config.project}\n", style="cyan")
        text.append("Priority: ", style="dim")
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
        text.append("Project: ", style="dim")
        text.append(f"{config.project}\n", style="cyan")
        text.append("Priority: ", style="dim")
        text.append(f"{config.priority_min.value if config.priority_min else 'All'}\n", style="cyan")
        if config.categories:
            text.append("Categories: ", style="dim")
            text.append(f"{', '.join(c.value for c in config.categories)}\n\n", style="cyan")
        else:
            text.append("Categories: ", style="dim")
            text.append("All\n\n", style="cyan")
        text.append("Select Model:\n", style="bold yellow")
        text.append("  1", style="yellow bold")
        text.append(" = Sonnet (recommended)\n", style="white")
        text.append("  2", style="yellow bold")
        text.append(" = Opus\n", style="white")
        text.append("  3", style="yellow bold")
        text.append(" = Haiku\n", style="white")

    elif state.mode == TUIMode.SPAWN_SESSION:
        text.append("Project: ", style="dim")
        text.append(f"{config.project}\n", style="cyan")
        text.append("Model: ", style="dim")
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
        text.append("  Project: ", style="dim")
        text.append(f"{config.project}\n", style="cyan")
        text.append("  Priority: ", style="dim")
        text.append(f"{config.priority_min.value if config.priority_min else 'All'}\n", style="cyan")
        if config.categories:
            text.append("  Categories: ", style="dim")
            text.append(f"{', '.join(c.value for c in config.categories)}\n", style="cyan")
        text.append("  Model: ", style="dim")
        text.append(f"{config.model.value}\n", style="cyan")
        # Session settings summary
        continuity_str = "Continue" if config.session_continuity else "Fresh"
        text.append("  Session: ", style="dim")
        text.append(f"{continuity_str}, expires in {config.session_expiry_hours}h\n", style="cyan")
        text.append("  Fix Plan: ", style="dim")
        text.append(f"{config.fix_plan_path}\n\n", style="cyan")
        text.append("  Enter", style="green bold")
        text.append(" = Spawn\n", style="white")
        text.append("  Esc", style="yellow bold")
        text.append(" = Cancel\n", style="dim")

    return Panel(text, title="Spawn Ralph (n)", border_style=BORDER_SPAWN, box=box.DOUBLE, padding=(1, 2))


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

    return Panel(text, title="Ralph Logs (l)", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))


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

    return Panel(text, title="History (H)", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))


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
        return Panel(text, title="Settings - Edit API Key", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))

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
        return Panel(text, title="Settings - Edit Max Ralphs", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))

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
        return Panel(text, title="Settings - Default Model", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))

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
        return Panel(text, title="Settings - Default Timeout", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))

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
        return Panel(text, title="Settings - Permissions", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))

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
        return Panel(text, title="Settings - Assignment Strategy", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))

    elif state.mode == TUIMode.SETTINGS_EDIT_AUTO_SPAWN:
        text.append("Auto-Scaling Settings\n\n", style="bold cyan")
        config = get_auto_scaling_config()
        text.append("Toggle settings with number keys:\n\n", style="dim")
        text.append("  1. Auto-Spawn Enabled: ", style="dim")
        text.append(f"{'Yes' if config['auto_spawn_enabled'] else 'No'}\n", style="green" if config['auto_spawn_enabled'] else "red")
        text.append("  2. Spawn Threshold:    ", style="dim")
        text.append(f"{config['auto_spawn_threshold']} pending tasks\n", style="white")
        text.append("  3. Auto-Cleanup:       ", style="dim")
        text.append(f"{'Yes' if config['auto_cleanup_enabled'] else 'No'}\n", style="green" if config['auto_cleanup_enabled'] else "red")
        text.append("  4. Idle Timeout:       ", style="dim")
        text.append(f"{config['auto_cleanup_idle_minutes']} minutes\n", style="white")
        text.append("\n")
        text.append("1-4", style="yellow bold")
        text.append(" = toggle/edit  ", style="dim")
        text.append("Esc", style="yellow bold")
        text.append(" = done", style="dim")
        return Panel(text, title="Settings - Auto-Scaling", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))

    elif state.mode == TUIMode.SETTINGS_EDIT_RALPH_LOOP:
        text.append("Ralph Loop Settings\n\n", style="bold cyan")
        loop_settings = get_ralph_loop_settings()
        text.append("Configure settings passed to ralph_loop.sh:\n\n", style="dim")
        # Session continuity
        continuity = loop_settings.get("session_continuity", True)
        text.append("  1. Session Continuity: ", style="dim")
        text.append(f"{'Yes (continue)' if continuity else 'No (fresh start)'}\n",
                   style="green" if continuity else "red")
        # Session expiry
        text.append("  2. Session Expiry:     ", style="dim")
        text.append(f"{loop_settings.get('session_expiry_hours', 24)} hours\n", style="white")
        # Output format
        output_fmt = loop_settings.get("output_format", "json")
        text.append("  3. Output Format:      ", style="dim")
        text.append(f"{output_fmt}\n", style="white")
        # Max calls per hour
        text.append("  4. Max Calls/Hour:     ", style="dim")
        text.append(f"{loop_settings.get('max_calls_per_hour', 100)}\n", style="white")
        text.append("\n")
        text.append("1-4", style="yellow bold")
        text.append(" = toggle/edit  ", style="dim")
        text.append("Esc", style="yellow bold")
        text.append(" = done", style="dim")
        return Panel(text, title="Settings - Ralph Loop", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))

    # Main settings view with sections
    text.append("Settings & Configuration\n\n", style="bold cyan")

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
    text.append("    Tasks:     ", style="dim")
    text.append(f"{'All' if state.show_all_tasks else 'Active only'}\n", style="white")
    text.append("    Instances: ", style="dim")
    text.append(f"{'All' if state.show_all_instances else 'Active only'}\n", style="white")
    text.append("    Focus:     ", style="dim")
    text.append(f"{state.view_focus.name}\n", style="white")

    text.append("\n")
    text.append("j/k", style="yellow bold")
    text.append("=select  ", style="dim")
    text.append("Enter", style="yellow bold")
    text.append("=edit  ", style="dim")
    text.append("Esc", style="yellow bold")
    text.append("=close", style="dim")

    return Panel(text, title="Settings (S)", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))


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

    return Panel(text, title="Search (/)", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))


def create_task_detail_panel(task, state: TUIState) -> Panel:
    """Create detailed task view panel with enhanced information."""
    text = Text()

    if not task:
        text.append("No task selected", style="dim")
        return Panel(text, title="Task Detail", border_style=BORDER_OVERLAY)

    # Header with status icon
    status_icons = {
        TaskClaimStatus.PENDING: (ICON_PENDING, "yellow"),
        TaskClaimStatus.IN_PROGRESS: (ICON_WORKING, "blue"),
        TaskClaimStatus.COMPLETED: (ICON_DONE, "green"),
        TaskClaimStatus.FAILED: (ICON_FAILED, "red"),
        TaskClaimStatus.RELEASED: (ICON_RELEASED, "dim"),
        TaskClaimStatus.RETRY_PENDING: (ICON_RETRY, "magenta"),
    }

    # Handle both TaskClaim objects and graded task dicts/SimpleNamespace
    task_status = getattr(task, "status", None)
    if task_status and hasattr(task_status, "value"):
        # TaskClaimStatus enum
        icon, icon_style = status_icons.get(task_status, (ICON_PENDING, "white"))
    else:
        # String status (graded tasks)
        status_str = getattr(task, "status", "pending")
        status_map = {
            "pending": (ICON_PENDING, "yellow"),
            "active": (ICON_WORKING, "blue"),
            "completed": (ICON_DONE, "green"),
            "blocked": (ICON_STALL, "red"),
            "needs_review": (ICON_ALERT_WARNING, "magenta"),
        }
        icon, icon_style = status_map.get(status_str, (ICON_PENDING, "white"))

    text.append(f"{icon} ", style=icon_style)

    # Get task title and ID (works for both types)
    title = getattr(task, "task_title", getattr(task, "title", "Untitled"))
    task_id = getattr(task, "task_id", getattr(task, "id", "unknown"))

    text.append(f"{title}\n", style="bold white")
    text.append(f"ID: {task_id}\n\n", style="dim cyan")

    # Status with attempt count for failed tasks
    text.append("Status: ", style="dim")

    # Handle both enum and string status
    if hasattr(task_status, "value"):
        status_display = task_status.value
        status_styles = {
            TaskClaimStatus.PENDING: "yellow",
            TaskClaimStatus.IN_PROGRESS: "blue",
            TaskClaimStatus.COMPLETED: "green",
            TaskClaimStatus.FAILED: "red",
            TaskClaimStatus.RELEASED: "dim",
            TaskClaimStatus.RETRY_PENDING: "magenta",
        }
        style = status_styles.get(task_status, "white")

        if task_status == TaskClaimStatus.FAILED and hasattr(task, "retry_count") and task.retry_count > 0:
            status_display += f" (attempt {task.retry_count + 1} of {task.max_retries})"
    else:
        # String status
        status_display = getattr(task, "status", "unknown")
        status_styles = {
            "pending": "yellow",
            "active": "blue",
            "completed": "green",
            "blocked": "red",
            "needs_review": "magenta",
        }
        style = status_styles.get(status_display, "white")

    text.append(f"{status_display}\n", style=style)

    # Priority (may not exist for graded tasks)
    if hasattr(task, "task_priority"):
        priority_styles = {"HIGH": "red", "MEDIUM": "yellow", "LOWER": "blue", "POLISH": "dim"}
        text.append("Priority: ", style="dim")
        priority_val = task.task_priority.value if hasattr(task.task_priority, "value") else task.task_priority
        text.append(f"{priority_val}\n", style=priority_styles.get(priority_val, "white"))

    # Category and Project
    if hasattr(task, "category") and task.category:
        text.append("Category: ", style="dim")
        cat_val = task.category.value if hasattr(task.category, "value") else task.category
        text.append(f"{cat_val}\n", style="magenta")

    project = getattr(task, "project", None)
    if project:
        text.append("Project: ", style="dim")
        text.append(f"{project}\n", style="blue")

    # Worker info
    claimed_by = getattr(task, "claimed_by_ralph_id", None)
    if claimed_by:
        text.append("Worker: ", style="dim")
        text.append(f"{claimed_by}\n", style="cyan")

    text.append("\n")

    # Graded task info (Ralph Loop Alignment)
    if hasattr(task, "grade") and task.grade is not None:
        from chiefwiggum.prompt_grader import get_grade_letter

        grade_letter = get_grade_letter(task.grade)

        # Grade display with color
        text.append("─" * 40 + "\n", style="dim")
        text.append("Prompt Quality Grade\n", style="bold cyan")
        text.append("\n")

        text.append("Grade: ", style="dim")
        if grade_letter == "A":
            text.append(f"{grade_letter} ({task.grade}/100)", style="bold green")
            text.append(" - Auto-spawn ready\n", style="green")
        elif grade_letter == "B":
            text.append(f"{grade_letter} ({task.grade}/100)", style="yellow")
            text.append(" - Auto-spawn ready\n", style="yellow")
        elif grade_letter == "C":
            text.append(f"{grade_letter} ({task.grade}/100)", style=f"bold {COLOR_WARNING}")
            text.append(" - Needs review\n", style=COLOR_WARNING)
        else:
            text.append(f"{grade_letter} ({task.grade}/100)", style="bold red")
            text.append(" - Blocked (improve spec)\n", style="red")

        text.append("\n")

        # Grade reasoning
        if hasattr(task, "grade_reasoning") and task.grade_reasoning:
            text.append("Grade Breakdown:\n", style="bold yellow")
            reasoning_lines = task.grade_reasoning.split("\n")
            for line in reasoning_lines[:6]:  # Show first 6 criteria
                if line.strip():
                    text.append(f"  {line}\n", style="dim white")

        # Generated prompt preview
        if hasattr(task, "generated_prompt") and task.generated_prompt:
            text.append("\n")
            text.append("Generated Prompt Preview:\n", style="bold yellow")
            prompt_lines = task.generated_prompt.split("\n")
            # Show first 10 lines
            for line in prompt_lines[:10]:
                text.append(f"  {line[:70]}\n", style="dim cyan")
            if len(prompt_lines) > 10:
                text.append(f"  ... ({len(prompt_lines) - 10} more lines)\n", style="dim")

        text.append("\n")

    # Timestamps and elapsed time
    if hasattr(task, "started_at") and task.started_at:
        text.append("Started: ", style="dim")
        text.append(f"{task.started_at.strftime('%Y-%m-%d %H:%M:%S')}\n", style="white")

        # Calculate elapsed time
        end_time = task.completed_at if task.completed_at else datetime.now()
        elapsed = (end_time - task.started_at).total_seconds()
        if elapsed < 60:
            elapsed_str = f"{int(elapsed)}s"
        elif elapsed < 3600:
            elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
        else:
            elapsed_str = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m"

        text.append("Elapsed: ", style="dim")
        if task.status == TaskClaimStatus.IN_PROGRESS:
            text.append(f"{elapsed_str}", style="yellow")
            # Show timeout warning if close to limit
            default_timeout = 30 * 60  # 30 min default
            if elapsed > default_timeout * 0.8:
                text.append(" (approaching timeout)", style="red")
            text.append("\n")
        else:
            text.append(f"{elapsed_str}\n", style="white")

    completed_at = getattr(task, "completed_at", None)
    if completed_at:
        text.append("Completed: ", style="dim")
        text.append(f"{completed_at.strftime('%Y-%m-%d %H:%M:%S')}\n", style="green")

    # Git commit
    git_commit = getattr(task, "git_commit_sha", None)
    if git_commit:
        text.append("\nCommit: ", style="dim")
        text.append(f"{git_commit[:12]}\n", style="yellow")

    # Error info section with full message
    error_message = getattr(task, "error_message", None)
    error_category = getattr(task, "error_category", None)
    if error_message or error_category:
        text.append("\n")
        text.append("─" * 40 + "\n", style="dim")

        if error_category:
            text.append("Error: ", style="bold red")
            cat_val = error_category.value if hasattr(error_category, "value") else error_category
            text.append(f"{cat_val}\n", style="red")

        if error_message:
            text.append("\nError Message:\n", style="bold red")
            # Show full error message (up to 500 chars)
            error_display = error_message[:500]
            if len(error_message) > 500:
                error_display += "..."
            text.append(f"{error_display}\n", style="white")

    # Retry info
    retry_count = getattr(task, "retry_count", 0)
    next_retry_at = getattr(task, "next_retry_at", None)
    if retry_count > 0 or next_retry_at:
        text.append("\n")
        if retry_count > 0:
            max_retries = getattr(task, "max_retries", 3)
            text.append(f"Retry Count: {retry_count}/{max_retries}\n", style="yellow")
        if next_retry_at:
            text.append(f"Next Retry: {next_retry_at.strftime('%H:%M:%S')}\n", style="yellow")

    # Log tail section - show last few lines if we have the worker ID
    # Check status appropriately
    show_logs = False
    if claimed_by:
        if hasattr(task_status, "value"):
            show_logs = task_status in (TaskClaimStatus.IN_PROGRESS, TaskClaimStatus.FAILED)
        else:
            status_str = getattr(task, "status", "")
            show_logs = status_str in ("active", "needs_review")

    if show_logs:
        text.append("\n")
        text.append("─" * 40 + "\n", style="dim")
        text.append("Recent Log Output:\n", style="bold yellow")
        try:
            log_content = read_ralph_log(claimed_by, 10)
            if log_content:
                lines = log_content.strip().split("\n")[-8:]  # Last 8 lines
                for line in lines:
                    line_lower = line.lower()
                    if "error" in line_lower or "failed" in line_lower:
                        text.append(f"  {line[:70]}\n", style="red")
                    elif "warning" in line_lower:
                        text.append(f"  {line[:70]}\n", style="yellow")
                    else:
                        text.append(f"  {line[:70]}\n", style="dim")
            else:
                text.append("  [No log data available]\n", style="dim")
        except Exception:
            text.append("  [Unable to read logs]\n", style="dim")

    # Footer with keyboard shortcuts
    text.append("\n")
    text.append("─" * 40 + "\n", style="dim")
    text.append("[r] Retry  ", style="yellow")
    text.append("[l] Full Logs  ", style="yellow")
    text.append("[Esc] Back", style="dim")

    return Panel(text, title="Task Detail (d)", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))


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

    return Panel(text, title="Bulk Actions (m)", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))


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

    return Panel(text, title="Log Stream (v)", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))


def create_confirm_panel(action: str, count: int) -> Panel:
    """Create confirmation panel for bulk operations."""
    text = Text()
    text.append(f"Confirm: {action}\n\n", style="bold red")
    text.append("This will affect ", style="white")
    text.append(f"{count}", style="yellow bold")
    text.append(" Ralph instance(s).\n\n", style="white")
    text.append("  y", style="green bold")
    text.append(" = Confirm\n", style="white")
    text.append("  n, Esc", style="yellow bold")
    text.append(" = Cancel\n", style="white")

    return Panel(text, title="Confirm Action", border_style=BORDER_ERROR, box=box.DOUBLE, padding=(1, 2))


def create_reconcile_panel(reconcile_result: dict | None) -> Panel:
    """Create panel showing reconciliation results."""
    text = Text()
    text.append("Task Reconciliation Results\n\n", style="bold cyan")

    if reconcile_result is None:
        text.append("No reconciliation data available.\n", style="dim")
        text.append("\nPress ", style="white")
        text.append("Esc", style="yellow bold")
        text.append(" to close", style="white")
        return Panel(text, title="Reconcile", border_style="cyan", box=box.ROUNDED, padding=(1, 2))

    # Summary statistics
    text.append("Summary:\n", style="bold white")
    text.append("  • Scanned: ", style="white")
    text.append(f"{reconcile_result['scanned']}\n", style="cyan bold")

    text.append("  • Updated: ", style="white")
    text.append(f"{reconcile_result['updated']}", style="green bold")
    text.append(" tasks marked complete in @fix_plan.md\n", style="white")

    text.append("  • Skipped: ", style="white")
    text.append(f"{reconcile_result['skipped']}", style="yellow")
    text.append(" (already marked)\n", style="white")

    text.append("  • Failed: ", style="white")
    text.append(f"{reconcile_result['failed']}\n", style="red bold")

    # Show details (first 10)
    details = reconcile_result.get("details", [])
    if details:
        text.append("\nDetails (first 10):\n", style="bold white")
        for i, detail in enumerate(details[:10]):
            task_id = detail.get("task_id", "unknown")
            action = detail.get("action", "unknown")
            reason = detail.get("reason", "")

            if action == "marked_complete" or action == "would_mark_complete":
                icon = "✓"
                style = "green"
                commit_verified = detail.get("commit_verified", False)
                status = " (commit verified)" if commit_verified else ""
            elif action == "skipped":
                icon = "○"
                style = "yellow"
                status = f" ({reason})"
            elif action == "failed":
                icon = "✗"
                style = "red"
                status = f" ({reason})"
            else:
                icon = "?"
                style = "dim"
                status = ""

            text.append(f"  {icon} ", style=style)
            text.append(f"{task_id}", style="cyan")
            text.append(f"{status}\n", style="dim")

        if len(details) > 10:
            text.append(f"\n  ... and {len(details) - 10} more\n", style="dim")

    text.append("\nPress ", style="white")
    text.append("Esc", style="yellow bold")
    text.append(" to close", style="white")

    border_style = "green" if reconcile_result["failed"] == 0 else "yellow"
    return Panel(text, title="Reconcile Results", border_style=border_style, box=box.ROUNDED, padding=(1, 2))


async def create_cleanup_panel() -> Panel:
    """Create cleanup confirmation panel showing what will be cleaned up."""
    from chiefwiggum.config import get_auto_scaling_config
    from chiefwiggum.coordination import get_idle_ralphs

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
        text.append("\nTotal: ", style="white")
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

    return Panel(text, title="Cleanup Confirmation (C)", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2))


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
                proj_display = proj_display[:-1] + "\u2026"
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
            # Strip hostname prefix for readability (same logic as instances table)
            id_display = inst.ralph_id
            if inst.hostname and inst.ralph_id.startswith(inst.hostname + "-"):
                id_display = inst.ralph_id[len(inst.hostname) + 1:]
            text.append(f"={id_display[:8]}", style="dim")
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
