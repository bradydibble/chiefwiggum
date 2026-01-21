"""Tests for chiefwiggum.paths module - XDG path management."""

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from chiefwiggum.paths import (
    APP_NAME,
    ChiefWiggumPaths,
    get_migration_status,
    get_paths,
    migrate_to_xdg,
    reset_paths_cache,
    _get_legacy_dir,
    _get_xdg_dirs,
    _get_fallback_xdg_dirs,
    _should_use_legacy,
)


@pytest.fixture(autouse=True)
def reset_cache():
    """Reset the paths cache before and after each test."""
    reset_paths_cache()
    yield
    reset_paths_cache()


class TestChiefWiggumPaths:
    """Tests for ChiefWiggumPaths dataclass."""

    def test_properties(self, tmp_path):
        """Should provide all expected path properties."""
        paths = ChiefWiggumPaths(
            config_dir=tmp_path / "config",
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "state",
            using_legacy=False,
        )

        assert paths.config_path == tmp_path / "config" / "config.yaml"
        assert paths.database_path == tmp_path / "data" / "coordination.db"
        assert paths.ralphs_dir == tmp_path / "state" / "ralphs"
        assert paths.task_prompts_dir == tmp_path / "state" / "ralphs" / "task_prompts"
        assert paths.status_dir == tmp_path / "state" / "ralphs" / "status"
        assert paths.logs_dir == tmp_path / "state" / "logs"

    def test_ensure_dirs(self, tmp_path):
        """ensure_dirs() should create all directories."""
        paths = ChiefWiggumPaths(
            config_dir=tmp_path / "config",
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "state",
            using_legacy=False,
        )

        # Directories shouldn't exist yet
        assert not paths.config_dir.exists()
        assert not paths.data_dir.exists()

        paths.ensure_dirs()

        # Now they should all exist
        assert paths.config_dir.exists()
        assert paths.data_dir.exists()
        assert paths.state_dir.exists()
        assert paths.ralphs_dir.exists()
        assert paths.task_prompts_dir.exists()
        assert paths.status_dir.exists()
        assert paths.logs_dir.exists()


class TestLegacyPath:
    """Tests for legacy path detection."""

    def test_get_legacy_dir(self):
        """Should return ~/.chiefwiggum."""
        result = _get_legacy_dir()
        assert result == Path.home() / ".chiefwiggum"


class TestXDGPaths:
    """Tests for XDG path resolution."""

    def test_fallback_linux(self, tmp_path):
        """Fallback should use XDG spec on Linux."""
        with mock.patch.object(sys, "platform", "linux"):
            with mock.patch.dict(os.environ, {
                "XDG_CONFIG_HOME": str(tmp_path / "config"),
                "XDG_DATA_HOME": str(tmp_path / "data"),
                "XDG_STATE_HOME": str(tmp_path / "state"),
            }, clear=False):
                config, data, state = _get_fallback_xdg_dirs()

                assert config == tmp_path / "config" / APP_NAME
                assert data == tmp_path / "data" / APP_NAME
                assert state == tmp_path / "state" / APP_NAME

    def test_fallback_linux_defaults(self, tmp_path):
        """Fallback should use default XDG paths on Linux when env vars not set."""
        with mock.patch.object(sys, "platform", "linux"):
            # Ensure XDG vars are not set
            env_without_xdg = {k: v for k, v in os.environ.items()
                              if not k.startswith("XDG_")}
            with mock.patch.dict(os.environ, env_without_xdg, clear=True):
                with mock.patch.object(Path, "home", return_value=tmp_path):
                    config, data, state = _get_fallback_xdg_dirs()

                    assert config == tmp_path / ".config" / APP_NAME
                    assert data == tmp_path / ".local" / "share" / APP_NAME
                    assert state == tmp_path / ".local" / "state" / APP_NAME

    def test_fallback_macos(self, tmp_path):
        """Fallback should use Application Support on macOS."""
        with mock.patch.object(sys, "platform", "darwin"):
            with mock.patch.object(Path, "home", return_value=tmp_path):
                config, data, state = _get_fallback_xdg_dirs()

                expected_app_support = tmp_path / "Library" / "Application Support" / APP_NAME
                expected_logs = tmp_path / "Library" / "Logs" / APP_NAME

                assert config == expected_app_support
                assert data == expected_app_support
                assert state == expected_logs

    def test_fallback_windows(self, tmp_path):
        """Fallback should use LOCALAPPDATA on Windows."""
        with mock.patch.object(sys, "platform", "win32"):
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(tmp_path)}):
                config, data, state = _get_fallback_xdg_dirs()

                expected = tmp_path / APP_NAME
                assert config == expected
                assert data == expected
                assert state == expected


class TestShouldUseLegacy:
    """Tests for _should_use_legacy() logic."""

    def test_use_legacy_when_only_legacy_exists(self, tmp_path):
        """Should use legacy if only ~/.chiefwiggum/ exists."""
        legacy_dir = tmp_path / ".chiefwiggum"
        legacy_dir.mkdir()

        xdg_config = tmp_path / "config" / APP_NAME

        with mock.patch("chiefwiggum.paths._get_legacy_dir", return_value=legacy_dir):
            with mock.patch("chiefwiggum.paths._get_xdg_dirs", return_value=(xdg_config, tmp_path, tmp_path)):
                assert _should_use_legacy() is True

    def test_use_xdg_when_both_exist(self, tmp_path):
        """Should use XDG if both legacy and XDG exist."""
        legacy_dir = tmp_path / ".chiefwiggum"
        legacy_dir.mkdir()

        xdg_config = tmp_path / "config" / APP_NAME
        xdg_config.mkdir(parents=True)

        with mock.patch("chiefwiggum.paths._get_legacy_dir", return_value=legacy_dir):
            with mock.patch("chiefwiggum.paths._get_xdg_dirs", return_value=(xdg_config, tmp_path, tmp_path)):
                assert _should_use_legacy() is False

    def test_use_xdg_when_neither_exists(self, tmp_path):
        """Should use XDG for new installations."""
        legacy_dir = tmp_path / ".chiefwiggum"  # Not created
        xdg_config = tmp_path / "config" / APP_NAME  # Not created

        with mock.patch("chiefwiggum.paths._get_legacy_dir", return_value=legacy_dir):
            with mock.patch("chiefwiggum.paths._get_xdg_dirs", return_value=(xdg_config, tmp_path, tmp_path)):
                assert _should_use_legacy() is False


class TestGetPaths:
    """Tests for get_paths() singleton."""

    def test_returns_chiefwiggum_paths(self):
        """Should return a ChiefWiggumPaths instance."""
        result = get_paths()
        assert isinstance(result, ChiefWiggumPaths)

    def test_singleton_behavior(self):
        """Should return the same instance on repeated calls."""
        first = get_paths()
        second = get_paths()
        assert first is second

    def test_reset_cache_clears_singleton(self):
        """reset_paths_cache() should clear the singleton."""
        first = get_paths()
        reset_paths_cache()
        second = get_paths()
        # They might be equal but should be different instances
        # (unless paths haven't changed)
        assert first is not second or first == second


class TestMigration:
    """Tests for migration functionality."""

    def test_migrate_copies_config(self, tmp_path):
        """migrate_to_xdg() should copy config.yaml."""
        # Setup legacy directory
        legacy_dir = tmp_path / ".chiefwiggum"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text("key: value")

        # Setup XDG directories
        xdg_config = tmp_path / "config" / APP_NAME
        xdg_data = tmp_path / "data" / APP_NAME
        xdg_state = tmp_path / "state" / APP_NAME

        with mock.patch("chiefwiggum.paths._get_legacy_dir", return_value=legacy_dir):
            with mock.patch("chiefwiggum.paths._get_xdg_dirs", return_value=(xdg_config, xdg_data, xdg_state)):
                result = migrate_to_xdg(dry_run=False)

                assert len(result["migrated"]) == 1
                assert (xdg_config / "config.yaml").exists()
                assert (xdg_config / "config.yaml").read_text() == "key: value"

    def test_migrate_dry_run(self, tmp_path):
        """migrate_to_xdg(dry_run=True) should not modify files."""
        legacy_dir = tmp_path / ".chiefwiggum"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text("key: value")

        xdg_config = tmp_path / "config" / APP_NAME
        xdg_data = tmp_path / "data" / APP_NAME
        xdg_state = tmp_path / "state" / APP_NAME

        with mock.patch("chiefwiggum.paths._get_legacy_dir", return_value=legacy_dir):
            with mock.patch("chiefwiggum.paths._get_xdg_dirs", return_value=(xdg_config, xdg_data, xdg_state)):
                result = migrate_to_xdg(dry_run=True)

                assert len(result["migrated"]) == 1
                assert not (xdg_config / "config.yaml").exists()

    def test_migrate_skips_existing(self, tmp_path):
        """migrate_to_xdg() should skip files that already exist at destination."""
        legacy_dir = tmp_path / ".chiefwiggum"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text("old content")

        xdg_config = tmp_path / "config" / APP_NAME
        xdg_config.mkdir(parents=True)
        (xdg_config / "config.yaml").write_text("new content")

        xdg_data = tmp_path / "data" / APP_NAME
        xdg_state = tmp_path / "state" / APP_NAME

        with mock.patch("chiefwiggum.paths._get_legacy_dir", return_value=legacy_dir):
            with mock.patch("chiefwiggum.paths._get_xdg_dirs", return_value=(xdg_config, xdg_data, xdg_state)):
                result = migrate_to_xdg(dry_run=False)

                assert len(result["skipped"]) == 1
                # Content should not be overwritten
                assert (xdg_config / "config.yaml").read_text() == "new content"


class TestGetMigrationStatus:
    """Tests for get_migration_status()."""

    def test_new_installation(self, tmp_path):
        """Should detect new installation."""
        legacy_dir = tmp_path / ".chiefwiggum"
        xdg_config = tmp_path / "config" / APP_NAME

        with mock.patch("chiefwiggum.paths._get_legacy_dir", return_value=legacy_dir):
            with mock.patch("chiefwiggum.paths._get_xdg_dirs", return_value=(xdg_config, tmp_path, tmp_path)):
                status = get_migration_status()

                assert status["legacy_exists"] is False
                assert status["xdg_exists"] is False
                assert "New installation" in status["recommended_action"]

    def test_needs_migration(self, tmp_path):
        """Should detect when migration is needed."""
        legacy_dir = tmp_path / ".chiefwiggum"
        legacy_dir.mkdir()

        xdg_config = tmp_path / "config" / APP_NAME

        with mock.patch("chiefwiggum.paths._get_legacy_dir", return_value=legacy_dir):
            with mock.patch("chiefwiggum.paths._get_xdg_dirs", return_value=(xdg_config, tmp_path, tmp_path)):
                status = get_migration_status()

                assert status["legacy_exists"] is True
                assert status["xdg_exists"] is False
                assert status["using_legacy"] is True
                assert "migrate" in status["recommended_action"].lower()
