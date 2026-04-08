"""TUI application entry point -- runs the main dashboard loop."""

import asyncio
import os
import time
from datetime import datetime

from rich.console import Console
from rich.live import Live

from chiefwiggum.config import get_config_value, get_view_state, load_config_on_startup
from chiefwiggum.keyboard import KeyboardListener
from chiefwiggum.models import TaskCategory, TaskSortOrder
from chiefwiggum.spawner import get_running_ralphs
from chiefwiggum.tui.state import TUIMode, TUIState, ViewFocus
from chiefwiggum.tui.panels import create_layout
from chiefwiggum.tui.dashboard import update_dashboard, update_display_only
from chiefwiggum.tui.handlers import handle_command


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
    layout = create_layout(has_alerts=True)  # Always include alerts row
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

        with Live(layout, console=console, refresh_per_second=4, screen=True) as live:
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

                    # Fast path for navigation keys - use cached data, no DB queries
                    # This makes j/k/z navigation feel instant (<10ms instead of 200-500ms)
                    is_navigation_key = (
                        state.mode == TUIMode.NORMAL and
                        key in ("j", "k", "z")
                    )

                    if is_navigation_key:
                        # Fast display-only update (no DB queries, uses cached data)
                        loop.run_until_complete(update_display_only(layout, state))
                    else:
                        # Full update with data refresh (DB queries, cleanup, etc.)
                        loop.run_until_complete(update_dashboard(layout, state))

                # Refresh data every 2 seconds
                current_time = time.time()
                if current_time - last_data_refresh >= 2:
                    # Update console width for responsive layout (handles terminal resize)
                    state.console_width = console.width
                    loop.run_until_complete(update_dashboard(layout, state))
                    last_data_refresh = current_time

                # Small sleep to prevent busy-waiting (10 Hz, matches keyboard listener)
                time.sleep(0.1)

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
