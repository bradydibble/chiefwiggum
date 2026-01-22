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

from chiefwiggum.database import get_connection, get_setting
from chiefwiggum.fix_plan_writer import update_task_completion_marker
from chiefwiggum.git_verifier import verify_commit_in_repo
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

    Supports multiple formats:

    Format 1 (numbered):
    - Section: `## HIGH PRIORITY - Get Data Flowing`
    - Task: `### 22. File Processing Workflow COMPLETE`
    - Subtask: `- [x] Create file upload endpoint`

    Format 2 (ID-based):
    - Section: `## PRODUCT FEEDBACK (IMMEDIATE PRIORITY)`
    - Task: `#### PF-1: Timezone to Pacific`
    - Subtask: `- [ ] Change timezone setting`

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
    task_counter = 0  # For ID-based tasks without numbers

    # Priority section patterns - check most specific first
    section_patterns = [
        # Explicit priority keywords
        (r"##\s*HIGH\s*PRIORITY", TaskPriority.HIGH),
        (r"##\s*MEDIUM\s*PRIORITY", TaskPriority.MEDIUM),
        (r"##\s*LOWER\s*PRIORITY", TaskPriority.LOWER),
        (r"##\s*POLISH", TaskPriority.POLISH),
        # Alternative patterns
        (r"##.*IMMEDIATE\s*PRIORITY", TaskPriority.HIGH),
        (r"##.*CRITICAL", TaskPriority.HIGH),
        (r"##.*Tier\s*1", TaskPriority.HIGH),
        (r"##.*Tier\s*2", TaskPriority.MEDIUM),
        (r"##.*Tier\s*3", TaskPriority.LOWER),
        (r"##.*Tier\s*4", TaskPriority.POLISH),
        (r"###\s*Tier\s*1", TaskPriority.HIGH),
        (r"###\s*Tier\s*2", TaskPriority.MEDIUM),
        (r"###\s*Tier\s*3", TaskPriority.LOWER),
        (r"###\s*Tier\s*4", TaskPriority.POLISH),
    ]

    for line in content.split("\n"):
        line = line.strip()

        # Check for section headers
        for pattern, priority in section_patterns:
            if re.match(pattern, line, re.IGNORECASE):
                current_priority = priority
                # Extract section name after the priority indicator
                match = re.search(r"[-:]\s*(.+)$", line)
                if match:
                    current_section = match.group(1).strip()
                else:
                    # Use the whole line minus ## as section
                    current_section = re.sub(r"^##+\s*", "", line).strip()
                break

        # Check for task headers - multiple formats
        task_match = None
        task_number = None
        title_part = None

        # Format 1: ### N. Title (numbered)
        numbered_match = re.match(r"#{2,4}\s*(\d+)\.\s*(.+)", line)
        if numbered_match:
            task_number = int(numbered_match.group(1))
            title_part = numbered_match.group(2)
            task_match = True

        # Format 2: #### ID-N: Title (ID-based like PF-1, BUG-42)
        if not task_match:
            id_match = re.match(r"#{2,4}\s*([A-Z]+-\d+)[:\s]+(.+)", line)
            if id_match:
                task_counter += 1
                task_number = task_counter
                title_part = f"{id_match.group(1)}: {id_match.group(2)}"
                task_match = True

        # Format 3: #### Title (no number, just header under a Tier section)
        if not task_match and current_priority:
            plain_match = re.match(r"####\s+([A-Z][^#].{5,})", line)  # At least 6 chars, starts with capital
            if plain_match and not plain_match.group(1).startswith("**"):  # Not bold text
                task_counter += 1
                task_number = task_counter
                title_part = plain_match.group(1)
                task_match = True

        if task_match and task_number and title_part and current_priority:
            if current_task:
                # Before appending, check if task is complete based on subtasks
                # A task is complete if it has subtasks and ALL are checked
                if not current_task.is_complete and current_task.completed_subtasks:
                    if not current_task.subtasks:  # No unchecked subtasks remain
                        current_task.is_complete = True
                tasks.append(current_task)

            # Check if complete: "COMPLETE" in title, or checkmark emoji
            is_complete = (
                "COMPLETE" in title_part.upper() or
                "✅" in title_part or
                "✓" in title_part
            )

            # Clean title (remove checkmarks and COMPLETE markers)
            title = re.sub(r"\s*[✅✓]\s*", "", title_part).strip()
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
        # Check if task is complete based on subtasks
        if not current_task.is_complete and current_task.completed_subtasks:
            if not current_task.subtasks:  # No unchecked subtasks remain
                current_task.is_complete = True
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
                # Always infer and update category for existing tasks
                category = infer_task_category(task.subtasks + task.completed_subtasks, task.title)

                if task.is_complete and existing_status != TaskClaimStatus.COMPLETED.value:
                    await conn.execute(
                        """UPDATE task_claims
                           SET status = ?, category = ?, updated_at = ?
                           WHERE task_id = ?""",
                        (TaskClaimStatus.COMPLETED.value, category.value, now, task.task_id)
                    )
                else:
                    # Update category even if status unchanged
                    await conn.execute(
                        """UPDATE task_claims
                           SET category = ?, updated_at = ?
                           WHERE task_id = ?""",
                        (category.value, now, task.task_id)
                    )
            else:
                status = TaskClaimStatus.COMPLETED.value if task.is_complete else TaskClaimStatus.PENDING.value
                # Infer category from task title and subtasks
                category = infer_task_category(task.subtasks + task.completed_subtasks, task.title)
                await conn.execute(
                    """INSERT INTO task_claims
                       (task_id, task_title, task_priority, task_section, project, status, created_at, category)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (task.task_id, task.title, task.priority.value, task.section, project, status, now, category.value)
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

    Uses BEGIN IMMEDIATE to acquire a RESERVED lock at transaction start,
    preventing race conditions where multiple Ralphs claim the same task.

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
        # BEGIN IMMEDIATE acquires RESERVED lock immediately
        # This prevents other Ralphs from claiming until we commit/rollback
        await conn.execute("BEGIN IMMEDIATE")

        now = datetime.now()
        expires_at = now + timedelta(minutes=CLAIM_EXPIRY_MINUTES)

        # Build query with optional project filter
        project_filter = "AND project = ?" if project else ""
        params_base = [project] if project else []

        for priority in PRIORITY_ORDER:
            # Params: SET values, then inner SELECT conditions, then outer WHERE conditions
            params = [ralph_id, now, expires_at, TaskClaimStatus.IN_PROGRESS.value, now, now,
                     priority.value] + params_base + [now, now, now]  # Extra 'now' for claimed_by check and outer WHERE

            query = f"""UPDATE task_claims
                   SET claimed_by_ralph_id = ?,
                       claimed_at = ?,
                       expires_at = ?,
                       status = ?,
                       updated_at = ?,
                       started_at = ?
                   WHERE task_id = (
                       SELECT task_id FROM task_claims
                       WHERE task_priority = ?
                         {project_filter}
                         AND (status = 'pending'
                              OR (status = 'in_progress' AND expires_at < ?))
                         AND (claimed_by_ralph_id IS NULL OR expires_at < ?)
                       ORDER BY created_at ASC
                       LIMIT 1
                   )
                   AND (status = 'pending' OR (status = 'in_progress' AND expires_at < ?))
                   RETURNING task_id, task_title, task_priority, task_section, project"""

            cursor = await conn.execute(query, params)
            result = await cursor.fetchone()

            if result:
                await conn.commit()  # Release lock

                # Clear any stale current_task_id from other Ralphs
                await conn.execute(
                    """UPDATE ralph_instances
                       SET current_task_id = NULL
                       WHERE current_task_id = ? AND ralph_id != ?""",
                    (result[0], ralph_id)
                )
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

        await conn.rollback()  # Release lock if no task found
        return None
    except Exception:
        await conn.rollback()  # Release lock on error
        raise
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

        # Get started_at before updating for history recording
        cursor = await conn.execute(
            "SELECT started_at FROM task_claims WHERE task_id = ? AND claimed_by_ralph_id = ?",
            (task_id, ralph_id)
        )
        row = await cursor.fetchone()
        started_at = row[0] if row else None

        cursor = await conn.execute(
            """UPDATE task_claims
               SET status = ?,
                   completion_message = ?,
                   git_commit_sha = ?,
                   completed_at = ?,
                   updated_at = ?
               WHERE task_id = ?
                 AND claimed_by_ralph_id = ?
                 AND status = 'in_progress'""",
            (TaskClaimStatus.COMPLETED.value, message, commit_sha, now, now, task_id, ralph_id)
        )

        if cursor.rowcount > 0:
            # Read cost data from status file
            from chiefwiggum.spawner import get_ralph_status_path
            cost_data = None
            status_file = get_ralph_status_path(ralph_id)
            if status_file.exists():
                try:
                    status_json = json.loads(status_file.read_text())
                    cost_data = status_json.get("cost_info", {})
                except Exception:
                    pass  # Ignore cost data errors, not critical

            # Record in task_history
            await _record_task_history(conn, task_id, ralph_id, started_at, now, "completed", cost_data=cost_data)
            # Commit before calling helpers that open their own connections
            await conn.commit()
            await _update_instance_task(ralph_id, None)
            # Update instance stats
            work_seconds = (now - datetime.fromisoformat(started_at)).total_seconds() if started_at else 0
            cost_usd = cost_data.get("accumulated_cost", 0.0) if cost_data else 0.0
            input_tok = cost_data.get("input_tokens", 0) if cost_data else 0
            output_tok = cost_data.get("output_tokens", 0) if cost_data else 0
            await _increment_instance_stats(
                ralph_id,
                completed=True,
                work_seconds=work_seconds,
                cost_increment=cost_usd,
                input_tokens=input_tok,
                output_tokens=output_tok
            )

            # Update @fix_plan.md with completion marker
            task_claim = await get_task_claim(task_id)
            if task_claim and task_claim.project:
                await update_fix_plan_on_completion(task_id, task_claim.project)

            return True
        return False
    finally:
        await conn.close()


async def update_fix_plan_on_completion(task_id: str, project: str | None = None) -> bool:
    """Update @fix_plan.md when a task completes in the database.

    Args:
        task_id: ID of the completed task
        project: Project name (used to locate @fix_plan.md)

    Returns:
        True if @fix_plan.md was updated successfully, False otherwise
    """
    # Check if auto-update is enabled
    auto_update = await get_setting("update_fix_plan_on_complete", "true")
    if auto_update.lower() != "true":
        logger.debug(f"Auto-update disabled, skipping @fix_plan.md update for {task_id}")
        return False

    try:
        # Get task details from database
        task_claim = await get_task_claim(task_id)
        if not task_claim:
            logger.warning(f"Task {task_id} not found in database")
            return False

        # Determine fix_plan_path from project
        # Default: look for @fix_plan.md in current directory or ../tian
        fix_plan_path = None
        if project == "tian":
            fix_plan_path = Path("../tian/@fix_plan.md")
        elif project == "chiefwiggum":
            fix_plan_path = Path("@fix_plan.md")
        else:
            # Try current directory first
            fix_plan_path = Path("@fix_plan.md")
            if not fix_plan_path.exists():
                fix_plan_path = Path("..") / (project or "tian") / "@fix_plan.md"

        if not fix_plan_path.exists():
            logger.warning(f"@fix_plan.md not found at {fix_plan_path}")
            return False

        # Extract task number from task_id if possible
        # Task IDs are typically in format "task-22" or "PF-1"
        task_number = None
        if match := re.match(r"task-(\d+)", task_id):
            task_number = int(match.group(1))

        # Update the fix plan file
        success = update_task_completion_marker(
            fix_plan_path=fix_plan_path,
            task_id=task_id,
            task_number=task_number,
            mark_complete=True,
        )

        if success:
            logger.info(f"Updated @fix_plan.md for completed task {task_id}")
        else:
            logger.warning(f"Failed to update @fix_plan.md for task {task_id}")

        return success

    except Exception as e:
        logger.error(f"Error updating @fix_plan.md for task {task_id}: {e}")
        return False


async def verify_and_record_commit(
    task_id: str, commit_sha: str, project: str | None = None
) -> bool:
    """Verify a commit exists in the project repository and record verification.

    Args:
        task_id: ID of the task
        commit_sha: Git commit SHA to verify
        project: Project name (used to determine repo path)

    Returns:
        True if commit was verified and recorded, False otherwise
    """
    # Check if auto-verify is enabled
    auto_verify = await get_setting("verify_commits_on_complete", "true")
    if auto_verify.lower() != "true":
        logger.debug(f"Auto-verify disabled, skipping commit verification for {task_id}")
        return False

    try:
        # Determine repo_path from project
        repo_path = Path(".")
        if project == "tian":
            repo_path = Path("../tian")
        elif project == "chiefwiggum":
            repo_path = Path(".")
        elif project:
            # Try to get from settings
            repo_setting = await get_setting(f"repo_path:{project}")
            if repo_setting:
                repo_path = Path(repo_setting)
            else:
                repo_path = Path("..") / project

        # Verify commit exists
        exists, commit_message = await verify_commit_in_repo(commit_sha, repo_path)

        if not exists:
            logger.warning(
                f"Commit {commit_sha[:8]} not found in repo {repo_path} for task {task_id}"
            )
            return False

        # Record verification timestamp in database
        conn = await get_connection()
        try:
            now = datetime.now()
            await conn.execute(
                """UPDATE task_claims
                   SET verified_at = ?
                   WHERE task_id = ?""",
                (now, task_id),
            )
            await conn.commit()
            logger.info(
                f"Verified commit {commit_sha[:8]} in {repo_path} for task {task_id}"
            )
            return True
        finally:
            await conn.close()

    except Exception as e:
        logger.error(f"Error verifying commit for task {task_id}: {e}")
        return False


async def complete_and_claim_next(
    ralph_id: str,
    task_id: str,
    project: str | None = None,
    commit_sha: str | None = None,
    message: str | None = None,
) -> dict | None:
    """Atomically complete task and claim next in single transaction.

    This prevents race conditions where another Ralph could claim a task
    between complete and claim operations.

    Args:
        ralph_id: ID of the Ralph instance
        task_id: ID of the task being completed
        project: Optional project to filter next task by
        commit_sha: Optional git commit SHA for the completed task
        message: Optional completion message

    Returns:
        Dict with next task info if claim successful, None if no tasks available
    """
    conn = await get_connection()
    try:
        await conn.execute("BEGIN EXCLUSIVE")
        now = datetime.now()

        # 1. Get started_at for history recording
        cursor = await conn.execute(
            "SELECT started_at FROM task_claims WHERE task_id = ? AND claimed_by_ralph_id = ?",
            (task_id, ralph_id)
        )
        row = await cursor.fetchone()
        started_at = row[0] if row else None

        # 2. Mark task complete
        await conn.execute(
            """UPDATE task_claims
               SET status = ?, completion_message = ?, git_commit_sha = ?,
                   completed_at = ?, updated_at = ?
               WHERE task_id = ? AND claimed_by_ralph_id = ?
                 AND status = 'in_progress'""",
            (TaskClaimStatus.COMPLETED.value, message, commit_sha, now, now, task_id, ralph_id)
        )

        # 3. Read cost data from status file
        from chiefwiggum.spawner import get_ralph_status_path
        cost_data = None
        status_file = get_ralph_status_path(ralph_id)
        if status_file.exists():
            try:
                status_json = json.loads(status_file.read_text())
                cost_data = status_json.get("cost_info", {})
            except Exception:
                pass  # Ignore cost data errors, not critical

        # 4. Record in task_history
        await _record_task_history(conn, task_id, ralph_id, started_at, now, "completed", cost_data=cost_data)

        # 5. Clear ralph's current task
        await conn.execute(
            "UPDATE ralph_instances SET current_task_id = NULL WHERE ralph_id = ?",
            (ralph_id,)
        )

        # 6. Claim next task in same transaction (only pending tasks)
        expires_at = now + timedelta(minutes=CLAIM_EXPIRY_MINUTES)
        project_filter = "AND project = ?" if project else ""
        params_base = [project] if project else []

        next_task = None
        for priority in PRIORITY_ORDER:
            params = [ralph_id, now, expires_at, TaskClaimStatus.IN_PROGRESS.value, now, now,
                     priority.value] + params_base

            query = f"""UPDATE task_claims
                   SET claimed_by_ralph_id = ?, claimed_at = ?, expires_at = ?,
                       status = ?, updated_at = ?, started_at = ?
                   WHERE task_id = (
                       SELECT task_id FROM task_claims
                       WHERE task_priority = ?
                         {project_filter}
                         AND status = 'pending'
                       ORDER BY created_at ASC
                       LIMIT 1
                   )
                   AND status = 'pending'
                   RETURNING task_id, task_title, task_priority, task_section, project"""

            cursor = await conn.execute(query, params)
            result = await cursor.fetchone()
            if result:
                # Clear any stale current_task_id from other Ralphs
                await conn.execute(
                    """UPDATE ralph_instances
                       SET current_task_id = NULL
                       WHERE current_task_id = ? AND ralph_id != ?""",
                    (result[0], ralph_id)
                )

                # Update ralph's current task
                await conn.execute(
                    "UPDATE ralph_instances SET current_task_id = ? WHERE ralph_id = ?",
                    (result[0], ralph_id)
                )
                next_task = {
                    "task_id": result[0],
                    "task_title": result[1],
                    "task_priority": result[2],
                    "task_section": result[3],
                    "project": result[4],
                }
                break

        await conn.commit()

        # Update instance stats (outside transaction, uses own connection)
        work_seconds = (now - datetime.fromisoformat(started_at)).total_seconds() if started_at else 0
        cost_usd = cost_data.get("accumulated_cost", 0.0) if cost_data else 0.0
        input_tok = cost_data.get("input_tokens", 0) if cost_data else 0
        output_tok = cost_data.get("output_tokens", 0) if cost_data else 0
        await _increment_instance_stats(
            ralph_id,
            completed=True,
            work_seconds=work_seconds,
            cost_increment=cost_usd,
            input_tokens=input_tok,
            output_tokens=output_tok
        )

        return next_task
    except Exception:
        await conn.rollback()
        raise
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


async def release_claim(ralph_id: str, task_id: str, reason: str = "manual") -> bool:
    """Release a claim without completing or failing the task.

    Returns the task to 'pending' status so another Ralph can claim it.

    Args:
        ralph_id: ID of the Ralph instance
        task_id: ID of the task
        reason: Reason for the release (e.g., "manual", "ralph_crashed", "ralph_died")

    Returns:
        True if claim was released, False if not found or not owned
    """
    conn = await get_connection()
    try:
        now = datetime.now()

        # Get started_at for history recording
        cursor = await conn.execute(
            "SELECT started_at FROM task_claims WHERE task_id = ? AND claimed_by_ralph_id = ?",
            (task_id, ralph_id)
        )
        row = await cursor.fetchone()
        started_at = row[0] if row else None

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

        if cursor.rowcount > 0:
            # Record release in task_history for audit trail
            await _record_task_history(
                conn, task_id, ralph_id, started_at, now,
                "released", error_message=f"Released: {reason}"
            )
            # Commit before calling helpers that open their own connections
            await conn.commit()
            await _update_instance_task(ralph_id, None)
            return True
        return False
    finally:
        await conn.close()


async def check_ralph_completions() -> list[dict]:
    """Check all running Ralphs for task completion markers.

    This function scans the logs of all running Ralph instances for
    TASK_COMPLETE or TASK_FAILED markers and updates task status accordingly.
    It also updates heartbeats for ALL running Ralphs to prevent stale detection.

    Returns:
        List of completion events: [{"ralph_id": str, "task_id": str, "status": "completed"|"failed", "message": str}]
    """
    from chiefwiggum.spawner import check_task_completion, get_running_ralphs, read_ralph_status

    events = []
    running_ralphs = get_running_ralphs()
    running_ralph_ids = {r["ralph_id"] for r in running_ralphs}

    conn = await get_connection()
    try:
        now = datetime.now()

        # Update heartbeat and loop_count for ALL running Ralphs (regardless of task assignment)
        # This prevents instances from showing as STALE when they're actually running
        for ralph_id in running_ralph_ids:
            # Read status file to get loop_count
            status_data = read_ralph_status(ralph_id)
            loop_count = status_data.get("loop_count", 0) if status_data else 0

            await conn.execute(
                """UPDATE ralph_instances
                   SET last_heartbeat = ?, loop_count = ?
                   WHERE ralph_id = ?""",
                (now, loop_count, ralph_id)
            )
        await conn.commit()

        # Extend claim expiry for all running Ralphs with active tasks
        # This prevents claims from expiring while Ralph is still running
        expires_at = now + timedelta(minutes=CLAIM_EXPIRY_MINUTES)
        for ralph_id in running_ralph_ids:
            await conn.execute(
                """UPDATE task_claims
                   SET expires_at = ?, updated_at = ?
                   WHERE claimed_by_ralph_id = ?
                     AND status = 'in_progress'""",
                (expires_at, now, ralph_id)
            )
        await conn.commit()

        # Get all active instances with assigned tasks (for completion checking)
        cursor = await conn.execute(
            """SELECT ri.ralph_id, ri.current_task_id, ri.project, ri.prompt_path
               FROM ralph_instances ri
               WHERE ri.status = 'active'
                 AND ri.current_task_id IS NOT NULL"""
        )
        rows = await cursor.fetchall()

        for row in rows:
            ralph_id = row[0]
            task_id = row[1]
            project = row[2]
            prompt_path = row[3]

            # Check if Ralph is still running
            if ralph_id not in running_ralph_ids:
                # Ralph died without completing - release the task
                await release_claim(ralph_id, task_id, reason="ralph_died")
                events.append({
                    "ralph_id": ralph_id,
                    "task_id": task_id,
                    "status": "released",
                    "message": "Ralph died without completing task",
                })
                continue

            # Check log for completion markers
            completed_task_id, failure_reason, commit_sha = check_task_completion(ralph_id)

            if completed_task_id and completed_task_id == task_id:
                if failure_reason:
                    # Task failed
                    await fail_task(ralph_id, task_id, failure_reason)
                    events.append({
                        "ralph_id": ralph_id,
                        "task_id": task_id,
                        "status": "failed",
                        "message": failure_reason,
                    })
                else:
                    # Task completed successfully - atomically complete and claim next
                    next_task = await complete_and_claim_next(
                        ralph_id, task_id, project=project,
                        commit_sha=commit_sha, message="Completed via log marker"
                    )
                    events.append({
                        "ralph_id": ralph_id,
                        "task_id": task_id,
                        "status": "completed",
                        "message": f"Task completed{f' (commit: {commit_sha[:8]})' if commit_sha else ''}",
                    })

                    if next_task:
                        # Generate and write new prompt to the same path
                        if prompt_path and project:
                            try:
                                from chiefwiggum.spawner import generate_task_prompt, write_ralph_status
                                from chiefwiggum.models import TaskClaim, TaskClaimStatus

                                # Build TaskClaim for prompt generation
                                new_task = TaskClaim(
                                    task_id=next_task["task_id"],
                                    task_title=next_task["task_title"],
                                    task_priority=next_task.get("task_priority", "MEDIUM"),
                                    task_section=next_task.get("task_section"),
                                    project=project,
                                    status=TaskClaimStatus.IN_PROGRESS,
                                )

                                # Get fix_plan path for this project
                                fix_plan_path = Path.home() / "claudecode" / project / "@fix_plan.md"

                                # Generate and write new prompt
                                new_prompt = generate_task_prompt(new_task, fix_plan_path)
                                Path(prompt_path).write_text(new_prompt, encoding="utf-8")
                                logger.info(f"Updated prompt for {ralph_id} with new task: {next_task['task_title']}")

                                # Update status file with new task info so TUI shows fresh activity
                                # Shell will update loop_count, but we set message for immediate TUI feedback
                                write_ralph_status(
                                    ralph_id,
                                    next_task["task_id"],
                                    "working",
                                    loop_count=1,  # Shell will update with real count
                                    message=f"Working on: {next_task['task_title']}"
                                )
                            except Exception as e:
                                logger.warning(f"Failed to update prompt for {ralph_id}: {e}")

                        events.append({
                            "ralph_id": ralph_id,
                            "task_id": next_task["task_id"],
                            "status": "claimed",
                            "message": f"Auto-claimed next task: {next_task['task_title']}",
                        })

        return events
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


async def delete_instance(ralph_id: str) -> bool:
    """Delete a Ralph instance from the database.

    Used for cleaning up stopped/crashed instances ("cattle not pets").
    Task history is preserved in task_claims table.
    Only the instance record is removed.

    Args:
        ralph_id: ID of the Ralph instance

    Returns:
        True if instance was deleted, False if not found
    """
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            "DELETE FROM ralph_instances WHERE ralph_id = ?",
            (ralph_id,)
        )
        await conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info(f"Ralph instance deleted: {ralph_id}")
        return deleted
    finally:
        await conn.close()


async def update_instance_status(ralph_id: str, status: str, error_message: str | None = None) -> bool:
    """Update a Ralph instance status.

    Args:
        ralph_id: ID of the Ralph instance
        status: New status (ACTIVE, IDLE, PAUSED, STOPPED, CRASHED)
        error_message: Optional error message (for CRASHED status)

    Returns:
        True if instance was updated, False if not found
    """
    conn = await get_connection()
    try:
        # Validate status
        try:
            status_enum = RalphInstanceStatus(status.lower())
        except ValueError:
            logger.warning(f"Invalid status '{status}' for Ralph {ralph_id}")
            return False

        # Build update query
        if error_message:
            cursor = await conn.execute(
                """UPDATE ralph_instances
                   SET status = ?,
                       last_error = ?
                   WHERE ralph_id = ?""",
                (status_enum.value, error_message, ralph_id)
            )
        else:
            cursor = await conn.execute(
                """UPDATE ralph_instances
                   SET status = ?
                   WHERE ralph_id = ?""",
                (status_enum.value, ralph_id)
            )

        await conn.commit()

        if cursor.rowcount > 0:
            logger.info(f"Updated Ralph {ralph_id} status to {status_enum.value}")
            return True
        else:
            logger.warning(f"Ralph instance {ralph_id} not found for status update")
            return False
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

        # Get tasks that will be released for history recording
        cursor = await conn.execute(
            f"""SELECT task_id, claimed_by_ralph_id, started_at FROM task_claims
               WHERE claimed_by_ralph_id IN ({placeholders})
                 AND status = 'in_progress'""",
            stale_ids
        )
        released_tasks = await cursor.fetchall()

        # Release tasks back to pending
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

        # Record history for each released task
        for task_id, ralph_id, started_at in released_tasks:
            await _record_task_history(
                conn, task_id, ralph_id, started_at, now,
                "released", error_message="Released: ralph_crashed"
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

        # Verify commit in target repository
        task_claim = await get_task_claim(task_id)
        if task_claim and task_claim.project and commit_sha:
            await verify_and_record_commit(task_id, commit_sha, task_claim.project)

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

            # Read cost data from status file
            from chiefwiggum.spawner import get_ralph_status_path
            cost_data = None
            status_file = get_ralph_status_path(ralph_id)
            if status_file.exists():
                try:
                    status_json = json.loads(status_file.read_text())
                    cost_data = status_json.get("cost_info", {})
                except Exception:
                    pass  # Ignore cost data errors, not critical

            # Record in history
            await _record_task_history(conn, task_id, ralph_id, started_at, now, "failed", error_message, cost_data=cost_data)

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
    cost_data: dict | None = None,
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

    # Extract cost fields
    input_tokens = cost_data.get("input_tokens", 0) if cost_data else 0
    output_tokens = cost_data.get("output_tokens", 0) if cost_data else 0
    cache_creation = cost_data.get("cache_creation_tokens", 0) if cost_data else 0
    cache_read = cost_data.get("cache_read_tokens", 0) if cost_data else 0
    estimated_cost = cost_data.get("accumulated_cost", 0.0) if cost_data else 0.0
    cost_source = cost_data.get("source", "estimation") if cost_data else "estimation"
    model_used = "claude-sonnet-4.5"  # Default model

    await conn.execute(
        """INSERT INTO task_history
           (task_id, task_title, ralph_id, project, started_at, completed_at,
            duration_seconds, status, commit_sha, error_message,
            input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
            estimated_cost_usd, cost_source, model_used)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (task_id, task_title, ralph_id, project, start_time, completed_at,
         duration, status, commit_sha, error_message,
         input_tokens, output_tokens, cache_creation, cache_read,
         estimated_cost, cost_source, model_used)
    )


async def _increment_instance_stats(
    ralph_id: str,
    completed: bool = False,
    failed: bool = False,
    work_seconds: float = 0.0,
    cost_increment: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
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
        if cost_increment > 0:
            updates.append("total_cost_usd = total_cost_usd + ?")
            params.append(cost_increment)
            updates.append("total_input_tokens = total_input_tokens + ?")
            params.append(input_tokens)
            updates.append("total_output_tokens = total_output_tokens + ?")
            params.append(output_tokens)
            updates.append("last_cost_update = ?")
            params.append(datetime.now().isoformat())

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


async def get_cost_stats(
    project: str | None = None,
    ralph_id: str | None = None,
    since_date: datetime | None = None
) -> dict:
    """Get aggregated cost statistics.

    Args:
        project: Filter by project name
        ralph_id: Filter by Ralph instance ID
        since_date: Filter tasks completed since this date

    Returns:
        Dict with cost statistics including:
        - task_count: Number of tasks
        - total_cost_usd: Total estimated cost
        - avg_cost_per_task: Average cost per task
        - total_input_tokens: Total input tokens used
        - total_output_tokens: Total output tokens used
    """
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
        if since_date:
            conditions.append("completed_at >= ?")
            params.append(since_date.isoformat())

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        cursor = await conn.execute(
            f"""SELECT
                COUNT(*) as task_count,
                SUM(estimated_cost_usd) as total_cost,
                AVG(estimated_cost_usd) as avg_cost_per_task,
                SUM(input_tokens) as total_input_tokens,
                SUM(output_tokens) as total_output_tokens
            FROM task_history
            {where_clause}""",
            params
        )
        row = await cursor.fetchone()

        return {
            "task_count": row[0] or 0,
            "total_cost_usd": row[1] or 0.0,
            "avg_cost_per_task": row[2] or 0.0,
            "total_input_tokens": row[3] or 0,
            "total_output_tokens": row[4] or 0,
        }
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
    prompt_path: str | None = None,
) -> str:
    """Register a new Ralph instance with configuration.

    Args:
        ralph_id: Unique ID for this Ralph instance
        session_file: Optional path to session file
        project: Optional project being worked on
        config: Optional Ralph configuration
        targeting: Optional task targeting configuration
        prompt_path: Optional path to the prompt file this Ralph reads from

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
                last_heartbeat, status, config_json, targeting_json, prompt_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ralph_id) DO UPDATE SET
                   hostname = excluded.hostname,
                   pid = excluded.pid,
                   session_file = excluded.session_file,
                   project = excluded.project,
                   started_at = excluded.started_at,
                   last_heartbeat = excluded.last_heartbeat,
                   status = excluded.status,
                   config_json = excluded.config_json,
                   targeting_json = excluded.targeting_json,
                   prompt_path = excluded.prompt_path""",
            (ralph_id, hostname, pid, session_file, project, now, now,
             RalphInstanceStatus.ACTIVE.value, config_json, targeting_json, prompt_path)
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


# ============================================================================
# Task Assignment Strategies
# ============================================================================


async def get_next_task_for_ralph(
    ralph_id: str,
    strategy: str = "priority",
    project: str | None = None,
    categories: list[TaskCategory] | None = None,
) -> dict | None:
    """Get the next task for a ralph based on the assignment strategy.

    Args:
        ralph_id: ID of the Ralph instance claiming the task
        strategy: Assignment strategy - 'priority', 'round_robin', or 'specialized'
        project: Optional project to filter tasks by
        categories: Optional categories for specialized assignment

    Returns:
        Dict with task info if available, None otherwise
    """
    if strategy == "priority":
        return await claim_next_by_priority(ralph_id, project)
    elif strategy == "round_robin":
        return await claim_next_round_robin(ralph_id, project)
    elif strategy == "specialized":
        return await claim_next_by_category(ralph_id, project, categories or [])
    else:
        return await claim_next_by_priority(ralph_id, project)


async def claim_next_by_priority(ralph_id: str, project: str | None = None) -> dict | None:
    """Claim the highest priority unclaimed task. Default behavior.

    Args:
        ralph_id: ID of the Ralph instance
        project: Optional project filter

    Returns:
        Dict with task info if claimed, None otherwise
    """
    return await claim_task(ralph_id, project)


async def claim_next_round_robin(ralph_id: str, project: str | None = None) -> dict | None:
    """Distribute tasks evenly across ralphs using round-robin.

    Args:
        ralph_id: ID of the Ralph instance
        project: Optional project filter

    Returns:
        Dict with task info if claimed, None otherwise
    """
    conn = await get_connection()
    try:
        now = datetime.now()
        expires_at = now + timedelta(minutes=CLAIM_EXPIRY_MINUTES)

        # Build query with optional project filter
        project_filter = "AND project = ?" if project else ""
        params_base = [project] if project else []

        # Get count of tasks currently assigned to each ralph
        # Then assign to the ralph with the fewest active tasks
        # For simplicity, we use oldest pending task to distribute work

        for priority in PRIORITY_ORDER:
            params = [ralph_id, now, expires_at, TaskClaimStatus.IN_PROGRESS.value, now,
                     priority.value] + params_base + [now]

            # Use random selection within priority level for distribution
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
                       ORDER BY RANDOM()
                       LIMIT 1
                   )
                   RETURNING task_id, task_title, task_priority, task_section, project"""

            cursor = await conn.execute(query, params)
            result = await cursor.fetchone()

            if result:
                await conn.commit()

                # Clear any stale current_task_id from other Ralphs
                await conn.execute(
                    """UPDATE ralph_instances
                       SET current_task_id = NULL
                       WHERE current_task_id = ? AND ralph_id != ?""",
                    (result[0], ralph_id)
                )
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


async def claim_next_by_category(
    ralph_id: str,
    project: str | None = None,
    categories: list[TaskCategory] | None = None,
) -> dict | None:
    """Claim a task matching the specified categories.

    Args:
        ralph_id: ID of the Ralph instance
        project: Optional project filter
        categories: List of categories this ralph handles

    Returns:
        Dict with task info if claimed, None otherwise
    """
    if not categories:
        # Fall back to priority if no categories specified
        return await claim_next_by_priority(ralph_id, project)

    conn = await get_connection()
    found_task = None
    try:
        now = datetime.now()
        expires_at = now + timedelta(minutes=CLAIM_EXPIRY_MINUTES)

        # Build category filter
        cat_values = [c.value for c in categories]
        cat_placeholders = ",".join("?" * len(cat_values))
        category_filter = f"AND category IN ({cat_placeholders})"

        # Build project filter
        project_filter = "AND project = ?" if project else ""
        params_base = [project] if project else []

        for priority in PRIORITY_ORDER:
            params = [ralph_id, now, expires_at, TaskClaimStatus.IN_PROGRESS.value, now,
                     priority.value] + cat_values + params_base + [now]

            query = f"""UPDATE task_claims
                   SET claimed_by_ralph_id = ?,
                       claimed_at = ?,
                       expires_at = ?,
                       status = ?,
                       updated_at = ?
                   WHERE task_id = (
                       SELECT task_id FROM task_claims
                       WHERE task_priority = ?
                         {category_filter}
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

                # Clear any stale current_task_id from other Ralphs
                await conn.execute(
                    """UPDATE ralph_instances
                       SET current_task_id = NULL
                       WHERE current_task_id = ? AND ralph_id != ?""",
                    (result[0], ralph_id)
                )
                await conn.commit()

                await _update_instance_task(ralph_id, result[0])
                found_task = {
                    "task_id": result[0],
                    "task_title": result[1],
                    "task_priority": result[2],
                    "task_section": result[3],
                    "project": result[4],
                    "claimed_at": now.isoformat(),
                    "expires_at": expires_at.isoformat(),
                }
                break
    finally:
        await conn.close()

    # Return found task or fallback to priority strategy
    if found_task:
        return found_task
    return await claim_next_by_priority(ralph_id, project)


def get_assigned_categories(ralph_id: str) -> list[TaskCategory]:
    """Get the categories assigned to a ralph based on its ID prefix.

    Args:
        ralph_id: ID of the Ralph instance

    Returns:
        List of TaskCategory values this ralph should handle
    """
    from chiefwiggum.config import get_category_assignments

    assignments = get_category_assignments()
    for prefix, categories in assignments.items():
        if ralph_id.startswith(prefix):
            return [TaskCategory(c) for c in categories if hasattr(TaskCategory, c.upper())]
    return []


# ============================================================================
# Auto-Scaling Logic
# ============================================================================


async def analyze_category_backlog() -> dict[str, float]:
    """Count pending tasks per category, weighted by priority.

    Returns:
        Dict mapping category names to weighted pending counts
    """
    from collections import defaultdict

    pending = await list_pending_tasks()
    needs: dict[str, float] = defaultdict(float)
    priority_weights = {
        TaskPriority.HIGH: 4.0,
        TaskPriority.MEDIUM: 2.0,
        TaskPriority.LOWER: 1.0,
        TaskPriority.POLISH: 0.5,
    }

    for task in pending:
        weight = priority_weights.get(task.task_priority, 1.0)
        category_name = task.category.value if task.category else "general"
        needs[category_name] += weight

    return dict(needs)


async def get_idle_ralphs(older_than_minutes: int = 30) -> list[RalphInstance]:
    """Get ralph instances that have been idle for too long.

    Args:
        older_than_minutes: Consider idle if no task for this long

    Returns:
        List of idle RalphInstance objects
    """
    instances = await list_active_instances()
    now = datetime.now()
    idle_ralphs = []

    for inst in instances:
        if inst.status == RalphInstanceStatus.IDLE:
            idle_seconds = (now - inst.last_heartbeat).total_seconds()
            if idle_seconds > older_than_minutes * 60:
                idle_ralphs.append(inst)

    return idle_ralphs


async def should_spawn_ralph() -> tuple[bool, str | None]:
    """Check if a new ralph should be spawned based on auto-scaling config.

    Returns:
        Tuple of (should_spawn, suggested_category_or_none)
    """
    from chiefwiggum.config import get_auto_scaling_config
    from chiefwiggum.spawner import get_running_ralphs

    config = get_auto_scaling_config()

    if not config["auto_spawn_enabled"]:
        return False, None

    # Check pending count against threshold
    pending = await list_pending_tasks()
    if len(pending) <= config["auto_spawn_threshold"]:
        return False, None

    # Check max concurrent limit
    running = len(get_running_ralphs())
    if running >= config["max_concurrent_ralphs"]:
        return False, None

    # Analyze category needs to suggest specialization
    category_needs = await analyze_category_backlog()
    if category_needs:
        highest_need = max(category_needs, key=lambda k: category_needs[k])
        return True, highest_need

    return True, None


async def cleanup_idle_ralphs(idle_minutes: int | None = None) -> int:
    """Stop ralphs that have been idle for too long.

    Only cleans up if there are no pending tasks.

    Args:
        idle_minutes: Override config idle timeout

    Returns:
        Number of ralphs cleaned up
    """
    from chiefwiggum.config import get_auto_scaling_config
    from chiefwiggum.spawner import stop_ralph_daemon

    config = get_auto_scaling_config()
    if not config["auto_cleanup_enabled"]:
        return 0

    timeout = idle_minutes or config["auto_cleanup_idle_minutes"]

    # Only cleanup if no pending work
    pending = await list_pending_tasks()
    if len(pending) > 0:
        return 0

    idle_ralphs = await get_idle_ralphs(timeout)
    cleaned = 0

    for ralph in idle_ralphs:
        try:
            stop_ralph_daemon(ralph.ralph_id, force=False)
            await shutdown_instance(ralph.ralph_id)
            cleaned += 1
            logger.info(f"Auto-cleaned idle ralph: {ralph.ralph_id}")
        except Exception as e:
            logger.warning(f"Failed to cleanup ralph {ralph.ralph_id}: {e}")

    return cleaned


async def count_pending_tasks() -> int:
    """Get count of pending tasks.

    Returns:
        Number of pending tasks
    """
    pending = await list_pending_tasks()
    return len(pending)


async def count_running_ralphs() -> int:
    """Get count of running ralph instances.

    Returns:
        Number of running ralphs
    """
    from chiefwiggum.spawner import get_running_ralphs
    return len(get_running_ralphs())


async def reconcile_completed_tasks(
    project: str | None = None, dry_run: bool = False
) -> dict:
    """Reconcile all completed tasks with @fix_plan.md.

    For each completed task in the database:
    1. Check if @fix_plan.md has a completion marker (✓)
    2. If not, verify the commit exists in git (if git_commit_sha present)
    3. If commit verified (or no commit needed), update @fix_plan.md

    Args:
        project: Optional project filter (e.g., "tian", "chiefwiggum")
        dry_run: If True, report what would be done without making changes

    Returns:
        Dictionary with:
        - scanned: Total tasks checked
        - updated: Tasks marked complete in file
        - skipped: Tasks already marked complete
        - failed: Verification or update failures
        - details: List of per-task results
    """
    from chiefwiggum.fix_plan_writer import check_task_marked_complete

    result = {
        "scanned": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "details": [],
    }

    try:
        # Query all completed tasks
        conn = await get_connection()
        try:
            query = "SELECT * FROM task_claims WHERE status = 'completed'"
            params = []
            if project:
                query += " AND project = ?"
                params.append(project)

            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
        finally:
            await conn.close()

        result["scanned"] = len(rows)

        # Determine fix_plan_path
        fix_plan_path = None
        if project == "tian":
            fix_plan_path = Path("../tian/@fix_plan.md")
        elif project == "chiefwiggum":
            fix_plan_path = Path("@fix_plan.md")
        else:
            # Try current directory first
            fix_plan_path = Path("@fix_plan.md")
            if not fix_plan_path.exists():
                fix_plan_path = Path("..") / (project or "tian") / "@fix_plan.md"

        if not fix_plan_path.exists():
            logger.error(f"@fix_plan.md not found at {fix_plan_path}")
            result["failed"] = result["scanned"]
            return result

        # Process each completed task
        for row in rows:
            task_claim = _row_to_task_claim(row)
            task_id = task_claim.task_id

            # Extract task number from task_id
            task_number = None
            if match := re.match(r"task-(\d+)", task_id):
                task_number = int(match.group(1))

            # Check if already marked complete
            already_marked = check_task_marked_complete(
                fix_plan_path, task_id, task_number
            )

            if already_marked:
                result["skipped"] += 1
                result["details"].append(
                    {
                        "task_id": task_id,
                        "action": "skipped",
                        "reason": "already_marked_complete",
                    }
                )
                continue

            # If task has a git commit, verify it exists
            commit_verified = False
            if task_claim.git_commit_sha:
                repo_path = Path(".")
                if task_claim.project == "tian":
                    repo_path = Path("../tian")
                elif task_claim.project == "chiefwiggum":
                    repo_path = Path(".")
                elif task_claim.project:
                    # Try to get from settings
                    repo_setting = await get_setting(f"repo_path:{task_claim.project}")
                    if repo_setting:
                        repo_path = Path(repo_setting)
                    else:
                        repo_path = Path("..") / task_claim.project

                exists, _ = await verify_commit_in_repo(
                    task_claim.git_commit_sha, repo_path
                )

                if not exists:
                    result["failed"] += 1
                    result["details"].append(
                        {
                            "task_id": task_id,
                            "action": "failed",
                            "reason": f"commit_not_found: {task_claim.git_commit_sha[:8]}",
                        }
                    )
                    logger.warning(
                        f"Reconcile: Commit {task_claim.git_commit_sha[:8]} not found for task {task_id}"
                    )
                    continue

                commit_verified = True

            # Update @fix_plan.md
            if not dry_run:
                success = update_task_completion_marker(
                    fix_plan_path=fix_plan_path,
                    task_id=task_id,
                    task_number=task_number,
                    mark_complete=True,
                )

                if success:
                    result["updated"] += 1
                    result["details"].append(
                        {
                            "task_id": task_id,
                            "action": "marked_complete",
                            "commit_verified": commit_verified,
                        }
                    )
                    logger.info(f"Reconcile: Marked task {task_id} as complete")
                else:
                    result["failed"] += 1
                    result["details"].append(
                        {
                            "task_id": task_id,
                            "action": "failed",
                            "reason": "update_failed",
                        }
                    )
                    logger.error(
                        f"Reconcile: Failed to mark task {task_id} as complete"
                    )
            else:
                # Dry run - just report what would be done
                result["updated"] += 1
                result["details"].append(
                    {
                        "task_id": task_id,
                        "action": "would_mark_complete",
                        "commit_verified": commit_verified,
                    }
                )

        logger.info(
            f"Reconciliation complete: scanned={result['scanned']}, "
            f"updated={result['updated']}, skipped={result['skipped']}, "
            f"failed={result['failed']}"
        )

        return result

    except Exception as e:
        logger.error(f"Error during reconciliation: {e}")
        result["failed"] = result["scanned"] - result["updated"] - result["skipped"]
        return result


def extract_progress_from_logs(ralph_id: str) -> dict:
    """Parse Ralph logs for progress markers and last update time.

    Looks for patterns like:
    - "Processing 5/10"
    - "[50%]"
    - "Step 3 of 6"
    - Progress bar patterns

    Args:
        ralph_id: The Ralph instance ID

    Returns:
        Dict with keys:
        - percent: int (0-100, or -1 if unknown)
        - last_update: datetime of last log line
    """
    from chiefwiggum.spawner import read_ralph_log

    result = {"percent": -1, "last_update": None}

    try:
        log_content = read_ralph_log(ralph_id, lines=50)
        if not log_content:
            return result

        lines = log_content.strip().split("\n")
        if not lines:
            return result

        # Get timestamp from last line (if available)
        # Many log formats include timestamps at the start
        last_line = lines[-1] if lines else ""

        # Try to extract timestamp from log line
        # Common format: [HH:MM:SS] or YYYY-MM-DD HH:MM:SS
        timestamp_match = re.search(r"(\d{2}:\d{2}:\d{2})", last_line)
        if timestamp_match:
            try:
                time_str = timestamp_match.group(1)
                today = datetime.now().date()
                result["last_update"] = datetime.combine(today, datetime.strptime(time_str, "%H:%M:%S").time())
            except ValueError:
                pass

        # If no timestamp in log, use file modification time as proxy
        if result["last_update"] is None:
            from chiefwiggum.paths import get_paths
            log_path = get_paths().log_dir / f"ralph-{ralph_id}.log"
            if log_path.exists():
                result["last_update"] = datetime.fromtimestamp(log_path.stat().st_mtime)

        # Look for progress indicators in recent lines (scan last 20)
        for line in reversed(lines[-20:]):
            line_lower = line.lower()

            # Pattern: "X/Y" (e.g., "Processing 5/10")
            ratio_match = re.search(r"(\d+)\s*/\s*(\d+)", line)
            if ratio_match:
                current = int(ratio_match.group(1))
                total = int(ratio_match.group(2))
                if total > 0 and current <= total:
                    result["percent"] = int(current / total * 100)
                    break

            # Pattern: "[XX%]" or "XX%"
            percent_match = re.search(r"(\d{1,3})\s*%", line)
            if percent_match:
                pct = int(percent_match.group(1))
                if 0 <= pct <= 100:
                    result["percent"] = pct
                    break

            # Pattern: "Step X of Y"
            step_match = re.search(r"step\s+(\d+)\s+of\s+(\d+)", line_lower)
            if step_match:
                current = int(step_match.group(1))
                total = int(step_match.group(2))
                if total > 0 and current <= total:
                    result["percent"] = int(current / total * 100)
                    break

            # Pattern: "Phase X/Y" or "Task X/Y"
            phase_match = re.search(r"(?:phase|task)\s+(\d+)\s*/\s*(\d+)", line_lower)
            if phase_match:
                current = int(phase_match.group(1))
                total = int(phase_match.group(2))
                if total > 0 and current <= total:
                    result["percent"] = int(current / total * 100)
                    break

    except Exception as e:
        logger.debug(f"Error extracting progress for {ralph_id}: {e}")

    return result


def get_all_instance_progress() -> dict[str, dict]:
    """Get progress data for all active Ralph instances.

    Returns:
        Dict mapping ralph_id -> {percent: int, last_update: datetime}
    """
    from chiefwiggum.spawner import get_running_ralphs

    progress_data = {}
    running_ralphs = get_running_ralphs()

    for ralph_info in running_ralphs:
        # get_running_ralphs returns list of dicts with 'ralph_id' key
        ralph_id = ralph_info.get("ralph_id") if isinstance(ralph_info, dict) else ralph_info
        if ralph_id:
            progress_data[ralph_id] = extract_progress_from_logs(ralph_id)

    return progress_data
