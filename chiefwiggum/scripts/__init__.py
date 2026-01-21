"""ChiefWiggum Scripts Package

Bundled shell scripts for self-contained distribution.
"""

import os
from pathlib import Path


def get_ralph_loop_path() -> Path:
    """Find ralph_loop.sh in priority order.

    Resolution order:
    1. Environment override (RALPH_LOOP_PATH) - for development/testing
    2. Bundled script (primary) - in this package's scripts/ directory
    3. Legacy location (backward compat) - ~/claudecode/ralph-claude-code/ralph_loop.sh

    Returns:
        Path to ralph_loop.sh

    Raises:
        RuntimeError: If ralph_loop.sh is not found in any location
    """
    # 1. Environment override (for development/testing)
    if env_path := os.environ.get("RALPH_LOOP_PATH"):
        path = Path(env_path)
        if path.exists():
            return path
        # If env var is set but file doesn't exist, fall through to other options

    # 2. Bundled script (primary)
    bundled = Path(__file__).parent / "ralph_loop.sh"
    if bundled.exists():
        return bundled

    # 3. Legacy location (backward compat during transition)
    legacy = Path.home() / "claudecode" / "ralph-claude-code" / "ralph_loop.sh"
    if legacy.exists():
        return legacy

    raise RuntimeError(
        "ralph_loop.sh not found. "
        "Reinstall chiefwiggum or set RALPH_LOOP_PATH environment variable."
    )


def get_scripts_dir() -> Path:
    """Get the bundled scripts directory path.

    Returns:
        Path to the scripts directory containing ralph_loop.sh and lib/
    """
    return Path(__file__).parent


__all__ = ["get_ralph_loop_path", "get_scripts_dir"]
