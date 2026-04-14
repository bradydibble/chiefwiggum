"""ChiefWiggum TUI Dashboard - package init with backward-compatible re-exports.

This package replaces the monolithic tui.py module. All public symbols are
re-exported here to maintain backward compatibility with existing imports.
"""

# State classes and enums
# Main entry point
from chiefwiggum.tui.app import run_tui

# Dashboard update functions
from chiefwiggum.tui.dashboard import (
    update_dashboard,
    update_display_only,
)

# Input handlers
from chiefwiggum.tui.handlers import (
    handle_bulk_operations,
    handle_bulk_task_action,
    handle_command,
    handle_instance_detail,
    handle_normal_mode,
    handle_project_filter,
    handle_release,
    handle_search,
    handle_settings,
    handle_shutdown,
    handle_spawn,
)

# Helper functions
from chiefwiggum.tui.helpers import (
    auto_save_view_state,
    compute_data_hash,
    create_progress_bar,
    discover_fix_plan_projects,
    format_age,
    get_current_project,
    invalidate_error_indicator_cache,
)

# Instance detail panel functions
from chiefwiggum.tui.instance_detail import (
    create_instance_dashboard_content,
    create_instance_detail_panel,
    create_instance_error_detail_overlay,
    create_instance_errors_content,
    create_instance_history_content,
    create_instance_logs_content,
    create_instance_tab_bar,
)

# Panel render functions
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
    create_layout,
    create_log_stream_panel,
    create_log_view_panel,
    create_reconcile_panel,
    create_search_panel,
    create_settings_panel,
    create_spawn_panel,
    create_stats_panel,
    create_stats_view_panel,
    create_task_detail_panel,
    create_tasks_table,
    generate_alerts,
    get_help_lines,
)
from chiefwiggum.tui.state import (
    Alert,
    AlertType,
    InstanceDetailTab,
    RenderState,
    SettingsSection,
    SpawnConfig,
    TUIMode,
    TUIState,
    ViewFocus,
)

__all__ = [
    # State
    "Alert",
    "AlertType",
    "InstanceDetailTab",
    "RenderState",
    "SettingsSection",
    "SpawnConfig",
    "TUIMode",
    "TUIState",
    "ViewFocus",
    # Helpers
    "auto_save_view_state",
    "compute_data_hash",
    "create_progress_bar",
    "discover_fix_plan_projects",
    "format_age",
    "get_current_project",
    "invalidate_error_indicator_cache",
    # Panels
    "create_alerts_panel",
    "create_bulk_action_panel",
    "create_cleanup_panel",
    "create_command_bar",
    "create_confirm_panel",
    "create_error_detail_panel",
    "create_graded_tasks_table",
    "create_help_panel",
    "create_history_panel",
    "create_instances_table",
    "create_instance_dashboard_content",
    "create_instance_detail_panel",
    "create_instance_error_detail_overlay",
    "create_instance_errors_content",
    "create_instance_history_content",
    "create_instance_logs_content",
    "create_instance_tab_bar",
    "create_layout",
    "create_log_stream_panel",
    "create_log_view_panel",
    "create_reconcile_panel",
    "create_search_panel",
    "create_settings_panel",
    "create_spawn_panel",
    "create_stats_panel",
    "create_stats_view_panel",
    "create_task_detail_panel",
    "create_tasks_table",
    "generate_alerts",
    "get_help_lines",
    # Handlers
    "handle_bulk_operations",
    "handle_bulk_task_action",
    "handle_command",
    "handle_instance_detail",
    "handle_normal_mode",
    "handle_project_filter",
    "handle_release",
    "handle_search",
    "handle_settings",
    "handle_shutdown",
    "handle_spawn",
    # Dashboard
    "update_dashboard",
    "update_display_only",
    # App
    "run_tui",
]
