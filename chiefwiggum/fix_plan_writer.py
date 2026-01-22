"""Update @fix_plan.md files with task completion markers.

This module provides functionality to update @fix_plan.md files while preserving
their format. It handles multiple task format conventions and uses file locking
to prevent concurrent write conflicts.
"""

import fcntl
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Regex patterns for the 3 supported formats
# Format 1: ### 22. Task Title
PATTERN_NUMBERED = re.compile(r"^(#{2,4})\s*(\d+)\.\s*(.+?)(\s*[✓✗]|\s*COMPLETE)?$")

# Format 2: #### PF-1: Task Title
PATTERN_ID_BASED = re.compile(
    r"^(#{2,4})\s*([A-Z]+-\d+)[:\s]+(.+?)(\s*[✓✗]|\s*COMPLETE)?$"
)

# Format 3: #### Task Title (plain, no number/ID)
PATTERN_PLAIN = re.compile(r"^(####)\s+([A-Z][^#].{5,})(\s*[✓✗]|\s*COMPLETE)?$")

# Completion markers
COMPLETE_MARKER = " ✓"
INCOMPLETE_MARKERS = [" ✓", " ✗", " COMPLETE"]


def _strip_completion_marker(line: str) -> str:
    """Remove any completion marker from the end of a line."""
    for marker in INCOMPLETE_MARKERS:
        if line.endswith(marker):
            return line[: -len(marker)]
    return line


def _find_task_line(
    lines: list[str], task_id: str, task_number: Optional[int]
) -> Optional[int]:
    """Find the line index for a task by ID or number.

    Args:
        lines: Lines of the file
        task_id: Task ID (e.g., "PF-1" or "task-123")
        task_number: Task number (e.g., 22)

    Returns:
        Line index (0-based), or None if not found
    """
    for i, line in enumerate(lines):
        # Try Format 1: numbered tasks
        if task_number is not None:
            match = PATTERN_NUMBERED.match(line)
            if match and int(match.group(2)) == task_number:
                return i

        # Try Format 2: ID-based tasks
        match = PATTERN_ID_BASED.match(line)
        if match and match.group(2) == task_id:
            return i

        # Try Format 3: plain tasks (match by task_id being in the line)
        # This is less precise but handles the case where task_id is the title
        match = PATTERN_PLAIN.match(line)
        if match and task_id.lower() in line.lower():
            return i

    return None


def update_task_completion_marker(
    fix_plan_path: str | Path,
    task_id: str,
    task_number: Optional[int] = None,
    mark_complete: bool = True,
) -> bool:
    """Update @fix_plan.md to mark a task as complete or incomplete.

    This function handles 3 task formats:
    1. Numbered: "### 22. Task Title" → "### 22. Task Title ✓"
    2. ID-based: "#### PF-1: Task Title" → "#### PF-1: Task Title ✓"
    3. Plain: "#### Task Title" → "#### Task Title ✓"

    The function uses file locking to prevent concurrent write conflicts and
    performs atomic writes (write to temp file, then rename).

    Args:
        fix_plan_path: Path to the @fix_plan.md file
        task_id: Task identifier (e.g., "PF-1", "task-123")
        task_number: Optional task number for numbered format
        mark_complete: True to add ✓ marker, False to remove it

    Returns:
        True if the file was updated successfully, False otherwise
    """
    fix_plan_path = Path(fix_plan_path)

    if not fix_plan_path.exists():
        logger.warning(f"@fix_plan.md not found at {fix_plan_path}")
        return False

    try:
        # Read the file with exclusive lock
        with open(fix_plan_path, "r+", encoding="utf-8") as f:
            # Acquire exclusive lock
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                logger.warning(
                    f"Could not acquire lock on {fix_plan_path} - another process is updating it"
                )
                return False

            try:
                content = f.read()
                lines = content.splitlines(keepends=True)

                # Find the task line
                line_idx = _find_task_line(lines, task_id, task_number)
                if line_idx is None:
                    logger.warning(
                        f"Task {task_id} (number={task_number}) not found in {fix_plan_path}"
                    )
                    return False

                # Get current line
                current_line = lines[line_idx].rstrip("\n\r")

                # Check if marker already present
                has_marker = any(
                    current_line.endswith(marker) for marker in INCOMPLETE_MARKERS
                )

                if mark_complete and has_marker:
                    logger.debug(
                        f"Task {task_id} already marked complete in {fix_plan_path}"
                    )
                    return True  # Already marked, no change needed

                if not mark_complete and not has_marker:
                    logger.debug(
                        f"Task {task_id} already unmarked in {fix_plan_path}"
                    )
                    return True  # Already unmarked, no change needed

                # Update the line
                if mark_complete:
                    # Add completion marker
                    new_line = _strip_completion_marker(current_line) + COMPLETE_MARKER
                else:
                    # Remove completion marker
                    new_line = _strip_completion_marker(current_line)

                # Preserve the original line ending
                original_ending = ""
                if lines[line_idx].endswith("\r\n"):
                    original_ending = "\r\n"
                elif lines[line_idx].endswith("\n"):
                    original_ending = "\n"
                elif lines[line_idx].endswith("\r"):
                    original_ending = "\r"

                lines[line_idx] = new_line + original_ending

                # Write to temporary file in same directory (atomic rename)
                temp_fd, temp_path = tempfile.mkstemp(
                    dir=fix_plan_path.parent, prefix=".fix_plan_", suffix=".tmp"
                )
                try:
                    with open(temp_fd, "w", encoding="utf-8") as temp_f:
                        temp_f.writelines(lines)
                        temp_f.flush()
                        # Ensure data is written to disk
                        import os

                        os.fsync(temp_f.fileno())

                    # Atomic rename
                    shutil.move(temp_path, fix_plan_path)
                    logger.info(
                        f"Updated task {task_id} in {fix_plan_path} (complete={mark_complete})"
                    )
                    return True

                except Exception as e:
                    # Clean up temp file on error
                    try:
                        Path(temp_path).unlink(missing_ok=True)
                    except:
                        pass
                    raise e

            finally:
                # Release lock
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    except Exception as e:
        logger.error(f"Error updating {fix_plan_path} for task {task_id}: {e}")
        return False


def check_task_marked_complete(
    fix_plan_path: str | Path,
    task_id: str,
    task_number: Optional[int] = None,
) -> bool:
    """Check if a task is marked as complete in @fix_plan.md.

    Args:
        fix_plan_path: Path to the @fix_plan.md file
        task_id: Task identifier
        task_number: Optional task number

    Returns:
        True if task is marked complete, False otherwise
    """
    fix_plan_path = Path(fix_plan_path)

    if not fix_plan_path.exists():
        return False

    try:
        with open(fix_plan_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()

            line_idx = _find_task_line(lines, task_id, task_number)
            if line_idx is None:
                return False

            line = lines[line_idx]
            return any(line.endswith(marker) for marker in INCOMPLETE_MARKERS)

    except Exception as e:
        logger.error(f"Error checking task {task_id} in {fix_plan_path}: {e}")
        return False


def create_backup(fix_plan_path: str | Path) -> Optional[Path]:
    """Create a backup of the @fix_plan.md file.

    Args:
        fix_plan_path: Path to the file to backup

    Returns:
        Path to the backup file, or None if backup failed
    """
    fix_plan_path = Path(fix_plan_path)

    if not fix_plan_path.exists():
        return None

    try:
        backup_path = fix_plan_path.with_suffix(".md.backup")
        shutil.copy2(fix_plan_path, backup_path)
        logger.info(f"Created backup at {backup_path}")
        return backup_path
    except Exception as e:
        logger.error(f"Failed to create backup of {fix_plan_path}: {e}")
        return None
