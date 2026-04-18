"""TUI state classes and enums.

Extracted from chiefwiggum.tui to support modular TUI architecture.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Optional

from chiefwiggum.models import ClaudeModel, TaskCategory, TaskPriority, TaskSortOrder


class TUIMode(Enum):
    """TUI interaction modes."""

    NORMAL = auto()
    HELP = auto()
    PROJECT_FILTER = auto()
    SHUTDOWN = auto()
    RELEASE = auto()
    # Removed SYNC mode - 'y' now syncs immediately
    GRADED_TASKS = auto()  # Graded task queue view (Ralph Loop Alignment)
    TASK_DETAIL = auto()  # Task detail view showing prompt and grade reasoning
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
    BULK_SELECT = auto()  # Bulk task selection mode
    BULK_ACTION = auto()  # Bulk action menu
    LOG_STREAM = auto()  # Live log streaming view
    INSTANCE_DETAIL = auto()  # Instance detail drill-down view
    INSTANCE_ERROR_DETAIL = auto()  # Full error message overlay
    CLEANUP_CONFIRM = auto()  # Confirm cleanup of idle ralphs
    RECONCILE = auto()  # Reconcile completed tasks with @fix_plan.md


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


class AlertType(Enum):
    """Types of alerts for the alerts panel."""
    TASK_FAILED = auto()
    INSTANCE_DOWN = auto()
    QUEUE_OVERDUE = auto()
    INSTANCE_STALE = auto()


@dataclass
class Alert:
    """An alert to display in the alerts panel."""
    alert_type: AlertType
    message: str
    created_at: float = field(default_factory=time.time)
    critical: bool = False  # Critical alerts persist until acknowledged
    source_id: str = ""  # Task ID or Ralph ID for deduplication

    def __hash__(self):
        return hash((self.alert_type, self.source_id))

    def __eq__(self, other):
        if not isinstance(other, Alert):
            return False
        return self.alert_type == other.alert_type and self.source_id == other.source_id


@dataclass
class RenderState:
    """State for dirty-bit rendering to reduce flicker."""
    previous_instances_hash: str = ""
    previous_tasks_hash: str = ""
    previous_alerts_hash: str = ""
    previous_stats_hash: str = ""
    previous_header_hash: str = ""
    previous_command_bar_hash: str = ""


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
    # Graded tasks (Ralph Loop Alignment)
    graded_tasks: list = field(default_factory=list)  # Tasks from new graded queue
    selected_graded_task_idx: int = 0  # Selected task in graded view
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
    # Alerts system
    alerts: list = field(default_factory=list)  # List of Alert objects
    alerts_scroll_offset: int = 0  # For scrolling through alerts
    # Reconcile results
    reconcile_result: Optional[dict] = None  # Results from reconcile_completed_tasks()
    # Dirty-bit rendering state
    render_state: RenderState = field(default_factory=RenderState)
    # Layout structure cache
    cached_layout_structure: Optional[str] = None
    # Daemon observability (refreshed in update_dashboard)
    daemon_running: bool = False
    daemon_pid: Optional[int] = None
    daemon_pending_spawn: int = 0
    daemon_pending_cancel: int = 0
    daemon_recent_errors: int = 0
