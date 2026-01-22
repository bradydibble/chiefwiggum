"""ChiefWiggum Pydantic Models

Models for Ralph instances, task claims, and fix plan parsing.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class TaskPriority(str, Enum):
    """Priority of a task from @fix_plan.md."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOWER = "LOWER"
    POLISH = "POLISH"


class TaskClaimStatus(str, Enum):
    """Status of a task claim."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    RELEASED = "released"
    RETRY_PENDING = "retry_pending"  # Waiting for auto-retry


class RalphInstanceStatus(str, Enum):
    """Status of a Ralph instance."""

    ACTIVE = "active"
    IDLE = "idle"
    PAUSED = "paused"  # Temporarily paused via bulk operation
    STOPPED = "stopped"
    CRASHED = "crashed"


class ErrorCategory(str, Enum):
    """Category of task failure for retry decisions."""

    TRANSIENT = "transient"  # Timeouts, rate limits - auto-retry
    CODE_ERROR = "code_error"  # Compilation/runtime errors - manual fix
    PERMISSION = "permission"  # Auth/access issues - manual fix
    CONFLICT = "conflict"  # Git conflicts - manual resolution
    TIMEOUT = "timeout"  # Task took too long - retry with longer timeout
    UNKNOWN = "unknown"  # Unclassified errors
    API_ERROR = "api_error"  # Rate limits, auth failures, API issues
    TOOL_FAILURE = "tool_failure"  # Tool execution failures


class TaskCategory(str, Enum):
    """Category inferred from file paths for task targeting."""

    UX = "ux"  # src/components/**, templates/**, static/**
    API = "api"  # src/api/**, routes/**, endpoints/**
    TESTING = "testing"  # tests/**, *_test.py, test_*.py
    DATABASE = "database"  # migrations/**, models/**, schema/**
    INFRA = "infra"  # scripts/**, docker/**, .github/**
    GENERAL = "general"  # Everything else


class ClaudeModel(str, Enum):
    """Claude model options for Ralph instances."""

    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"


class TaskSortOrder(str, Enum):
    """Sort order for task lists in TUI."""

    PRIORITY = "priority"  # Default: HIGH > MEDIUM > LOWER > POLISH
    STATUS = "status"  # pending > in_progress > failed > completed
    AGE_NEWEST = "age_newest"  # Newest first
    AGE_OLDEST = "age_oldest"  # Oldest first
    PROJECT = "project"  # Alphabetical by project


class TargetingConfig(BaseModel):
    """Task targeting configuration for a Ralph instance."""

    project: str | None = None  # Specific project to work on
    priority_min: TaskPriority | None = None  # Minimum priority (HIGH only, MEDIUM+, etc.)
    task_id: str | None = None  # Target a specific task
    categories: list[TaskCategory] = Field(default_factory=list)  # Task categories to handle


class RalphConfig(BaseModel):
    """Configuration for a Ralph instance."""

    timeout_minutes: int = 30  # Max time per task before killing
    no_continue: bool = True  # Stop after one task by default (vs loop continuously)
    max_loops: int | None = None  # Stop after N tasks completed (None = unlimited)
    model: ClaudeModel = ClaudeModel.SONNET  # Which Claude model to use
    persona: str | None = None  # Skill/persona to load (e.g., "ux-specialist")
    # Ralph Loop Settings (passed to ralph_loop.sh)
    session_expiry_hours: int = 24  # --session-expiry value
    output_format: str = "json"  # --output-format (json/text)
    max_calls_per_hour: int = 100  # --calls value


class TaskClaim(BaseModel):
    """A task claim for multi-Ralph coordination."""

    task_id: str
    task_title: str
    task_priority: TaskPriority
    task_section: str | None = None
    project: str | None = None  # Project this task belongs to
    category: TaskCategory | None = None  # Inferred category from file paths
    claimed_by_ralph_id: str | None = None
    claimed_at: datetime | None = None
    expires_at: datetime | None = None
    status: TaskClaimStatus = TaskClaimStatus.PENDING
    completion_message: str | None = None
    git_commit_sha: str | None = None
    # Error tracking
    error_category: ErrorCategory | None = None
    error_message: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    next_retry_at: datetime | None = None
    # Branch isolation (US7)
    branch_name: str | None = None
    has_conflict: bool = False
    # Timestamps
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime | None = None
    started_at: datetime | None = None  # When work actually started
    completed_at: datetime | None = None  # When task finished
    verified_at: datetime | None = None  # When git commit was verified in target repo


class RalphInstance(BaseModel):
    """A registered Ralph instance for coordination."""

    ralph_id: str
    hostname: str | None = None
    pid: int | None = None
    session_file: str | None = None
    project: str | None = None  # Current project being worked on
    started_at: datetime = Field(default_factory=datetime.now)
    last_heartbeat: datetime = Field(default_factory=datetime.now)
    current_task_id: str | None = None
    loop_count: int = 0
    status: RalphInstanceStatus = RalphInstanceStatus.ACTIVE
    # Configuration (US9)
    config: RalphConfig = Field(default_factory=RalphConfig)
    # Targeting (US4)
    targeting: TargetingConfig = Field(default_factory=TargetingConfig)
    # Statistics (US11)
    tasks_completed: int = 0
    tasks_failed: int = 0
    total_work_seconds: float = 0.0
    # Cost tracking
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    last_cost_update: datetime | None = None


class FixPlanTask(BaseModel):
    """A task parsed from @fix_plan.md."""

    task_id: str
    task_number: int
    title: str
    priority: TaskPriority
    section: str | None = None
    is_complete: bool = False
    subtasks: list[str] = Field(default_factory=list)
    completed_subtasks: list[str] = Field(default_factory=list)
    # File paths for category inference
    file_paths: list[str] = Field(default_factory=list)


class TaskHistory(BaseModel):
    """Historical record of a completed task (US12)."""

    task_id: str
    task_title: str
    ralph_id: str
    project: str | None = None
    started_at: datetime
    completed_at: datetime
    duration_seconds: float
    status: TaskClaimStatus
    commit_sha: str | None = None
    error_message: str | None = None
    # Cost tracking
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    estimated_cost_usd: float = 0.0
    actual_cost_usd: float | None = None
    cost_source: str = "estimation"  # "estimation" | "api_actual" | "reconciled"
    model_used: str | None = None


class SystemStats(BaseModel):
    """System-wide statistics (US11)."""

    total_tasks: int = 0
    pending_tasks: int = 0
    in_progress_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    active_instances: int = 0
    idle_instances: int = 0
    tasks_per_hour: float = 0.0
    eta_minutes: float | None = None
    session_start: datetime | None = None
    # Cost tracking
    total_cost_today_usd: float = 0.0
    total_cost_week_usd: float = 0.0
    avg_cost_per_task: float = 0.0
    avg_cost_per_instance: float = 0.0
