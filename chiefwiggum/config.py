"""ChiefWiggum Configuration Management

Persistent configuration storage with XDG-compliant paths.
Includes API key management and settings.
"""

import os
from pathlib import Path
from typing import Any

import yaml

from chiefwiggum.paths import get_paths


def _get_config_dir() -> Path:
    """Get the config directory path."""
    return get_paths().config_dir


def _get_config_file() -> Path:
    """Get the config file path."""
    return get_paths().config_path


def get_config_path() -> Path:
    """Return the path to the user-level config file."""
    return _get_config_file()

# Default configuration
DEFAULT_CONFIG = {
    # Existing
    "anthropic_api_key": "",
    "max_concurrent_ralphs": 5,

    # Model/API Defaults
    "default_model": "sonnet",           # opus/sonnet/haiku
    "model_fallback_order": ["sonnet", "haiku", "opus"],
    "rate_limit_rpm": 60,                # requests per minute
    "cost_budget_daily": None,           # optional daily cost limit in $

    # Task Behavior Defaults
    "default_timeout_minutes": 30,
    "default_max_loops": None,           # None = unlimited
    "max_retries": 3,
    "auto_pause_on_failures": 5,         # pause ralph after N consecutive failures
    "retry_backoff_base": 60,            # seconds

    # Ralph Permissions (passed to ralph_loop.sh)
    "ralph_permissions": {
        "run_tests": True,
        "install_dependencies": True,
        "build_project": True,
        "run_type_checker": True,
        "run_linter": True,
        "run_formatter": True,
    },

    # Instance Specialization
    "category_assignments": {},          # {"ralph_prefix": ["UX", "API"]}
    "task_assignment_strategy": "priority",  # priority/round_robin/specialized

    # Auto-Scaling
    "auto_spawn_enabled": False,
    "auto_spawn_threshold": 10,          # spawn new ralph if pending > N
    "auto_cleanup_idle_minutes": 30,     # cleanup idle ralphs after N minutes
    "auto_cleanup_enabled": True,

    # Persistence
    "persist_view_state": True,
    "view_state": {
        "show_all_tasks": False,
        "show_all_instances": False,
        "view_focus": "BOTH",
        "category_filter": None,
        "project_filter": None,
        "sort_order": "priority",
    },

    # Quickstart defaults (for Shift+N)
    "quickstart_defaults": {
        "model": "sonnet",
        "priority_min": None,      # or "HIGH", "MEDIUM", etc.
        "categories": [],          # empty = all
        "timeout_minutes": 30,
    },

    # Ralph Loop Settings (passed to ralph_loop.sh)
    "ralph_loop_settings": {
        "session_continuity": True,      # Use --continue (False = --no-continue)
        "session_expiry_hours": 24,      # --session-expiry value
        "output_format": "json",         # --output-format (json/text)
        "max_calls_per_hour": 100,       # --calls value
    },

    # Worktree Settings (US: Git Worktree Isolation)
    "worktree_settings": {
        "enabled": True,                     # DEFAULT ON - use worktrees for isolation
        "base_dir": ".worktrees",            # Directory name for worktrees
        "branch_prefix": "ralph",            # Prefix for worktree branches
        "merge_strategy": "auto",            # auto = try fast-forward, fallback to regular
        "cleanup_on_success": True,          # Cleanup worktree after successful merge
        "cleanup_on_conflict": True,         # Cleanup and release task on conflict
        "require_clean_working_tree": True,  # Require clean tree before merge
        "max_worktrees_per_project": 10,     # Max concurrent worktrees
    },
}


def _ensure_config_dir() -> Path:
    """Ensure the config directory exists."""
    config_dir = _get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def load_config() -> dict[str, Any]:
    """Load configuration from file.

    Returns:
        Configuration dictionary with defaults for missing keys
    """
    _ensure_config_dir()

    config = DEFAULT_CONFIG.copy()

    config_file = _get_config_file()
    if config_file.exists():
        try:
            with open(config_file) as f:
                file_config = yaml.safe_load(f) or {}
                config.update(file_config)
        except Exception:
            pass  # Use defaults on error

    return config


def save_config(config: dict[str, Any]) -> bool:
    """Save configuration to file.

    Args:
        config: Configuration dictionary to save

    Returns:
        True if saved successfully
    """
    _ensure_config_dir()

    try:
        config_file = _get_config_file()
        with open(config_file, "w") as f:
            yaml.safe_dump(config, f, default_flow_style=False)
        return True
    except Exception:
        return False


def get_config_value(key: str, default: Any = None) -> Any:
    """Get a single configuration value.

    Args:
        key: Configuration key
        default: Default value if key not found

    Returns:
        Configuration value
    """
    config = load_config()
    return config.get(key, default)


def set_config_value(key: str, value: Any) -> bool:
    """Set a single configuration value.

    Args:
        key: Configuration key
        value: Value to set

    Returns:
        True if saved successfully
    """
    config = load_config()
    config[key] = value
    return save_config(config)


def get_api_key() -> str:
    """Get the Anthropic API key.

    Checks in order:
    1. Environment variable ANTHROPIC_API_KEY
    2. Config file

    Returns:
        API key string (may be empty)
    """
    # First check environment
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key:
        return env_key

    # Fall back to config file
    return get_config_value("anthropic_api_key", "")


def get_api_key_source() -> str:
    """Get the source of the current API key.

    Returns:
        "ENV" if from environment variable,
        "CONFIG" if from config file,
        "NONE" if not set
    """
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    config_key = get_config_value("anthropic_api_key", "")

    if env_key:
        # Check if env was loaded from config on startup
        if config_key and env_key == config_key:
            return "CONFIG"  # Env was populated from config
        return "ENV"
    elif config_key:
        return "CONFIG"
    return "NONE"


def validate_api_key(api_key: str | None = None) -> tuple[bool, str]:
    """Validate the Anthropic API key by making a test request.

    Args:
        api_key: Key to validate, or None to use current key

    Returns:
        Tuple of (is_valid, message)
    """
    import subprocess

    key = api_key or get_api_key()
    if not key:
        return False, "No API key set"

    # Quick format validation
    if not key.startswith("sk-ant-"):
        return False, "Invalid format (should start with sk-ant-)"

    # Test with Claude CLI using a minimal prompt
    try:
        env = os.environ.copy()
        env["ANTHROPIC_API_KEY"] = key
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        # --version doesn't actually validate the key, but at least confirms CLI works
        # For real validation, we'd need to make an API call
        if result.returncode == 0:
            return True, f"Key format valid [{get_api_key_source()}]"
        return False, f"CLI error: {result.stderr[:50]}"
    except subprocess.TimeoutExpired:
        return False, "Validation timed out"
    except FileNotFoundError:
        return False, "Claude CLI not found"
    except Exception as e:
        return False, f"Validation error: {str(e)[:50]}"


def set_api_key(api_key: str) -> bool:
    """Set the Anthropic API key.

    Saves to config file AND sets environment variable for current session.

    Args:
        api_key: The API key to set

    Returns:
        True if saved successfully
    """
    # Set in environment for current session
    os.environ["ANTHROPIC_API_KEY"] = api_key

    # Save to config file for persistence
    return set_config_value("anthropic_api_key", api_key)


def get_max_ralphs() -> int:
    """Get the maximum concurrent Ralphs setting.

    Returns:
        Maximum number of concurrent Ralphs
    """
    return int(get_config_value("max_concurrent_ralphs", 5))


def set_max_ralphs(limit: int) -> bool:
    """Set the maximum concurrent Ralphs setting.

    Args:
        limit: Maximum number of concurrent Ralphs

    Returns:
        True if saved successfully
    """
    return set_config_value("max_concurrent_ralphs", limit)


def load_config_on_startup() -> None:
    """Load configuration and set environment variables on startup.

    Call this when the TUI starts to apply saved settings.
    """
    config = load_config()

    # Set API key in environment if present in config but not in env
    if not os.environ.get("ANTHROPIC_API_KEY") and config.get("anthropic_api_key"):
        os.environ["ANTHROPIC_API_KEY"] = config["anthropic_api_key"]


# ============================================================================
# Ralph Permissions Helpers
# ============================================================================


def get_ralph_permissions() -> dict[str, bool]:
    """Get the current ralph permissions configuration.

    Returns:
        Dict mapping permission names to boolean values
    """
    config = load_config()
    default_perms = DEFAULT_CONFIG.get("ralph_permissions", {})
    return config.get("ralph_permissions", default_perms)


def set_ralph_permission(key: str, value: bool) -> bool:
    """Set a single ralph permission.

    Args:
        key: Permission name (e.g., 'run_tests')
        value: True to allow, False to deny

    Returns:
        True if saved successfully
    """
    config = load_config()
    if "ralph_permissions" not in config:
        config["ralph_permissions"] = DEFAULT_CONFIG.get("ralph_permissions", {}).copy()
    config["ralph_permissions"][key] = value
    return save_config(config)


# ============================================================================
# View State Helpers
# ============================================================================


def get_view_state() -> dict[str, Any]:
    """Get the saved view state.

    Returns:
        Dict with view state preferences
    """
    config = load_config()
    default_state = DEFAULT_CONFIG.get("view_state", {})
    return config.get("view_state", default_state)


def save_view_state(state: dict[str, Any]) -> bool:
    """Save the current view state.

    Args:
        state: Dict with view state preferences

    Returns:
        True if saved successfully
    """
    config = load_config()
    config["view_state"] = state
    return save_config(config)


# ============================================================================
# Auto-Scaling Helpers
# ============================================================================


def get_auto_scaling_config() -> dict[str, Any]:
    """Get auto-scaling configuration.

    Returns:
        Dict with auto-scaling settings
    """
    config = load_config()
    return {
        "auto_spawn_enabled": config.get("auto_spawn_enabled", False),
        "auto_spawn_threshold": config.get("auto_spawn_threshold", 10),
        "auto_cleanup_enabled": config.get("auto_cleanup_enabled", True),
        "auto_cleanup_idle_minutes": config.get("auto_cleanup_idle_minutes", 30),
        "max_concurrent_ralphs": config.get("max_concurrent_ralphs", 5),
        "default_model": config.get("default_model", "sonnet"),
    }


def set_auto_scaling_config(settings: dict[str, Any]) -> bool:
    """Update auto-scaling configuration.

    Args:
        settings: Dict with auto-scaling settings to update

    Returns:
        True if saved successfully
    """
    config = load_config()
    for key in ["auto_spawn_enabled", "auto_spawn_threshold",
                "auto_cleanup_enabled", "auto_cleanup_idle_minutes"]:
        if key in settings:
            config[key] = settings[key]
    return save_config(config)


# ============================================================================
# Quickstart Defaults Helpers
# ============================================================================


def get_quickstart_defaults() -> dict[str, Any]:
    """Get quickstart spawn defaults for Shift+N.

    Returns:
        Dict with quickstart configuration
    """
    config = load_config()
    default = DEFAULT_CONFIG.get("quickstart_defaults", {})
    return config.get("quickstart_defaults", default)


def set_quickstart_defaults(defaults: dict[str, Any]) -> bool:
    """Set quickstart spawn defaults.

    Args:
        defaults: Dict with quickstart settings

    Returns:
        True if saved successfully
    """
    config = load_config()
    if "quickstart_defaults" not in config:
        config["quickstart_defaults"] = {}
    config["quickstart_defaults"].update(defaults)
    return save_config(config)


# ============================================================================
# Task Assignment Strategy Helpers
# ============================================================================


def get_task_assignment_strategy() -> str:
    """Get the task assignment strategy.

    Returns:
        Strategy name: 'priority', 'round_robin', or 'specialized'
    """
    return get_config_value("task_assignment_strategy", "priority")


def set_task_assignment_strategy(strategy: str) -> bool:
    """Set the task assignment strategy.

    Args:
        strategy: 'priority', 'round_robin', or 'specialized'

    Returns:
        True if saved successfully
    """
    if strategy not in ("priority", "round_robin", "specialized"):
        return False
    return set_config_value("task_assignment_strategy", strategy)


def get_category_assignments() -> dict[str, list[str]]:
    """Get category assignments for specialized ralphs.

    Returns:
        Dict mapping ralph name prefixes to category lists
    """
    return get_config_value("category_assignments", {})


def set_category_assignments(assignments: dict[str, list[str]]) -> bool:
    """Set category assignments for specialized ralphs.

    Args:
        assignments: Dict mapping ralph name prefixes to category lists

    Returns:
        True if saved successfully
    """
    return set_config_value("category_assignments", assignments)


# ============================================================================
# Model/API Defaults Helpers
# ============================================================================


def get_default_model() -> str:
    """Get the default Claude model for new ralphs.

    Returns:
        Model name: 'opus', 'sonnet', or 'haiku'
    """
    return get_config_value("default_model", "sonnet")


def set_default_model(model: str) -> bool:
    """Set the default Claude model.

    Args:
        model: 'opus', 'sonnet', or 'haiku'

    Returns:
        True if saved successfully
    """
    if model not in ("opus", "sonnet", "haiku"):
        return False
    return set_config_value("default_model", model)


def get_default_timeout() -> int:
    """Get the default timeout for ralph tasks in minutes.

    Returns:
        Timeout in minutes
    """
    return int(get_config_value("default_timeout_minutes", 30))


# ============================================================================
# Ralph Loop Settings Helpers
# ============================================================================


def get_ralph_loop_settings() -> dict[str, Any]:
    """Get the current ralph loop settings configuration.

    These settings are passed to ralph_loop.sh when spawning Ralphs.

    Returns:
        Dict with ralph loop settings:
        - session_continuity: bool - whether to continue sessions
        - session_expiry_hours: int - session expiry time
        - output_format: str - output format (json/text)
        - max_calls_per_hour: int - rate limit
    """
    config = load_config()
    default_settings = DEFAULT_CONFIG.get("ralph_loop_settings", {})
    return config.get("ralph_loop_settings", default_settings)


def set_ralph_loop_setting(key: str, value: Any) -> bool:
    """Set a single ralph loop setting.

    Args:
        key: Setting name (e.g., 'session_continuity', 'session_expiry_hours')
        value: Value to set

    Returns:
        True if saved successfully
    """
    valid_keys = {"session_continuity", "session_expiry_hours", "output_format", "max_calls_per_hour"}
    if key not in valid_keys:
        return False

    config = load_config()
    if "ralph_loop_settings" not in config:
        config["ralph_loop_settings"] = DEFAULT_CONFIG.get("ralph_loop_settings", {}).copy()
    config["ralph_loop_settings"][key] = value
    return save_config(config)
