"""TUI input handler functions.

Extracted from chiefwiggum.tui to support modular TUI architecture.
All key-press handling logic for the TUI dashboard lives here.
"""

import logging
import time
from datetime import datetime
from pathlib import Path

from chiefwiggum import (
    list_all_tasks,
    pause_all_instances,
    pause_instance,
    release_claim,
    resume_all_instances,
    resume_instance,
    shutdown_instance,
    stop_all_instances,
    sync_tasks_from_fix_plan,
)
from chiefwiggum.config import (
    get_api_key_source,
    get_auto_scaling_config,
    get_config_value,
    get_default_timeout,
    get_max_ralphs,
    get_quickstart_defaults,
    get_ralph_loop_settings,
    get_ralph_permissions,
    save_view_state,
    set_api_key,
    set_auto_scaling_config,
    set_config_value,
    set_default_model,
    set_max_ralphs,
    set_ralph_loop_setting,
    set_ralph_permission,
    set_task_assignment_strategy,
)
from chiefwiggum.models import (
    ClaudeModel,
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
    read_ralph_log,
    spawn_ralph_with_task_claim,
    stop_all_ralph_daemons,
    stop_ralph_daemon,
)
from chiefwiggum.tui.helpers import (
    auto_save_view_state,
    discover_fix_plan_projects,
    get_current_project,
)
from chiefwiggum.tui.panels import get_help_lines
from chiefwiggum.tui.state import (
    InstanceDetailTab,
    SpawnConfig,
    TUIMode,
    TUIState,
    ViewFocus,
)

logger = logging.getLogger(__name__)


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
    elif key == "R":  # Shift+R: Reconcile completed tasks
        state.mode = TUIMode.RECONCILE
        state.status_message = "Starting reconciliation..."
        state.status_message_time = time.time()
        return False  # Will be handled in handle_command (async)
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
    elif key == "g":  # Toggle graded tasks view (Ralph Loop Alignment)
        state.mode = TUIMode.GRADED_TASKS
        state.selected_graded_task_idx = 0
        state.task_scroll_offset = 0
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

            # Route through the chiefwiggum daemon when it's running: the TUI
            # inserts a spawn_request row, the daemon executes it on its next
            # tick. The TUI can then die freely without losing the spawn. Fall
            # back to direct spawn when the daemon is down so the TUI still
            # works in bare-metal operation.
            from chiefwiggum.coordination import enqueue_spawn_request
            from chiefwiggum.daemon import is_daemon_running

            daemon_running, daemon_pid = is_daemon_running()
            if daemon_running:
                req_id = await enqueue_spawn_request(
                    project_path=config.project,
                    fix_plan_path=config.fix_plan_path,
                    priority=0,
                    requested_by="tui",
                    config_json=ralph_config.model_dump_json(),
                    targeting_json=targeting.model_dump_json(),
                )
                state.status_message = (
                    f"Spawn requested (id={req_id}); daemon pid={daemon_pid} will execute on next tick"
                )
            else:
                ralph_id = generate_ralph_id(config.project[:8])
                success, message, task_id = await spawn_ralph_with_task_claim(
                    ralph_id=ralph_id,
                    project=config.project,
                    fix_plan_path=config.fix_plan_path,
                    config=ralph_config,
                    targeting=targeting,
                )
                state.status_message = (
                    f"{message} (daemon not running — spawned directly; "
                    "run `wig service install` for walk-away reliability)"
                )

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
        # Note: Search executes on Enter to avoid DB query on every keystroke
    elif len(key) == 1 and key.isprintable():  # Regular character
        state.search_query += key
        # Note: Search executes on Enter to avoid DB query on every keystroke


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
    from chiefwiggum import list_all_tasks, release_claim
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
                except Exception as e:
                    logger.warning(f"Failed to release task {task_id}: {e}")
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
        from chiefwiggum.spawner import handle_stuck_ralph, is_ralph_stuck

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

        # Handle 'R' (Shift+R) for reconcile completed tasks
        if key == "R":
            from chiefwiggum.coordination import reconcile_completed_tasks

            state.status_message = "Reconciling completed tasks..."
            state.status_message_time = time.time()

            # Run reconciliation with project filter if set
            result = await reconcile_completed_tasks(
                project=state.project_filter,
                dry_run=False
            )

            state.reconcile_result = result
            state.status_message = (
                f"Reconciled: {result['updated']} updated, "
                f"{result['skipped']} skipped, {result['failed']} failed"
            )
            state.status_message_time = time.time()
            # Keep mode as RECONCILE to display the results panel
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

            ralph_config = RalphConfig(model=model, timeout_minutes=timeout)
            targeting = TargetingConfig(
                project=project,
                priority_min=None,  # All priorities
                categories=[],  # All categories
            )

            # Prefer the daemon route for the same durability reasons as the
            # wizard spawn. Falls back to direct spawning when the daemon is
            # not running.
            from chiefwiggum.coordination import enqueue_spawn_request
            from chiefwiggum.daemon import is_daemon_running

            daemon_running, _ = is_daemon_running()
            if daemon_running:
                req_id = await enqueue_spawn_request(
                    project_path=project,
                    fix_plan_path=str(fix_plan_path),
                    priority=0,
                    requested_by="tui-quickstart",
                    config_json=ralph_config.model_dump_json(),
                    targeting_json=targeting.model_dump_json(),
                )
                state.status_message = f"Quickstart: spawn requested (id={req_id}) for {project}"
            else:
                ralph_id = generate_ralph_id(project[:8])
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

    elif state.mode == TUIMode.RECONCILE:
        if key in ("q", "ESCAPE"):  # q or Esc to close
            state.mode = TUIMode.NORMAL
            state.reconcile_result = None  # Clear results
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

    elif state.mode == TUIMode.GRADED_TASKS:
        # Handle graded tasks view navigation
        if key in ("g", "q", "ESCAPE"):  # g toggle, q, or Esc
            state.mode = TUIMode.NORMAL
            state.selected_graded_task_idx = 0
            state.task_scroll_offset = 0
        elif key == "j":  # Move selection down
            if state.graded_tasks:
                state.selected_graded_task_idx = min(
                    state.selected_graded_task_idx + 1,
                    len(state.graded_tasks) - 1
                )
                # Auto-scroll if selection goes off-screen
                if state.selected_graded_task_idx >= state.task_scroll_offset + state.tasks_per_page:
                    state.task_scroll_offset = state.selected_graded_task_idx - state.tasks_per_page + 1
        elif key == "k":  # Move selection up
            if state.graded_tasks:
                state.selected_graded_task_idx = max(0, state.selected_graded_task_idx - 1)
                # Auto-scroll if selection goes off-screen
                if state.selected_graded_task_idx < state.task_scroll_offset:
                    state.task_scroll_offset = state.selected_graded_task_idx
        elif key in ("\r", "\n"):  # Enter - view task detail
            if state.graded_tasks and 0 <= state.selected_graded_task_idx < len(state.graded_tasks):
                # Switch to TASK_DETAIL mode for the selected graded task
                # Store the selected task in state
                task = state.graded_tasks[state.selected_graded_task_idx]
                # Convert dict to a simple object for compatibility
                from types import SimpleNamespace
                state.selected_task = SimpleNamespace(**task)
                state.mode = TUIMode.TASK_DETAIL
        # Other keys ignored
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
