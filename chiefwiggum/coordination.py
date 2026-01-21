"""ChiefWiggum Multi-Instance Task Coordination

SQLite-based task claiming system so 2-5 Ralph instances can work
concurrently without git conflicts or duplicate work.
"""

import json
import logging
import os
import re
import socket
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from chiefwiggum.database import get_connection
from chiefwiggum.models import (
    ClaudeModel,
    ErrorCategory,
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

logger = logging.getLogger(__name__)

# Constants
CLAIM_EXPIRY_MINUTES = 7  # Auto-release claims after 7 minutes
HEARTBEAT_STALE_MINUTES = 10  # Mark instances crashed after 10 minutes
PRIORITY_ORDER = [TaskPriority.HIGH, TaskPriority.MEDIUM, TaskPriority.LOWER, TaskPriority.POLISH]


def _slugify(text: str) -> str:
    """Convert text to URL-safe slug."""
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[-\s]+", "-", slug).strip("-")
    return slug[:50]


def _generate_task_id(task_number: int, title: str) -> str:
    """Generate task ID from number and title."""
    return f"task-{task_number}-{_slugify(title)}"


def parse_fix_plan(path: str | Path) -> list[FixPlanTask]:
    """Parse @fix_plan.md and return list of tasks.

    Parses patterns like:
    - Section: `## HIGH PRIORITY - Get Data Flowing`
    - Task: `### 22. File Processing Workflow COMPLETE`
    - Subtask: `- [x] Create file upload endpoint`

    Args:
        path: Path to the fix_plan.md file

    Returns:
        List of FixPlanTask objects
    """
    path = Path(path)
    if not path.exists():
        logger.warning(f"Fix plan not found: {path}")
        return []

    content = path.read_text()
    tasks: list[FixPlanTask] = []

    current_section: str | None = None
    current_priority: TaskPriority | None = None
    current_task: FixPlanTask | None = None

    # Priority section patterns
    section_patterns = {
        r"##\s*HIGH\s*PRIORITY": TaskPriority.HIGH,
        r"##\s*MEDIUM\s*PRIORITY": TaskPriority.MEDIUM,
        r"##\s*LOWER\s*PRIORITY": TaskPriority.LOWER,
        r"##\s*POLISH": TaskPriority.POLISH,
    }

    for line in content.split("\n"):
        line = line.strip()

        # Check for section headers
        for pattern, priority in section_patterns.items():
            if re.match(pattern, line, re.IGNORECASE):
                current_priority = priority
                match = re.search(r"-\s*(.+)$", line)
                current_section = match.group(1).strip() if match else None
                break

        # Check for task headers (### N. Title)
        task_match = re.match(r"###\s*(\d+)\.\s*(.+)", line)
        if task_match and current_priority:
            if current_task:
                tasks.append(current_task)

            task_number = int(task_match.group(1))
            title_part = task_match.group(2)

            # Check if complete (COMPLETE in title, with or without checkmark)
            is_complete = "COMPLETE" in title_part.upper()

            # Clean title (remove checkmarks and COMPLETE)
            title = re.sub(r"\s*[✅✓]\s*COMPLETE\s*", "", title_part, flags=re.IGNORECASE).strip()
            title = re.sub(r"\s*COMPLETE\s*", "", title, flags=re.IGNORECASE).strip()

            current_task = FixPlanTask(
                task_id=_generate_task_id(task_number, title),
                task_number=task_number,
                title=title,
                priority=current_priority,
                section=current_section,
                is_complete=is_complete,
                subtasks=[],
                completed_subtasks=[],
            )

        # Check for subtasks (- [ ] or - [x])
        if current_task:
            subtask_match = re.match(r"-\s*\[([ x])\]\s*(.+)", line)
            if subtask_match:
                is_checked = subtask_match.group(1).lower() == "x"
                subtask_text = subtask_match.group(2).strip()

                if is_checked:
                    current_task.completed_subtasks.append(subtask_text)
                else:
                    current_task.subtasks.append(subtask_text)

    # Don't forget the last task
    if current_task:
        tasks.append(current_task)

    return tasks


async def sync_tasks_from_fix_plan(fix_plan_path: str | Path, project: str | None = None) -> int:
    """Sync tasks from @fix_plan.md to the database.

    Updates the task_claims table with tasks from the fix plan.
    Marks tasks as completed if they show COMPLETE in the fix plan.

    Args:
        fix_plan_path: Path to @fix_plan.md
        project: Project name to associate with tasks

    Returns:
        Number of tasks synced
    """
    tasks = parse_fix_plan(fix_plan_path)
    if not tasks:
        return 0

    # Auto-detect project from path if not provided
    if project is None:
        fix_plan_path = Path(fix_plan_path)
        project = fix_plan_path.parent.name

    conn = await get_connection()
    try:
        now = datetime.now()

        for task in tasks:
            cursor = await conn.execute(
                "SELECT task_id, status FROM task_claims WHERE task_id = ?",
                (task.task_id,)
            )
            existing = await cursor.fetchone()

            if existing:
                existing_status = existing[1]
                if task.is_complete and existing_status != TaskClaimStatus.COMPLETED.value:
                    await conn.execute(
                        """UPDATE task_claims
                           SET status = ?, updated_at = ?
                           WHERE task_id = ?""",
                        (TaskClaimStatus.COMPLETED.value, now, task.task_id)
                    )
            else:
                status = TaskClaimStatus.COMPLETED.value if task.is_complete else TaskClaimStatus.PENDING.value
                await conn.execute(
                    """INSERT INTO task_claims
                       (task_id, task_title, task_priority, task_section, project, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (task.task_id, task.title, task.priority.value, task.section, project, status, now)
                )

        await conn.commit()
        return len(tasks)
    finally:
        await conn.close()


async def claim_task(ralph_id: str, project: str | None = None, fix_plan_path: str | Path | None = None) -> dict | None:
    """Atomically claim the next available task for a Ralph instance.

    Tasks are claimed in priority order: HIGH > MEDIUM > LOWER > POLISH.
    A task is available if:
    - status is 'pending' OR
    - status is 'in_progress' AND expires_at < now (stale claim)

    Args:
        ralph_id: ID of the Ralph instance claiming the task
        project: Optional project to filter tasks by
        fix_plan_path: Optional path to sync tasks from first

    Returns:
        Dict with task info if claim successful, None if no tasks available
    """
    if fix_plan_path:
        await sync_tasks_from_fix_plan(fix_plan_path, project)

    conn = await get_connection()
    try:
        now = datetime.now()
        expires_at = now + timedelta(minutes=CLAIM_EXPIRY_MINUTES)

        # Build query with optional project filter
        project_filter = "AND project = ?" if project else ""
        params_base = [project] if project else []

        for priority in PRIORITY_ORDER:
            params = [ralph_id, now, expires_at, TaskClaimStatus.IN_PROGRESS.value, now,
                     priority.value] + params_base + [now]

            query = f"""UPDATE task_claims
                   SET claimed_by_ralph_id = ?,
                       claimed_at = ?,
                       expires_at = ?,
                       status = ?,
                       updated_at = ?
                   WHERE task_id = (
                       SELECT task_id FROM task_claims
                       WHERE task_priority = ?
                         {project_filter}
                         AND (status = 'pending'
                              OR (status = 'in_progress' AND expires_at < ?))
                       ORDER BY created_at ASC
                       LIMIT 1
                   )
                   RETURNING task_id, task_title, task_priority, task_section, project"""

            cursor = await conn.execute(query, params)
            result = await cursor.fetchone()

            if result:
                await conn.commit()
                await _update_instance_task(ralph_id, result[0])

                return {
                    "task_id": result[0],
                    "task_title": result[1],
                    "task_priority": result[2],
                    "task_section": result[3],
                    "project": result[4],
                    "claimed_at": now.isoformat(),
                    "expires_at": expires_at.isoformat(),
                }

        return None
    finally:
        await conn.close()


async def extend_claim(ralph_id: str, task_id: str) -> bool:
    """Extend the expiry time for an existing claim (heartbeat).

    Args:
        ralph_id: ID of the Ralph instance
        task_id: ID of the task

    Returns:
        True if claim was extended, False if claim not found or not owned
    """
    conn = await get_connection()
    try:
        now = datetime.now()
        expires_at = now + timedelta(minutes=CLAIM_EXPIRY_MINUTES)

        cursor = await conn.execute(
            """UPDATE task_claims
               SET expires_at = ?, updated_at = ?
               WHERE task_id = ?
                 AND claimed_by_ralph_id = ?
                 AND status = 'in_progress'""",
            (expires_at, now, task_id, ralph_id)
        )
        await conn.commit()
        return cursor.rowcount > 0
    finally:
        await conn.close()


async def complete_task(
    ralph_id: str,
    task_id: str,
    commit_sha: str | None = None,
    message: str | None = None
) -> bool:
    """Mark a task as completed.

    Args:
        ralph_id: ID of the Ralph instance
        task_id: ID of the task
        commit_sha: Optional git commit SHA
        message: Optional completion message

    Returns:
        True if task was marked complete, False if not found or not owned
    """
    conn = await get_connection()
    try:
        now = datetime.now()

        cursor = await conn.execute(
            """UPDATE task_claims
               SET status = ?,
                   completion_message = ?,
                   git_commit_sha = ?,
                   updated_at = ?
               WHERE task_id = ?
                 AND claimed_by_ralph_id = ?
                 AND status = 'in_progress'""",
            (TaskClaimStatus.COMPLETED.value, message, commit_sha, now, task_id, ralph_id)
        )
        await conn.commit()

        if cursor.rowcount > 0:
            await _update_instance_task(ralph_id, None)
            return True
        return False
    finally:
        await conn.close()


async def fail_task(ralph_id: str, task_id: str, error_message: str) -> bool:
    """Mark a task as failed.

    Args:
        ralph_id: ID of the Ralph instance
        task_id: ID of the task
        error_message: Error message explaining the failure

    Returns:
        True if task was marked failed, False if not found or not owned
    """
    conn = await get_connection()
    try:
        now = datetime.now()

        cursor = await conn.execute(
            """UPDATE task_claims
               SET status = ?,
                   completion_message = ?,
                   updated_at = ?
               WHERE task_id = ?
                 AND claimed_by_ralph_id = ?
                 AND status = 'in_progress'""",
            (TaskClaimStatus.FAILED.value, error_message, now, task_id, ralph_id)
        )
        await conn.commit()

        if cursor.rowcount > 0:
            await _update_instance_task(ralph_id, None)
            return True
        return False
    finally:
        await conn.close()


async def release_claim(ralph_id: str, task_id: str) -> bool:
    """Release a claim without completing or failing the task.

    Returns the task to 'pending' status so another Ralph can claim it.

    Args:
        ralph_id: ID of the Ralph instance
        task_id: ID of the task

    Returns:
        True if claim was released, False if not found or not owned
    """
    conn = await get_connection()
    try:
        now = datetime.now()

        cursor = await conn.execute(
            """UPDATE task_claims
               SET status = 'pending',
                   claimed_by_ralph_id = NULL,
                   claimed_at = NULL,
                   expires_at = NULL,
                   updated_at = ?
               WHERE task_id = ?
                 AND claimed_by_ralph_id = ?
                 AND status = 'in_progress'""",
            (now, task_id, ralph_id)
        )
        await conn.commit()

        if cursor.rowcount > 0:
            await _update_instance_task(ralph_id, None)
            return True
        return False
    finally:
        await conn.close()


async def register_ralph_instance(ralph_id: str, session_file: str | None = None, project: str | None = None) -> str:
    """Register a new Ralph instance.

    Args:
        ralph_id: Unique ID for this Ralph instance
        session_file: Optional path to session file
        project: Optional project being worked on

    Returns:
        The ralph_id (useful for auto-generated IDs)
    """
    conn = await get_connection()
    try:
        now = datetime.now()
        hostname = socket.gethostname()
        pid = os.getpid()

        await conn.execute(
            """INSERT INTO ralph_instances
               (ralph_id, hostname, pid, session_file, project, started_at, last_heartbeat, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ralph_id) DO UPDATE SET
                   hostname = excluded.hostname,
                   pid = excluded.pid,
                   session_file = excluded.session_file,
                   project = excluded.project,
                   started_at = excluded.started_at,
                   last_heartbeat = excluded.last_heartbeat,
                   status = excluded.status""",
            (ralph_id, hostname, pid, session_file, project, now, now, RalphInstanceStatus.ACTIVE.value)
        )
        await conn.commit()

        logger.info(f"Registered Ralph instance: {ralph_id} (host={hostname}, pid={pid})")
        return ralph_id
    finally:
        await conn.close()


async def heartbeat(ralph_id: str) -> None:
    """Update heartbeat timestamp for a Ralph instance.

    Args:
        ralph_id: ID of the Ralph instance
    """
    conn = await get_connection()
    try:
        now = datetime.now()

        await conn.execute(
            """UPDATE ralph_instances
               SET last_heartbeat = ?,
                   loop_count = loop_count + 1
               WHERE ralph_id = ?""",
            (now, ralph_id)
        )
        await conn.commit()
    finally:
        await conn.close()


async def shutdown_instance(ralph_id: str) -> None:
    """Mark a Ralph instance as stopped (clean shutdown).

    Args:
        ralph_id: ID of the Ralph instance
    """
    conn = await get_connection()
    try:
        now = datetime.now()

        # Release any claimed tasks back to pending so other Ralphs can pick them up
        await conn.execute(
            """UPDATE task_claims
               SET status = 'pending',
                   claimed_by_ralph_id = NULL,
                   claimed_at = NULL,
                   expires_at = NULL,
                   updated_at = ?
               WHERE claimed_by_ralph_id = ?
                 AND status = 'in_progress'""",
            (now, ralph_id)
        )

        # Mark instance as stopped
        await conn.execute(
            """UPDATE ralph_instances
               SET status = ?,
                   current_task_id = NULL
               WHERE ralph_id = ?""",
            (RalphInstanceStatus.STOPPED.value, ralph_id)
        )

        await conn.commit()
        logger.info(f"Ralph instance shut down: {ralph_id}")
    finally:
        await conn.close()


async def mark_stale_instances_crashed() -> int:
    """Mark instances without recent heartbeat as crashed.

    Returns:
        Number of instances marked as crashed
    """
    conn = await get_connection()
    try:
        now = datetime.now()
        stale_threshold = now - timedelta(minutes=HEARTBEAT_STALE_MINUTES)

        cursor = await conn.execute(
            """SELECT ralph_id FROM ralph_instances
               WHERE status = 'active'
                 AND last_heartbeat < ?""",
            (stale_threshold,)
        )
        stale_instances = await cursor.fetchall()

        if not stale_instances:
            return 0

        stale_ids = [row[0] for row in stale_instances]

        placeholders = ",".join("?" * len(stale_ids))
        await conn.execute(
            f"""UPDATE task_claims
               SET status = 'pending',
                   claimed_by_ralph_id = NULL,
                   claimed_at = NULL,
                   expires_at = NULL,
                   updated_at = ?
               WHERE claimed_by_ralph_id IN ({placeholders})
                 AND status = 'in_progress'""",
            [now] + stale_ids
        )

        await conn.execute(
            f"""UPDATE ralph_instances
               SET status = ?,
                   current_task_id = NULL
               WHERE ralph_id IN ({placeholders})""",
            [RalphInstanceStatus.CRASHED.value] + stale_ids
        )

        await conn.commit()

        for ralph_id in stale_ids:
            logger.warning(f"Marked Ralph instance as crashed: {ralph_id}")

        return len(stale_ids)
    finally:
        await conn.close()


async def verify_claim_before_commit(ralph_id: str, task_id: str) -> tuple[bool, str]:
    """Verify claim is still valid before committing.

    Args:
        ralph_id: ID of the Ralph instance
        task_id: ID of the task

    Returns:
        Tuple of (is_valid, message)
    """
    conn = await get_connection()
    try:
        now = datetime.now()

        cursor = await conn.execute(
            """SELECT claimed_by_ralph_id, status, expires_at
               FROM task_claims
               WHERE task_id = ?""",
            (task_id,)
        )
        result = await cursor.fetchone()

        if not result:
            return (False, f"Task {task_id} not found")

        claimed_by, status, expires_at_str = result

        if claimed_by != ralph_id:
            return (False, f"Task claimed by different instance: {claimed_by}")

        if status != TaskClaimStatus.IN_PROGRESS.value:
            return (False, f"Task status is {status}, expected in_progress")

        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str) if isinstance(expires_at_str, str) else expires_at_str
            if expires_at < now:
                return (False, "Claim has expired")

        return (True, "Claim verified")
    finally:
        await conn.close()


async def safe_git_commit(
    ralph_id: str,
    task_id: str,
    message: str,
    files: list[str] | None = None
) -> tuple[bool, str]:
    """Safely create a git commit with claim verification.

    Args:
        ralph_id: ID of the Ralph instance
        task_id: ID of the task
        message: Commit message
        files: Optional list of files to add (default: -A for all)

    Returns:
        Tuple of (success, result_message_or_sha)
    """
    is_valid, verify_msg = await verify_claim_before_commit(ralph_id, task_id)
    if not is_valid:
        return (False, f"Claim verification failed: {verify_msg}")

    extended = await extend_claim(ralph_id, task_id)
    if not extended:
        return (False, "Failed to extend claim")

    try:
        if files:
            for f in files:
                subprocess.run(["git", "add", f], check=True, capture_output=True)
        else:
            subprocess.run(["git", "add", "-A"], check=True, capture_output=True)

        subprocess.run(
            ["git", "commit", "-m", message],
            check=True,
            capture_output=True,
            text=True
        )

        sha_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True
        )
        commit_sha = sha_result.stdout.strip()

        await complete_task(ralph_id, task_id, commit_sha, message)

        return (True, commit_sha)

    except subprocess.CalledProcessError as e:
        error_msg = e.stderr if e.stderr else str(e)
        logger.error(f"Git commit failed: {error_msg}")
        return (False, f"Git commit failed: {error_msg}")


async def _update_instance_task(ralph_id: str, task_id: str | None) -> None:
    """Update the current_task_id for a Ralph instance."""
    conn = await get_connection()
    try:
        await conn.execute(
            "UPDATE ralph_instances SET current_task_id = ? WHERE ralph_id = ?",
            (task_id, ralph_id)
        )
        await conn.commit()
    finally:
        await conn.close()


async def get_task_claim(task_id: str) -> TaskClaim | None:
    """Get a task claim by ID."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """SELECT task_id, task_title, task_priority, task_section, project,
                      claimed_by_ralph_id, claimed_at, expires_at, status,
                      completion_message, git_commit_sha, created_at, updated_at,
                      category, error_category, error_message, retry_count, max_retries,
                      next_retry_at, branch_name, has_conflict, started_at, completed_at
               FROM task_claims WHERE task_id = ?""",
            (task_id,)
        )
        row = await cursor.fetchone()

        if not row:
            return None

        return _row_to_task_claim(row)
    finally:
        await conn.close()


async def get_ralph_instance(ralph_id: str) -> RalphInstance | None:
    """Get a Ralph instance by ID."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """SELECT ralph_id, hostname, pid, session_file, project, started_at,
                      last_heartbeat, current_task_id, loop_count, status,
                      config_json, targeting_json, tasks_completed, tasks_failed, total_work_seconds
               FROM ralph_instances WHERE ralph_id = ?""",
            (ralph_id,)
        )
        row = await cursor.fetchone()

        if not row:
            return None

        return _row_to_ralph_instance(row)
    finally:
        await conn.close()


async def list_active_instances() -> list[RalphInstance]:
    """List all active Ralph instances (active or idle, not stopped/crashed)."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """SELECT ralph_id, hostname, pid, session_file, project, started_at,
                      last_heartbeat, current_task_id, loop_count, status,
                      config_json, targeting_json, tasks_completed, tasks_failed, total_work_seconds
               FROM ralph_instances
               WHERE status IN ('active', 'idle', 'paused')
               ORDER BY started_at DESC"""
        )
        rows = await cursor.fetchall()

        return [_row_to_ralph_instance(row) for row in rows]
    finally:
        await conn.close()


async def list_all_instances() -> list[RalphInstance]:
    """List all Ralph instances (including stopped/crashed)."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """SELECT ralph_id, hostname, pid, session_file, project, started_at,
                      last_heartbeat, current_task_id, loop_count, status,
                      config_json, targeting_json, tasks_completed, tasks_failed, total_work_seconds
               FROM ralph_instances
               ORDER BY last_heartbeat DESC"""
        )
        rows = await cursor.fetchall()

        return [_row_to_ralph_instance(row) for row in rows]
    finally:
        await conn.close()


async def list_stopped_instances(older_than_hours: float | None = None) -> list[RalphInstance]:
    """List stopped/crashed Ralph instances.

    Args:
        older_than_hours: If provided, only return instances older than this many hours

    Returns:
        List of stopped/crashed RalphInstance objects
    """
    conn = await get_connection()
    try:
        if older_than_hours is not None:
            cutoff = datetime.now() - timedelta(hours=older_than_hours)
            cursor = await conn.execute(
                """SELECT ralph_id, hostname, pid, session_file, project, started_at,
                          last_heartbeat, current_task_id, loop_count, status,
                          config_json, targeting_json, tasks_completed, tasks_failed, total_work_seconds
                   FROM ralph_instances
                   WHERE status IN ('stopped', 'crashed')
                     AND last_heartbeat < ?
                   ORDER BY last_heartbeat DESC""",
                (cutoff,)
            )
        else:
            cursor = await conn.execute(
                """SELECT ralph_id, hostname, pid, session_file, project, started_at,
                          last_heartbeat, current_task_id, loop_count, status,
                          config_json, targeting_json, tasks_completed, tasks_failed, total_work_seconds
                   FROM ralph_instances
                   WHERE status IN ('stopped', 'crashed')
                   ORDER BY last_heartbeat DESC"""
            )
        rows = await cursor.fetchall()

        return [_row_to_ralph_instance(row) for row in rows]
    finally:
        await conn.close()


def cleanup_instance_files(ralph_id: str, dry_run: bool = False) -> dict[str, bool]:
    """Clean up files for a specific Ralph instance.

    Deletes session files, log files, and PID files.
    Does NOT delete database records (preserves history).

    Args:
        ralph_id: ID of the Ralph instance to clean up
        dry_run: If True, only report what would be deleted

    Returns:
        Dict mapping file paths to whether they were deleted (or would be)
    """
    from chiefwiggum.spawner import (
        get_ralph_log_path,
        get_ralph_pid_path,
        get_ralph_session_path,
    )

    results: dict[str, bool] = {}

    # Get file paths
    paths = [
        get_ralph_session_path(ralph_id),
        get_ralph_log_path(ralph_id),
        get_ralph_pid_path(ralph_id),
    ]

    for path in paths:
        if path.exists():
            if dry_run:
                results[str(path)] = True  # Would delete
            else:
                try:
                    path.unlink()
                    results[str(path)] = True
                    logger.info(f"Deleted: {path}")
                except Exception as e:
                    logger.error(f"Failed to delete {path}: {e}")
                    results[str(path)] = False
        else:
            results[str(path)] = False  # File doesn't exist

    return results


async def list_pending_tasks(project: str | None = None) -> list[TaskClaim]:
    """List all pending tasks in priority order."""
    conn = await get_connection()
    try:
        project_filter = "AND project = ?" if project else ""
        params = [project] if project else []

        cursor = await conn.execute(
            f"""SELECT task_id, task_title, task_priority, task_section, project,
                      claimed_by_ralph_id, claimed_at, expires_at, status,
                      completion_message, git_commit_sha, created_at, updated_at,
                      category, error_category, error_message, retry_count, max_retries,
                      next_retry_at, branch_name, has_conflict, started_at, completed_at
               FROM task_claims
               WHERE status = 'pending' {project_filter}
               ORDER BY
                   CASE task_priority
                       WHEN 'HIGH' THEN 1
                       WHEN 'MEDIUM' THEN 2
                       WHEN 'LOWER' THEN 3
                       WHEN 'POLISH' THEN 4
                   END,
                   created_at ASC""",
            params
        )
        rows = await cursor.fetchall()

        return [_row_to_task_claim(row) for row in rows]
    finally:
        await conn.close()


async def list_in_progress_tasks(project: str | None = None) -> list[TaskClaim]:
    """List all in-progress tasks."""
    conn = await get_connection()
    try:
        project_filter = "AND project = ?" if project else ""
        params = [project] if project else []

        cursor = await conn.execute(
            f"""SELECT task_id, task_title, task_priority, task_section, project,
                      claimed_by_ralph_id, claimed_at, expires_at, status,
                      completion_message, git_commit_sha, created_at, updated_at,
                      category, error_category, error_message, retry_count, max_retries,
                      next_retry_at, branch_name, has_conflict, started_at, completed_at
               FROM task_claims
               WHERE status = 'in_progress' {project_filter}
               ORDER BY claimed_at ASC""",
            params
        )
        rows = await cursor.fetchall()

        return [_row_to_task_claim(row) for row in rows]
    finally:
        await conn.close()


async def list_all_tasks(project: str | None = None) -> list[TaskClaim]:
    """List all tasks."""
    conn = await get_connection()
    try:
        project_filter = "WHERE project = ?" if project else ""
        params = [project] if project else []

        cursor = await conn.execute(
            f"""SELECT task_id, task_title, task_priority, task_section, project,
                      claimed_by_ralph_id, claimed_at, expires_at, status,
                      completion_message, git_commit_sha, created_at, updated_at,
                      category, error_category, error_message, retry_count, max_retries,
                      next_retry_at, branch_name, has_conflict, started_at, completed_at
               FROM task_claims
               {project_filter}
               ORDER BY
                   CASE task_priority
                       WHEN 'HIGH' THEN 1
                       WHEN 'MEDIUM' THEN 2
                       WHEN 'LOWER' THEN 3
                       WHEN 'POLISH' THEN 4
                   END,
                   created_at ASC""",
            params
        )
        rows = await cursor.fetchall()

        return [_row_to_task_claim(row) for row in rows]
    finally:
        await conn.close()


def _row_to_task_claim(row: tuple) -> TaskClaim:
    """Convert a database row to a TaskClaim object."""
    return TaskClaim(
        task_id=row[0],
        task_title=row[1],
        task_priority=TaskPriority(row[2]),
        task_section=row[3],
        project=row[4],
        claimed_by_ralph_id=row[5],
        claimed_at=datetime.fromisoformat(row[6]) if row[6] else None,
        expires_at=datetime.fromisoformat(row[7]) if row[7] else None,
        status=TaskClaimStatus(row[8]) if row[8] else TaskClaimStatus.PENDING,
        completion_message=row[9],
        git_commit_sha=row[10],
        created_at=datetime.fromisoformat(row[11]) if row[11] else datetime.now(),
        updated_at=datetime.fromisoformat(row[12]) if row[12] else None,
        category=TaskCategory(row[13]) if row[13] else None,
        error_category=ErrorCategory(row[14]) if row[14] else None,
        error_message=row[15],
        retry_count=row[16] or 0,
        max_retries=row[17] or 3,
        next_retry_at=datetime.fromisoformat(row[18]) if row[18] else None,
        branch_name=row[19],
        has_conflict=bool(row[20]) if row[20] is not None else False,
        started_at=datetime.fromisoformat(row[21]) if row[21] else None,
        completed_at=datetime.fromisoformat(row[22]) if row[22] else None,
    )


def _row_to_ralph_instance(row: tuple) -> RalphInstance:
    """Convert a database row to a RalphInstance object."""
    config = RalphConfig()
    targeting = TargetingConfig()

    # Parse JSON config if present
    if len(row) > 10 and row[10]:
        try:
            config_dict = json.loads(row[10])
            config = RalphConfig(**config_dict)
        except (json.JSONDecodeError, Exception):
            pass

    # Parse JSON targeting if present
    if len(row) > 11 and row[11]:
        try:
            targeting_dict = json.loads(row[11])
            targeting = TargetingConfig(**targeting_dict)
        except (json.JSONDecodeError, Exception):
            pass

    return RalphInstance(
        ralph_id=row[0],
        hostname=row[1],
        pid=row[2],
        session_file=row[3],
        project=row[4],
        started_at=datetime.fromisoformat(row[5]) if row[5] else datetime.now(),
        last_heartbeat=datetime.fromisoformat(row[6]) if row[6] else datetime.now(),
        current_task_id=row[7],
        loop_count=row[8] or 0,
        status=RalphInstanceStatus(row[9]) if row[9] else RalphInstanceStatus.ACTIVE,
        config=config,
        targeting=targeting,
        tasks_completed=row[12] if len(row) > 12 else 0,
        tasks_failed=row[13] if len(row) > 13 else 0,
        total_work_seconds=row[14] if len(row) > 14 else 0.0,
    )


# ============================================================================
# Task Category Inference (US4)
# ============================================================================

CATEGORY_PATTERNS = {
    TaskCategory.UX: [
        r"src/components/.*",
        r"templates/.*",
        r"static/.*",
        r".*\.css$",
        r".*\.scss$",
        r".*\.html$",
        r"frontend/.*",
        r"ui/.*",
    ],
    TaskCategory.API: [
        r"src/api/.*",
        r"routes/.*",
        r"endpoints/.*",
        r"api/.*",
        r".*_api\.py$",
        r".*_routes\.py$",
    ],
    TaskCategory.TESTING: [
        r"tests/.*",
        r".*_test\.py$",
        r"test_.*\.py$",
        r".*\.test\.(ts|js)$",
        r"__tests__/.*",
        r"spec/.*",
    ],
    TaskCategory.DATABASE: [
        r"migrations/.*",
        r"models/.*",
        r"schema/.*",
        r".*_models\.py$",
        r".*_schema\.py$",
        r"alembic/.*",
    ],
    TaskCategory.INFRA: [
        r"scripts/.*",
        r"docker/.*",
        r"\.github/.*",
        r"Dockerfile.*",
        r"docker-compose.*",
        r"\.ci/.*",
        r"deploy/.*",
    ],
}


def infer_task_category(file_paths: list[str], task_title: str = "") -> TaskCategory:
    """Infer task category from file paths or title.

    Args:
        file_paths: List of file paths associated with the task
        task_title: Task title for keyword matching

    Returns:
        Inferred TaskCategory
    """
    category_scores: dict[TaskCategory, int] = {cat: 0 for cat in TaskCategory}

    # Score based on file paths
    # Tests patterns get +2 to prioritize test categorization
    for path in file_paths:
        for category, patterns in CATEGORY_PATTERNS.items():
            for pattern in patterns:
                if re.match(pattern, path, re.IGNORECASE):
                    # Give tests higher weight since test files often contain other keywords
                    weight = 2 if category == TaskCategory.TESTING else 1
                    category_scores[category] += weight
                    break

    # Score based on title keywords
    title_lower = task_title.lower()
    keyword_map = {
        TaskCategory.UX: ["ui", "ux", "component", "template", "style", "css", "frontend", "layout"],
        TaskCategory.API: ["api", "endpoint", "route", "rest", "graphql", "request", "response"],
        TaskCategory.TESTING: ["test", "spec", "fixture", "mock", "coverage", "pytest", "jest"],
        TaskCategory.DATABASE: ["database", "migration", "model", "schema", "sql", "query"],
        TaskCategory.INFRA: ["deploy", "docker", "ci", "cd", "script", "pipeline", "config"],
    }

    for category, keywords in keyword_map.items():
        for keyword in keywords:
            if keyword in title_lower:
                category_scores[category] += 1

    # Find highest scoring category
    max_score = max(category_scores.values())
    if max_score > 0:
        for category, score in category_scores.items():
            if score == max_score:
                return category

    return TaskCategory.GENERAL


# ============================================================================
# Enhanced Error Handling (US5, US6)
# ============================================================================

def classify_error(error_message: str) -> ErrorCategory:
    """Classify an error message into a category.

    Args:
        error_message: The error message to classify

    Returns:
        ErrorCategory for retry decisions
    """
    error_lower = error_message.lower()

    # Transient errors (auto-retry)
    transient_patterns = [
        "rate limit", "too many requests", "429",
        "connection refused", "connection reset", "connection timeout",
        "temporary", "temporarily", "try again",
        "503", "502", "504",
        "overloaded", "busy",
    ]
    for pattern in transient_patterns:
        if pattern in error_lower:
            return ErrorCategory.TRANSIENT

    # Timeout errors
    timeout_patterns = ["timeout", "timed out", "deadline exceeded", "took too long"]
    for pattern in timeout_patterns:
        if pattern in error_lower:
            return ErrorCategory.TIMEOUT

    # Permission errors
    permission_patterns = [
        "permission denied", "access denied", "unauthorized", "forbidden",
        "401", "403", "not authorized", "authentication failed",
    ]
    for pattern in permission_patterns:
        if pattern in error_lower:
            return ErrorCategory.PERMISSION

    # Git conflicts
    conflict_patterns = [
        "merge conflict", "conflict", "cannot merge", "unmerged",
        "both modified", "diverged",
    ]
    for pattern in conflict_patterns:
        if pattern in error_lower:
            return ErrorCategory.CONFLICT

    # Code errors
    code_patterns = [
        "syntax error", "type error", "name error", "import error",
        "compilation failed", "build failed", "lint error",
        "traceback", "exception", "error:",
    ]
    for pattern in code_patterns:
        if pattern in error_lower:
            return ErrorCategory.CODE_ERROR

    return ErrorCategory.UNKNOWN


async def fail_task_with_retry(
    ralph_id: str,
    task_id: str,
    error_message: str,
    error_category: ErrorCategory | None = None,
) -> tuple[bool, bool]:
    """Mark a task as failed with retry logic.

    Args:
        ralph_id: ID of the Ralph instance
        task_id: ID of the task
        error_message: Error message explaining the failure
        error_category: Optional pre-classified error category

    Returns:
        Tuple of (success, will_retry)
    """
    if error_category is None:
        error_category = classify_error(error_message)

    conn = await get_connection()
    try:
        now = datetime.now()

        # Get current task state
        cursor = await conn.execute(
            "SELECT retry_count, max_retries, started_at FROM task_claims WHERE task_id = ?",
            (task_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return (False, False)

        retry_count = (row[0] or 0) + 1
        max_retries = row[1] or 3
        started_at = row[2]

        # Determine if we should retry
        should_retry = (
            error_category in (ErrorCategory.TRANSIENT, ErrorCategory.TIMEOUT)
            and retry_count <= max_retries
        )

        if should_retry:
            # Calculate next retry time with exponential backoff
            backoff_seconds = min(300, 30 * (2 ** (retry_count - 1)))  # Max 5 minutes
            next_retry = now + timedelta(seconds=backoff_seconds)

            cursor = await conn.execute(
                """UPDATE task_claims
                   SET status = ?,
                       error_category = ?,
                       error_message = ?,
                       retry_count = ?,
                       next_retry_at = ?,
                       claimed_by_ralph_id = NULL,
                       claimed_at = NULL,
                       expires_at = NULL,
                       updated_at = ?
                   WHERE task_id = ?
                     AND claimed_by_ralph_id = ?""",
                (
                    TaskClaimStatus.RETRY_PENDING.value,
                    error_category.value,
                    error_message,
                    retry_count,
                    next_retry,
                    now,
                    task_id,
                    ralph_id,
                )
            )
        else:
            # Mark as permanently failed
            cursor = await conn.execute(
                """UPDATE task_claims
                   SET status = ?,
                       error_category = ?,
                       error_message = ?,
                       retry_count = ?,
                       completed_at = ?,
                       updated_at = ?
                   WHERE task_id = ?
                     AND claimed_by_ralph_id = ?""",
                (
                    TaskClaimStatus.FAILED.value,
                    error_category.value,
                    error_message,
                    retry_count,
                    now,
                    now,
                    task_id,
                    ralph_id,
                )
            )

            # Record in history
            await _record_task_history(conn, task_id, ralph_id, started_at, now, "failed", error_message)

        await conn.commit()

        if cursor.rowcount > 0:
            await _update_instance_task(ralph_id, None)
            # Update instance stats
            await _increment_instance_stats(ralph_id, failed=True, work_seconds=(now - datetime.fromisoformat(started_at)).total_seconds() if started_at else 0)
            return (True, should_retry)
        return (False, False)
    finally:
        await conn.close()


async def process_retry_tasks() -> int:
    """Process tasks that are ready for retry.

    Returns:
        Number of tasks moved back to pending
    """
    conn = await get_connection()
    try:
        now = datetime.now()

        cursor = await conn.execute(
            """UPDATE task_claims
               SET status = 'pending',
                   next_retry_at = NULL,
                   updated_at = ?
               WHERE status = 'retry_pending'
                 AND next_retry_at <= ?""",
            (now, now)
        )
        await conn.commit()
        return cursor.rowcount
    finally:
        await conn.close()


async def list_failed_tasks(project: str | None = None) -> list[TaskClaim]:
    """List all failed tasks with error details."""
    conn = await get_connection()
    try:
        project_filter = "AND project = ?" if project else ""
        params = [project] if project else []

        cursor = await conn.execute(
            f"""SELECT task_id, task_title, task_priority, task_section, project,
                      claimed_by_ralph_id, claimed_at, expires_at, status,
                      completion_message, git_commit_sha, created_at, updated_at,
                      category, error_category, error_message, retry_count, max_retries,
                      next_retry_at, branch_name, has_conflict, started_at, completed_at
               FROM task_claims
               WHERE status IN ('failed', 'retry_pending') {project_filter}
               ORDER BY updated_at DESC""",
            params
        )
        rows = await cursor.fetchall()

        return [_row_to_task_claim(row) for row in rows]
    finally:
        await conn.close()


# ============================================================================
# Pause/Resume Operations (US10)
# ============================================================================

async def pause_instance(ralph_id: str) -> bool:
    """Pause a Ralph instance (finish current task, then idle).

    Args:
        ralph_id: ID of the Ralph instance

    Returns:
        True if instance was paused
    """
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """UPDATE ralph_instances
               SET status = ?
               WHERE ralph_id = ?
                 AND status = 'active'""",
            (RalphInstanceStatus.PAUSED.value, ralph_id)
        )
        await conn.commit()
        return cursor.rowcount > 0
    finally:
        await conn.close()


async def resume_instance(ralph_id: str) -> bool:
    """Resume a paused Ralph instance.

    Args:
        ralph_id: ID of the Ralph instance

    Returns:
        True if instance was resumed
    """
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """UPDATE ralph_instances
               SET status = ?
               WHERE ralph_id = ?
                 AND status = 'paused'""",
            (RalphInstanceStatus.ACTIVE.value, ralph_id)
        )
        await conn.commit()
        return cursor.rowcount > 0
    finally:
        await conn.close()


async def pause_all_instances() -> int:
    """Pause all active Ralph instances.

    Returns:
        Number of instances paused
    """
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """UPDATE ralph_instances
               SET status = ?
               WHERE status = 'active'""",
            (RalphInstanceStatus.PAUSED.value,)
        )
        await conn.commit()
        return cursor.rowcount
    finally:
        await conn.close()


async def resume_all_instances() -> int:
    """Resume all paused Ralph instances.

    Returns:
        Number of instances resumed
    """
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """UPDATE ralph_instances
               SET status = ?
               WHERE status = 'paused'""",
            (RalphInstanceStatus.ACTIVE.value,)
        )
        await conn.commit()
        return cursor.rowcount
    finally:
        await conn.close()


async def stop_all_instances() -> int:
    """Emergency stop all Ralph instances.

    Returns:
        Number of instances stopped
    """
    conn = await get_connection()
    try:
        now = datetime.now()

        # Get all active/paused instances
        cursor = await conn.execute(
            "SELECT ralph_id FROM ralph_instances WHERE status IN ('active', 'paused', 'idle')"
        )
        instances = await cursor.fetchall()

        if not instances:
            return 0

        ralph_ids = [row[0] for row in instances]
        placeholders = ",".join("?" * len(ralph_ids))

        # Release all their claims back to pending
        await conn.execute(
            f"""UPDATE task_claims
               SET status = 'pending',
                   claimed_by_ralph_id = NULL,
                   claimed_at = NULL,
                   expires_at = NULL,
                   updated_at = ?
               WHERE claimed_by_ralph_id IN ({placeholders})
                 AND status = 'in_progress'""",
            [now] + ralph_ids
        )

        # Stop all instances
        cursor = await conn.execute(
            f"""UPDATE ralph_instances
               SET status = ?,
                   current_task_id = NULL
               WHERE ralph_id IN ({placeholders})""",
            [RalphInstanceStatus.STOPPED.value] + ralph_ids
        )

        await conn.commit()
        return cursor.rowcount
    finally:
        await conn.close()


async def release_all_claims_for_instance(ralph_id: str) -> int:
    """Release all claims for a specific instance (e.g., crashed).

    Returns tasks to pending so another Ralph can claim them.

    Args:
        ralph_id: ID of the Ralph instance

    Returns:
        Number of claims released
    """
    conn = await get_connection()
    try:
        now = datetime.now()

        cursor = await conn.execute(
            """UPDATE task_claims
               SET status = 'pending',
                   claimed_by_ralph_id = NULL,
                   claimed_at = NULL,
                   expires_at = NULL,
                   updated_at = ?
               WHERE claimed_by_ralph_id = ?
                 AND status = 'in_progress'""",
            (now, ralph_id)
        )
        await conn.commit()
        return cursor.rowcount
    finally:
        await conn.close()


# ============================================================================
# Statistics (US11)
# ============================================================================

async def get_system_stats() -> SystemStats:
    """Get system-wide statistics."""
    conn = await get_connection()
    try:
        # Task counts
        cursor = await conn.execute(
            """SELECT
                   COUNT(*) as total,
                   SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                   SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) as in_progress,
                   SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
               FROM task_claims"""
        )
        task_row = await cursor.fetchone()

        # Instance counts
        cursor = await conn.execute(
            """SELECT
                   SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active,
                   SUM(CASE WHEN status = 'idle' OR status = 'paused' THEN 1 ELSE 0 END) as idle
               FROM ralph_instances"""
        )
        inst_row = await cursor.fetchone()

        # Tasks per hour (from history in last hour)
        cursor = await conn.execute(
            """SELECT COUNT(*) FROM task_history
               WHERE completed_at >= datetime('now', '-1 hour')"""
        )
        hour_row = await cursor.fetchone()
        tasks_per_hour = float(hour_row[0]) if hour_row else 0.0

        # Session start (earliest active instance)
        cursor = await conn.execute(
            """SELECT MIN(started_at) FROM ralph_instances
               WHERE status IN ('active', 'idle', 'paused')"""
        )
        session_row = await cursor.fetchone()
        session_start = datetime.fromisoformat(session_row[0]) if session_row and session_row[0] else None

        # Calculate ETA
        pending_count = task_row[1] or 0
        eta_minutes = None
        if tasks_per_hour > 0 and pending_count > 0:
            eta_minutes = (pending_count / tasks_per_hour) * 60

        return SystemStats(
            total_tasks=task_row[0] or 0,
            pending_tasks=pending_count,
            in_progress_tasks=task_row[2] or 0,
            completed_tasks=task_row[3] or 0,
            failed_tasks=task_row[4] or 0,
            active_instances=inst_row[0] or 0,
            idle_instances=inst_row[1] or 0,
            tasks_per_hour=tasks_per_hour,
            eta_minutes=eta_minutes,
            session_start=session_start,
        )
    finally:
        await conn.close()


async def _record_task_history(
    conn: Any,
    task_id: str,
    ralph_id: str,
    started_at: str | None,
    completed_at: datetime,
    status: str,
    error_message: str | None = None,
) -> None:
    """Record task completion in history table."""
    # Get task details
    cursor = await conn.execute(
        "SELECT task_title, project, git_commit_sha FROM task_claims WHERE task_id = ?",
        (task_id,)
    )
    row = await cursor.fetchone()
    if not row:
        return

    task_title, project, commit_sha = row
    start_time = datetime.fromisoformat(started_at) if started_at else completed_at
    duration = (completed_at - start_time).total_seconds()

    await conn.execute(
        """INSERT INTO task_history
           (task_id, task_title, ralph_id, project, started_at, completed_at,
            duration_seconds, status, commit_sha, error_message)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (task_id, task_title, ralph_id, project, start_time, completed_at,
         duration, status, commit_sha, error_message)
    )


async def _increment_instance_stats(
    ralph_id: str,
    completed: bool = False,
    failed: bool = False,
    work_seconds: float = 0.0,
) -> None:
    """Increment instance statistics."""
    conn = await get_connection()
    try:
        updates = []
        params = []

        if completed:
            updates.append("tasks_completed = tasks_completed + 1")
        if failed:
            updates.append("tasks_failed = tasks_failed + 1")
        if work_seconds > 0:
            updates.append("total_work_seconds = total_work_seconds + ?")
            params.append(work_seconds)

        if updates:
            params.append(ralph_id)
            await conn.execute(
                f"UPDATE ralph_instances SET {', '.join(updates)} WHERE ralph_id = ?",
                params
            )
            await conn.commit()
    finally:
        await conn.close()


async def list_task_history(
    project: str | None = None,
    ralph_id: str | None = None,
    limit: int = 100,
) -> list[TaskHistory]:
    """List task history with optional filters."""
    conn = await get_connection()
    try:
        conditions = []
        params = []

        if project:
            conditions.append("project = ?")
            params.append(project)
        if ralph_id:
            conditions.append("ralph_id = ?")
            params.append(ralph_id)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        cursor = await conn.execute(
            f"""SELECT task_id, task_title, ralph_id, project, started_at,
                       completed_at, duration_seconds, status, commit_sha, error_message
               FROM task_history
               {where_clause}
               ORDER BY completed_at DESC
               LIMIT ?""",
            params
        )
        rows = await cursor.fetchall()

        return [
            TaskHistory(
                task_id=row[0],
                task_title=row[1],
                ralph_id=row[2],
                project=row[3],
                started_at=datetime.fromisoformat(row[4]),
                completed_at=datetime.fromisoformat(row[5]),
                duration_seconds=row[6],
                status=TaskClaimStatus(row[7]),
                commit_sha=row[8],
                error_message=row[9],
            )
            for row in rows
        ]
    finally:
        await conn.close()


# ============================================================================
# Fix Plan Source Management (US1)
# ============================================================================

async def register_fix_plan_source(
    source_path: str,
    project: str | None = None,
    source_type: str = "file",
) -> int:
    """Register a fix plan source for syncing.

    Args:
        source_path: Path to fix plan file or API endpoint
        project: Project name
        source_type: Type of source ('file', 'github_issues', 'jira')

    Returns:
        Source ID
    """
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """INSERT INTO fix_plan_sources (source_type, source_path, project)
               VALUES (?, ?, ?)
               ON CONFLICT DO NOTHING
               RETURNING id""",
            (source_type, source_path, project)
        )
        row = await cursor.fetchone()
        await conn.commit()
        return row[0] if row else 0
    finally:
        await conn.close()


async def list_fix_plan_sources(project: str | None = None) -> list[dict]:
    """List registered fix plan sources."""
    conn = await get_connection()
    try:
        if project:
            cursor = await conn.execute(
                "SELECT id, source_type, source_path, project, last_synced_at FROM fix_plan_sources WHERE project = ?",
                (project,)
            )
        else:
            cursor = await conn.execute(
                "SELECT id, source_type, source_path, project, last_synced_at FROM fix_plan_sources"
            )
        rows = await cursor.fetchall()

        return [
            {
                "id": row[0],
                "source_type": row[1],
                "source_path": row[2],
                "project": row[3],
                "last_synced_at": row[4],
            }
            for row in rows
        ]
    finally:
        await conn.close()


# ============================================================================
# Enhanced Instance Registration with Config (US9)
# ============================================================================

async def register_ralph_instance_with_config(
    ralph_id: str,
    session_file: str | None = None,
    project: str | None = None,
    config: RalphConfig | None = None,
    targeting: TargetingConfig | None = None,
) -> str:
    """Register a new Ralph instance with configuration.

    Args:
        ralph_id: Unique ID for this Ralph instance
        session_file: Optional path to session file
        project: Optional project being worked on
        config: Optional Ralph configuration
        targeting: Optional task targeting configuration

    Returns:
        The ralph_id
    """
    conn = await get_connection()
    try:
        now = datetime.now()
        hostname = socket.gethostname()
        pid = os.getpid()

        config_json = json.dumps(config.model_dump()) if config else None
        targeting_json = json.dumps(targeting.model_dump()) if targeting else None

        await conn.execute(
            """INSERT INTO ralph_instances
               (ralph_id, hostname, pid, session_file, project, started_at,
                last_heartbeat, status, config_json, targeting_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ralph_id) DO UPDATE SET
                   hostname = excluded.hostname,
                   pid = excluded.pid,
                   session_file = excluded.session_file,
                   project = excluded.project,
                   started_at = excluded.started_at,
                   last_heartbeat = excluded.last_heartbeat,
                   status = excluded.status,
                   config_json = excluded.config_json,
                   targeting_json = excluded.targeting_json""",
            (ralph_id, hostname, pid, session_file, project, now, now,
             RalphInstanceStatus.ACTIVE.value, config_json, targeting_json)
        )
        await conn.commit()

        logger.info(f"Registered Ralph instance with config: {ralph_id}")
        return ralph_id
    finally:
        await conn.close()


async def update_ralph_config(ralph_id: str, config: RalphConfig) -> bool:
    """Update configuration for a Ralph instance.

    Args:
        ralph_id: ID of the Ralph instance
        config: New configuration

    Returns:
        True if updated successfully
    """
    conn = await get_connection()
    try:
        config_json = json.dumps(config.model_dump())
        cursor = await conn.execute(
            "UPDATE ralph_instances SET config_json = ? WHERE ralph_id = ?",
            (config_json, ralph_id)
        )
        await conn.commit()
        return cursor.rowcount > 0
    finally:
        await conn.close()


async def update_ralph_targeting(ralph_id: str, targeting: TargetingConfig) -> bool:
    """Update targeting for a Ralph instance.

    Args:
        ralph_id: ID of the Ralph instance
        targeting: New targeting configuration

    Returns:
        True if updated successfully
    """
    conn = await get_connection()
    try:
        targeting_json = json.dumps(targeting.model_dump())
        cursor = await conn.execute(
            "UPDATE ralph_instances SET targeting_json = ? WHERE ralph_id = ?",
            (targeting_json, ralph_id)
        )
        await conn.commit()
        return cursor.rowcount > 0
    finally:
        await conn.close()


# ============================================================================
# History Export (US12)
# ============================================================================

async def export_task_history_csv(
    output_path: str | Path,
    project: str | None = None,
    ralph_id: str | None = None,
    limit: int = 1000,
) -> int:
    """Export task history to a CSV file.

    Args:
        output_path: Path to write CSV file
        project: Optional project filter
        ralph_id: Optional Ralph filter
        limit: Maximum number of records

    Returns:
        Number of records exported
    """
    import csv

    history = await list_task_history(project=project, ralph_id=ralph_id, limit=limit)

    if not history:
        return 0

    output_path = Path(output_path)
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        # Header row
        writer.writerow([
            "task_id",
            "task_title",
            "ralph_id",
            "project",
            "started_at",
            "completed_at",
            "duration_seconds",
            "status",
            "commit_sha",
            "error_message",
        ])
        # Data rows
        for task in history:
            writer.writerow([
                task.task_id,
                task.task_title,
                task.ralph_id,
                task.project or "",
                task.started_at.isoformat(),
                task.completed_at.isoformat(),
                task.duration_seconds,
                task.status.value,
                task.commit_sha or "",
                task.error_message or "",
            ])

    logger.info(f"Exported {len(history)} history records to {output_path}")
    return len(history)


# ============================================================================
# JSON Export (Feature 3.3)
# ============================================================================

async def export_tasks_json(output_path: str | Path | None = None) -> str:
    """Export all tasks and history to JSON file.

    Args:
        output_path: Optional output path. Defaults to ~/.chiefwiggum/export_{timestamp}.json

    Returns:
        Path to the exported file
    """
    # Default path
    if output_path is None:
        chiefwiggum_dir = Path.home() / ".chiefwiggum"
        chiefwiggum_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = chiefwiggum_dir / f"export_{timestamp}.json"
    else:
        output_path = Path(output_path)

    # Gather all data
    tasks = await list_all_tasks()
    history = await list_task_history(limit=1000)
    instances = await list_all_instances()

    # Convert to dict format
    export_data = {
        "exported_at": datetime.now().isoformat(),
        "metadata": {
            "total_tasks": len(tasks),
            "total_history": len(history),
            "total_instances": len(instances),
        },
        "tasks": [
            {
                "task_id": t.task_id,
                "task_title": t.task_title,
                "task_priority": t.task_priority.value,
                "task_section": t.task_section,
                "project": t.project,
                "category": t.category.value if t.category else None,
                "status": t.status.value,
                "claimed_by_ralph_id": t.claimed_by_ralph_id,
                "claimed_at": t.claimed_at.isoformat() if t.claimed_at else None,
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                "error_category": t.error_category.value if t.error_category else None,
                "error_message": t.error_message,
                "retry_count": t.retry_count,
                "git_commit_sha": t.git_commit_sha,
            }
            for t in tasks
        ],
        "history": [
            {
                "task_id": h.task_id,
                "task_title": h.task_title,
                "ralph_id": h.ralph_id,
                "project": h.project,
                "started_at": h.started_at.isoformat(),
                "completed_at": h.completed_at.isoformat(),
                "duration_seconds": h.duration_seconds,
                "status": h.status.value,
                "commit_sha": h.commit_sha,
                "error_message": h.error_message,
            }
            for h in history
        ],
        "instances": [
            {
                "ralph_id": i.ralph_id,
                "hostname": i.hostname,
                "project": i.project,
                "status": i.status.value,
                "started_at": i.started_at.isoformat(),
                "last_heartbeat": i.last_heartbeat.isoformat(),
                "current_task_id": i.current_task_id,
                "tasks_completed": i.tasks_completed,
                "tasks_failed": i.tasks_failed,
            }
            for i in instances
        ],
    }

    # Write to file
    with open(output_path, "w") as f:
        json.dump(export_data, f, indent=2)

    logger.info(f"Exported data to {output_path}")
    return str(output_path)
