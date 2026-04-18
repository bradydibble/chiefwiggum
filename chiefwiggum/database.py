"""ChiefWiggum Database

SQLite with WAL mode for concurrent access.
Database path determined by the paths module (XDG-compliant).
"""

import logging
import os
from pathlib import Path

import aiosqlite

from chiefwiggum.paths import get_paths

logger = logging.getLogger(__name__)


def get_database_path() -> Path:
    """Get database path from environment or use XDG-compliant default.

    Priority:
    1. CHIEFWIGGUM_DB environment variable (for testing/override)
    2. XDG-compliant path from paths module (with legacy fallback)
    """
    env_path = os.environ.get("CHIEFWIGGUM_DB")
    if env_path:
        return Path(env_path)
    return get_paths().database_path


# SQLite configuration for reliability
SQLITE_PRAGMAS = {
    "journal_mode": "WAL",
    "busy_timeout": 5000,
    "foreign_keys": "ON",
    "synchronous": "NORMAL",
}


async def get_connection() -> aiosqlite.Connection:
    """Get a database connection with proper configuration."""
    db_path = get_database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path, isolation_level=None)

    for pragma, value in SQLITE_PRAGMAS.items():
        await conn.execute(f"PRAGMA {pragma} = {value}")

    return conn


async def init_db():
    """Initialize database schema."""
    conn = await get_connection()

    try:
        await conn.executescript("""
            -- Ralph Task Claims: Coordinate multi-instance work
            CREATE TABLE IF NOT EXISTS task_claims (
                task_id TEXT PRIMARY KEY,
                task_title TEXT NOT NULL,
                task_priority TEXT NOT NULL,  -- 'HIGH', 'MEDIUM', 'LOWER', 'POLISH'
                task_section TEXT,
                project TEXT,                 -- Project this task belongs to
                category TEXT,                -- Inferred task category (ux, api, testing, etc.)
                claimed_by_ralph_id TEXT,
                claimed_at TIMESTAMP,
                expires_at TIMESTAMP,
                status TEXT DEFAULT 'pending',  -- 'pending', 'in_progress', 'completed', 'failed', 'released', 'retry_pending'
                completion_message TEXT,
                git_commit_sha TEXT,
                -- Error tracking (US5, US6)
                error_category TEXT,          -- 'transient', 'code_error', 'permission', 'conflict', 'timeout', 'unknown'
                error_message TEXT,
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3,
                next_retry_at TIMESTAMP,
                -- Branch isolation (US7)
                branch_name TEXT,
                has_conflict INTEGER DEFAULT 0,
                -- Timestamps
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP,
                started_at TIMESTAMP,         -- When work actually started
                completed_at TIMESTAMP        -- When task finished
            );

            CREATE INDEX IF NOT EXISTS idx_task_claims_status ON task_claims(status);
            CREATE INDEX IF NOT EXISTS idx_task_claims_ralph ON task_claims(claimed_by_ralph_id);
            CREATE INDEX IF NOT EXISTS idx_task_claims_expires ON task_claims(expires_at);
            CREATE INDEX IF NOT EXISTS idx_task_claims_project ON task_claims(project);
            -- Note: indexes for category and next_retry_at are created in _run_migrations()
            -- to handle existing databases that don't have these columns yet

            -- Ralph Instances: Track active instances
            CREATE TABLE IF NOT EXISTS ralph_instances (
                ralph_id TEXT PRIMARY KEY,
                hostname TEXT,
                pid INTEGER,
                session_file TEXT,
                project TEXT,                 -- Current project being worked on
                started_at TIMESTAMP NOT NULL,
                last_heartbeat TIMESTAMP NOT NULL,
                current_task_id TEXT,
                loop_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',  -- 'active', 'idle', 'paused', 'stopped', 'crashed'
                last_error TEXT,              -- Last error message (for CRASHED status)
                -- Configuration (US9) - stored as JSON
                config_json TEXT,             -- RalphConfig serialized
                -- Targeting (US4) - stored as JSON
                targeting_json TEXT,          -- TargetingConfig serialized
                -- Statistics (US11)
                tasks_completed INTEGER DEFAULT 0,
                tasks_failed INTEGER DEFAULT 0,
                total_work_seconds REAL DEFAULT 0.0
            );

            CREATE INDEX IF NOT EXISTS idx_ralph_instances_heartbeat ON ralph_instances(last_heartbeat);
            CREATE INDEX IF NOT EXISTS idx_ralph_instances_project ON ralph_instances(project);
            CREATE INDEX IF NOT EXISTS idx_ralph_instances_status ON ralph_instances(status);

            -- Task History: Audit trail for completed tasks (US12)
            CREATE TABLE IF NOT EXISTS task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                task_title TEXT NOT NULL,
                ralph_id TEXT NOT NULL,
                project TEXT,
                started_at TIMESTAMP NOT NULL,
                completed_at TIMESTAMP NOT NULL,
                duration_seconds REAL NOT NULL,
                status TEXT NOT NULL,
                commit_sha TEXT,
                error_message TEXT,
                -- Cost tracking
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_creation_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                estimated_cost_usd REAL DEFAULT 0.0,
                actual_cost_usd REAL,
                cost_source TEXT DEFAULT 'estimation',
                model_used TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_task_history_task ON task_history(task_id);
            CREATE INDEX IF NOT EXISTS idx_task_history_ralph ON task_history(ralph_id);
            CREATE INDEX IF NOT EXISTS idx_task_history_project ON task_history(project);
            CREATE INDEX IF NOT EXISTS idx_task_history_completed ON task_history(completed_at);

            -- System Settings: Global configuration and state
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Fix Plan Sources: Track where tasks come from (US1 - future multi-source)
            CREATE TABLE IF NOT EXISTS fix_plan_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,    -- 'file', 'github_issues', 'jira'
                source_path TEXT NOT NULL,    -- File path or API endpoint
                project TEXT,
                last_synced_at TIMESTAMP,
                sync_status TEXT DEFAULT 'idle',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_fix_plan_sources_project ON fix_plan_sources(project);

            -- Tasks: New task queue with prompt grading (Ralph Loop Alignment)
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,              -- T1.2, T1.3, etc.
                title TEXT NOT NULL,
                description TEXT NOT NULL,        -- Original text from @fix_plan.md
                generated_prompt TEXT,            -- Task-specific prompt for Ralph
                grade INTEGER,                    -- 0-100 score
                grade_reasoning TEXT,             -- Why this grade
                status TEXT DEFAULT 'pending',    -- 'pending', 'active', 'completed', 'blocked', 'needs_review'
                claimed_by_ralph_id TEXT,
                depends_on TEXT,                  -- JSON array of task IDs this depends on
                source_file TEXT,                 -- @fix_plan.md or other source
                source_line INTEGER,              -- Line number in source file
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (claimed_by_ralph_id) REFERENCES ralph_instances(ralph_id)
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_grade ON tasks(grade);
            CREATE INDEX IF NOT EXISTS idx_tasks_ralph ON tasks(claimed_by_ralph_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

            -- Spawn Requests: Durable TUI/CLI → daemon intent channel.
            -- TUI or CLI inserts a row; the chiefwiggum daemon picks it up on
            -- its next reconcile tick and actually spawns the ralph process.
            -- Lets the TUI die freely without losing user intent.
            CREATE TABLE IF NOT EXISTS spawn_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_path TEXT NOT NULL,
                fix_plan_path TEXT,
                task_id TEXT,                        -- NULL = claim next available
                priority INTEGER DEFAULT 0,          -- higher runs first
                requested_by TEXT,                   -- 'tui' | 'cli' | 'daemon-reconcile'
                requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                consumed_at TIMESTAMP,               -- NULL until daemon acts
                spawned_ralph_id TEXT,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_spawn_requests_pending
                ON spawn_requests(consumed_at, priority DESC, requested_at);

            -- Cancel Requests: same pattern, for explicit ralph termination.
            CREATE TABLE IF NOT EXISTS cancel_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ralph_id TEXT NOT NULL,
                requested_by TEXT,
                requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                consumed_at TIMESTAMP,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_cancel_requests_pending
                ON cancel_requests(consumed_at, requested_at);
        """)

        # Run migrations for existing databases
        await _run_migrations(conn)

        await conn.commit()
        logger.info(f"Database schema initialized at {get_database_path()}")

    finally:
        await conn.close()


async def _run_migrations(conn: aiosqlite.Connection):
    """Run schema migrations for existing databases."""
    # Convert any 'released' tasks to 'pending' so they can be picked up
    # This handles legacy data from before the fix
    await conn.execute(
        "UPDATE task_claims SET status = 'pending' WHERE status = 'released'"
    )

    # Check which columns exist and add missing ones
    cursor = await conn.execute("PRAGMA table_info(task_claims)")
    existing_columns = {row[1] for row in await cursor.fetchall()}

    # New columns for task_claims
    new_task_columns = [
        ("category", "TEXT"),
        ("error_category", "TEXT"),
        ("error_message", "TEXT"),
        ("retry_count", "INTEGER DEFAULT 0"),
        ("max_retries", "INTEGER DEFAULT 3"),
        ("next_retry_at", "TIMESTAMP"),
        ("branch_name", "TEXT"),
        ("has_conflict", "INTEGER DEFAULT 0"),
        ("started_at", "TIMESTAMP"),
        ("completed_at", "TIMESTAMP"),
        ("verified_at", "TIMESTAMP"),
        # Worktree columns
        ("worktree_path", "TEXT"),
        ("worktree_branch", "TEXT"),
        ("merge_status", "TEXT"),
        ("merge_strategy", "TEXT"),
        ("merge_attempted_at", "TIMESTAMP"),
        ("merge_error", "TEXT"),
        # Rich task parsing columns
        ("stable_id", "TEXT"),
        ("description", "TEXT"),
        ("code_blocks_json", "TEXT"),
        ("file_paths_json", "TEXT"),
        ("depends_on_json", "TEXT"),
        ("source_line", "INTEGER"),
    ]

    for col_name, col_type in new_task_columns:
        if col_name not in existing_columns:
            try:
                await conn.execute(f"ALTER TABLE task_claims ADD COLUMN {col_name} {col_type}")
                logger.info(f"Added column {col_name} to task_claims")
            except Exception as e:
                logger.debug(f"Column {col_name} may already exist: {e}")

    # Create indexes for columns that may have been added via migration
    # These are created here (not in main executescript) to ensure columns exist first
    migration_indexes = [
        "CREATE INDEX IF NOT EXISTS idx_task_claims_category ON task_claims(category)",
        "CREATE INDEX IF NOT EXISTS idx_task_claims_retry ON task_claims(next_retry_at)",
        "CREATE INDEX IF NOT EXISTS idx_task_claims_worktree ON task_claims(worktree_path)",
        "CREATE INDEX IF NOT EXISTS idx_task_claims_merge_status ON task_claims(merge_status)",
        "CREATE INDEX IF NOT EXISTS idx_task_claims_stable_id ON task_claims(stable_id)",
    ]
    for index_sql in migration_indexes:
        try:
            await conn.execute(index_sql)
        except Exception as e:
            logger.debug(f"Index creation skipped: {e}")

    # Check ralph_instances columns
    cursor = await conn.execute("PRAGMA table_info(ralph_instances)")
    existing_columns = {row[1] for row in await cursor.fetchall()}

    new_instance_columns = [
        ("config_json", "TEXT"),
        ("targeting_json", "TEXT"),
        ("tasks_completed", "INTEGER DEFAULT 0"),
        ("tasks_failed", "INTEGER DEFAULT 0"),
        ("total_work_seconds", "REAL DEFAULT 0.0"),
        ("last_error", "TEXT"),  # Last error message (for CRASHED status)
        ("prompt_path", "TEXT"),  # Path to the prompt file this Ralph reads from
        # Worktree columns
        ("worktree_base_path", "TEXT"),
        ("use_worktrees", "INTEGER DEFAULT 1"),  # Default ON
    ]

    for col_name, col_type in new_instance_columns:
        if col_name not in existing_columns:
            try:
                await conn.execute(f"ALTER TABLE ralph_instances ADD COLUMN {col_name} {col_type}")
                logger.info(f"Added column {col_name} to ralph_instances")
            except Exception as e:
                logger.debug(f"Column {col_name} may already exist: {e}")

    # Add cost tracking columns to ralph_instances
    cost_instance_columns = [
        ("total_input_tokens", "INTEGER DEFAULT 0"),
        ("total_output_tokens", "INTEGER DEFAULT 0"),
        ("total_cost_usd", "REAL DEFAULT 0.0"),
        ("last_cost_update", "TIMESTAMP"),
    ]

    for col_name, col_type in cost_instance_columns:
        if col_name not in existing_columns:
            try:
                await conn.execute(f"ALTER TABLE ralph_instances ADD COLUMN {col_name} {col_type}")
                logger.info(f"Added cost tracking column {col_name} to ralph_instances")
            except Exception as e:
                logger.debug(f"Column {col_name} may already exist: {e}")

    # Check task_history columns for cost tracking
    cursor = await conn.execute("PRAGMA table_info(task_history)")
    existing_history_columns = {row[1] for row in await cursor.fetchall()}

    cost_history_columns = [
        ("input_tokens", "INTEGER DEFAULT 0"),
        ("output_tokens", "INTEGER DEFAULT 0"),
        ("cache_creation_tokens", "INTEGER DEFAULT 0"),
        ("cache_read_tokens", "INTEGER DEFAULT 0"),
        ("estimated_cost_usd", "REAL DEFAULT 0.0"),
        ("actual_cost_usd", "REAL"),
        ("cost_source", "TEXT DEFAULT 'estimation'"),
        ("model_used", "TEXT"),
    ]

    for col_name, col_type in cost_history_columns:
        if col_name not in existing_history_columns:
            try:
                await conn.execute(f"ALTER TABLE task_history ADD COLUMN {col_name} {col_type}")
                logger.info(f"Added cost tracking column {col_name} to task_history")
            except Exception as e:
                logger.debug(f"Column {col_name} may already exist: {e}")

    # Check tasks table columns for rich parsing data
    cursor = await conn.execute("PRAGMA table_info(tasks)")
    existing_tasks_columns = {row[1] for row in await cursor.fetchall()}

    new_tasks_columns = [
        ("code_blocks_json", "TEXT"),
        ("file_paths_json", "TEXT"),
        ("stable_id", "TEXT"),
    ]

    for col_name, col_type in new_tasks_columns:
        if col_name not in existing_tasks_columns:
            try:
                await conn.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_type}")
                logger.info(f"Added column {col_name} to tasks")
            except Exception as e:
                logger.debug(f"Column {col_name} may already exist: {e}")


async def reset_db():
    """Reset the database (drop all data)."""
    conn = await get_connection()
    try:
        await conn.executescript("""
            DELETE FROM task_claims;
            DELETE FROM ralph_instances;
            DELETE FROM task_history;
            DELETE FROM system_settings;
            DELETE FROM fix_plan_sources;
            DELETE FROM tasks;
            DELETE FROM spawn_requests;
            DELETE FROM cancel_requests;
        """)
        await conn.commit()
        logger.info("Database reset complete")
    finally:
        await conn.close()


async def get_setting(key: str, default: str | None = None) -> str | None:
    """Get a system setting value."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            "SELECT value FROM system_settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else default
    finally:
        await conn.close()


async def set_setting(key: str, value: str) -> None:
    """Set a system setting value."""
    conn = await get_connection()
    try:
        await conn.execute(
            """INSERT INTO system_settings (key, value, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
            (key, value),
        )
        await conn.commit()
    finally:
        await conn.close()
