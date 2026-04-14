"""ChiefWiggum - Multi-Ralph Coordination System

A standalone coordination product that orchestrates multiple Ralph (Claude Code)
instances working on the same codebase.

Usage as a library:
    from chiefwiggum import claim_task, register_ralph_instance, complete_task

Usage as a CLI:
    chiefwiggum status
    chiefwiggum tui
    chiefwiggum register --name my-ralph
    chiefwiggum claim my-project
"""

from chiefwiggum._version import __version__
from chiefwiggum.coordination import (
    CLAIM_EXPIRY_MINUTES,
    HEARTBEAT_STALE_MINUTES,
    archive_task,
    check_ralph_completions,
    # Task operations
    claim_task,
    # Error handling (US5, US6)
    classify_error,
    # Cleanup
    cleanup_instance_files,
    complete_and_claim_next,
    complete_task,
    export_task_history_csv,
    extend_claim,
    fail_task,
    fail_task_with_retry,
    # Query operations
    get_ralph_instance,
    # Statistics (US11)
    get_system_stats,
    get_task_claim,
    heartbeat,
    # Task targeting (US4)
    infer_task_category,
    list_active_instances,
    list_all_instances,
    list_all_tasks,
    list_failed_tasks,
    list_fix_plan_sources,
    list_in_progress_tasks,
    list_pending_tasks,
    list_stopped_instances,
    list_task_history,
    mark_stale_instances_crashed,
    # Parser
    parse_fix_plan,
    pause_all_instances,
    # Pause/Resume (US10)
    pause_instance,
    process_retry_tasks,
    # Fix plan sources (US1)
    register_fix_plan_source,
    # Instance operations
    register_ralph_instance,
    register_ralph_instance_with_config,
    release_all_claims_for_instance,
    release_claim,
    resume_all_instances,
    resume_instance,
    safe_git_commit,
    shutdown_instance,
    stop_all_instances,
    sync_tasks_from_fix_plan,
    # Config management (US9)
    update_ralph_config,
    update_ralph_targeting,
    verify_claim_before_commit,
)
from chiefwiggum.database import get_database_path, get_setting, init_db, reset_db, set_setting
from chiefwiggum.git_merge import MergeResult, attempt_merge, detect_conflicts
from chiefwiggum.models import (
    # Enums
    ClaudeModel,
    ErrorCategory,
    # Models
    FixPlanTask,
    RalphConfig,
    RalphInstance,
    RalphInstanceStatus,
    SystemStats,
    TargetingConfig,
    TaskCategory,
    TaskClaim,
    TaskClaimStatus,
    TaskHistory,
    TaskPriority,
)
from chiefwiggum.worktree_manager import (
    cleanup_stale_worktrees,
    cleanup_worktree,
    create_worktree,
    get_worktree_branch_name,
    get_worktree_status,
    list_active_worktrees,
)

__all__ = [
    "__version__",
    # Constants
    "CLAIM_EXPIRY_MINUTES",
    "HEARTBEAT_STALE_MINUTES",
    # Task operations
    "claim_task",
    "complete_task",
    "complete_and_claim_next",
    "archive_task",
    "extend_claim",
    "fail_task",
    "release_claim",
    "sync_tasks_from_fix_plan",
    "verify_claim_before_commit",
    "safe_git_commit",
    "check_ralph_completions",
    # Instance operations
    "register_ralph_instance",
    "register_ralph_instance_with_config",
    "heartbeat",
    "shutdown_instance",
    "mark_stale_instances_crashed",
    # Query operations
    "get_ralph_instance",
    "get_task_claim",
    "list_active_instances",
    "list_all_instances",
    "list_stopped_instances",
    "list_pending_tasks",
    "list_in_progress_tasks",
    "list_all_tasks",
    # Cleanup
    "cleanup_instance_files",
    # Parser
    "parse_fix_plan",
    # Error handling (US5, US6)
    "classify_error",
    "fail_task_with_retry",
    "process_retry_tasks",
    "list_failed_tasks",
    # Pause/Resume (US10)
    "pause_instance",
    "resume_instance",
    "pause_all_instances",
    "resume_all_instances",
    "stop_all_instances",
    "release_all_claims_for_instance",
    # Statistics (US11)
    "get_system_stats",
    "list_task_history",
    "export_task_history_csv",
    # Task targeting (US4)
    "infer_task_category",
    # Config management (US9)
    "update_ralph_config",
    "update_ralph_targeting",
    # Fix plan sources (US1)
    "register_fix_plan_source",
    "list_fix_plan_sources",
    # Worktree management
    "create_worktree",
    "cleanup_worktree",
    "cleanup_stale_worktrees",
    "list_active_worktrees",
    "get_worktree_branch_name",
    "get_worktree_status",
    # Git merge
    "attempt_merge",
    "detect_conflicts",
    "MergeResult",
    # Database
    "init_db",
    "reset_db",
    "get_database_path",
    "get_setting",
    "set_setting",
    # Models - Enums
    "TaskPriority",
    "TaskClaimStatus",
    "RalphInstanceStatus",
    "ErrorCategory",
    "TaskCategory",
    "ClaudeModel",
    # Models - Classes
    "TaskClaim",
    "RalphInstance",
    "FixPlanTask",
    "RalphConfig",
    "TargetingConfig",
    "SystemStats",
    "TaskHistory",
]
