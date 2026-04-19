"""ChiefWiggum Ralph Spawner

Spawn and manage Ralph (Claude Code) instances as background daemons.
"""

import logging
import os
import shutil
import signal
import socket
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from chiefwiggum.cache import process_health_cache
from chiefwiggum.config import get_ralph_loop_settings
from chiefwiggum.database import get_setting, set_setting
from chiefwiggum.models import RalphConfig, TargetingConfig, TaskClaim
from chiefwiggum.paths import get_paths
from chiefwiggum.scripts import get_ralph_loop_path

logger = logging.getLogger(__name__)


def _get_ralph_data_dir() -> Path:
    """Get the Ralph data directory (for session files and logs)."""
    return get_paths().ralphs_dir


def _get_task_prompts_dir() -> Path:
    """Get the task prompts directory."""
    return get_paths().task_prompts_dir


def _get_status_dir() -> Path:
    """Get the status directory."""
    return get_paths().status_dir


def _load_dotenv_into(env: dict, dotenv_path: Path) -> None:
    """Load .env file into env dict without overriding existing vars."""
    if not dotenv_path.exists():
        return
    with open(dotenv_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in env:  # don't override existing env vars
                env[key] = value


def _validate_spawn_requirements() -> tuple[bool, str]:
    """Synchronous validation of spawn requirements.

    Checks:
    1. ANTHROPIC_API_KEY is set
    2. Claude CLI is available

    Note: This does NOT check concurrent Ralph limits (requires async).
    Use can_spawn_ralph() for complete validation including limits.

    Returns:
        Tuple of (can_spawn, reason)
    """
    # Check 1: API key
    # If ANTHROPIC_API_KEY is set at all, require a real `sk-ant-` prefix — this
    # catches placeholder values like "new-api-key" that would otherwise be
    # forwarded into the spawned ralph and fail at the Claude CLI boundary.
    # If the env var is NOT set, allow the spawn to proceed: Claude Code also
    # supports `claude login` (OAuth / stored session tokens) which doesn't
    # require ANTHROPIC_API_KEY at all. The real auth check happens in the
    # spawned process.
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key and not api_key.startswith("sk-ant-"):
        return (False, "ANTHROPIC_API_KEY appears invalid (must start with sk-ant-). Unset it or set a real key.")

    # Check 2: Claude CLI available
    if shutil.which("claude") is None:
        return (False, "Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code")

    return (True, "Requirements validated")


def _ensure_project_permissions(project_dir: Path) -> None:
    """Ensure project has Claude Code permissions for autonomous Ralph operation.

    Updates the project-level .claude/settings.json to add Edit and other
    necessary permissions while preserving existing settings.
    This abstracts permission management from the user.
    """
    import json

    claude_dir = project_dir / ".claude"
    settings_file = claude_dir / "settings.json"

    # Required permissions for Ralph - autonomous code modification
    # Note: Uses colon format Bash(cmd:*) to match Claude CLI expectations
    required_allow = [
        # Core tools
        "Read",
        "Write",
        "Edit",  # Essential for modifying existing files
        "Glob",
        "Grep",
        # Bash commands - comprehensive set for autonomous operation
        "Bash(git:*)",
        "Bash(npm:*)",
        "Bash(yarn:*)",
        "Bash(pip:*)",
        "Bash(uv:*)",
        "Bash(pytest:*)",
        "Bash(python:*)",
        "Bash(python3:*)",
        "Bash(node:*)",
        "Bash(make:*)",
        "Bash(ruff:*)",
        "Bash(mypy:*)",
        "Bash(cargo:*)",
        "Bash(ls:*)",
        "Bash(cat:*)",
        "Bash(head:*)",
        "Bash(tail:*)",
        "Bash(mkdir:*)",
        "Bash(cp:*)",
        "Bash(mv:*)",
        "Bash(touch:*)",
        "Bash(chmod:*)",
        "Bash(echo:*)",
        "Bash(grep:*)",
        "Bash(find:*)",
        "Bash(sed:*)",
    ]

    # Create .claude directory if needed
    claude_dir.mkdir(parents=True, exist_ok=True)

    # Load existing settings or start fresh
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except json.JSONDecodeError:
            settings = {}
    else:
        settings = {}

    # Ensure permissions structure exists
    if "permissions" not in settings:
        settings["permissions"] = {}
    if "allow" not in settings["permissions"]:
        settings["permissions"]["allow"] = []

    # Add required permissions if missing
    allow_list = settings["permissions"]["allow"]
    modified = False
    for perm in required_allow:
        if perm not in allow_list:
            allow_list.append(perm)
            modified = True
            logger.info(f"Added '{perm}' permission for Ralph")

    # Set defaultMode to bypassPermissions for autonomous operation
    # This is critical - without it, dontAsk mode blocks even allowed tools
    if settings["permissions"].get("defaultMode") != "bypassPermissions":
        settings["permissions"]["defaultMode"] = "bypassPermissions"
        modified = True
        logger.info("Set defaultMode to bypassPermissions for autonomous Ralph operation")

    # Write back if modified
    if modified:
        settings_file.write_text(json.dumps(settings, indent=2))
        logger.info(f"Updated Claude permissions at {settings_file}")


def _get_ralphs_by_working_dir() -> dict[Path, list[str]]:
    """Get a mapping of working directories to Ralph IDs.

    This helps detect if multiple Ralphs are running on the same project,
    which can cause session file conflicts.

    Returns:
        Dict mapping working_dir -> list of ralph_ids
    """
    result: dict[Path, list[str]] = {}
    data_dir = _ensure_data_dir()

    for pid_file in data_dir.glob("*.pid"):
        ralph_id = pid_file.stem
        running, _ = is_ralph_running(ralph_id)
        if running:
            # Try to get working dir from the log file
            log_path = get_ralph_log_path(ralph_id)
            if log_path.exists():
                try:
                    content = log_path.read_text()
                    # Look for "Fix Plan:" line which contains the path
                    for line in content.split("\n"):
                        if line.startswith("Fix Plan:"):
                            fix_plan_path = Path(line.split(":", 1)[1].strip())
                            working_dir = fix_plan_path.parent.resolve()
                            if working_dir not in result:
                                result[working_dir] = []
                            result[working_dir].append(ralph_id)
                            break
                except Exception:
                    pass

    return result


def _ensure_data_dir() -> Path:
    """Ensure the Ralph data directory exists."""
    data_dir = _get_ralph_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def generate_ralph_id(name: str | None = None) -> str:
    """Generate a unique Ralph ID."""
    hostname = socket.gethostname().split(".")[0]
    unique = uuid.uuid4().hex[:4]
    if name:
        return f"{hostname}-{name}-{unique}"
    return f"{hostname}-{unique}"


def get_ralph_log_path(ralph_id: str) -> Path:
    """Get the log file path for a Ralph instance."""
    return _ensure_data_dir() / f"{ralph_id}.log"


def get_ralph_pid_path(ralph_id: str) -> Path:
    """Get the PID file path for a Ralph instance."""
    return _ensure_data_dir() / f"{ralph_id}.pid"


def get_ralph_session_path(ralph_id: str) -> Path:
    """Get the session file path for a Ralph instance."""
    return _ensure_data_dir() / f"{ralph_id}.session"


def get_ralph_status_path(ralph_id: str) -> Path:
    """Get the status file path for a Ralph instance."""
    status_dir = _get_status_dir()
    status_dir.mkdir(parents=True, exist_ok=True)
    return status_dir / f"{ralph_id}.json"


def get_task_prompt_path(ralph_id: str, task_id: str) -> Path:
    """Get the task-specific prompt file path."""
    prompts_dir = _get_task_prompts_dir()
    prompts_dir.mkdir(parents=True, exist_ok=True)
    return prompts_dir / f"{ralph_id}_{task_id}.md"


def generate_task_prompt(task: TaskClaim, fix_plan_path: Path) -> str:
    """Generate a focused prompt for a specific task.

    Instead of giving Ralph the entire @fix_plan.md, we extract just the
    relevant task and provide clear instructions.

    Args:
        task: The task claim with task details
        fix_plan_path: Path to the original @fix_plan.md

    Returns:
        A focused task prompt string
    """
    # Read the original fix_plan to extract task context
    fix_plan_content = ""
    if fix_plan_path.exists():
        fix_plan_content = fix_plan_path.read_text(encoding="utf-8", errors="replace")

    # Extract the task section from fix_plan if possible
    task_section_content = ""
    if task.task_section and fix_plan_content:
        # Try to find the section in the fix_plan
        import re
        # Look for the section header (### or #### followed by the section name)
        pattern = rf"(#{2,4}\s*{re.escape(task.task_section)}.*?)(?=\n#{2,4}\s|\Z)"
        match = re.search(pattern, fix_plan_content, re.DOTALL | re.IGNORECASE)
        if match:
            task_section_content = match.group(1).strip()

    # Build the focused prompt
    prompt = f"""# Task Assignment: {task.task_title}

## 🚨 CRITICAL: Task Completion Signal

When you finish this task, you MUST output this exact block:

---RALPH_STATUS---
STATUS: COMPLETE
EXIT_SIGNAL: true
TASK_ID: {task.task_id}
COMMIT: <your_git_commit_sha>
VERIFICATION: <brief verification description>
---END_RALPH_STATUS---

⚠️ WITHOUT THIS BLOCK, your work will NOT be recorded as complete in the database.
The task will remain "pending" even though you completed it.

## Task Details
- **Task ID**: {task.task_id}
- **Priority**: {task.task_priority.value}
- **Project**: {task.project or "Unknown"}
{f"- **Category**: {task.category.value}" if task.category else ""}
{f"- **Section**: {task.task_section}" if task.task_section else ""}

## Instructions

You are working on a SINGLE task. Focus ONLY on this task.

**Your task**: {task.task_title}

{f'''## Task Context from @fix_plan.md

{task_section_content}
''' if task_section_content else ""}

## Before Marking Complete

You MUST verify completion by:
1. Running tests: `pytest` or the project's test command
2. Checking for errors: Review any error output from tests or linting
3. Validating against spec: Re-read the task description and confirm all requirements are met

Only mark the task complete if ALL verification steps pass.

## Completion Criteria

When you have completed this task:
1. Ensure all changes are saved
2. Run all relevant tests and ensure they pass
3. Verify your changes match the task requirements
4. Commit your changes with a descriptive message
5. 🚨 **OUTPUT THE RALPH_STATUS BLOCK** (see top of prompt - this is REQUIRED)

## Example: What Complete Output Should Look Like

After you commit your changes, output exactly this format:

```
All changes committed successfully.

---RALPH_STATUS---
STATUS: COMPLETE
EXIT_SIGNAL: true
TASK_ID: {task.task_id}
COMMIT: abc1234567890abcdef1234567890abcdef1234
VERIFICATION: All 1183 tests pass, changes verified in development environment
---END_RALPH_STATUS---
```

This tells the system your task is complete.

## Other Status Blocks

If you are still working and NOT done, output:

---RALPH_STATUS---
STATUS: IN_PROGRESS
EXIT_SIGNAL: false
---END_RALPH_STATUS---

If you cannot complete the task due to an error:

---RALPH_STATUS---
STATUS: FAILED
EXIT_SIGNAL: true
TASK_ID: {task.task_id}
REASON: <brief description of what went wrong>
---END_RALPH_STATUS---

## Important Notes

- Focus ONLY on this specific task
- Do not work on other tasks from the fix_plan
- If the task is unclear, make reasonable assumptions and proceed
- **THE COMMIT IS NOT THE FINAL STEP** - you must output the RALPH_STATUS block after committing
- The ---RALPH_STATUS--- block is REQUIRED for task completion tracking
"""
    return prompt


def generate_prompt_for_task(task_id: str, fix_plan_path: str | Path | None = None) -> str:
    """Generate a prompt for a task by ID (synchronous wrapper for bash scripts).

    This is a convenience function for bash scripts that need to generate
    prompts for tasks. It handles async database access internally.

    Args:
        task_id: The task ID to generate a prompt for
        fix_plan_path: Optional path to @fix_plan.md (defaults to current project's)

    Returns:
        The generated prompt string

    Raises:
        ValueError: If task_id is invalid or task not found
    """
    import asyncio

    from chiefwiggum.coordination import get_task_claim

    # Validate task_id parameter
    if not task_id or not task_id.strip():
        raise ValueError("task_id parameter is empty or whitespace")

    if task_id == "null":
        raise ValueError("task_id parameter is the string 'null' - likely a race condition in task claiming")

    # Run the async function synchronously
    loop = asyncio.get_event_loop()
    if loop.is_running():
        # If event loop is already running, create a new one
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        task_claim = loop.run_until_complete(get_task_claim(task_id))
        if not task_claim:
            raise ValueError(
                f"Task '{task_id}' not found in database. "
                "This may indicate a race condition where the task was claimed "
                "but the database hasn't synced yet, or the task_id is invalid."
            )

        # Determine fix_plan_path
        if fix_plan_path is None:
            # Try to find @fix_plan.md in current directory or project
            if task_claim.project:
                project_dir = Path(task_claim.project)
                if project_dir.exists():
                    fix_plan_path = project_dir / "@fix_plan.md"
                else:
                    fix_plan_path = Path.cwd() / "@fix_plan.md"
            else:
                fix_plan_path = Path.cwd() / "@fix_plan.md"
        else:
            fix_plan_path = Path(fix_plan_path)

        # Generate the prompt using the existing function
        return generate_task_prompt(task_claim, fix_plan_path)

    except ValueError as e:
        # Preserve ValueError messages (they have context about the failure)
        logger.error(f"Validation error for task_id '{task_id}': {e}")
        raise
    except Exception as e:
        logger.error(f"Failed to generate prompt for task '{task_id}': {e}")
        raise ValueError(f"Failed to generate prompt for task '{task_id}': {e}") from e


def write_ralph_status(
    ralph_id: str,
    task_id: str | None,
    status: str,
    loop_count: int = 0,
    message: str = "",
    error_info: dict | None = None,
    progress_data: dict | None = None,
    needs_human_intervention: bool = False,
    hitl_reason: str | None = None,
) -> None:
    """Write Ralph status to a JSON file for chiefwiggum to read.

    Args:
        ralph_id: The Ralph instance ID
        task_id: Current task being worked on (or None)
        status: Status string (working, idle, complete, failed)
        loop_count: Current loop iteration
        message: Optional status message
        error_info: Optional error information dict with category, count, details
        progress_data: Optional progress data dict with test_results and stuck_indicators
        needs_human_intervention: Whether human intervention is needed
        hitl_reason: Reason why human intervention is needed
    """
    import json

    status_path = get_ralph_status_path(ralph_id)

    # Preserve existing cost_info if it exists (written by ralph_loop.sh)
    existing_cost_info = None
    if status_path.exists():
        try:
            existing_data = json.loads(status_path.read_text())
            existing_cost_info = existing_data.get("cost_info")
        except (json.JSONDecodeError, Exception):
            pass  # Ignore errors reading existing file

    status_data = {
        "ralph_id": ralph_id,
        "task_id": task_id,
        "status": status,
        "loop_count": loop_count,
        "message": message,
        "updated_at": datetime.now().isoformat(),
    }

    # Add error_info if provided
    if error_info:
        status_data["error_info"] = error_info
    else:
        status_data["error_info"] = {
            "category": "none",
            "count": 0,
            "details": [],
        }

    # Add progress_data if provided
    if progress_data:
        status_data["progress_data"] = progress_data
    else:
        status_data["progress_data"] = {
            "test_results": None,
            "stuck_indicators": {
                "same_test_failures": 0,
                "loop_count_without_progress": 0,
            },
        }

    # Add HITL fields
    status_data["needs_human_intervention"] = needs_human_intervention
    status_data["hitl_reason"] = hitl_reason

    # Preserve cost_info if it existed
    if existing_cost_info is not None:
        status_data["cost_info"] = existing_cost_info

    status_path.write_text(json.dumps(status_data, indent=2))

    # Invalidate error indicator cache when status changes
    from chiefwiggum.cache import error_indicator_cache
    error_indicator_cache.invalidate(f"error:{ralph_id}")


def read_ralph_status(ralph_id: str) -> dict | None:
    """Read Ralph status from file.

    Returns:
        Status dict or None if not found
    """
    import json

    status_path = get_ralph_status_path(ralph_id)
    if not status_path.exists():
        return None
    try:
        return json.loads(status_path.read_text())
    except (json.JSONDecodeError, IOError):
        return None


def write_ralph_status_if_not_terminal(
    ralph_id: str,
    task_id: str | None,
    status: str,
    message: str = "",
    **kwargs
) -> bool:
    """Write Ralph status only if current status is not terminal.

    Terminal statuses: crashed, completed, stopped
    This prevents race conditions where spawner overwrites ralph_loop.sh's final status.

    Args:
        ralph_id: The Ralph instance ID
        task_id: Current task being worked on
        status: New status to write
        message: Status message
        **kwargs: Additional args passed to write_ralph_status()

    Returns:
        True if status was written, False if skipped (already terminal)
    """
    current = read_ralph_status(ralph_id)
    if current and current.get("status") in ("crashed", "completed", "stopped"):
        logger.debug(
            f"Skipping status write for {ralph_id} - already terminal: {current.get('status')}"
        )
        return False

    write_ralph_status(ralph_id, task_id, status, message=message, **kwargs)
    return True


# Claude Code-specific error patterns (mirrors ralph_loop.sh patterns)
CLAUDE_PERMISSION_PATTERNS = [
    r"Tool '.*' is not allowed",
    r"Permission denied",
    r"not authorized",
    r"Bash command not allowed",
    r"Tool is not available",
    r"Action not permitted",
]

CLAUDE_API_PATTERNS = [
    r"rate limit",
    r"HTTP.*429|status.*429|error.*429",  # More specific than just "429"
    r"API error",
    r"quota exceeded",
    r"insufficient_quota",
    r"overloaded",
    r"service unavailable",
    r"HTTP.*503|status.*503|error.*503",  # More specific than just "503"
    r"HTTP.*500|status.*500|error.*500",  # More specific than just "500"
]

CLAUDE_TOOL_FAILURE_PATTERNS = [
    r"Tool execution failed",
    r"tool failed",
    r"could not execute",
    r"command failed",
    r"execution error",
    r"tool error",
]


def get_recent_errors(ralph_id: str, max_errors: int = 10) -> list[dict]:
    """Scan ralph.log for Claude Code error patterns.

    Args:
        ralph_id: The Ralph instance ID
        max_errors: Maximum number of errors to return

    Returns:
        List of error dicts with timestamp, category, message
    """
    import re

    log_path = get_ralph_log_path(ralph_id)
    if not log_path.exists():
        return []

    errors = []

    try:
        # Read last 100KB of log to find recent errors
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)  # Go to end
            size = f.tell()
            f.seek(max(0, size - 100000))  # Read last 100KB
            content = f.read()

        lines = content.split("\n")

        for line in lines:
            # Extract timestamp if present (format: [YYYY-MM-DD HH:MM:SS])
            timestamp_match = re.search(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
            timestamp = timestamp_match.group(1) if timestamp_match else None

            # Check for permission errors
            for pattern in CLAUDE_PERMISSION_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    errors.append({
                        "timestamp": timestamp,
                        "category": "permission",
                        "message": line.strip()[:200],  # Truncate long lines
                    })
                    break

            # Check for API errors
            for pattern in CLAUDE_API_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    errors.append({
                        "timestamp": timestamp,
                        "category": "api_error",
                        "message": line.strip()[:200],
                    })
                    break

            # Check for tool failures
            for pattern in CLAUDE_TOOL_FAILURE_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    errors.append({
                        "timestamp": timestamp,
                        "category": "tool_failure",
                        "message": line.strip()[:200],
                    })
                    break

            # Stop if we have enough errors
            if len(errors) >= max_errors:
                break

        # Return most recent errors first
        return errors[-max_errors:][::-1]

    except IOError as e:
        logger.warning(f"Error reading log for {ralph_id}: {e}")
        return []


def get_error_summary(ralph_id: str) -> dict:
    """Aggregate errors by category and flag critical issues.

    Args:
        ralph_id: The Ralph instance ID

    Returns:
        Dict with:
            - total_errors: int
            - by_category: dict[str, int] - counts per category
            - has_critical: bool - True if permission or API errors need attention
            - recent_errors: list - last 5 errors
            - last_error_time: str | None - timestamp of most recent error
    """
    # First check status file for error_info (written by ralph_loop.sh)
    status = read_ralph_status(ralph_id)
    error_info_from_status = status.get("error_info", {}) if status else {}

    # Also scan logs for additional context
    recent_errors = get_recent_errors(ralph_id, max_errors=20)

    # Aggregate by category
    by_category: dict[str, int] = {}
    for error in recent_errors:
        cat = error.get("category", "unknown")
        by_category[cat] = by_category.get(cat, 0) + 1

    # Include counts from status file if available
    if error_info_from_status.get("category") and error_info_from_status.get("category") != "none":
        status_cat = error_info_from_status["category"]
        status_count = error_info_from_status.get("count", 0)
        by_category[status_cat] = max(by_category.get(status_cat, 0), status_count)

    total_errors = sum(by_category.values())

    # Check for critical errors that need user attention
    critical_categories = {"permission", "api_error"}
    has_critical = any(cat in critical_categories for cat in by_category.keys())

    # Get last error time
    last_error_time = None
    if recent_errors and recent_errors[0].get("timestamp"):
        last_error_time = recent_errors[0]["timestamp"]
    elif error_info_from_status.get("last_error_time"):
        last_error_time = error_info_from_status["last_error_time"]

    return {
        "total_errors": total_errors,
        "by_category": by_category,
        "has_critical": has_critical,
        "recent_errors": recent_errors[:5],
        "last_error_time": last_error_time,
    }


def parse_test_progress(log_content: str) -> dict | None:
    """Parse test results from Ralph's log.

    Supports formats:
    - "pytest: 12 passed, 3 failed"
    - "npm test: 8/10 tests passed"
    - "12 tests run, 2 failures"
    - "Tests: 8 passed, 2 failed, 10 total"
    - "PASS: 8, FAIL: 2"

    Args:
        log_content: Content of the log file (typically last 10KB)

    Returns:
        Dict with:
            - passed: int
            - failed: int
            - total: int
            - timestamp: ISO datetime
        or None if no test results found
    """
    import re
    from datetime import datetime

    # Common test result patterns
    patterns = [
        # pytest format: "12 passed, 3 failed"
        r"(\d+)\s+passed.*?(\d+)\s+failed",
        # npm/jest format: "Tests: 8 passed, 2 failed, 10 total"
        r"Tests:\s*(\d+)\s+passed.*?(\d+)\s+failed.*?(\d+)\s+total",
        # Simple format: "8/10 tests passed"
        r"(\d+)/(\d+)\s+tests?\s+passed",
        # Ruby/RSpec format: "12 examples, 2 failures"
        r"(\d+)\s+examples?,\s*(\d+)\s+failures?",
        # PASS/FAIL format: "PASS: 8, FAIL: 2"
        r"PASS:\s*(\d+).*?FAIL:\s*(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, log_content, re.IGNORECASE | re.MULTILINE)
        if match:
            groups = match.groups()

            # Different patterns return different capture groups
            if len(groups) == 2:
                # Pattern 1, 4, 5: (passed, failed)
                passed = int(groups[0])
                failed = int(groups[1])
                total = passed + failed
            elif len(groups) == 3:
                # Pattern 2: (passed, failed, total)
                # Pattern 3: (passed, total) - calculate failed
                if "total" in pattern:
                    passed = int(groups[0])
                    failed = int(groups[1])
                    total = int(groups[2])
                else:
                    passed = int(groups[0])
                    total = int(groups[1])
                    failed = total - passed
            else:
                continue

            return {
                "passed": passed,
                "failed": failed,
                "total": total,
                "timestamp": datetime.now().isoformat(),
            }

    return None


def detect_hitl_needed(ralph_id: str) -> tuple[bool, str | None]:
    """Detect if human intervention is needed for this Ralph instance.

    Triggers:
    - 3+ permission errors in recent history
    - Same test failing for 5+ consecutive loops
    - Ralph stuck (no progress for 10+ loops)
    - Critical API errors (quota/rate limit)
    - Explicit HITL request in Ralph's message

    Args:
        ralph_id: The Ralph instance ID

    Returns:
        Tuple of (needs_hitl: bool, reason: str | None)
    """
    # Get error summary
    error_summary = get_error_summary(ralph_id)

    # Check for critical permission errors (3+)
    permission_errors = error_summary["by_category"].get("permission", 0)
    if permission_errors >= 3:
        return (True, f"{permission_errors} permission errors detected - requires configuration fix")

    # Check for API rate limit/quota errors
    api_errors = error_summary["by_category"].get("api_error", 0)
    if api_errors >= 2:
        # Check recent errors for specific patterns
        for err in error_summary["recent_errors"][:5]:
            message = err.get("message", "").lower()
            if any(kw in message for kw in ["quota", "rate limit", "overloaded"]):
                return (True, f"API {err.get('category', 'error')}: {message[:80]}")

    # Check status file for explicit HITL request
    status = read_ralph_status(ralph_id)
    if status:
        message = status.get("message", "").lower()
        if any(kw in message for kw in ["need help", "stuck", "human intervention", "blocked"]):
            return (True, f"Explicit request: {status.get('message', '')[:80]}")

        # Check progress data for stuck indicators
        progress_data = status.get("progress_data", {})
        if progress_data:
            stuck_indicators = progress_data.get("stuck_indicators", {})
            same_test_failures = stuck_indicators.get("same_test_failures", 0)
            loops_without_progress = stuck_indicators.get("loop_count_without_progress", 0)

            if same_test_failures >= 5:
                return (True, f"Same test failing {same_test_failures} times - manual fix needed")

            if loops_without_progress >= 10:
                return (True, f"No progress for {loops_without_progress} loops - check task complexity")

    # Check for Ralph being stuck (using existing function)
    from chiefwiggum.spawner import is_ralph_stuck
    timeout_mins = 30  # Default timeout
    is_stuck, stuck_reason = is_ralph_stuck(ralph_id, timeout_mins)
    if is_stuck:
        return (True, f"Ralph stuck: {stuck_reason}")

    return (False, None)


def check_task_completion(ralph_id: str) -> tuple[str | None, str | None, str | None]:
    """Check if Ralph has signaled task completion.

    Scans Ralph's log for TASK_COMPLETE, TASK_FAILED markers, or RALPH_STATUS blocks.

    Returns:
        Tuple of (completed_task_id, failure_reason, commit_sha) - all None if no completion
    """
    log_path = get_ralph_log_path(ralph_id)
    if not log_path.exists():
        return (None, None, None)

    try:
        # Read last 50KB of log to check for completion markers
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)  # Go to end
            size = f.tell()
            f.seek(max(0, size - 50000))  # Read last 50KB
            content = f.read()

        # Check for new RALPH_STATUS block format (preferred)
        import re
        status_block_match = re.search(
            r"---RALPH_STATUS---.*?---END_RALPH_STATUS---",
            content,
            re.DOTALL
        )
        if status_block_match:
            block = status_block_match.group(0)

            # Extract individual fields from the block
            status_match = re.search(r"STATUS:\s*(\w+)", block)
            task_id_match = re.search(r"TASK_ID:\s*(\S+)", block)
            commit_match = re.search(r"COMMIT:\s*([a-fA-F0-9]{7,40})", block)

            if status_match and task_id_match:
                status = status_match.group(1)
                task_id = task_id_match.group(1)
                commit_sha = commit_match.group(1) if commit_match else None

                if status == "COMPLETE":
                    return (task_id, None, commit_sha)
                elif status == "FAILED":
                    # Try to extract failure reason from the block
                    reason_match = re.search(r"REASON:\s*(.+)", block)
                    reason = reason_match.group(1).strip() if reason_match else "Task failed"
                    return (task_id, reason, None)

        # Fallback: Check for old-style completion marker
        complete_match = re.search(r"TASK_COMPLETE:\s*(\S+)", content)
        if complete_match:
            task_id = complete_match.group(1)
            # Look for commit SHA nearby (within next few lines)
            commit_match = re.search(r"COMMIT:\s*([a-fA-F0-9]{7,40})", content)
            commit_sha = commit_match.group(1) if commit_match else None
            return (task_id, None, commit_sha)

        # Fallback: Check for old-style failure marker
        fail_match = re.search(r"TASK_FAILED:\s*(\S+)\s*\nREASON:\s*(.+)", content)
        if fail_match:
            return (fail_match.group(1), fail_match.group(2), None)

        return (None, None, None)
    except IOError:
        return (None, None, None)


def is_ralph_running(ralph_id: str) -> tuple[bool, int | None]:
    """Check if a Ralph instance is running.

    Returns:
        Tuple of (is_running, pid)
    """
    pid_path = get_ralph_pid_path(ralph_id)
    if not pid_path.exists():
        return (False, None)

    try:
        pid = int(pid_path.read_text().strip())
        # Check if process is running
        os.kill(pid, 0)
        return (True, pid)
    except (ValueError, ProcessLookupError, PermissionError):
        # Process not running, clean up stale PID file
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass
        return (False, None)


def get_process_health(ralph_id: str) -> dict:
    """Get detailed health information about a Ralph process.

    Returns a dict with:
        - running: bool - whether process exists
        - pid: int | None - process ID
        - state: str - process state (running, sleeping, zombie, stopped, dead, unknown)
        - state_code: str | None - raw state code from ps
        - elapsed: str | None - how long process has been running
        - healthy: bool - True if running and not a zombie
        - message: str - human-readable status message
    """
    result = {
        "running": False,
        "pid": None,
        "state": "dead",
        "state_code": None,
        "elapsed": None,
        "healthy": False,
        "message": "Process not found",
    }

    pid_path = get_ralph_pid_path(ralph_id)
    if not pid_path.exists():
        result["message"] = "No PID file"
        return result

    try:
        pid = int(pid_path.read_text().strip())
        result["pid"] = pid
    except (ValueError, IOError) as e:
        result["message"] = f"Invalid PID file: {e}"
        return result

    # Use ps to get detailed process state
    try:
        import subprocess
        ps_result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "state=,etime="],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if ps_result.returncode != 0:
            # Process doesn't exist
            result["message"] = "Process not found (PID file stale)"
            # Clean up stale PID file
            try:
                pid_path.unlink()
                logger.info(f"Cleaned up stale PID file for {ralph_id}")
            except FileNotFoundError:
                pass
            return result

        output = ps_result.stdout.strip()
        if output:
            parts = output.split(None, 1)
            state_code = parts[0] if parts else ""
            elapsed = parts[1].strip() if len(parts) > 1 else ""

            result["running"] = True
            result["state_code"] = state_code
            result["elapsed"] = elapsed

            # Interpret state code
            # Common states: R=running, S=sleeping, D=disk sleep, Z=zombie, T=stopped
            if state_code.startswith("Z"):
                result["state"] = "zombie"
                result["healthy"] = False
                result["message"] = f"ZOMBIE process (defunct) - elapsed: {elapsed}"
                logger.warning(f"Ralph {ralph_id} is a zombie process (PID {pid})")
            elif state_code.startswith("T"):
                result["state"] = "stopped"
                result["healthy"] = False
                result["message"] = f"Process stopped/suspended - elapsed: {elapsed}"
            elif state_code.startswith("D"):
                result["state"] = "disk_sleep"
                result["healthy"] = True  # Usually temporary
                result["message"] = f"Process in disk sleep - elapsed: {elapsed}"
            elif state_code.startswith(("R", "S")):
                result["state"] = "running" if state_code.startswith("R") else "sleeping"
                result["healthy"] = True
                result["message"] = f"Process healthy ({result['state']}) - elapsed: {elapsed}"
            else:
                result["state"] = "unknown"
                result["healthy"] = True  # Assume healthy if not zombie
                result["message"] = f"Process state '{state_code}' - elapsed: {elapsed}"

    except subprocess.TimeoutExpired:
        result["message"] = "Timeout checking process state"
        logger.error(f"Timeout checking process state for Ralph {ralph_id}")
    except Exception as e:
        result["message"] = f"Error checking process: {e}"
        logger.error(f"Error checking process health for Ralph {ralph_id}: {e}")

    return result


def get_process_health_cached(ralph_id: str) -> dict:
    """
    Get process health with caching to avoid repeated subprocess calls.

    This is a cached wrapper around get_process_health() with 5s TTL.
    Use this in UI rendering to avoid blocking on subprocess calls.

    Args:
        ralph_id: Ralph instance ID

    Returns:
        Health info dict (same as get_process_health)
    """
    cache_key = f"health:{ralph_id}"
    cached_result = process_health_cache.get(cache_key)

    if cached_result is not None:
        return cached_result

    # Cache miss - fetch fresh data
    result = get_process_health(ralph_id)
    process_health_cache.set(cache_key, result)
    return result


def invalidate_process_health_cache(ralph_id: str = None) -> None:
    """
    Invalidate process health cache.

    Args:
        ralph_id: Specific Ralph to invalidate, or None to clear all
    """
    if ralph_id:
        process_health_cache.invalidate(f"health:{ralph_id}")
    else:
        process_health_cache.invalidate_pattern("health:")


def get_status_staleness(ralph_id: str) -> dict:
    """Check how stale the status file is.

    Returns a dict with:
        - exists: bool - whether status file exists
        - last_updated: datetime | None - when status was last updated
        - age_seconds: float | None - seconds since last update
        - stale: bool - True if > 60 seconds old or doesn't exist
        - message: str - human-readable staleness message
    """
    result = {
        "exists": False,
        "last_updated": None,
        "age_seconds": None,
        "stale": True,
        "status_data": None,
        "message": "No status file",
    }

    status_data = read_ralph_status(ralph_id)
    if not status_data:
        return result

    result["exists"] = True
    result["status_data"] = status_data

    # Check log activity first - if log is very recent, instance is clearly working
    # This handles the case where Claude runs for 30+ minutes without status updates
    log_path = get_ralph_log_path(ralph_id)
    if log_path.exists():
        try:
            log_mtime = log_path.stat().st_mtime
            log_age = datetime.now().timestamp() - log_mtime
            if log_age < 30:
                # Log proves instance is actively working
                result["stale"] = False
                result["message"] = f"Active (log updated {log_age:.0f}s ago)"
                # Still populate timestamp info if available, but don't let it override staleness
                updated_at_str = status_data.get("updated_at") or status_data.get("timestamp")
                if updated_at_str:
                    try:
                        updated_at = datetime.fromisoformat(updated_at_str)
                        result["last_updated"] = updated_at
                        result["age_seconds"] = (datetime.now() - updated_at).total_seconds()
                    except (ValueError, TypeError):
                        pass
                return result
        except (OSError, IOError):
            pass  # Fall through to status file check

    # Check both field names for backwards compatibility
    # ralph_loop.sh writes "timestamp", but older code may write "updated_at"
    updated_at_str = status_data.get("updated_at") or status_data.get("timestamp")
    if updated_at_str:
        try:
            updated_at = datetime.fromisoformat(updated_at_str)
            result["last_updated"] = updated_at
            age = (datetime.now() - updated_at).total_seconds()
            result["age_seconds"] = age

            if age < 30:
                result["stale"] = False
                result["message"] = f"Updated {age:.0f}s ago"
            elif age < 60:
                result["stale"] = False
                result["message"] = f"Updated {age:.0f}s ago (recent)"
            elif age < 300:
                result["stale"] = True
                result["message"] = f"⚠ Updated {age/60:.1f}m ago (possibly stale)"
            else:
                result["stale"] = True
                result["message"] = f"⚠ Updated {age/60:.0f}m ago (STALE)"
        except (ValueError, TypeError) as e:
            result["message"] = f"Invalid timestamp: {e}"

    return result


def get_ralph_activity(ralph_id: str) -> dict:
    """Check if Ralph is showing signs of life by monitoring file activity.

    This provides external heartbeat detection by monitoring:
    - Log file modification time and growth
    - Status file freshness
    - Process state

    Returns a dict with:
        - log_age_seconds: float | None - seconds since log file modified
        - status_age_seconds: float | None - seconds since status file updated
        - log_growing: bool - whether log file has grown recently
        - log_size: int | None - current log file size
        - process_state: str - process state from get_process_health
        - is_responsive: bool - True if showing recent activity
        - last_check: datetime - when this check was performed
    """
    import time

    result = {
        "log_age_seconds": None,
        "status_age_seconds": None,
        "log_growing": False,
        "log_size": None,
        "process_state": "unknown",
        "is_responsive": False,
        "last_check": datetime.now(),
    }

    # Check log file activity
    log_path = get_ralph_log_path(ralph_id)
    if log_path.exists():
        try:
            stat = log_path.stat()
            result["log_age_seconds"] = (datetime.now() - datetime.fromtimestamp(stat.st_mtime)).total_seconds()
            result["log_size"] = stat.st_size

            # Check if log is growing (compare size over brief interval)
            initial_size = stat.st_size
            time.sleep(0.5)  # Brief wait
            stat2 = log_path.stat()
            result["log_growing"] = stat2.st_size > initial_size
        except (OSError, IOError) as e:
            logger.warning(f"Error checking log file for {ralph_id}: {e}")

    # Check status file staleness
    staleness = get_status_staleness(ralph_id)
    if staleness["age_seconds"] is not None:
        result["status_age_seconds"] = staleness["age_seconds"]

    # Check process health
    health = get_process_health(ralph_id)
    result["process_state"] = health["state"]

    # Determine if responsive
    # Consider responsive if:
    # - Log updated in last 60 seconds, OR
    # - Status file updated in last 120 seconds
    # AND process is healthy
    log_recent = result["log_age_seconds"] is not None and result["log_age_seconds"] < 60
    status_recent = result["status_age_seconds"] is not None and result["status_age_seconds"] < 120

    result["is_responsive"] = health["healthy"] and (log_recent or status_recent)

    return result


def is_ralph_stuck(ralph_id: str, timeout_minutes: int = 30) -> tuple[bool, str]:
    """Detect if Ralph is stuck and needs intervention.

    Checks multiple indicators:
    1. Process is dead/zombie/stopped
    2. Log hasn't updated in 5+ minutes while supposedly active
    3. Status file is stale
    4. Task running longer than 2x timeout

    Args:
        ralph_id: The Ralph instance ID
        timeout_minutes: Task timeout in minutes (default 30)

    Returns:
        Tuple of (is_stuck: bool, reason: str)
    """
    # First check if there's even a PID file - if not, Ralph was never started
    pid_path = get_ralph_pid_path(ralph_id)
    if not pid_path.exists():
        return False, "Not running"

    activity = get_ralph_activity(ralph_id)

    # Check 1: Process state issues
    if activity["process_state"] == "zombie":
        return True, "Process is ZOMBIE (defunct)"

    if activity["process_state"] == "stopped":
        return True, "Process is STOPPED/suspended"

    if activity["process_state"] == "dead":
        # Dead process is definitely stuck (PID file exists but process is dead)
        return True, "Process is DEAD (not running)"

    # If process state is unknown but we have a PID file, check health
    if activity["process_state"] == "unknown":
        health = get_process_health(ralph_id)
        if not health["running"]:
            return False, "Not running"

    # Check 2: Log hasn't updated in 5+ minutes AND status is also stale
    # (Fresh status file proves Ralph is actively working even without log output)
    if activity["log_age_seconds"] is not None and activity["log_age_seconds"] > 300:
        # Only stuck if status file is ALSO stale (> 2 minutes)
        status_also_stale = (
            activity["status_age_seconds"] is None or
            activity["status_age_seconds"] > 120
        )
        if status_also_stale:
            return True, f"No log activity for {activity['log_age_seconds']/60:.1f}m"
        # else: status is fresh, Ralph is working (Claude thinking cycle)

    # Check 3: Status file extremely stale (8+ minutes)
    if activity["status_age_seconds"] is not None:
        if activity["status_age_seconds"] > 480:  # 8 minutes
            return True, f"Status file stale for {activity['status_age_seconds']/60:.1f}m"

    # Check 4: Check if current task is running too long
    # This requires reading the status file for task start time
    status = read_ralph_status(ralph_id)
    if status and status.get("status") == "working":
        updated_at_str = status.get("updated_at")
        if updated_at_str:
            try:
                updated_at = datetime.fromisoformat(updated_at_str)
                elapsed = (datetime.now() - updated_at).total_seconds()
                # If status says "working" but hasn't updated in a long time
                # and it's past 2x timeout, it's stuck
                max_time = timeout_minutes * 60 * 2  # 2x timeout
                if elapsed > max_time:
                    return True, f"Task running {elapsed/60:.0f}m (timeout: {timeout_minutes}m)"
            except (ValueError, TypeError):
                pass

    return False, "OK"


async def handle_stuck_ralph(ralph_id: str, reason: str) -> dict:
    """Take action on a stuck Ralph instance.

    Actions taken:
    1. Log the issue
    2. Release any claimed task so another Ralph can take it
    3. Update instance status to UNHEALTHY/CRASHED
    4. Attempt graceful termination, then force if needed

    Args:
        ralph_id: The Ralph instance ID
        reason: Why the Ralph is stuck (for logging)

    Returns:
        Dict with action results:
        - task_released: bool - whether a task was released
        - task_id: str | None - the released task ID
        - status_updated: bool - whether status was updated
        - terminated: bool - whether process was terminated
        - message: str - summary message
    """
    from chiefwiggum.coordination import get_ralph_instance, release_claim, update_instance_status

    result = {
        "task_released": False,
        "task_id": None,
        "status_updated": False,
        "terminated": False,
        "message": "",
    }

    logger.warning(f"[HEALTH] Ralph {ralph_id} stuck: {reason}")

    # Step 1: Get instance info and release any claimed task
    try:
        instance = await get_ralph_instance(ralph_id)
        if instance and instance.current_task_id:
            try:
                await release_claim(ralph_id, instance.current_task_id)
                result["task_released"] = True
                result["task_id"] = instance.current_task_id
                logger.info(f"[HEALTH] Released task {instance.current_task_id} from stuck Ralph {ralph_id}")
            except Exception as e:
                logger.warning(f"[HEALTH] Failed to release task from {ralph_id}: {e}")
    except Exception as e:
        logger.warning(f"[HEALTH] Failed to get instance {ralph_id}: {e}")

    # Step 2: Update instance status
    try:
        # Write status file (only if not already terminal)
        write_ralph_status_if_not_terminal(
            ralph_id,
            task_id=None,
            status="crashed",
            message=f"Marked crashed: {reason}",
        )

        # Update database if available
        try:
            await update_instance_status(ralph_id, "CRASHED", error_message=reason)
            result["status_updated"] = True
            logger.info(f"[HEALTH] Updated {ralph_id} status to CRASHED")
        except Exception:
            # update_instance_status might not exist, use direct approach
            pass

    except Exception as e:
        logger.warning(f"[HEALTH] Failed to update status for {ralph_id}: {e}")

    # Step 3: Attempt to terminate the process
    health = get_process_health(ralph_id)
    if health["running"] and health["pid"]:
        pid = health["pid"]
        try:
            # First try graceful termination (SIGTERM)
            logger.info(f"[HEALTH] Sending SIGTERM to Ralph {ralph_id} (PID {pid})")
            os.kill(pid, signal.SIGTERM)

            # Wait briefly for graceful shutdown
            import time
            time.sleep(2)

            # Check if still running
            try:
                os.kill(pid, 0)  # Check if process exists
                # Still running, force kill
                logger.info(f"[HEALTH] Sending SIGKILL to Ralph {ralph_id} (PID {pid})")
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # Process already terminated

            result["terminated"] = True
            logger.info(f"[HEALTH] Terminated stuck Ralph {ralph_id}")

            # Clean up PID file
            pid_path = get_ralph_pid_path(ralph_id)
            try:
                pid_path.unlink()
            except FileNotFoundError:
                pass

            # Log to Ralph's log file
            log_path = get_ralph_log_path(ralph_id)
            try:
                with open(log_path, "a") as f:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"Ralph {ralph_id} TERMINATED (stuck) at {datetime.now().isoformat()}\n")
                    f.write(f"Reason: {reason}\n")
                    f.write(f"PID: {pid}\n")
                    f.write(f"{'='*60}\n")
            except Exception:
                pass

        except ProcessLookupError:
            # Process already dead
            result["terminated"] = True
        except PermissionError:
            logger.error(f"[HEALTH] Permission denied to kill Ralph {ralph_id}")
        except Exception as e:
            logger.error(f"[HEALTH] Error terminating Ralph {ralph_id}: {e}")

    # Build summary message
    actions = []
    if result["task_released"]:
        actions.append(f"released task {result['task_id']}")
    if result["status_updated"]:
        actions.append("updated status to CRASHED")
    if result["terminated"]:
        actions.append("terminated process")

    result["message"] = f"Handled stuck Ralph {ralph_id}: {', '.join(actions) if actions else 'no actions taken'}"
    logger.info(f"[HEALTH] {result['message']}")

    return result


def reap_zombie_ralph(ralph_id: str) -> bool:
    """Attempt to reap a zombie Ralph process.

    Returns True if the zombie was cleaned up.
    """
    health = get_process_health(ralph_id)

    if health["state"] != "zombie":
        return False

    pid = health["pid"]
    if not pid:
        return False

    logger.info(f"Attempting to reap zombie Ralph {ralph_id} (PID {pid})")

    try:
        # Try to wait on the zombie process
        # This only works if we're the parent
        import os
        result = os.waitpid(pid, os.WNOHANG)
        if result[0] == pid:
            logger.info(f"Successfully reaped zombie Ralph {ralph_id}")

            # Clean up PID file
            pid_path = get_ralph_pid_path(ralph_id)
            try:
                pid_path.unlink()
            except FileNotFoundError:
                pass

            # Update status file to indicate crash (only if not already terminal)
            write_ralph_status_if_not_terminal(
                ralph_id,
                task_id=None,
                status="crashed",
                message="Process became zombie and was reaped",
            )

            # Log to Ralph's log file
            log_path = get_ralph_log_path(ralph_id)
            try:
                with open(log_path, "a") as f:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"Ralph {ralph_id} CRASHED (zombie reaped) at {datetime.now().isoformat()}\n")
                    f.write(f"PID: {pid}\n")
                    f.write(f"{'='*60}\n")
            except Exception as e:
                logger.warning(f"Could not write crash to log: {e}")

            return True
        else:
            logger.warning(f"waitpid returned {result} for zombie {ralph_id}")
            return False

    except ChildProcessError:
        # Not our child — we can't reap it, but we CAN stop treating it as
        # an active ralph. Unlink the PID file so cleanup_dead_ralphs
        # doesn't re-flag the same zombie on every subsequent daemon tick,
        # and so new spawns with the same ralph_id don't look collision-y.
        logger.warning(
            f"Cannot reap zombie {ralph_id} (not our child) — removing its PID file instead"
        )
        pid_path = get_ralph_pid_path(ralph_id)
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass

        write_ralph_status_if_not_terminal(
            ralph_id,
            task_id=None,
            status="crashed",
            message="Process is zombie (not our child; PID file cleared)",
        )
        # Return True: from the caller's perspective we DID clean it up
        # (the PID file is gone; the zombie will be reaped by init when
        # its real parent exits). Returning False here made callers loop.
        return True

    except Exception as e:
        logger.error(f"Error reaping zombie {ralph_id}: {e}")
        return False


def cleanup_dead_ralphs() -> list[str]:
    """Find and clean up dead/zombie Ralph processes.

    Returns list of ralph_ids that were cleaned up.
    """
    cleaned = []
    data_dir = _ensure_data_dir()

    for pid_file in data_dir.glob("*.pid"):
        ralph_id = pid_file.stem
        health = get_process_health(ralph_id)

        if health["state"] == "zombie":
            if reap_zombie_ralph(ralph_id):
                cleaned.append(ralph_id)
                logger.info(f"Cleaned up zombie Ralph: {ralph_id}")
        elif not health["running"] and health["pid"]:
            # PID file exists but process is dead
            logger.info(f"Cleaning up dead Ralph: {ralph_id}")
            try:
                # PID file may have already been cleaned up by get_process_health
                if pid_file.exists():
                    pid_file.unlink()
                cleaned.append(ralph_id)

                # Update status (only if not already terminal)
                write_ralph_status_if_not_terminal(
                    ralph_id,
                    task_id=None,
                    status="crashed",
                    message="Process died unexpectedly",
                )
            except FileNotFoundError:
                # PID file already cleaned up by get_process_health
                cleaned.append(ralph_id)
                write_ralph_status_if_not_terminal(
                    ralph_id,
                    task_id=None,
                    status="crashed",
                    message="Process died unexpectedly",
                )
            except Exception as e:
                logger.error(f"Error cleaning up {ralph_id}: {e}")

    return cleaned


def spawn_ralph_daemon(
    ralph_id: str,
    project: str,
    fix_plan_path: str | Path,
    config: RalphConfig | None = None,
    targeting: TargetingConfig | None = None,
    working_dir: str | Path | None = None,
) -> tuple[bool, str]:
    """Spawn a Ralph instance as a background daemon.

    Args:
        ralph_id: Unique ID for this Ralph instance
        project: Project to work on
        fix_plan_path: Path to the @fix_plan.md file
        config: Optional Ralph configuration
        targeting: Optional task targeting configuration (logged but not passed to CLI;
                   targeting should be embedded in the @fix_plan.md content)
        working_dir: Working directory for Ralph (defaults to fix_plan parent)

    Returns:
        Tuple of (success, message)

    Note:
        This function performs basic validation (API key, CLI availability).
        For complete validation including concurrent limits, use can_spawn_ralph() first.
    """
    # Pre-spawn validation (sync checks only)
    valid, reason = _validate_spawn_requirements()
    if not valid:
        logger.error(f"Spawn validation failed: {reason}")
        return (False, reason)

    config = config or RalphConfig()
    targeting = targeting or TargetingConfig()

    # Check if already running
    running, existing_pid = is_ralph_running(ralph_id)
    if running:
        return (False, f"Ralph {ralph_id} is already running (PID: {existing_pid})")

    # Ensure paths
    fix_plan_path = Path(fix_plan_path).resolve()
    if not fix_plan_path.exists():
        return (False, f"Fix plan not found: {fix_plan_path}")

    if working_dir:
        working_dir = Path(working_dir).resolve()
    else:
        working_dir = fix_plan_path.parent

    # Multiple Ralphs on same project is ALLOWED - that's the whole point of chiefwiggum!
    # Each Ralph should work on different tasks (coordinated via task claiming).
    # Log for visibility but don't block.
    ralphs_by_dir = _get_ralphs_by_working_dir()
    if working_dir in ralphs_by_dir:
        existing_ralphs = ralphs_by_dir[working_dir]
        logger.info(
            f"Adding Ralph to project with existing Ralph(s): {existing_ralphs}. "
            f"Ensure task coordination to avoid conflicts."
        )

    # Ensure project has proper Claude Code permissions for autonomous operation
    # This creates .claude/settings.json with Edit and other necessary permissions
    _ensure_project_permissions(working_dir)

    # Prepare log and session files
    log_path = get_ralph_log_path(ralph_id)
    session_path = get_ralph_session_path(ralph_id)
    pid_path = get_ralph_pid_path(ralph_id)

    # Build the Claude command
    # Using the ralph-loop skill from the Tian project
    cmd = _build_ralph_command(
        ralph_id=ralph_id,
        project=project,
        fix_plan_path=str(fix_plan_path),
        config=config,
        targeting=targeting,
        session_path=str(session_path),
    )

    logger.info(f"[SPAWN] Starting spawn process for Ralph {ralph_id}")
    logger.info(f"[SPAWN] Project: {project}, Working dir: {working_dir}")
    logger.info(f"[SPAWN] Fix plan: {fix_plan_path}")
    logger.info(f"[SPAWN] Config: model={config.model.value}, timeout={config.timeout_minutes}m")

    try:
        # Open log file for output
        log_file = open(log_path, "a")
        log_file.write(f"\n{'='*60}\n")
        log_file.write(f"Ralph {ralph_id} starting at {datetime.now().isoformat()}\n")
        log_file.write(f"Project: {project}\n")
        log_file.write(f"Working Directory: {working_dir}\n")
        log_file.write(f"Fix Plan: {fix_plan_path}\n")
        log_file.write(f"Config: {config.model_dump()}\n")
        log_file.write(f"Targeting: {targeting.model_dump()}\n")
        log_file.write(f"Command: {' '.join(cmd)}\n")
        log_file.write(f"Parent PID: {os.getpid()}\n")
        log_file.write(f"ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))}\n")
        log_file.write(f"{'='*60}\n\n")
        log_file.flush()

        logger.info(f"[SPAWN] Log file ready: {log_path}")

        # Spawn the process
        # CRITICAL: Explicitly pass environment to ensure ANTHROPIC_API_KEY is available
        # Without explicit env=, daemon processes (start_new_session=True) may not
        # inherit environment variables reliably on all systems
        spawn_env = os.environ.copy()
        # Load project .env so ralphs get project-level env vars (e.g. ANTHROPIC_API_KEY).
        # Existing os.environ values take precedence.
        _load_dotenv_into(spawn_env, working_dir / ".env")
        logger.info(f"[SPAWN] Executing: {' '.join(cmd[:3])}...")  # First 3 parts of command
        logger.info(f"[SPAWN] ANTHROPIC_API_KEY in spawn_env: {bool(spawn_env.get('ANTHROPIC_API_KEY'))}")
        process = subprocess.Popen(
            cmd,
            cwd=str(working_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=spawn_env,  # Explicitly pass environment
            start_new_session=True,  # Detach from terminal
        )

        # Write PID file
        pid_path.write_text(str(process.pid))
        logger.info(f"[SPAWN] PID file written: {pid_path} -> {process.pid}")

        # Verify process started correctly (brief check)
        import time
        time.sleep(0.2)  # Brief wait for process to either start or fail immediately

        # Check if process is still running
        poll_result = process.poll()
        if poll_result is not None:
            # Process exited - trust exit code + status file (Ralph Loop Alignment)
            status_file = get_ralph_status_path(ralph_id)

            # Exit code contract (see Plan):
            # 0: Success - task complete
            # 2: Script error - crashed
            # 3: Circuit breaker - needs review
            # 10: Cost limit - stopped
            # 130: SIGINT - interrupted

            is_graceful = False
            exit_message = ""

            if poll_result == 0:
                # Success
                is_graceful = True
                exit_message = f"Ralph {ralph_id} completed successfully (exit 0)"
                logger.info(f"[SPAWN] {exit_message}")

            elif poll_result == 130:
                # SIGINT - graceful interrupt
                is_graceful = True
                exit_message = f"Ralph {ralph_id} interrupted (SIGINT)"
                logger.info(f"[SPAWN] {exit_message}")

            elif poll_result in (3, 10):
                # Circuit breaker or cost limit - graceful halt
                is_graceful = True
                halt_reason = "circuit breaker" if poll_result == 3 else "cost limit"
                exit_message = f"Ralph {ralph_id} halted ({halt_reason})"
                logger.warning(f"[SPAWN] {exit_message}")

            else:
                # Check status file for additional context
                if status_file.exists():
                    try:
                        import json
                        with open(status_file) as f:
                            status_data = json.load(f)
                            last_status = status_data.get("status", "")
                            last_message = status_data.get("message", "")

                            # Trust status file if it indicates completion
                            if last_status in ("completed", "complete") or "complete" in last_message.lower():
                                is_graceful = True
                                exit_message = f"Ralph {ralph_id} completed (status file indicates completion)"
                                logger.info(f"[SPAWN] {exit_message}")
                    except Exception as e:
                        logger.debug(f"[SPAWN] Could not check status file: {e}")

            if is_graceful:
                # Graceful exit - don't mark as crashed
                return (True, exit_message)

            # Non-graceful exit - this is a crash
            logger.error(f"[SPAWN] Ralph {ralph_id} crashed immediately with exit code {poll_result}")

            # Read any error output from log
            log_file.flush()
            try:
                with open(log_path, "r") as f:
                    recent_log = f.read()[-2000:]  # Last 2KB
                logger.error(f"[SPAWN] Recent log output:\n{recent_log}")
            except Exception:
                pass

            # Clean up PID file and mark as crashed
            try:
                pid_path.unlink()
            except FileNotFoundError:
                pass

            write_ralph_status(ralph_id, None, "crashed", "Process died unexpectedly")
            return (False, f"Ralph {ralph_id} exited immediately (code: {poll_result})")

        # Verify it's actually running
        health = get_process_health(ralph_id)
        logger.info(f"[SPAWN] Initial health check: {health['message']}")

        if not health["healthy"]:
            logger.warning(f"[SPAWN] Process started but not healthy: {health}")

        logger.info(f"[SPAWN] Successfully spawned Ralph {ralph_id} with PID {process.pid}")

        # Close log file in parent - child has its own copy of the fd
        log_file.close()

        # Invalidate caches after successful spawn
        invalidate_process_health_cache(ralph_id)
        from chiefwiggum.cache import error_indicator_cache
        error_indicator_cache.invalidate(f"error:{ralph_id}")

        return (True, f"Spawned Ralph {ralph_id} (PID: {process.pid})")

    except FileNotFoundError as e:
        logger.error(f"[SPAWN] File not found during spawn of {ralph_id}: {e}")
        return (False, f"File not found: {e}")
    except PermissionError as e:
        logger.error(f"[SPAWN] Permission error during spawn of {ralph_id}: {e}")
        return (False, f"Permission denied: {e}")
    except Exception as e:
        logger.error(f"[SPAWN] Failed to spawn Ralph {ralph_id}: {e}", exc_info=True)
        return (False, f"Failed to spawn: {e}")


async def spawn_ralph_with_task_claim(
    ralph_id: str,
    project: str,
    fix_plan_path: str | Path,
    config: RalphConfig | None = None,
    targeting: TargetingConfig | None = None,
    working_dir: str | Path | None = None,
) -> tuple[bool, str, str | None]:
    """Spawn a Ralph instance that claims and works on a SINGLE task.

    This is the preferred method for spawning Ralphs. It:
    1. Syncs tasks from @fix_plan.md to the database
    2. Claims the next available task (respecting targeting)
    3. Generates a task-specific prompt
    4. Spawns Ralph with that focused prompt
    5. Registers the Ralph in the database

    Args:
        ralph_id: Unique ID for this Ralph instance
        project: Project to work on
        fix_plan_path: Path to the @fix_plan.md file
        config: Optional Ralph configuration
        targeting: Optional task targeting configuration
        working_dir: Working directory for Ralph (defaults to fix_plan parent)

    Returns:
        Tuple of (success, message, task_id) - task_id is None on failure
    """
    from chiefwiggum.coordination import (
        _update_instance_task,
        claim_task,
        register_ralph_instance_with_config,
        sync_tasks_from_fix_plan,
    )

    # Load ralph loop settings defaults from config
    loop_settings = get_ralph_loop_settings()

    # Create config with defaults from settings if not provided
    if config is None:
        config = RalphConfig(
            # Invert session_continuity to no_continue
            # session_continuity=True means no_continue=False (continue sessions)
            no_continue=not loop_settings.get("session_continuity", True),
            session_expiry_hours=loop_settings.get("session_expiry_hours", 24),
            output_format=loop_settings.get("output_format", "json"),
            max_calls_per_hour=loop_settings.get("max_calls_per_hour", 100),
        )
    targeting = targeting or TargetingConfig()
    fix_plan_path = Path(fix_plan_path).resolve()

    # Check daily cost budget if configured
    from datetime import datetime

    from chiefwiggum.coordination import get_cost_stats
    cost_budget_daily = loop_settings.get("cost_budget_daily")
    if cost_budget_daily:
        try:
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_stats = await get_cost_stats(since_date=today_start)
            if today_stats["total_cost_usd"] >= cost_budget_daily:
                logger.warning(f"[SPAWN_WITH_TASK] Daily cost budget exceeded: ${today_stats['total_cost_usd']:.2f} / ${cost_budget_daily:.2f}")
                return (False, f"Daily cost budget exceeded: ${today_stats['total_cost_usd']:.2f} / ${cost_budget_daily:.2f}", None)
        except Exception as e:
            logger.warning(f"[SPAWN_WITH_TASK] Failed to check cost budget: {e}")
            # Continue anyway - cost check is not critical

    logger.info(f"[SPAWN_WITH_TASK] Starting for {ralph_id} on project {project}")
    logger.info(f"[SPAWN_WITH_TASK] Fix plan: {fix_plan_path}")

    if not fix_plan_path.exists():
        logger.error(f"[SPAWN_WITH_TASK] Fix plan not found: {fix_plan_path}")
        return (False, f"Fix plan not found: {fix_plan_path}", None)

    # Step 1: Sync tasks from fix_plan to database
    logger.info("[SPAWN_WITH_TASK] Step 1: Syncing tasks from fix_plan")
    try:
        synced_count = await sync_tasks_from_fix_plan(fix_plan_path, project)
        logger.info(f"[SPAWN_WITH_TASK] Synced {synced_count} tasks")
    except Exception as e:
        logger.warning(f"[SPAWN_WITH_TASK] Failed to sync tasks: {e}")
        # Continue anyway - tasks might already be synced

    # Step 2: Claim the next available task
    logger.info(f"[SPAWN_WITH_TASK] Step 2: Claiming next task for {ralph_id}")
    claimed = await claim_task(ralph_id, project=project)
    if not claimed:
        logger.warning(f"[SPAWN_WITH_TASK] No tasks available to claim for {ralph_id}")
        return (False, "No tasks available to claim", None)

    task_id = claimed["task_id"]
    task_title = claimed["task_title"]
    logger.info(f"[SPAWN_WITH_TASK] Claimed task: {task_id} - {task_title}")

    # Create a TaskClaim object for prompt generation
    from chiefwiggum.models import TaskClaim, TaskClaimStatus
    task = TaskClaim(
        task_id=task_id,
        task_title=task_title,
        task_priority=claimed.get("task_priority", "MEDIUM"),
        task_section=claimed.get("task_section"),
        project=project,
        status=TaskClaimStatus.IN_PROGRESS,
    )

    # Step 3: Generate task-specific prompt
    logger.info("[SPAWN_WITH_TASK] Step 3: Generating task-specific prompt")
    prompt_content = generate_task_prompt(task, fix_plan_path)
    prompt_path = get_task_prompt_path(ralph_id, task_id)
    prompt_path.write_text(prompt_content)
    logger.info(f"[SPAWN_WITH_TASK] Prompt written to: {prompt_path}")

    # Step 4: Register Ralph in database with config and task info
    logger.info("[SPAWN_WITH_TASK] Step 4: Registering Ralph in database")
    try:
        await register_ralph_instance_with_config(
            ralph_id=ralph_id,
            project=project,
            config=config,
            targeting=targeting,
            prompt_path=str(prompt_path),
        )
        await _update_instance_task(ralph_id, task_id)
        logger.info("[SPAWN_WITH_TASK] Ralph registered successfully")
    except Exception as e:
        logger.warning(f"[SPAWN_WITH_TASK] Failed to register Ralph in database: {e}")
        # Continue anyway - spawning is more important

    # Step 5: Spawn Ralph with the task-specific prompt
    logger.info("[SPAWN_WITH_TASK] Step 5: Spawning Ralph daemon")
    working_dir = working_dir or fix_plan_path.parent
    success, message = spawn_ralph_daemon(
        ralph_id=ralph_id,
        project=project,
        fix_plan_path=str(prompt_path),  # Use task-specific prompt, not full fix_plan
        config=config,
        targeting=targeting,
        working_dir=working_dir,
    )
    logger.info(f"[SPAWN_WITH_TASK] Spawn result: success={success}, message={message}")

    if success:
        # Write initial status
        write_ralph_status(ralph_id, task_id, "working", loop_count=1, message=f"Working on: {task_title}")
        return (True, f"Spawned {ralph_id} on task: {task_title[:40]}", task_id)
    else:
        # Release the claim since spawn failed
        from chiefwiggum.coordination import release_claim
        await release_claim(ralph_id, task_id)
        return (False, message, None)


async def spawn_ralph_for_graded_task(
    ralph_id: str,
    task_id: str,
    project: str,
    working_dir: str | Path,
    config: RalphConfig | None = None,
) -> tuple[bool, str]:
    """Spawn a Ralph instance for a specific task from the graded task queue.

    This is the NEW spawning method for Ralph Loop Alignment. It:
    1. Gets the task from the graded tasks table
    2. Uses the pre-generated, graded prompt (no generation at spawn time)
    3. Spawns Ralph with fresh context (no --continue)
    4. One Ralph = One Task = One Process

    Args:
        ralph_id: Unique ID for this Ralph instance
        task_id: Task ID from the graded tasks table
        project: Project name
        working_dir: Working directory for Ralph
        config: Optional Ralph configuration

    Returns:
        Tuple of (success, message)

    Raises:
        ValueError: If task not found or has no generated prompt
    """
    from chiefwiggum.coordination import get_graded_task

    logger.info(f"[SPAWN_GRADED] Spawning {ralph_id} for task {task_id}")

    # Get task from graded queue
    task = await get_graded_task(task_id)
    if not task:
        logger.error(f"[SPAWN_GRADED] Task not found: {task_id}")
        return (False, f"Task not found: {task_id}")

    if not task.get("generated_prompt"):
        logger.error(f"[SPAWN_GRADED] Task {task_id} has no generated prompt")
        return (False, f"Task {task_id} has no generated prompt. Run 'wig sync --with-grading' first.")

    # Check task status
    status = task.get("status")
    if status == "completed":
        return (False, f"Task {task_id} already completed")
    elif status == "active":
        claimed_by = task.get("claimed_by_ralph_id")
        return (False, f"Task {task_id} already claimed by {claimed_by}")
    elif status == "blocked":
        return (False, f"Task {task_id} is blocked (Grade F). Improve spec before spawning.")

    # Create config with FRESH CONTEXT and SINGLE TASK enforced
    if config is None:
        config = RalphConfig(
            no_continue=True,  # CRITICAL: Fresh context per Ralph Loop principle
            single_task=True,  # CRITICAL: One Ralph = One Task
            session_expiry_hours=24,
            output_format="json",
            max_calls_per_hour=100,
        )
    else:
        # Override no_continue and single_task to ensure fresh context and one task per Ralph
        config = config.model_copy(update={"no_continue": True, "single_task": True})

    # Write task-specific prompt to temp file
    prompt_path = get_task_prompt_path(ralph_id, task_id)
    prompt_path.write_text(task["generated_prompt"])
    logger.info(f"[SPAWN_GRADED] Prompt written to: {prompt_path}")

    # Update task status to active and claim it
    from datetime import datetime

    from chiefwiggum.database import get_connection

    conn = await get_connection()
    try:
        now = datetime.now()
        await conn.execute(
            """UPDATE tasks
               SET status = 'active', claimed_by_ralph_id = ?, updated_at = ?
               WHERE id = ? AND status = 'pending'""",
            (ralph_id, now, task_id)
        )
        await conn.commit()

        # Check if update succeeded (task might have been claimed by another Ralph)
        cursor = await conn.execute(
            "SELECT claimed_by_ralph_id FROM tasks WHERE id = ?",
            (task_id,)
        )
        row = await cursor.fetchone()
        if row and row[0] != ralph_id:
            logger.warning(f"[SPAWN_GRADED] Task {task_id} was claimed by {row[0]} during spawn")
            return (False, f"Task {task_id} was claimed by another Ralph")

    finally:
        await conn.close()

    # Register Ralph instance
    from chiefwiggum.coordination import register_ralph_instance_with_config

    try:
        await register_ralph_instance_with_config(
            ralph_id=ralph_id,
            project=project,
            config=config,
            targeting=None,
            prompt_path=str(prompt_path),
        )
        logger.info(f"[SPAWN_GRADED] Ralph {ralph_id} registered")
    except Exception as e:
        logger.warning(f"[SPAWN_GRADED] Failed to register Ralph: {e}")
        # Continue anyway

    # Spawn Ralph daemon
    working_dir = Path(working_dir).resolve()
    success, message = spawn_ralph_daemon(
        ralph_id=ralph_id,
        project=project,
        fix_plan_path=str(prompt_path),  # Use task-specific prompt
        config=config,
        targeting=None,
        working_dir=working_dir,
    )

    if success:
        write_ralph_status(
            ralph_id,
            task_id,
            "working",
            loop_count=1,
            message=f"Working on: {task['title'][:40]}"
        )
        logger.info(f"[SPAWN_GRADED] Successfully spawned {ralph_id} for task {task_id}")
        return (True, f"Spawned {ralph_id} on task: {task['title'][:40]}")
    else:
        # Release the claim since spawn failed
        conn = await get_connection()
        try:
            await conn.execute(
                """UPDATE tasks
                   SET status = 'pending', claimed_by_ralph_id = NULL, updated_at = ?
                   WHERE id = ?""",
                (datetime.now(), task_id)
            )
            await conn.commit()
        finally:
            await conn.close()

        logger.error(f"[SPAWN_GRADED] Failed to spawn {ralph_id}: {message}")
        return (False, message)


def _build_ralph_command(
    ralph_id: str,
    project: str,
    fix_plan_path: str,
    config: RalphConfig,
    targeting: TargetingConfig,
    session_path: str,
) -> list[str]:
    """Build the command to run ralph_loop.sh.

    Uses the bundled ralph_loop.sh script from chiefwiggum.scripts,
    which handles Claude orchestration, rate limiting, and session management.

    Note: Chiefwiggum manages permissions by passing --allowed-tools to ralph_loop.sh.
    This abstracts permission configuration from the user.
    """
    # Get ralph_loop.sh path (bundled or from env override)
    ralph_script = get_ralph_loop_path()

    cmd = [str(ralph_script)]

    # Pass ralph_id for per-instance call counting
    cmd.extend(["--ralph-id", ralph_id])

    # Add timeout
    if config.timeout_minutes:
        cmd.extend(["--timeout", str(config.timeout_minutes)])

    # Don't use --monitor as it creates its own tmux session that we can't track
    # Run directly so we can track the PID

    # Session continuity (existing, but honor config default)
    if config.no_continue:
        cmd.append("--no-continue")

    # Single task mode (Ralph Loop Alignment)
    if config.single_task:
        cmd.append("--single-task")

    # Session expiry (only pass if non-default)
    if config.session_expiry_hours != 24:
        cmd.extend(["--session-expiry", str(config.session_expiry_hours)])

    # Output format (only pass if non-default)
    if config.output_format != "json":
        cmd.extend(["--output-format", config.output_format])

    # Rate limiting (only pass if non-default)
    if config.max_calls_per_hour != 100:
        cmd.extend(["--calls", str(config.max_calls_per_hour)])

    # Use verbose to see progress in logs
    cmd.append("--verbose")

    # CRITICAL: Pass allowed tools including Edit to ensure Ralph can make changes
    # This abstracts permission management from the user
    # Note: Uses colon format Bash(cmd:*) to match Claude CLI expectations
    allowed_tools = [
        # Core tools
        "Read",
        "Write",
        "Edit",  # Essential for modifying existing files
        "Glob",
        "Grep",
        # Bash commands - comprehensive set for autonomous operation
        "Bash(git:*)",
        "Bash(npm:*)",
        "Bash(yarn:*)",
        "Bash(pip:*)",
        "Bash(uv:*)",
        "Bash(pytest:*)",
        "Bash(python:*)",
        "Bash(python3:*)",
        "Bash(node:*)",
        "Bash(make:*)",
        "Bash(ruff:*)",
        "Bash(mypy:*)",
        "Bash(cargo:*)",
        "Bash(ls:*)",
        "Bash(cat:*)",
        "Bash(head:*)",
        "Bash(tail:*)",
        "Bash(mkdir:*)",
        "Bash(cp:*)",
        "Bash(mv:*)",
        "Bash(touch:*)",
        "Bash(chmod:*)",
        "Bash(echo:*)",
        "Bash(grep:*)",
        "Bash(find:*)",
        "Bash(sed:*)",
    ]
    cmd.extend(["--allowed-tools", ",".join(allowed_tools)])

    # Use the fix_plan as the prompt file
    cmd.extend(["--prompt", fix_plan_path])

    # Pass status file path so ralph_loop.sh writes to the correct location
    # This ensures status is written to ~/.chiefwiggum/ralphs/status/{ralph_id}.json
    # instead of a per-project status.json (which causes conflicts with multiple ralphs)
    status_path = get_ralph_status_path(ralph_id)
    cmd.extend(["--status-file", str(status_path)])

    return cmd


def stop_ralph_daemon(ralph_id: str, force: bool = False) -> tuple[bool, str]:
    """Stop a running Ralph daemon.

    Args:
        ralph_id: ID of the Ralph instance to stop
        force: If True, use SIGKILL instead of SIGTERM

    Returns:
        Tuple of (success, message)
    """
    running, pid = is_ralph_running(ralph_id)
    if not running or pid is None:
        return (False, f"Ralph {ralph_id} is not running")

    try:
        sig = signal.SIGKILL if force else signal.SIGTERM
        os.kill(pid, sig)

        # Clean up PID file
        pid_path = get_ralph_pid_path(ralph_id)
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass

        # Log the stop
        log_path = get_ralph_log_path(ralph_id)
        with open(log_path, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"Ralph {ralph_id} stopped at {datetime.now().isoformat()}\n")
            f.write(f"Signal: {'SIGKILL' if force else 'SIGTERM'}\n")
            f.write(f"{'='*60}\n")

        logger.info(f"Stopped Ralph {ralph_id} (PID: {pid})")

        # Invalidate caches after stopping
        invalidate_process_health_cache(ralph_id)
        from chiefwiggum.cache import error_indicator_cache
        error_indicator_cache.invalidate(f"error:{ralph_id}")

        return (True, f"Stopped Ralph {ralph_id}")

    except ProcessLookupError:
        return (False, f"Ralph {ralph_id} process not found")
    except PermissionError:
        return (False, f"Permission denied to stop Ralph {ralph_id}")
    except Exception as e:
        return (False, f"Error stopping Ralph {ralph_id}: {e}")


def get_running_ralphs() -> list[dict]:
    """Get list of currently running Ralph daemons.

    Returns:
        List of dicts with ralph_id, pid, log_path
    """
    ralphs = []
    data_dir = _ensure_data_dir()

    for pid_file in data_dir.glob("*.pid"):
        ralph_id = pid_file.stem
        running, pid = is_ralph_running(ralph_id)
        if running:
            ralphs.append({
                "ralph_id": ralph_id,
                "pid": pid,
                "log_path": str(get_ralph_log_path(ralph_id)),
                "session_path": str(get_ralph_session_path(ralph_id)),
            })

    return ralphs


def read_ralph_log(ralph_id: str, lines: int = 100) -> str:
    """Read the last N lines of a Ralph's log file.

    Args:
        ralph_id: ID of the Ralph instance
        lines: Number of lines to read

    Returns:
        Log content
    """
    log_path = get_ralph_log_path(ralph_id)
    if not log_path.exists():
        return f"No log file found for Ralph {ralph_id}"

    try:
        # Use errors='replace' to handle non-UTF-8 bytes (terminal color codes, etc.)
        content = log_path.read_text(encoding="utf-8", errors="replace")
        log_lines = content.splitlines()
        if len(log_lines) > lines:
            log_lines = log_lines[-lines:]
        return "\n".join(log_lines)
    except Exception as e:
        return f"Error reading log: {e}"


def stop_all_ralph_daemons() -> list[tuple[str, bool, str]]:
    """Stop all running Ralph daemons.

    Returns:
        List of (ralph_id, success, message) tuples
    """
    results = []
    for ralph in get_running_ralphs():
        success, message = stop_ralph_daemon(ralph["ralph_id"])
        results.append((ralph["ralph_id"], success, message))
    return results


async def count_running_ralphs() -> int:
    """Get count of running Ralph daemons."""
    return len(get_running_ralphs())


# ============================================================================
# Resource Limits (US13)
# ============================================================================

async def get_max_concurrent_ralphs() -> int:
    """Get the maximum number of concurrent Ralphs allowed."""
    value = await get_setting("max_concurrent_ralphs", "5")
    return int(value or "5")


async def set_max_concurrent_ralphs(limit: int) -> None:
    """Set the maximum number of concurrent Ralphs allowed."""
    await set_setting("max_concurrent_ralphs", str(limit))


def find_orphaned_tmux_sessions() -> list[str]:
    """Find tmux sessions named ralph-* that aren't tracked by PID files.

    Returns:
        List of orphaned tmux session names
    """
    orphans = []
    try:
        # Get all tmux sessions
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []

        tmux_sessions = result.stdout.strip().split("\n") if result.stdout.strip() else []

        # Filter for ralph-* sessions
        ralph_sessions = [s for s in tmux_sessions if s.startswith("ralph-")]

        # Get tracked ralph IDs from PID files
        tracked_ralphs = {r["ralph_id"] for r in get_running_ralphs()}

        # Find orphans (tmux sessions not in tracked ralphs)
        for session in ralph_sessions:
            # Session names might be ralph-{timestamp} or ralph-{id}
            # Check if any tracked ralph matches
            is_tracked = any(
                session == f"ralph-{r.split('-')[-1]}" or session in r or r in session
                for r in tracked_ralphs
            )
            if not is_tracked:
                orphans.append(session)

    except FileNotFoundError:
        # tmux not installed
        pass
    except Exception as e:
        logger.warning(f"Error checking for orphaned sessions: {e}")

    return orphans


def cleanup_orphaned_tmux_sessions(dry_run: bool = False) -> list[tuple[str, bool]]:
    """Kill orphaned ralph-* tmux sessions.

    Args:
        dry_run: If True, only report what would be killed

    Returns:
        List of (session_name, was_killed) tuples
    """
    results = []
    orphans = find_orphaned_tmux_sessions()

    for session in orphans:
        if dry_run:
            results.append((session, False))
            logger.info(f"Would kill orphaned tmux session: {session}")
        else:
            try:
                subprocess.run(
                    ["tmux", "kill-session", "-t", session],
                    capture_output=True,
                    check=True,
                )
                results.append((session, True))
                logger.info(f"Killed orphaned tmux session: {session}")
            except subprocess.CalledProcessError:
                results.append((session, False))
                logger.warning(f"Failed to kill tmux session: {session}")

    return results


async def can_spawn_ralph() -> tuple[bool, str]:
    """Check if we can spawn another Ralph instance.

    Validates:
    1. ANTHROPIC_API_KEY is set
    2. Claude CLI is available
    3. Concurrent Ralph limit not exceeded

    Returns:
        Tuple of (can_spawn, reason)
    """
    # Check 1: API key (soft-require — empty is OK when using `claude login`)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key and not api_key.startswith("sk-ant-"):
        return (False, "ANTHROPIC_API_KEY appears invalid (must start with sk-ant-). Unset it or set a real key.")

    # Check 2: Claude CLI available
    if shutil.which("claude") is None:
        return (False, "Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code")

    # Check 3: Concurrent limit
    current = len(get_running_ralphs())
    max_limit = await get_max_concurrent_ralphs()

    if current >= max_limit:
        return (False, f"At limit: {current}/{max_limit} Ralphs running")

    return (True, f"Ready: {current}/{max_limit} Ralphs")
