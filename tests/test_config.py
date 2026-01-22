"""Tests for ChiefWiggum configuration system.

Tests cover:
- Config schema and defaults
- Config loading/saving
- Ralph permissions helpers
- View state persistence
- Auto-scaling configuration
- Model defaults and quickstart settings
"""

import os
from unittest.mock import patch

import pytest

from chiefwiggum.config import (
    DEFAULT_CONFIG,
    get_api_key,
    get_auto_scaling_config,
    get_category_assignments,
    get_config_value,
    get_default_model,
    get_default_timeout,
    get_max_ralphs,
    get_quickstart_defaults,
    get_ralph_loop_settings,
    get_ralph_permissions,
    get_task_assignment_strategy,
    get_view_state,
    load_config,
    save_config,
    save_view_state,
    set_api_key,
    set_auto_scaling_config,
    set_category_assignments,
    set_config_value,
    set_default_model,
    set_max_ralphs,
    set_ralph_loop_setting,
    set_ralph_permission,
    set_quickstart_defaults,
    set_task_assignment_strategy,
)


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def temp_config_dir(tmp_path):
    """Create a temporary config directory."""
    config_dir = tmp_path / ".config" / "chiefwiggum"
    config_dir.mkdir(parents=True)
    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        yield config_dir


# =============================================================================
# Config Schema Tests
# =============================================================================


class TestDefaultConfig:
    """Tests for default configuration schema."""

    def test_default_config_has_all_keys(self):
        """DEFAULT_CONFIG contains all required keys."""
        required_keys = [
            "anthropic_api_key",
            "max_concurrent_ralphs",
            "default_model",
            "model_fallback_order",
            "rate_limit_rpm",
            "default_timeout_minutes",
            "max_retries",
            "ralph_permissions",
            "category_assignments",
            "task_assignment_strategy",
            "auto_spawn_enabled",
            "auto_spawn_threshold",
            "auto_cleanup_enabled",
            "auto_cleanup_idle_minutes",
            "persist_view_state",
            "view_state",
            "quickstart_defaults",
        ]
        for key in required_keys:
            assert key in DEFAULT_CONFIG, f"Missing key: {key}"

    def test_default_model_valid_enum(self):
        """Default model is a valid value."""
        assert DEFAULT_CONFIG["default_model"] in ("opus", "sonnet", "haiku")

    def test_model_fallback_order_contains_valid_models(self):
        """Model fallback order contains valid models."""
        valid = {"opus", "sonnet", "haiku"}
        for model in DEFAULT_CONFIG["model_fallback_order"]:
            assert model in valid

    def test_rate_limit_rpm_positive_int(self):
        """Rate limit is a positive integer."""
        assert isinstance(DEFAULT_CONFIG["rate_limit_rpm"], int)
        assert DEFAULT_CONFIG["rate_limit_rpm"] > 0

    def test_default_timeout_minutes_positive(self):
        """Default timeout is a positive value."""
        assert DEFAULT_CONFIG["default_timeout_minutes"] > 0

    def test_max_retries_in_range(self):
        """Max retries is in a reasonable range."""
        assert 1 <= DEFAULT_CONFIG["max_retries"] <= 10

    def test_auto_pause_on_failures_threshold(self):
        """Auto-pause threshold is reasonable."""
        assert DEFAULT_CONFIG["auto_pause_on_failures"] > 0


# =============================================================================
# Config Loading/Saving Tests
# =============================================================================


class TestConfigLoadSave:
    """Tests for config loading and saving."""

    def test_load_config_returns_dict(self, temp_config_dir):
        """load_config returns a dictionary."""
        config = load_config()
        assert isinstance(config, dict)

    def test_load_config_with_missing_keys_uses_defaults(self, temp_config_dir):
        """Missing keys use default values."""
        config = load_config()
        # Should have default values for keys not in file
        assert "max_concurrent_ralphs" in config

    def test_save_config_persists_to_yaml(self, temp_config_dir):
        """save_config persists configuration."""
        test_config = {"test_key": "test_value"}
        result = save_config(test_config)
        assert result is True

        loaded = load_config()
        assert loaded.get("test_key") == "test_value"

    def test_get_set_config_value(self, temp_config_dir):
        """get_config_value and set_config_value work correctly."""
        set_config_value("test_setting", 42)
        assert get_config_value("test_setting") == 42
        assert get_config_value("nonexistent", "default") == "default"


# =============================================================================
# Ralph Permissions Tests
# =============================================================================


class TestRalphPermissions:
    """Tests for ralph permissions helpers."""

    def test_get_ralph_permissions_returns_dict(self, temp_config_dir):
        """get_ralph_permissions returns a dictionary."""
        perms = get_ralph_permissions()
        assert isinstance(perms, dict)

    def test_default_permissions_all_true(self):
        """Default permissions are all enabled."""
        default_perms = DEFAULT_CONFIG["ralph_permissions"]
        for key, value in default_perms.items():
            assert value is True, f"{key} should be True by default"

    def test_set_ralph_permission_updates_single_key(self, temp_config_dir):
        """set_ralph_permission updates a single permission."""
        result = set_ralph_permission("run_tests", False)
        assert result is True

        perms = get_ralph_permissions()
        assert perms["run_tests"] is False


# =============================================================================
# View State Tests
# =============================================================================


class TestViewState:
    """Tests for view state persistence."""

    def test_get_view_state_returns_defaults_if_missing(self, temp_config_dir):
        """get_view_state returns defaults when not set."""
        state = get_view_state()
        assert "show_all_tasks" in state
        assert "view_focus" in state

    def test_save_view_state_persists(self, temp_config_dir):
        """save_view_state persists state correctly."""
        test_state = {
            "show_all_tasks": True,
            "show_all_instances": True,
            "view_focus": "TASKS",
            "category_filter": "api",
            "project_filter": "test_project",
            "sort_order": "status",
        }
        result = save_view_state(test_state)
        assert result is True

        loaded = get_view_state()
        assert loaded["show_all_tasks"] is True
        assert loaded["view_focus"] == "TASKS"
        assert loaded["project_filter"] == "test_project"


# =============================================================================
# Auto-Scaling Config Tests
# =============================================================================


class TestAutoScalingConfig:
    """Tests for auto-scaling configuration."""

    def test_get_auto_scaling_config(self, temp_config_dir):
        """get_auto_scaling_config returns all settings."""
        config = get_auto_scaling_config()
        assert "auto_spawn_enabled" in config
        assert "auto_spawn_threshold" in config
        assert "auto_cleanup_enabled" in config
        assert "auto_cleanup_idle_minutes" in config
        assert "max_concurrent_ralphs" in config
        assert "default_model" in config

    def test_set_auto_scaling_config(self, temp_config_dir):
        """set_auto_scaling_config updates settings."""
        result = set_auto_scaling_config({
            "auto_spawn_enabled": True,
            "auto_spawn_threshold": 5,
        })
        assert result is True

        config = get_auto_scaling_config()
        assert config["auto_spawn_enabled"] is True
        assert config["auto_spawn_threshold"] == 5


# =============================================================================
# Model/API Defaults Tests
# =============================================================================


class TestModelDefaults:
    """Tests for model and API default settings."""

    def test_get_default_model(self, temp_config_dir):
        """get_default_model returns valid model."""
        model = get_default_model()
        assert model in ("opus", "sonnet", "haiku")

    def test_set_default_model_valid(self, temp_config_dir):
        """set_default_model accepts valid models."""
        assert set_default_model("opus") is True
        assert get_default_model() == "opus"

        assert set_default_model("haiku") is True
        assert get_default_model() == "haiku"

    def test_set_default_model_invalid(self, temp_config_dir):
        """set_default_model rejects invalid models."""
        original = get_default_model()
        result = set_default_model("invalid_model")
        assert result is False
        assert get_default_model() == original

    def test_get_default_timeout(self, temp_config_dir):
        """get_default_timeout returns positive integer."""
        timeout = get_default_timeout()
        assert isinstance(timeout, int)
        assert timeout > 0


# =============================================================================
# Quickstart Defaults Tests
# =============================================================================


class TestQuickstartDefaults:
    """Tests for quickstart spawn defaults."""

    def test_get_quickstart_defaults(self, temp_config_dir):
        """get_quickstart_defaults returns dict with required keys."""
        defaults = get_quickstart_defaults()
        assert "model" in defaults
        assert "timeout_minutes" in defaults

    def test_set_quickstart_defaults(self, temp_config_dir):
        """set_quickstart_defaults updates settings."""
        result = set_quickstart_defaults({
            "model": "opus",
            "timeout_minutes": 60,
        })
        assert result is True

        defaults = get_quickstart_defaults()
        assert defaults["model"] == "opus"
        assert defaults["timeout_minutes"] == 60


# =============================================================================
# Task Assignment Strategy Tests
# =============================================================================


class TestTaskAssignmentStrategy:
    """Tests for task assignment strategy settings."""

    def test_get_task_assignment_strategy(self, temp_config_dir):
        """get_task_assignment_strategy returns valid strategy."""
        strategy = get_task_assignment_strategy()
        assert strategy in ("priority", "round_robin", "specialized")

    def test_set_task_assignment_strategy_valid(self, temp_config_dir):
        """set_task_assignment_strategy accepts valid strategies."""
        assert set_task_assignment_strategy("round_robin") is True
        assert get_task_assignment_strategy() == "round_robin"

        assert set_task_assignment_strategy("specialized") is True
        assert get_task_assignment_strategy() == "specialized"

    def test_set_task_assignment_strategy_invalid(self, temp_config_dir):
        """set_task_assignment_strategy rejects invalid strategies."""
        original = get_task_assignment_strategy()
        result = set_task_assignment_strategy("invalid")
        assert result is False
        assert get_task_assignment_strategy() == original


# =============================================================================
# Category Assignments Tests
# =============================================================================


class TestCategoryAssignments:
    """Tests for category assignment settings."""

    def test_get_category_assignments_default_empty(self, temp_config_dir):
        """get_category_assignments returns empty dict by default."""
        assignments = get_category_assignments()
        assert isinstance(assignments, dict)

    def test_set_category_assignments(self, temp_config_dir):
        """set_category_assignments persists assignments."""
        assignments = {
            "frontend-": ["ux"],
            "backend-": ["api", "database"],
        }
        result = set_category_assignments(assignments)
        assert result is True

        loaded = get_category_assignments()
        assert loaded["frontend-"] == ["ux"]
        assert "api" in loaded["backend-"]


# =============================================================================
# Max Ralphs Tests
# =============================================================================


class TestMaxRalphs:
    """Tests for max concurrent ralphs setting."""

    def test_get_max_ralphs(self, temp_config_dir):
        """get_max_ralphs returns positive integer."""
        max_ralphs = get_max_ralphs()
        assert isinstance(max_ralphs, int)
        assert max_ralphs > 0

    def test_set_max_ralphs(self, temp_config_dir):
        """set_max_ralphs updates value."""
        result = set_max_ralphs(10)
        assert result is True
        assert get_max_ralphs() == 10


# =============================================================================
# API Key Tests
# =============================================================================


class TestApiKey:
    """Tests for API key management."""

    def test_get_api_key_from_env(self, temp_config_dir):
        """get_api_key returns value from environment."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key-123"}):
            key = get_api_key()
            assert key == "test-key-123"

    def test_set_api_key(self, temp_config_dir):
        """set_api_key saves key to config."""
        result = set_api_key("new-api-key")
        assert result is True

        config = load_config()
        assert config.get("anthropic_api_key") == "new-api-key"


# =============================================================================
# Ralph Loop Settings Tests
# =============================================================================


class TestRalphLoopSettings:
    """Tests for ralph loop settings helpers."""

    def test_default_ralph_loop_settings(self):
        """DEFAULT_CONFIG contains ralph_loop_settings with correct keys."""
        settings = DEFAULT_CONFIG.get("ralph_loop_settings", {})
        assert "session_continuity" in settings
        assert "session_expiry_hours" in settings
        assert "output_format" in settings
        assert "max_calls_per_hour" in settings

    def test_default_values(self):
        """Default values are correct."""
        settings = DEFAULT_CONFIG.get("ralph_loop_settings", {})
        assert settings["session_continuity"] is True
        assert settings["session_expiry_hours"] == 24
        assert settings["output_format"] == "json"
        assert settings["max_calls_per_hour"] == 100

    def test_get_ralph_loop_settings_returns_dict(self, temp_config_dir):
        """get_ralph_loop_settings returns a dictionary."""
        settings = get_ralph_loop_settings()
        assert isinstance(settings, dict)

    def test_get_ralph_loop_settings_has_required_keys(self, temp_config_dir):
        """get_ralph_loop_settings returns all required keys."""
        settings = get_ralph_loop_settings()
        assert "session_continuity" in settings
        assert "session_expiry_hours" in settings
        assert "output_format" in settings
        assert "max_calls_per_hour" in settings

    def test_set_ralph_loop_setting_session_continuity(self, temp_config_dir):
        """set_ralph_loop_setting updates session_continuity."""
        result = set_ralph_loop_setting("session_continuity", False)
        assert result is True

        settings = get_ralph_loop_settings()
        assert settings["session_continuity"] is False

    def test_set_ralph_loop_setting_session_expiry(self, temp_config_dir):
        """set_ralph_loop_setting updates session_expiry_hours."""
        result = set_ralph_loop_setting("session_expiry_hours", 48)
        assert result is True

        settings = get_ralph_loop_settings()
        assert settings["session_expiry_hours"] == 48

    def test_set_ralph_loop_setting_output_format(self, temp_config_dir):
        """set_ralph_loop_setting updates output_format."""
        result = set_ralph_loop_setting("output_format", "text")
        assert result is True

        settings = get_ralph_loop_settings()
        assert settings["output_format"] == "text"

    def test_set_ralph_loop_setting_max_calls(self, temp_config_dir):
        """set_ralph_loop_setting updates max_calls_per_hour."""
        result = set_ralph_loop_setting("max_calls_per_hour", 200)
        assert result is True

        settings = get_ralph_loop_settings()
        assert settings["max_calls_per_hour"] == 200

    def test_set_ralph_loop_setting_invalid_key(self, temp_config_dir):
        """set_ralph_loop_setting rejects invalid keys."""
        result = set_ralph_loop_setting("invalid_key", "value")
        assert result is False
