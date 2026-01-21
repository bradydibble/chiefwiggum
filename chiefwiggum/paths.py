"""ChiefWiggum Path Management

XDG Base Directory compliant path management with platform detection.
Provides centralized path resolution for config, data, and state directories.

Platform-specific paths:
- Linux: ~/.config/chiefwiggum/, ~/.local/share/chiefwiggum/, ~/.local/state/chiefwiggum/
- macOS: ~/Library/Application Support/chiefwiggum/ for config/data, ~/Library/Logs/chiefwiggum/ for state
- Windows: %LOCALAPPDATA%/chiefwiggum/ for all

Migration strategy:
- If legacy ~/.chiefwiggum/ exists AND XDG doesn't -> use legacy (backward compat)
- If both exist -> use XDG (already migrated)
- New install -> use XDG
"""

import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

APP_NAME = "chiefwiggum"
LEGACY_DIR_NAME = ".chiefwiggum"


@dataclass
class ChiefWiggumPaths:
    """Centralized path management for ChiefWiggum.

    Attributes:
        config_dir: Directory for configuration files (config.yaml)
        data_dir: Directory for data files (database, sessions)
        state_dir: Directory for runtime state (logs, PIDs, status)
        using_legacy: True if using legacy ~/.chiefwiggum/ paths
    """

    config_dir: Path
    data_dir: Path
    state_dir: Path
    using_legacy: bool

    @property
    def config_path(self) -> Path:
        """Path to the configuration file."""
        return self.config_dir / "config.yaml"

    @property
    def database_path(self) -> Path:
        """Path to the coordination database."""
        return self.data_dir / "coordination.db"

    @property
    def ralphs_dir(self) -> Path:
        """Directory for Ralph session files and PIDs."""
        return self.state_dir / "ralphs"

    @property
    def task_prompts_dir(self) -> Path:
        """Directory for task-specific prompts."""
        return self.state_dir / "ralphs" / "task_prompts"

    @property
    def status_dir(self) -> Path:
        """Directory for Ralph status files."""
        return self.state_dir / "ralphs" / "status"

    @property
    def logs_dir(self) -> Path:
        """Directory for log files."""
        return self.state_dir / "logs"

    def ensure_dirs(self) -> None:
        """Create all directories if they don't exist."""
        for dir_path in [
            self.config_dir,
            self.data_dir,
            self.state_dir,
            self.ralphs_dir,
            self.task_prompts_dir,
            self.status_dir,
            self.logs_dir,
        ]:
            dir_path.mkdir(parents=True, exist_ok=True)


def _get_legacy_dir() -> Path:
    """Get the legacy ~/.chiefwiggum/ directory path."""
    return Path.home() / LEGACY_DIR_NAME


def _get_xdg_dirs() -> tuple[Path, Path, Path]:
    """Get XDG-compliant directories for the current platform.

    Returns:
        Tuple of (config_dir, data_dir, state_dir)
    """
    try:
        import platformdirs
        config_dir = Path(platformdirs.user_config_dir(APP_NAME))
        data_dir = Path(platformdirs.user_data_dir(APP_NAME))
        # platformdirs doesn't have user_state_dir in all versions, fall back
        try:
            state_dir = Path(platformdirs.user_state_dir(APP_NAME))
        except AttributeError:
            # Older platformdirs, use log dir for state on macOS, data dir elsewhere
            if sys.platform == "darwin":
                state_dir = Path(platformdirs.user_log_dir(APP_NAME))
            else:
                state_dir = Path(platformdirs.user_data_dir(APP_NAME)) / "state"
        return config_dir, data_dir, state_dir
    except ImportError:
        # Fallback if platformdirs not available
        return _get_fallback_xdg_dirs()


def _get_fallback_xdg_dirs() -> tuple[Path, Path, Path]:
    """Fallback XDG directory resolution without platformdirs.

    Returns:
        Tuple of (config_dir, data_dir, state_dir)
    """
    home = Path.home()

    if sys.platform == "darwin":
        # macOS
        app_support = home / "Library" / "Application Support" / APP_NAME
        return (
            app_support,  # config
            app_support,  # data
            home / "Library" / "Logs" / APP_NAME,  # state
        )
    elif sys.platform == "win32":
        # Windows
        local_app_data = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        app_dir = local_app_data / APP_NAME
        return (app_dir, app_dir, app_dir)
    else:
        # Linux/BSD - follow XDG spec
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        data_home = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
        state_home = Path(os.environ.get("XDG_STATE_HOME", home / ".local" / "state"))
        return (
            config_home / APP_NAME,
            data_home / APP_NAME,
            state_home / APP_NAME,
        )


def _should_use_legacy() -> bool:
    """Determine if we should use legacy paths.

    Migration logic:
    - If legacy exists AND XDG config doesn't exist -> use legacy (backward compat)
    - If both exist -> use XDG (already migrated)
    - If neither exists -> use XDG (new install)

    Returns:
        True if should use legacy ~/.chiefwiggum/ paths
    """
    legacy_dir = _get_legacy_dir()
    xdg_config, _, _ = _get_xdg_dirs()

    legacy_exists = legacy_dir.exists()
    xdg_exists = xdg_config.exists()

    if legacy_exists and not xdg_exists:
        return True
    return False


# Singleton instance
_paths_instance: ChiefWiggumPaths | None = None


def get_paths() -> ChiefWiggumPaths:
    """Get the ChiefWiggumPaths singleton.

    This is the primary entry point for accessing paths throughout the application.
    Path resolution is cached after first call.

    Returns:
        ChiefWiggumPaths instance with all resolved paths
    """
    global _paths_instance

    if _paths_instance is not None:
        return _paths_instance

    if _should_use_legacy():
        legacy = _get_legacy_dir()
        _paths_instance = ChiefWiggumPaths(
            config_dir=legacy,
            data_dir=legacy,
            state_dir=legacy,
            using_legacy=True,
        )
        logger.debug(f"Using legacy paths: {legacy}")
    else:
        config_dir, data_dir, state_dir = _get_xdg_dirs()
        _paths_instance = ChiefWiggumPaths(
            config_dir=config_dir,
            data_dir=data_dir,
            state_dir=state_dir,
            using_legacy=False,
        )
        logger.debug(f"Using XDG paths: config={config_dir}, data={data_dir}, state={state_dir}")

    return _paths_instance


def reset_paths_cache() -> None:
    """Reset the paths singleton cache.

    Useful for testing or after migration.
    """
    global _paths_instance
    _paths_instance = None


def migrate_to_xdg(dry_run: bool = False) -> dict[str, list[tuple[Path, Path]]]:
    """Migrate from legacy ~/.chiefwiggum/ to XDG paths.

    Args:
        dry_run: If True, only report what would be migrated without moving files

    Returns:
        Dict with keys 'migrated', 'skipped', 'errors' containing lists of (src, dst) tuples
    """
    result: dict[str, list[tuple[Path, Path]]] = {
        "migrated": [],
        "skipped": [],
        "errors": [],
    }

    legacy_dir = _get_legacy_dir()
    if not legacy_dir.exists():
        logger.info("No legacy directory to migrate")
        return result

    config_dir, data_dir, state_dir = _get_xdg_dirs()

    # Define file mappings: (src_relative, dst_dir)
    migrations = [
        # Config files
        ("config.yaml", config_dir),
        # Data files
        ("coordination.db", data_dir),
        ("coordination.db-shm", data_dir),
        ("coordination.db-wal", data_dir),
    ]

    # Create destination directories
    if not dry_run:
        for dir_path in [config_dir, data_dir, state_dir]:
            dir_path.mkdir(parents=True, exist_ok=True)

    # Migrate individual files
    for src_relative, dst_dir in migrations:
        src = legacy_dir / src_relative
        dst = dst_dir / src_relative

        if not src.exists():
            continue

        if dst.exists():
            result["skipped"].append((src, dst))
            logger.debug(f"Skipping {src} -> {dst} (destination exists)")
            continue

        try:
            if dry_run:
                logger.info(f"Would migrate: {src} -> {dst}")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                logger.info(f"Migrated: {src} -> {dst}")
            result["migrated"].append((src, dst))
        except Exception as e:
            logger.error(f"Failed to migrate {src}: {e}")
            result["errors"].append((src, dst))

    # Migrate ralphs directory (contains logs, PIDs, sessions)
    ralphs_src = legacy_dir / "ralphs"
    ralphs_dst = state_dir / "ralphs"

    if ralphs_src.exists():
        if ralphs_dst.exists():
            result["skipped"].append((ralphs_src, ralphs_dst))
            logger.debug(f"Skipping {ralphs_src} -> {ralphs_dst} (destination exists)")
        else:
            try:
                if dry_run:
                    logger.info(f"Would migrate directory: {ralphs_src} -> {ralphs_dst}")
                else:
                    shutil.copytree(ralphs_src, ralphs_dst)
                    logger.info(f"Migrated directory: {ralphs_src} -> {ralphs_dst}")
                result["migrated"].append((ralphs_src, ralphs_dst))
            except Exception as e:
                logger.error(f"Failed to migrate {ralphs_src}: {e}")
                result["errors"].append((ralphs_src, ralphs_dst))

    # Reset the paths cache so subsequent calls use XDG
    if not dry_run and result["migrated"]:
        reset_paths_cache()

    return result


def get_migration_status() -> dict[str, bool | str]:
    """Get the current migration status.

    Returns:
        Dict with:
        - using_legacy: bool - whether currently using legacy paths
        - legacy_exists: bool - whether legacy directory exists
        - xdg_exists: bool - whether XDG directories exist
        - recommended_action: str - what the user should do
    """
    legacy_dir = _get_legacy_dir()
    config_dir, _, _ = _get_xdg_dirs()

    legacy_exists = legacy_dir.exists()
    xdg_exists = config_dir.exists()
    using_legacy = _should_use_legacy()

    if legacy_exists and not xdg_exists:
        action = "Run 'chiefwiggum migrate' to move to XDG paths"
    elif legacy_exists and xdg_exists:
        action = "Migration complete. Legacy directory can be removed after verification."
    elif not legacy_exists and xdg_exists:
        action = "Using XDG paths (no migration needed)"
    else:
        action = "New installation - XDG paths will be used"

    return {
        "using_legacy": using_legacy,
        "legacy_exists": legacy_exists,
        "xdg_exists": xdg_exists,
        "legacy_path": str(legacy_dir),
        "xdg_config_path": str(config_dir),
        "recommended_action": action,
    }


__all__ = [
    "ChiefWiggumPaths",
    "get_paths",
    "reset_paths_cache",
    "migrate_to_xdg",
    "get_migration_status",
]
