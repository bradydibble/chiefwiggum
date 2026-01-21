"""ChiefWiggum Ralph Spawner

Spawn and manage Ralph (Claude Code) instances as background daemons.
"""

import asyncio
import logging
import os
import signal
import socket
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from chiefwiggum.database import get_setting, set_setting
from chiefwiggum.models import ClaudeModel, RalphConfig, TargetingConfig, TaskCategory, TaskPriority

logger = logging.getLogger(__name__)

# Directory for Ralph session files and logs
RALPH_DATA_DIR = Path.home() / ".chiefwiggum" / "ralphs"


def _ensure_data_dir() -> Path:
    """Ensure the Ralph data directory exists."""
    RALPH_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return RALPH_DATA_DIR


def generate_ralph_id(name: str | None = None) -> str:
    """Generate a unique Ralph ID."""
    hostname = socket.gethostname().split(".")[0]
    if name:
        return f"{hostname}-{name}"
    return f"{hostname}-{uuid.uuid4().hex[:6]}"


def get_ralph_log_path(ralph_id: str) -> Path:
    """Get the log file path for a Ralph instance."""
    return _ensure_data_dir() / f"{ralph_id}.log"


def get_ralph_pid_path(ralph_id: str) -> Path:
    """Get the PID file path for a Ralph instance."""
    return _ensure_data_dir() / f"{ralph_id}.pid"


def get_ralph_session_path(ralph_id: str) -> Path:
    """Get the session file path for a Ralph instance."""
    return _ensure_data_dir() / f"{ralph_id}.session"


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
        targeting: Optional task targeting configuration
        working_dir: Working directory for Ralph (defaults to fix_plan parent)

    Returns:
        Tuple of (success, message)
    """
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

    try:
        # Open log file for output
        log_file = open(log_path, "a")
        log_file.write(f"\n{'='*60}\n")
        log_file.write(f"Ralph {ralph_id} starting at {datetime.now().isoformat()}\n")
        log_file.write(f"Project: {project}\n")
        log_file.write(f"Fix Plan: {fix_plan_path}\n")
        log_file.write(f"Config: {config.model_dump()}\n")
        log_file.write(f"Targeting: {targeting.model_dump()}\n")
        log_file.write(f"Command: {' '.join(cmd)}\n")
        log_file.write(f"{'='*60}\n\n")
        log_file.flush()

        # Spawn the process
        process = subprocess.Popen(
            cmd,
            cwd=str(working_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # Detach from terminal
        )

        # Write PID file
        pid_path.write_text(str(process.pid))

        logger.info(f"Spawned Ralph {ralph_id} with PID {process.pid}")
        return (True, f"Spawned Ralph {ralph_id} (PID: {process.pid})")

    except Exception as e:
        logger.error(f"Failed to spawn Ralph {ralph_id}: {e}")
        return (False, f"Failed to spawn: {e}")


def _build_ralph_command(
    ralph_id: str,
    project: str,
    fix_plan_path: str,
    config: RalphConfig,
    targeting: TargetingConfig,
    session_path: str,
) -> list[str]:
    """Build the Claude command for a Ralph instance.

    This constructs a command that:
    1. Starts Claude with the ralph-loop skill
    2. Passes configuration via environment or arguments
    3. Sets up the appropriate model and persona
    """
    cmd = ["claude"]

    # Add model selection
    model_map = {
        ClaudeModel.OPUS: "opus",
        ClaudeModel.SONNET: "sonnet",
        ClaudeModel.HAIKU: "haiku",
    }
    if config.model in model_map:
        cmd.extend(["--model", model_map[config.model]])

    # Add the ralph-loop prompt with configuration
    prompt_parts = [
        f"/ralph-loop",
        f"--ralph-id {ralph_id}",
        f"--project {project}",
        f"--fix-plan {fix_plan_path}",
    ]

    # Add config options
    if config.timeout_minutes:
        prompt_parts.append(f"--timeout {config.timeout_minutes}")
    if config.no_continue:
        prompt_parts.append("--no-continue")
    if config.max_loops:
        prompt_parts.append(f"--max-loops {config.max_loops}")

    # Add targeting options
    if targeting.priority_min:
        prompt_parts.append(f"--priority-min {targeting.priority_min.value}")
    if targeting.task_id:
        prompt_parts.append(f"--task-id {targeting.task_id}")
    if targeting.categories:
        cats = ",".join(c.value for c in targeting.categories)
        prompt_parts.append(f"--categories {cats}")

    # Add persona if specified
    if config.persona:
        prompt_parts.append(f"--persona {config.persona}")

    # Combine into single prompt argument
    cmd.extend(["--print", " ".join(prompt_parts)])

    # Add dangerously skip permissions to avoid prompts
    cmd.append("--dangerously-skip-permissions")

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
    if not running:
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
        content = log_path.read_text()
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
    return int(value)


async def set_max_concurrent_ralphs(limit: int) -> None:
    """Set the maximum number of concurrent Ralphs allowed."""
    await set_setting("max_concurrent_ralphs", str(limit))


async def can_spawn_ralph() -> tuple[bool, str]:
    """Check if we can spawn another Ralph instance.

    Returns:
        Tuple of (can_spawn, reason)
    """
    current = len(get_running_ralphs())
    max_limit = await get_max_concurrent_ralphs()

    if current >= max_limit:
        return (False, f"At limit: {current}/{max_limit} Ralphs running")

    return (True, f"Can spawn: {current}/{max_limit} Ralphs running")
