"""TUI helper functions extracted from the main tui module.

Pure/utility functions used by the TUI dashboard that don't depend on
Rich rendering or the main TUI class.
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from chiefwiggum.cache import error_indicator_cache
from chiefwiggum.config import get_config_value, get_view_state, save_view_state
from chiefwiggum.spawner import read_ralph_status
from chiefwiggum.icons import (
    ICON_ERROR_PERMISSION,
    ICON_ERROR_API,
    ICON_ERROR_TOOL,
    ICON_ERROR_GENERAL,
    PROGRESS_FILLED,
    PROGRESS_EMPTY,
)
from chiefwiggum.tui.state import TUIState


def compute_data_hash(data: Any) -> str:
    """Compute stable hash for dirty-bit detection."""
    if hasattr(data, '__dict__'):
        serializable = {k: str(v) for k, v in data.__dict__.items()}
    elif isinstance(data, list):
        serializable = [
            {k: str(v) for k, v in item.__dict__.items()}
            if hasattr(item, '__dict__') else str(item)
            for item in data
        ]
    else:
        serializable = str(data)

    return hashlib.md5(json.dumps(serializable, sort_keys=True).encode()).hexdigest()


def discover_fix_plan_projects(project: str | None = None) -> list[tuple[str, Path]]:
    """Discover projects by scanning for @fix_plan.md files.

    If project is specified, only returns that project's fix_plan.
    Otherwise scans ~/claudecode/*/ for @fix_plan.md files and also checks cwd.

    Args:
        project: Optional specific project to look for

    Returns:
        List of (project_name, fix_plan_path) tuples.
    """
    projects = []
    claudecode_dir = Path.home() / "claudecode"

    # If a specific project is requested, only look for that one
    if project is not None:
        fix_plan = claudecode_dir / project / "@fix_plan.md"
        if fix_plan.exists():
            return [(project, fix_plan)]
        # Also check cwd if it matches the project name
        if Path.cwd().name == project:
            cwd_fix_plan = Path.cwd() / "@fix_plan.md"
            if cwd_fix_plan.exists():
                return [(project, cwd_fix_plan)]
        return []

    # Otherwise, scan for all projects
    if claudecode_dir.exists():
        for project_dir in claudecode_dir.iterdir():
            if project_dir.is_dir():
                fix_plan = project_dir / "@fix_plan.md"
                if fix_plan.exists():
                    projects.append((project_dir.name, fix_plan))
    # Also check cwd
    cwd_fix_plan = Path.cwd() / "@fix_plan.md"
    if cwd_fix_plan.exists():
        project_name = Path.cwd().name
        if not any(p[0] == project_name for p in projects):
            projects.append((project_name, cwd_fix_plan))
    return projects


def auto_save_view_state(state: "TUIState") -> None:
    """Auto-save view state if persistence is enabled."""
    if get_config_value("persist_view_state", True):
        save_view_state({
            "show_all_tasks": state.show_all_tasks,
            "show_all_instances": state.show_all_instances,
            "view_focus": state.view_focus.name,
            "category_filter": state.category_filter.value if state.category_filter else None,
            "project_filter": state.project_filter,
            "sort_order": state.sort_order.value,
        })


def get_current_project(state: "TUIState") -> str | None:
    """Determine the current project context.

    Priority:
    1. If project_filter is set in TUI state -> use that
    2. If cwd is under ~/claudecode/{project}/ -> use that project
    3. Else -> return None

    Args:
        state: Current TUI state

    Returns:
        Project name or None if unknown
    """
    # 1. Check project filter
    if state.project_filter:
        return state.project_filter

    # 2. Check if cwd is under claudecode
    cwd = Path.cwd()
    claudecode_dir = Path.home() / "claudecode"
    try:
        if claudecode_dir in cwd.parents or cwd.parent == claudecode_dir:
            if cwd.parent == claudecode_dir:
                return cwd.name
            else:
                return cwd.relative_to(claudecode_dir).parts[0]
    except ValueError:
        pass

    return None


def _get_error_indicator(ralph_id: str) -> str:
    """Get error indicator icon and style for a Ralph instance.

    Returns a Rich-formatted string with appropriate icon based on error category.
    """
    status = read_ralph_status(ralph_id)
    if not status:
        return ""

    error_info = status.get("error_info", {})
    category = error_info.get("category", "none")
    count = error_info.get("count", 0)

    if category == "none" or count == 0:
        return ""

    # Map category to icon and color
    error_icons = {
        "permission": (ICON_ERROR_PERMISSION, "magenta"),
        "api_error": (ICON_ERROR_API, "red"),
        "tool_failure": (ICON_ERROR_TOOL, "yellow"),
    }

    icon, color = error_icons.get(category, (ICON_ERROR_GENERAL, "orange1"))
    return f"[{color}]{icon}[/{color}]"


def _get_error_indicator_cached(ralph_id: str) -> str:
    """
    Get error indicator with caching to avoid repeated file I/O.

    This is a cached wrapper around _get_error_indicator() with 5s TTL.
    Use this in UI rendering to avoid blocking on file reads.

    Args:
        ralph_id: Ralph instance ID

    Returns:
        Rich-formatted error indicator string
    """
    cache_key = f"error:{ralph_id}"
    cached_result = error_indicator_cache.get(cache_key)

    if cached_result is not None:
        return cached_result

    # Cache miss - fetch fresh data
    result = _get_error_indicator(ralph_id)
    error_indicator_cache.set(cache_key, result)
    return result


def invalidate_error_indicator_cache(ralph_id: str = None) -> None:
    """
    Invalidate error indicator cache.

    Args:
        ralph_id: Specific Ralph to invalidate, or None to clear all
    """
    if ralph_id:
        error_indicator_cache.invalidate(f"error:{ralph_id}")
    else:
        error_indicator_cache.invalidate_pattern("error:")


def create_progress_bar(percent: int, width: int = 5) -> str:
    """Create a progress bar string from percentage.

    Args:
        percent: Progress percentage (0-100)
        width: Number of characters in the bar

    Returns:
        Progress bar string like [███░░]
    """
    if percent < 0:
        percent = 0
    elif percent > 100:
        percent = 100

    filled = int(width * percent / 100)
    empty = width - filled
    return f"[{PROGRESS_FILLED * filled}{PROGRESS_EMPTY * empty}]"


def format_age(seconds: float) -> str:
    """Format age in seconds to human-readable string."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds / 60)}m"
    elif seconds < 86400:
        return f"{int(seconds / 3600)}h"
    else:
        return f"{int(seconds / 86400)}d"
