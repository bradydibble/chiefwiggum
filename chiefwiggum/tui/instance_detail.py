"""Instance detail panel rendering functions.

Extracted from chiefwiggum.tui — creates the tabbed instance detail view
including dashboard, history, errors, and logs tabs.
"""

import logging
from datetime import datetime
from pathlib import Path

from rich import box
from rich.panel import Panel
from rich.text import Text

from chiefwiggum.icons import (
    ICON_ACTIVE,
    ICON_CRASHED,
    ICON_DONE,
    ICON_IDLE,
    ICON_PAUSED,
    ICON_STALE,
    ICON_STOPPED,
    ICON_ERROR_PERMISSION,
    ICON_ERROR_API,
    ICON_ERROR_TOOL,
    ICON_ERROR_GENERAL,
    BORDER_ERROR,
    BORDER_INSTANCES,
)
from chiefwiggum.models import (
    ErrorCategory,
    RalphInstanceStatus,
    TaskClaimStatus,
)
from chiefwiggum.spawner import (
    get_error_summary,
    get_process_health_cached,
)
from chiefwiggum.tui.state import InstanceDetailTab, TUIState

logger = logging.getLogger(__name__)


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
    from chiefwiggum.spawner import get_process_health, get_status_staleness, read_ralph_status

    text = Text()

    # Skip expensive health checks for dead instances
    if skip_health_checks:
        process_health = {"healthy": False, "state": "dead", "pid": None, "elapsed": None}
        status_staleness = {"stale": True, "exists": False, "message": "Instance is crashed/stopped"}
    else:
        # Get health/staleness if not provided
        if process_health is None:
            process_health = get_process_health_cached(instance.ralph_id)
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
    text.append("\n\n")

    # Read status file for progress_data and HITL info
    status = read_ralph_status(instance.ralph_id)

    # === SECTION 1: HITL ALERT (highest priority) ===
    needs_hitl = status.get("needs_human_intervention", False) if status else False
    hitl_reason = status.get("hitl_reason") if status else None

    if needs_hitl and hitl_reason:
        text.append("\u2501\u2501\u2501 NEEDS ATTENTION \u2501\u2501\u2501\n", style="bold red on yellow")
        text.append("\U0001f6a8 ", style="red bold")
        text.append(f"{hitl_reason}\n", style="yellow bold")
        text.append("Action required - check logs and fix issue\n", style="dim")
        text.append("\n")

    # === SECTION 2: ERROR STATUS (show errors prominently) ===
    error_summary = get_error_summary(instance.ralph_id)
    if error_summary["total_errors"] > 0:
        text.append("\u2501\u2501\u2501 ERRORS DETECTED \u2501\u2501\u2501\n", style="bold red")

        # Error icons mapping
        error_icons = {
            "permission": (ICON_ERROR_PERMISSION, "magenta"),
            "api_error": (ICON_ERROR_API, "red"),
            "tool_failure": (ICON_ERROR_TOOL, "yellow"),
        }

        # Show counts by category with larger text
        for category, count in error_summary["by_category"].items():
            icon, color = error_icons.get(category, (ICON_ERROR_GENERAL, "orange1"))
            text.append(f"  {icon} {category.upper()}: {count} error(s)\n", style=f"{color} bold")

        # Show last error time
        if error_summary["last_error_time"]:
            text.append(f"  Last: {error_summary['last_error_time']}\n", style="dim")

        if error_summary["has_critical"]:
            text.append("  \u26a0 Critical - requires immediate attention\n", style="yellow bold")

        text.append("  (Tab 3 for full details)\n", style="dim")
        text.append("\n")

    # === DIAGNOSTIC INFO (only show if there are issues) ===
    # Check if stuck and show diagnosis (skip for dead instances)
    if skip_health_checks:
        is_stuck = False
        stuck_reason = ""
        activity = {"log_age_seconds": None, "is_responsive": False}
        has_issues = True  # Dead instances always have issues
    else:
        from chiefwiggum.spawner import is_ralph_stuck, get_ralph_activity
        timeout_mins = instance.config.timeout_minutes if instance.config else 30
        is_stuck, stuck_reason = is_ralph_stuck(instance.ralph_id, timeout_mins)
        activity = get_ralph_activity(instance.ralph_id)

        # Determine if there are any health issues
        has_issues = (
            is_stuck or
            not activity["is_responsive"] or
            not process_health["healthy"] or
            status_staleness.get("stale", False) or
            (activity.get("log_age_seconds", 0) > 300)
        )

    # Only show diagnostic section if there are issues
    if not skip_health_checks and has_issues:
        text.append("\u2501\u2501\u2501 DIAGNOSTICS \u2501\u2501\u2501\n", style="bold yellow")

        # Show diagnosis first (most important)
        if is_stuck:
            text.append(f"  \U0001f534 STUCK: {stuck_reason}\n", style="red bold")
            text.append("  Action: Press K to kill\n", style="dim")
        elif not activity["is_responsive"]:
            text.append("  \U0001f7e1 Unresponsive (no log activity)\n", style="yellow")
        elif not process_health["healthy"]:
            state = process_health["state"]
            if state == "zombie":
                text.append("  \U0001f534 Process is ZOMBIE (defunct)\n", style="red bold")
            elif state == "dead":
                text.append("  \U0001f534 Process NOT RUNNING\n", style="red bold")
            else:
                text.append(f"  \U0001f7e1 Process issue: {state}\n", style="yellow")

        # Show log activity if stale
        if activity.get("log_age_seconds") is not None:
            age = activity["log_age_seconds"]
            if age > 300:
                text.append(f"  \u26a0 No log updates for {age/60:.1f}m\n", style="yellow")

        # Show process details if not healthy
        if not process_health["healthy"] and process_health.get("pid"):
            text.append(f"  PID: {process_health['pid']}", style="dim")
            if process_health.get("elapsed"):
                text.append(f" (running {process_health['elapsed']})", style="dim")
            text.append("\n")

        # Show status file staleness if an issue
        if status_staleness.get("stale", False):
            text.append(f"  Status file: {status_staleness['message']}\n", style="yellow")

        text.append("\n")

    # === SECTION 3: CURRENT TASK & PROGRESS (improved display) ===
    text.append("\u2501\u2501\u2501 CURRENT TASK \u2501\u2501\u2501\n", style="bold cyan")
    if instance.current_task_id and current_task:
        text.append(f"  {current_task.task_title[:50]}\n", style="white bold")

        # Calculate time on task
        if current_task.started_at:
            elapsed = (datetime.now() - current_task.started_at).total_seconds()
            timeout_minutes = instance.config.timeout_minutes if instance.config else 30
            timeout_seconds = timeout_minutes * 60

            # Format elapsed and remaining time
            if elapsed < 60:
                elapsed_str = f"{int(elapsed)}s"
            elif elapsed < 3600:
                elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
            else:
                elapsed_str = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m"

            remaining = max(0, timeout_seconds - elapsed)
            if remaining < 60:
                remaining_str = f"{int(remaining)}s"
            elif remaining < 3600:
                remaining_str = f"{int(remaining // 60)}m"
            else:
                remaining_str = f"{int(remaining // 3600)}h {int((remaining % 3600) // 60)}m"

            # Progress bar (larger, more prominent)
            progress = min(elapsed / timeout_seconds, 1.0)
            bar_width = 20  # Doubled from 10
            filled = int(progress * bar_width)
            bar_style = "green" if progress < 0.5 else ("yellow" if progress < 0.75 else "red")

            # Single-line time display with progress bar
            text.append("  Time: ", style="dim")
            text.append(f"{elapsed_str} / {timeout_minutes}m ", style="cyan bold")
            text.append("\u2588" * filled, style=bar_style)  # filled
            text.append("\u2591" * (bar_width - filled), style="dim")  # empty
            text.append(f" {int(progress * 100)}%", style=bar_style)

            if remaining < 300:  # Less than 5 minutes remaining
                text.append(f"  ({remaining_str} left)", style="yellow bold")

            text.append("\n")

            # Loop count + last update on same line
            real_loop_count = status.get("loop_count", instance.loop_count) if status else instance.loop_count
            text.append(f"  Loop: #{real_loop_count}  ", style="cyan")

            # Last update time
            if status and status.get("updated_at"):
                try:
                    from datetime import datetime as dt_parser
                    updated_at = dt_parser.fromisoformat(status["updated_at"])
                    update_age = (datetime.now() - updated_at).total_seconds()
                    if update_age < 60:
                        update_str = f"{int(update_age)}s ago"
                        update_style = "green"
                    elif update_age < 300:
                        update_str = f"{int(update_age/60)}m ago"
                        update_style = "cyan"
                    else:
                        update_str = f"{int(update_age/60)}m ago"
                        update_style = "yellow"
                    text.append(f"Last update: {update_str}", style=update_style)
                except (ValueError, AttributeError):
                    pass

            text.append("\n")
    else:
        text.append("  Idle - waiting for task\n", style="dim")

    # === SECTION 4: PROGRESS DATA (test results) ===
    if status and status.get("progress_data"):
        progress_data = status["progress_data"]
        test_results = progress_data.get("test_results")

        if test_results and test_results.get("total", 0) > 0:
            text.append("\u2501\u2501\u2501 TEST PROGRESS \u2501\u2501\u2501\n", style="bold magenta")

            passed = test_results.get("passed", 0)
            failed = test_results.get("failed", 0)
            total = test_results.get("total", 0)

            # Calculate pass percentage
            pass_pct = (passed / total * 100) if total > 0 else 0

            # Progress bar for tests
            test_bar_width = 20
            test_filled = int(pass_pct / 100 * test_bar_width)
            test_bar_style = "green" if pass_pct >= 80 else ("yellow" if pass_pct >= 50 else "red")

            text.append(f"  Tests: {passed}/{total} passing  ", style="white bold")
            text.append("\u2588" * test_filled, style=test_bar_style)
            text.append("\u2591" * (test_bar_width - test_filled), style="dim")
            text.append(f" {int(pass_pct)}%\n", style=test_bar_style)

            if failed > 0:
                text.append(f"  \u26a0 {failed} test(s) failing", style="yellow bold")

                # Check stuck indicators
                stuck_indicators = progress_data.get("stuck_indicators", {})
                same_failures = stuck_indicators.get("same_test_failures", 0)
                if same_failures >= 3:
                    text.append(f" (stuck {same_failures}x)", style="red bold")

                text.append("\n")

            # Show timestamp
            if test_results.get("timestamp"):
                try:
                    from datetime import datetime as dt_parser
                    test_time = dt_parser.fromisoformat(test_results["timestamp"])
                    test_age = (datetime.now() - test_time).total_seconds()
                    if test_age < 60:
                        time_str = f"{int(test_age)}s ago"
                    elif test_age < 3600:
                        time_str = f"{int(test_age/60)}m ago"
                    else:
                        time_str = f"{int(test_age/3600)}h ago"
                    text.append(f"  Last test run: {time_str}\n", style="dim")
                except (ValueError, AttributeError):
                    pass

            text.append("\n")

    text.append("\n")

    # === TOKEN USAGE (only show if critical - 60%+ or error reading) ===
    if instance.project:
        project_dir = Path.home() / "claudecode" / instance.project
        token_usage_file = project_dir / ".token_usage"
        token_pct_file = project_dir / ".token_percentage"

        if token_usage_file.exists() and token_pct_file.exists():
            try:
                tokens = token_usage_file.read_text().strip()
                pct = token_pct_file.read_text().strip()
                pct_int = int(pct)

                # Only show if usage is >= 60% (yellow/red zone)
                if pct_int >= 60:
                    text.append("\u2501\u2501\u2501 TOKEN USAGE \u2501\u2501\u2501\n", style="bold yellow")

                    # Color code by percentage
                    if pct_int >= 80:
                        token_color = "red"
                        warning = " \u26a0 HIGH - session reset soon"
                    elif pct_int >= 60:
                        token_color = "yellow"
                        warning = ""
                    else:
                        token_color = "green"
                        warning = ""

                    text.append(f"  ~{tokens} tokens ({pct}%){warning}\n", style=token_color)
                    text.append("\n")
            except (ValueError, IOError) as e:
                logger.debug(f"Could not read token usage files: {e}")

    # === SECTION 5: ACTIVITY & HEALTH ===
    text.append("\u2501\u2501\u2501 ACTIVITY \u2501\u2501\u2501\n", style="bold yellow")

    # Activity message from status file
    if status and status.get("message"):
        text.append(f"  {status['message'][:60]}\n", style="white")
    else:
        text.append("  No recent activity\n", style="dim")

    # Success rate on same line as tasks
    total_tasks = instance.tasks_completed + instance.tasks_failed
    if total_tasks > 0:
        success_rate = (instance.tasks_completed / total_tasks) * 100
        rate_style = "green" if success_rate >= 80 else ("yellow" if success_rate >= 50 else "red")
        text.append(f"  Tasks: {instance.tasks_completed} completed, {instance.tasks_failed} failed  ", style="dim")
        text.append(f"({success_rate:.0f}% success)\n", style=rate_style)
    else:
        text.append("  Tasks: None completed yet\n", style="dim")

    # Failure streak warning
    if state.instance_failure_streak >= 3:
        text.append(f"  \u26a0 {state.instance_failure_streak} consecutive failures\n", style="red bold")

    # Tasks/hour
    if instance.started_at and total_tasks > 0:
        hours_running = max(0.1, (datetime.now() - instance.started_at).total_seconds() / 3600)
        tasks_per_hour = instance.tasks_completed / hours_running
        text.append(f"  Rate: {tasks_per_hour:.1f} tasks/hour\n", style="cyan")

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

    # Cost Metrics
    text.append("Cost Metrics\n", style="bold yellow")
    text.append("  Total Cost: ", style="dim")
    cost_color = "cyan"
    if instance.total_cost_usd > 10.0:
        cost_color = "yellow"
    if instance.total_cost_usd > 50.0:
        cost_color = "red"
    text.append(f"${instance.total_cost_usd:.2f}\n", style=cost_color)

    # Cost per task
    if instance.tasks_completed > 0:
        cost_per_task = instance.total_cost_usd / instance.tasks_completed
        text.append("  Cost/Task: ", style="dim")
        text.append(f"${cost_per_task:.3f}\n", style="cyan")

    # Cost per hour
    if instance.started_at:
        hours_running = max(0.1, (datetime.now() - instance.started_at).total_seconds() / 3600)
        cost_per_hour = instance.total_cost_usd / hours_running
        text.append("  Cost/Hour: ", style="dim")
        text.append(f"${cost_per_hour:.2f}\n", style="cyan")

    # Token usage summary
    text.append("  Tokens: ", style="dim")
    text.append(f"{instance.total_input_tokens:,} in / {instance.total_output_tokens:,} out\n", style="dim")

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
    """Create errors tab content for instance detail view.

    Shows two sections:
    1. Claude Code Errors: Permission denials, API errors, tool failures (from status file)
    2. Failed Tasks: Task-level errors with retry information
    """
    text = Text()

    # Get selected instance
    instance = None
    if state.instances and state.selected_instance_idx < len(state.instances):
        instance = state.instances[state.selected_instance_idx]

    # Section 1: Claude Code Errors (from error_info in status)
    if instance:
        error_summary = get_error_summary(instance.ralph_id)
        if error_summary["total_errors"] > 0:
            text.append("Claude Code Errors\n", style="bold cyan")
            text.append("\u2500" * 40 + "\n", style="dim")

            # Error category icons
            error_icons = {
                "permission": (ICON_ERROR_PERMISSION, "magenta"),
                "api_error": (ICON_ERROR_API, "red"),
                "tool_failure": (ICON_ERROR_TOOL, "yellow"),
            }

            # Show counts by category
            for category, count in error_summary["by_category"].items():
                icon, color = error_icons.get(category, (ICON_ERROR_GENERAL, "orange1"))
                text.append(f"  {icon} ", style=color)
                text.append(f"{category}: ", style="white")
                text.append(f"{count}\n", style=f"bold {color}")

            # Show critical warning if applicable
            if error_summary["has_critical"]:
                text.append("\n")
                text.append(f"  {ICON_STALE} ", style="yellow")
                text.append("Critical errors detected - may need user attention\n", style="yellow")

            # Show recent error messages
            if error_summary["recent_errors"]:
                text.append("\nRecent Errors:\n", style="dim")
                for err in error_summary["recent_errors"][:3]:
                    timestamp = err.get("timestamp", "")
                    msg = err.get("message", "")[:60]
                    if timestamp:
                        text.append(f"  [{timestamp}] ", style="dim")
                    text.append(f"{msg}\n", style="white")

            text.append("\n")

    # Section 2: Failed Tasks
    if not state.instance_failed_tasks:
        if not instance or get_error_summary(instance.ralph_id)["total_errors"] == 0:
            text.append("No errors for this instance", style="dim")
        else:
            text.append("No failed tasks for this instance", style="dim")
        return text

    text.append("Failed Tasks ", style="bold red")
    text.append("(Enter for full error)\n", style="dim")
    text.append("\u2500" * 40 + "\n", style="dim")

    # Scrollable list with selection
    visible_start = state.instance_error_scroll
    visible_end = visible_start + 8  # Reduced to make room for Claude errors
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
                ErrorCategory.API_ERROR: "red",
                ErrorCategory.TOOL_FAILURE: "yellow",
                ErrorCategory.UNKNOWN: "dim",
            }
            cat_style = cat_styles.get(task.error_category, "white")
            text.append(f"    [{task.error_category.value}]", style=cat_style)
            text.append(f" retry {task.retry_count}/{task.max_retries}\n", style="dim")

    # Scroll hint
    if len(state.instance_failed_tasks) > 8:
        remaining = len(state.instance_failed_tasks) - visible_end
        if remaining > 0:
            text.append(f"\n... {remaining} more (j/k to scroll)\n", style="dim")

    return text


def create_instance_error_detail_overlay(state: TUIState) -> Panel:
    """Create full error message overlay for selected error."""
    text = Text()

    if not state.instance_failed_tasks or state.instance_selected_error_idx >= len(state.instance_failed_tasks):
        text.append("No error selected", style="dim")
        return Panel(text, title="Error Detail", border_style=BORDER_ERROR)

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

    return Panel(text, title="Error Detail", border_style=BORDER_ERROR, box=box.DOUBLE, padding=(1, 2))


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

    return Panel(full_content, title=f"Instance: {id_display[:20]}", border_style=BORDER_INSTANCES, box=box.DOUBLE, padding=(1, 2))
