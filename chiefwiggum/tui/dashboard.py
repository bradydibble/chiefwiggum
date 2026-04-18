"""Dashboard update/orchestration functions for the TUI.

These functions coordinate panel rendering and layout updates.
- update_display_only: Fast display-only refresh using cached data (no DB queries).
- update_dashboard: Full refresh with DB queries, data processing, and layout updates.
"""

import asyncio
import logging
import time
from datetime import datetime

from rich import box
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

from chiefwiggum import (
    check_ralph_completions,
    get_system_stats,
    list_all_instances,
    list_all_tasks,
    list_task_history,
    mark_stale_instances_crashed,
    process_retry_tasks,
)
from chiefwiggum.coordination import (
    get_all_instance_progress_cached,
    list_graded_tasks,
)
from chiefwiggum.icons import (
    BG_HEADER,
    BORDER_INSTANCES,
    BORDER_OVERLAY,
    BORDER_TASKS,
    COLOR_ACCENT,
    COLOR_MUTED,
    COLOR_SUCCESS,
    COLOR_WARNING,
)
from chiefwiggum.models import (
    RalphInstanceStatus,
    TaskClaimStatus,
    TaskPriority,
    TaskSortOrder,
)
from chiefwiggum.spawner import (
    cleanup_dead_ralphs,
    get_running_ralphs,
    read_ralph_log,
)
from chiefwiggum.tui.helpers import compute_data_hash
from chiefwiggum.tui.instance_detail import (
    create_instance_detail_panel,
    create_instance_error_detail_overlay,
)
from chiefwiggum.tui.panels import (
    create_alerts_panel,
    create_bulk_action_panel,
    create_cleanup_panel,
    create_command_bar,
    create_confirm_panel,
    create_error_detail_panel,
    create_graded_tasks_table,
    create_help_panel,
    create_history_panel,
    create_instances_table,
    create_log_stream_panel,
    create_log_view_panel,
    create_reconcile_panel,
    create_search_panel,
    create_settings_panel,
    create_spawn_panel,
    create_stats_panel,
    create_task_detail_panel,
    create_tasks_table,
    generate_alerts,
)
from chiefwiggum.tui.state import (
    InstanceDetailTab,
    TUIMode,
    TUIState,
    ViewFocus,
)

logger = logging.getLogger(__name__)


async def update_display_only(layout: Layout, state: TUIState) -> None:
    """Fast display update for navigation - uses cached data, no DB queries or I/O.

    This is a lightweight alternative to update_dashboard() for simple navigation
    operations (j/k/z keys). It skips expensive operations and just re-renders
    the view with existing cached data.

    Use this for:
    - Navigation keys (j/k/z)
    - Simple state changes that don't require fresh data

    Use update_dashboard() for:
    - Initial render
    - Manual refresh (r key)
    - Commands that modify state (spawn, stop, etc.)
    - Periodic background refresh (every 2s)
    """
    # Get cached progress data (2s TTL, very cheap)
    progress_data = get_all_instance_progress_cached()

    # Use existing state data (already fetched by last update_dashboard call)
    instances = state.instances or []
    tasks = state.all_tasks_cache or []

    # Update only the main content panels with current selection
    if state.view_focus == ViewFocus.TASKS:
        # Tasks only - full width
        show_task_numbers = not state.bulk_mode_active
        layout["main"].update(
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
                border_style=BORDER_TASKS,
            )
        )
    elif state.view_focus == ViewFocus.INSTANCES:
        # Instances only - full width
        layout["main"].update(
            Panel(
                create_instances_table(
                    instances,
                    state.show_all_instances,
                    selected_idx=state.selected_instance_idx,
                    progress_data=progress_data
                ),
                border_style=BORDER_INSTANCES
            )
        )
    else:
        # Both - split view (default)
        # Recreate the split layout if it was previously replaced by a single panel
        try:
            # Try to access the sublayouts - if this fails, we need to recreate them
            _ = layout["main"]["instances"]
            _ = layout["main"]["tasks"]
        except KeyError:
            # Layout was flattened - recreate the split
            layout["main"].split_row(
                Layout(name="instances"),
                Layout(name="tasks"),
            )

        show_task_numbers = not state.bulk_mode_active
        layout["main"]["instances"].update(
            Panel(
                create_instances_table(
                    instances,
                    state.show_all_instances,
                    selected_idx=state.selected_instance_idx,
                    progress_data=progress_data
                ),
                border_style=BORDER_INSTANCES
            )
        )
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
                border_style=BORDER_TASKS,
            )
        )


async def update_dashboard(layout: Layout, state: TUIState) -> None:
    """Update all dashboard components."""
    # Clean up dead/zombie Ralph processes
    cleaned = cleanup_dead_ralphs()
    if cleaned:
        state.status_message = f"Cleaned up {len(cleaned)} dead Ralph(s): {', '.join(r[:12] for r in cleaned)}"
        state.status_message_time = time.time()

    # Check for task completions from Ralph logs FIRST
    # This also updates heartbeats for running Ralphs, preventing false stale detection
    completion_events = await check_ralph_completions()

    # Mark stale instances and process retries AFTER heartbeats are updated
    await mark_stale_instances_crashed()
    await process_retry_tasks()
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
    # Also fetch graded tasks for Ralph Loop Alignment
    from chiefwiggum.coordination import (
        count_pending_intents,
        count_recent_intent_errors,
    )
    from chiefwiggum.daemon import is_daemon_running

    all_instances, all_tasks, graded_tasks, intents, recent_err = await asyncio.gather(
        list_all_instances(),
        list_all_tasks(),
        list_graded_tasks(),
        count_pending_intents(),
        count_recent_intent_errors(),
    )

    # Refresh the daemon snapshot fields on TUIState (rendered by stats panel).
    state.daemon_running, state.daemon_pid = is_daemon_running()
    state.daemon_pending_spawn = intents["spawn"]
    state.daemon_pending_cancel = intents["cancel"]
    state.daemon_recent_errors = recent_err

    # Store graded tasks in state
    state.graded_tasks = graded_tasks

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

    # Generate alerts from current state
    state.alerts = generate_alerts(state)

    # Get progress data for all running instances (cached for performance)
    progress_data = get_all_instance_progress_cached()

    # Compute base data hashes for dirty-bit detection
    instances_base_hash = compute_data_hash(all_instances)
    tasks_base_hash = compute_data_hash(all_tasks)
    alerts_hash = compute_data_hash(state.alerts)

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

    # Compute display hashes including all state that affects rendering
    # NOTE: Deliberately exclude selected_instance_idx and selected_task_idx to avoid
    # full table rebuilds on navigation. Selection highlighting is handled separately.
    instances_display_hash = f"{instances_base_hash}:{state.show_all_instances}:{state.project_filter}"
    tasks_display_hash = f"{tasks_base_hash}:{state.show_all_tasks}:{state.project_filter}:{state.sort_order}:{state.task_scroll_offset}:{state.bulk_mode_active}:{len(state.selected_task_ids)}"

    # Check if display state actually changed
    alerts_changed = alerts_hash != state.render_state.previous_alerts_hash

    # Update stored hashes
    state.render_state.previous_instances_hash = instances_display_hash
    state.render_state.previous_tasks_hash = tasks_display_hash
    state.render_state.previous_alerts_hash = alerts_hash

    # Update header with branding (only when state changes)
    running_count = len(get_running_ralphs())

    # Count actively working instances for contextual spinner
    active_working = sum(
        1 for i in state.all_instances
        if i.status == RalphInstanceStatus.ACTIVE and i.current_task_id
    )

    # Compute header hash (excluding timestamp to reduce flicker)
    header_state = f"{running_count}:{active_working}"

    if header_state != state.render_state.previous_header_hash:
        # Build header text with branding and version (with subtle background)
        from chiefwiggum._version import __version__
        header_text = Text()
        header_text.append(" ", style=f"on {BG_HEADER}")
        header_text.append(" CHIEF", style=f"bold {COLOR_ACCENT} on {BG_HEADER}")
        header_text.append("WIGGUM ", style=f"bold white on {BG_HEADER}")
        header_text.append(f"v{__version__} ", style=f"{COLOR_MUTED} on {BG_HEADER}")

        # Daemon count with icon and static status indicator
        if running_count > 0:
            # Use static icon based on activity
            if active_working > 0:
                header_text.append(" ⚡ ", style=f"bold {COLOR_SUCCESS} on {BG_HEADER}")
            else:
                header_text.append(" ● ", style=f"grey50 on {BG_HEADER}")
            header_text.append(f"{ICON_DAEMON} {running_count}", style=f"bold {COLOR_SUCCESS} on {BG_HEADER}")
        else:
            header_text.append(f"{ICON_DAEMON} 0", style=f"dim on {BG_HEADER}")
        header_text.append(" daemons ", style=f"dim on {BG_HEADER}")

        header = Panel(
            header_text,
            style=COLOR_ACCENT,
            box=box.ROUNDED,
        )
        layout["header"].update(header)
        state.render_state.previous_header_hash = header_state

    # Update stats (reuse all_tasks fetched at start) - only if data changed
    # Compute stats hash
    stats_summary = f"{running_count}:{len(state.all_instances)}:{len(all_tasks)}"
    stats_hash = compute_data_hash(stats_summary)

    if stats_hash != state.render_state.previous_stats_hash:
        layout["stats"].update(create_stats_panel(state.all_instances, all_tasks, state))
        state.render_state.previous_stats_hash = stats_hash

    # Update alerts panel if it exists in layout and there are alerts - only if changed
    if alerts_changed:
        alerts_panel = create_alerts_panel(state)
        try:
            if alerts_panel:
                layout["alerts"].update(alerts_panel)
            else:
                # No alerts - show an empty panel
                layout["alerts"].update(Panel("", height=3, border_style="dim"))
        except KeyError:
            logger.debug("Layout does not have an alerts section")

    # Determine required layout structure based on mode
    if state.mode == TUIMode.HELP:
        required_structure = "help"
    elif state.mode == TUIMode.GRADED_TASKS:
        required_structure = "graded_tasks"
    elif state.mode == TUIMode.STATS:
        required_structure = "stats"
    elif state.mode == TUIMode.ERROR_DETAIL:
        required_structure = "error_detail"
    elif state.mode in (TUIMode.SPAWN_PROJECT, TUIMode.SPAWN_PRIORITY, TUIMode.SPAWN_CATEGORY, TUIMode.SPAWN_MODEL, TUIMode.SPAWN_SESSION, TUIMode.SPAWN_CONFIRM):
        required_structure = "spawn"
    elif state.mode == TUIMode.LOG_VIEW:
        required_structure = "log_view"
    elif state.mode == TUIMode.HISTORY:
        required_structure = "history"
    elif state.mode == TUIMode.CONFIRM_BULK_STOP:
        required_structure = "confirm_bulk_stop"
    elif state.mode == TUIMode.CONFIRM_BULK_PAUSE:
        required_structure = "confirm_bulk_pause"
    elif state.mode == TUIMode.RECONCILE:
        required_structure = "reconcile"
    elif state.mode == TUIMode.CLEANUP_CONFIRM:
        required_structure = "cleanup"
    elif state.mode in (TUIMode.SETTINGS, TUIMode.SETTINGS_EDIT_API_KEY, TUIMode.SETTINGS_EDIT_MAX_RALPHS,
                        TUIMode.SETTINGS_EDIT_MODEL, TUIMode.SETTINGS_EDIT_TIMEOUT,
                        TUIMode.SETTINGS_EDIT_PERMISSIONS, TUIMode.SETTINGS_EDIT_STRATEGY,
                        TUIMode.SETTINGS_EDIT_AUTO_SPAWN, TUIMode.SETTINGS_EDIT_RALPH_LOOP):
        required_structure = "settings"
    elif state.mode == TUIMode.SEARCH:
        required_structure = "search"
    elif state.mode == TUIMode.TASK_DETAIL:
        required_structure = "task_detail"
    elif state.mode == TUIMode.BULK_ACTION:
        required_structure = "bulk_action"
    elif state.mode == TUIMode.LOG_STREAM:
        required_structure = "log_stream"
    elif state.mode == TUIMode.INSTANCE_DETAIL:
        required_structure = "instance_detail"
    elif state.mode == TUIMode.INSTANCE_ERROR_DETAIL:
        required_structure = "instance_error_detail"
    elif state.view_focus == ViewFocus.TASKS:
        required_structure = "tasks_only"
    elif state.view_focus == ViewFocus.INSTANCES:
        required_structure = "instances_only"
    else:
        required_structure = "split"

    # Only rebuild layout structure if it changed
    if state.cached_layout_structure != required_structure:
        layout["main"].unsplit()
        # Create split structure immediately if needed
        if required_structure == "split":
            layout["main"].split_row(
                Layout(name="instances"),
                Layout(name="tasks"),
            )
        state.cached_layout_structure = required_structure

    # Check for overlay modes
    if state.mode == TUIMode.HELP:
        # Calculate visible lines (main area ~height - header - stats - command_bar - padding)
        visible_lines = max(10, state.console_width // 4)  # Rough estimate based on width
        layout["main"].update(create_help_panel(state.help_scroll_offset, visible_lines))
    elif state.mode == TUIMode.STATS:
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
        if stats.archived_tasks > 0:
            text.append(f"  Archived:    {stats.archived_tasks}\n", style="dim")

        text.append("\nPerformance\n", style="bold yellow")
        text.append(f"  Tasks/Hour:  {stats.tasks_per_hour:.1f}\n")
        if stats.eta_minutes:
            if stats.eta_minutes < 60:
                text.append(f"  ETA:         {stats.eta_minutes:.0f} minutes\n", style="green")
            else:
                text.append(f"  ETA:         {stats.eta_minutes/60:.1f} hours\n", style="yellow")
        else:
            text.append("  ETA:         Unknown\n", style="dim")

        text.append("\nInstances\n", style="bold yellow")
        text.append(f"  Active:      {stats.active_instances}\n", style="green")
        text.append(f"  Idle/Paused: {stats.idle_instances}\n", style="yellow")

        if stats.session_start:
            duration = datetime.now() - stats.session_start
            hours = int(duration.total_seconds() // 3600)
            minutes = int((duration.total_seconds() % 3600) // 60)
            text.append(f"\nSession:       {hours}h {minutes}m\n", style="dim")

        text.append("\n\nPress any key to close", style="dim")
        layout["main"].update(Panel(text, title="Statistics", border_style=BORDER_OVERLAY, box=box.DOUBLE, padding=(1, 2)))

    elif state.mode == TUIMode.GRADED_TASKS:
        # Show graded tasks queue with grade distribution
        graded_table = create_graded_tasks_table(
            state.graded_tasks,
            offset=state.task_scroll_offset,
            limit=state.tasks_per_page,
            selected_idx=state.selected_graded_task_idx,
            expanded=(state.view_focus == ViewFocus.TASKS),
        )

        # Add grade summary panel
        grade_summary = Text()
        grade_summary.append("Grade Distribution\n\n", style="bold cyan")

        grade_counts = {"A": 0, "B": 0, "C": 0, "F": 0, "?": 0}
        from chiefwiggum.prompt_grader import get_grade_letter
        for task in state.graded_tasks:
            grade = task.get("grade")
            letter = get_grade_letter(grade) if grade is not None else "?"
            grade_counts[letter] = grade_counts.get(letter, 0) + 1

        grade_summary.append(f"[bold green]A (90-100):[/bold green] {grade_counts['A']} tasks (auto-spawn)\n")
        grade_summary.append(f"[yellow]B (70-89):[/yellow] {grade_counts['B']} tasks (auto-spawn)\n")
        grade_summary.append(f"[bold {COLOR_WARNING}]C (50-69):[/bold {COLOR_WARNING}] {grade_counts['C']} tasks (needs review)\n")
        grade_summary.append(f"[bold red]F (<50):[/bold red] {grade_counts['F']} tasks (blocked)\n")
        if grade_counts["?"]:
            grade_summary.append(f"[dim]? (ungraded):[/dim] {grade_counts['?']} tasks\n")

        grade_summary.append("\n[dim]Enter: View details  |  ESC: Back  |  g: Toggle view[/dim]")

        summary_panel = Panel(grade_summary, title="Grade Summary", border_style="cyan", box=box.ROUNDED, padding=(1, 2))

        # Create layout with table and summary
        layout["main"].split_column(
            Layout(graded_table, name="graded_table", ratio=7),
            Layout(summary_panel, name="grade_summary", ratio=3),
        )

    elif state.mode == TUIMode.ERROR_DETAIL:
        task = state.failed_tasks[state.selected_task_idx] if state.failed_tasks else None
        layout["main"].update(create_error_detail_panel(task, state))

    elif state.mode in (TUIMode.SPAWN_PROJECT, TUIMode.SPAWN_PRIORITY, TUIMode.SPAWN_CATEGORY, TUIMode.SPAWN_MODEL, TUIMode.SPAWN_SESSION, TUIMode.SPAWN_CONFIRM):
        layout["main"].update(create_spawn_panel(state))

    elif state.mode == TUIMode.LOG_VIEW:
        layout["main"].update(create_log_view_panel(state))

    elif state.mode == TUIMode.HISTORY:
        # Load history data
        state.history_tasks = await list_task_history(project=state.project_filter, limit=50)
        layout["main"].update(create_history_panel(state))

    elif state.mode == TUIMode.CONFIRM_BULK_STOP:
        count = len(state.instances)
        layout["main"].update(create_confirm_panel("STOP ALL Ralphs", count))

    elif state.mode == TUIMode.CONFIRM_BULK_PAUSE:
        count = len([i for i in state.instances if i.status == RalphInstanceStatus.ACTIVE])
        layout["main"].update(create_confirm_panel("PAUSE ALL Ralphs", count))

    elif state.mode == TUIMode.RECONCILE:
        layout["main"].update(create_reconcile_panel(state.reconcile_result))

    elif state.mode == TUIMode.CLEANUP_CONFIRM:
        layout["main"].update(await create_cleanup_panel())

    elif state.mode in (TUIMode.SETTINGS, TUIMode.SETTINGS_EDIT_API_KEY, TUIMode.SETTINGS_EDIT_MAX_RALPHS,
                        TUIMode.SETTINGS_EDIT_MODEL, TUIMode.SETTINGS_EDIT_TIMEOUT,
                        TUIMode.SETTINGS_EDIT_PERMISSIONS, TUIMode.SETTINGS_EDIT_STRATEGY,
                        TUIMode.SETTINGS_EDIT_AUTO_SPAWN, TUIMode.SETTINGS_EDIT_RALPH_LOOP):
        layout["main"].update(create_settings_panel(state))

    elif state.mode == TUIMode.SEARCH:
        layout["main"].update(create_search_panel(state))

    elif state.mode == TUIMode.TASK_DETAIL:
        layout["main"].update(create_task_detail_panel(state.selected_task, state))

    elif state.mode == TUIMode.BULK_ACTION:
        layout["main"].update(create_bulk_action_panel(state))

    elif state.mode == TUIMode.LOG_STREAM:
        # Refresh log content for streaming
        if state.instances and state.selected_instance_idx < len(state.instances):
            ralph_id = state.instances[state.selected_instance_idx].ralph_id
            state.log_content = read_ralph_log(ralph_id, 100)
        layout["main"].update(create_log_stream_panel(state))

    elif state.mode == TUIMode.INSTANCE_DETAIL:
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

        if state.view_focus == ViewFocus.TASKS:
            # Tasks only - full width
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
                    border_style=BORDER_TASKS,
                )
            )
        elif state.view_focus == ViewFocus.INSTANCES:
            # Instances only - full width
            layout["main"].update(
                Panel(create_instances_table(instances, state.show_all_instances, selected_idx=state.selected_instance_idx, progress_data=progress_data), border_style=BORDER_INSTANCES)
            )
        else:
            # Both - split view (default)
            # Recreate the split layout if it was previously replaced by a single panel
            try:
                # Try to access the sublayouts - if this fails, we need to recreate them
                _ = layout["main"]["instances"]
                _ = layout["main"]["tasks"]
            except KeyError:
                # Layout was flattened - recreate the split
                layout["main"].split_row(
                    Layout(name="instances"),
                    Layout(name="tasks"),
                )

            # Always update split view panels (main content area)
            # The dirty checking is more effective for header/stats/alerts which update less frequently
            layout["main"]["instances"].update(
                Panel(create_instances_table(instances, state.show_all_instances,
                      selected_idx=state.selected_instance_idx, progress_data=progress_data),
                      border_style=BORDER_INSTANCES)
            )
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
                    border_style=BORDER_TASKS,
                )
            )

    # Update command bar (only when relevant state changes)
    command_bar_state = f"{state.mode}:{state.status_message}:{state.view_focus}"

    if command_bar_state != state.render_state.previous_command_bar_hash:
        layout["command_bar"].update(create_command_bar(state, state.console_width))
        state.render_state.previous_command_bar_hash = command_bar_state
