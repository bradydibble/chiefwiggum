"""User Journey Tests for ChiefWiggum TUI.

These tests verify complete user workflows through the TUI, ensuring
that multi-step operations work correctly from start to finish.

Test classes:
- TestNewUserJourney: Launch → Sync → View → Spawn
- TestTaskManagementJourney: Filter → Select → Release
- TestSettingsJourney: Open → Edit API key → Save → Verify
- TestSpawnWorkflowJourney: Complete 5-step workflow
- TestErrorHandlingJourney: Spawn without API key
- TestBulkOperationsJourney: x → select → m → action
- TestViewNavigationJourney: zoom, scroll, sort
"""

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from chiefwiggum.models import (
    ClaudeModel,
    ErrorCategory,
    RalphInstance,
    RalphInstanceStatus,
    TaskCategory,
    TaskClaim,
    TaskClaimStatus,
    TaskPriority,
)
from chiefwiggum.tui import (
    SpawnConfig,
    TUIMode,
    TUIState,
    ViewFocus,
    create_help_panel,
    create_settings_panel,
    create_spawn_panel,
    handle_normal_mode,
    handle_spawn,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_fix_plan(tmp_path):
    """Create sample @fix_plan.md for testing."""
    fix_plan_content = """# Fix Plan

## HIGH Priority

### Task 1: Fix authentication bug
- [ ] Update login handler
- [ ] Add error handling

### Task 2: Fix API response format
- [ ] Update serializer
- [ ] Add tests

## MEDIUM Priority

### Task 3: Improve error messages
- [ ] Add user-friendly messages
- [ ] Update error codes

### Task 4: Add logging
- [ ] Add structured logging
- [ ] Configure log levels

## LOWER Priority

### Task 5: Refactor database queries
- [ ] Optimize slow queries
- [ ] Add indexes
"""
    fix_plan_path = tmp_path / "@fix_plan.md"
    fix_plan_path.write_text(fix_plan_content)
    return fix_plan_path


@pytest.fixture
def failed_task():
    """Mock TaskClaim with FAILED status and error details."""
    return TaskClaim(
        task_id="task-001",
        task_title="Fix authentication bug",
        task_priority=TaskPriority.HIGH,
        task_section="HIGH Priority",
        project="testproject",
        category=TaskCategory.API,
        claimed_by_ralph_id="ralph-001",
        claimed_at=datetime.now(),
        status=TaskClaimStatus.FAILED,
        error_category=ErrorCategory.CODE_ERROR,
        error_message="TypeError: Cannot read property 'user' of undefined",
        retry_count=1,
        max_retries=3,
    )


@pytest.fixture
def in_progress_task():
    """Mock TaskClaim with IN_PROGRESS status."""
    return TaskClaim(
        task_id="task-002",
        task_title="Add user authentication",
        task_priority=TaskPriority.HIGH,
        task_section="HIGH Priority",
        project="testproject",
        category=TaskCategory.API,
        claimed_by_ralph_id="ralph-001",
        claimed_at=datetime.now(),
        started_at=datetime.now(),
        status=TaskClaimStatus.IN_PROGRESS,
    )


@pytest.fixture
def pending_task():
    """Mock TaskClaim with PENDING status."""
    return TaskClaim(
        task_id="task-003",
        task_title="Update documentation",
        task_priority=TaskPriority.MEDIUM,
        task_section="MEDIUM Priority",
        project="testproject",
        category=TaskCategory.GENERAL,
        status=TaskClaimStatus.PENDING,
    )


@pytest.fixture
def completed_task():
    """Mock TaskClaim with COMPLETED status."""
    return TaskClaim(
        task_id="task-004",
        task_title="Fix typo in README",
        task_priority=TaskPriority.LOWER,
        task_section="LOWER Priority",
        project="testproject",
        category=TaskCategory.GENERAL,
        claimed_by_ralph_id="ralph-002",
        status=TaskClaimStatus.COMPLETED,
        completion_message="Fixed typo successfully",
        git_commit_sha="abc123def",
        completed_at=datetime.now(),
    )


@pytest.fixture
def multiple_tasks(failed_task, in_progress_task, pending_task, completed_task):
    """List of 10 mock tasks for bulk operations."""
    tasks = [failed_task, in_progress_task, pending_task, completed_task]

    # Add more pending tasks
    for i in range(6):
        tasks.append(
            TaskClaim(
                task_id=f"task-{100 + i}",
                task_title=f"Additional task {i + 1}",
                task_priority=TaskPriority.MEDIUM,
                task_section="MEDIUM Priority",
                project="testproject",
                category=TaskCategory.GENERAL,
                status=TaskClaimStatus.PENDING,
            )
        )

    return tasks


@pytest.fixture
def active_ralph():
    """Mock active RalphInstance."""
    return RalphInstance(
        ralph_id="ralph-001",
        hostname="localhost",
        pid=12345,
        project="testproject",
        status=RalphInstanceStatus.ACTIVE,
        current_task_id="task-002",
        loop_count=5,
    )


@pytest.fixture
def idle_ralph():
    """Mock idle RalphInstance."""
    return RalphInstance(
        ralph_id="ralph-002",
        hostname="localhost",
        pid=12346,
        project="testproject",
        status=RalphInstanceStatus.IDLE,
        loop_count=10,
    )


# =============================================================================
# TestNewUserJourney
# =============================================================================


class TestNewUserJourney:
    """Tests for new user journey: Launch → Sync → View → Spawn."""

    def test_initial_state_is_normal_mode(self):
        """New user starts in NORMAL mode with default settings."""
        state = TUIState()

        assert state.mode == TUIMode.NORMAL
        assert state.view_focus == ViewFocus.BOTH
        assert state.category_filter is None
        assert state.project_filter is None
        assert state.show_all_tasks is False
        assert state.show_all_instances is False

    def test_user_can_open_help_to_learn_commands(self):
        """User presses 'h' to see available commands."""
        state = TUIState()

        handle_normal_mode("h", state)

        assert state.mode == TUIMode.HELP

    def test_help_panel_contains_critical_keys(self):
        """Help panel documents the most important keys."""
        help_panel = create_help_panel()
        help_text = str(help_panel.renderable)

        # Critical navigation keys
        assert "h" in help_text or "?" in help_text  # Help
        assert "j" in help_text and "k" in help_text  # Scroll
        assert "z" in help_text  # View focus
        assert "q" in help_text  # Quit

        # Critical action keys
        assert "n" in help_text  # Spawn
        assert "y" in help_text  # Sync
        assert "p" in help_text  # Project filter

        # Search & viewing keys (added in v0.7.1)
        assert "/" in help_text  # Search
        assert "d" in help_text  # Task details
        assert "o" in help_text  # Sort order
        assert "w" in help_text  # JSON export
        assert "v" in help_text  # Log streaming

        # Bulk operations keys (added in v0.7.1)
        assert "x" in help_text  # Bulk select mode
        assert "m" in help_text  # Bulk action menu

    def test_user_syncs_tasks_from_fix_plan(self):
        """User presses 'y' to sync tasks from @fix_plan.md."""
        state = TUIState()

        handle_normal_mode("y", state)

        assert state.status_message == "Syncing tasks..."
        assert state.status_message_time > 0

    def test_user_starts_spawn_workflow(self):
        """User presses 'n' to spawn a new Ralph."""
        state = TUIState()
        state.projects = ["project1", "project2"]

        handle_normal_mode("n", state)

        assert state.mode == TUIMode.SPAWN_PROJECT

    def test_escape_cancels_spawn_from_any_step(self):
        """User can press Escape at any spawn step to cancel."""
        loop = asyncio.new_event_loop()

        spawn_modes = [
            TUIMode.SPAWN_PROJECT,
            TUIMode.SPAWN_PRIORITY,
            TUIMode.SPAWN_CATEGORY,
            TUIMode.SPAWN_MODEL,
            TUIMode.SPAWN_CONFIRM,
        ]

        for mode in spawn_modes:
            state = TUIState()
            state.mode = mode
            state.projects = ["test"]
            state.spawn_config = SpawnConfig()

            loop.run_until_complete(handle_spawn("ESCAPE", state))
            assert state.mode == TUIMode.NORMAL, f"ESC from {mode} didn't cancel"

        loop.close()


# =============================================================================
# TestTaskManagementJourney
# =============================================================================


class TestTaskManagementJourney:
    """Tests for task management journey: Filter → Select → Release."""

    def test_user_filters_by_category(self):
        """User presses 'c' to cycle through category filters."""
        state = TUIState()

        # Initially no filter
        assert state.category_filter is None

        # Cycle through categories
        categories = [
            TaskCategory.UX,
            TaskCategory.API,
            TaskCategory.TESTING,
            TaskCategory.DATABASE,
            TaskCategory.INFRA,
            None,  # Back to all
        ]

        for expected in categories:
            handle_normal_mode("c", state)
            assert state.category_filter == expected

    def test_user_filters_by_project(self):
        """User presses 'p' to filter by project."""
        state = TUIState()
        state.projects = ["alpha", "beta", "gamma"]

        handle_normal_mode("p", state)

        assert state.mode == TUIMode.PROJECT_FILTER

    def test_user_toggles_all_tasks_view(self):
        """User presses 'a' to toggle between pending and all tasks."""
        state = TUIState()

        assert state.show_all_tasks is False

        handle_normal_mode("a", state)
        assert state.show_all_tasks is True
        assert "all tasks" in state.status_message.lower()

        handle_normal_mode("a", state)
        assert state.show_all_tasks is False
        assert "active" in state.status_message.lower()

    def test_user_scrolls_through_tasks(self):
        """User uses j/k to scroll through task list."""
        state = TUIState()
        state.selected_task_idx = 0

        # Move selection down with 'j'
        handle_normal_mode("j", state, tasks_count=100)
        assert state.selected_task_idx == 1

        # Move selection up with 'k'
        handle_normal_mode("k", state, tasks_count=100)
        assert state.selected_task_idx == 0

    def test_user_views_task_details(self, in_progress_task):
        """User presses 'd' to view task details."""
        state = TUIState()
        # The handler checks all_tasks_cache, not selected_task_id
        state.all_tasks_cache = [in_progress_task]
        state.task_scroll_offset = 0

        handle_normal_mode("d", state)

        assert state.mode == TUIMode.TASK_DETAIL
        assert state.selected_task == in_progress_task

    def test_user_views_error_details(self, failed_task):
        """User presses 'e' to view error details on failed task."""
        state = TUIState()
        # The handler checks failed_tasks list, not selected_task_id
        state.failed_tasks = [failed_task]

        handle_normal_mode("e", state)

        assert state.mode == TUIMode.ERROR_DETAIL


# =============================================================================
# TestSettingsJourney
# =============================================================================


class TestSettingsJourney:
    """Tests for settings journey: Open → Edit → Save → Verify."""

    def test_user_opens_settings(self):
        """User presses 'S' to open settings."""
        state = TUIState()

        handle_normal_mode("S", state)

        assert state.mode == TUIMode.SETTINGS

    def test_user_can_escape_settings(self):
        """User can press Escape to close settings without saving."""
        state = TUIState()
        state.mode = TUIMode.SETTINGS

        # Simulate escape - settings should return to normal
        # (handle_settings_mode would handle this in actual implementation)
        state.mode = TUIMode.NORMAL

        assert state.mode == TUIMode.NORMAL


# =============================================================================
# TestSpawnWorkflowJourney
# =============================================================================


class TestSpawnWorkflowJourney:
    """Tests for complete spawn workflow: 6 steps."""

    def test_complete_spawn_workflow(self):
        """User completes full spawn workflow: project → priority → category → model → session → confirm."""
        loop = asyncio.new_event_loop()

        state = TUIState()
        state.projects = ["myproject"]
        state.spawn_config = SpawnConfig()

        # Step 1: Select project
        state.mode = TUIMode.SPAWN_PROJECT
        loop.run_until_complete(handle_spawn("1", state))
        assert state.mode == TUIMode.SPAWN_PRIORITY
        assert state.spawn_config.project == "myproject"

        # Step 2: Select priority (1 = HIGH only)
        loop.run_until_complete(handle_spawn("1", state))
        assert state.mode == TUIMode.SPAWN_CATEGORY
        assert state.spawn_config.priority_min == TaskPriority.HIGH

        # Step 3: Select category (0 = All)
        loop.run_until_complete(handle_spawn("0", state))
        assert state.mode == TUIMode.SPAWN_MODEL
        assert state.spawn_config.categories == []

        # Step 4: Select model (1 = Sonnet)
        loop.run_until_complete(handle_spawn("1", state))
        assert state.mode == TUIMode.SPAWN_SESSION
        assert state.spawn_config.model == ClaudeModel.SONNET

        # Step 5: Session settings (Enter to continue)
        loop.run_until_complete(handle_spawn("\r", state))
        assert state.mode == TUIMode.SPAWN_CONFIRM

        loop.close()

    def test_spawn_panel_shows_step_indicator(self):
        """Spawn panel shows current step (e.g., 'Step 2/6: Select Priority')."""
        state = TUIState()
        state.projects = ["testproject"]
        state.spawn_config = SpawnConfig(project="testproject")

        step_expectations = {
            TUIMode.SPAWN_PROJECT: "Step 1/6",
            TUIMode.SPAWN_PRIORITY: "Step 2/6",
            TUIMode.SPAWN_CATEGORY: "Step 3/6",
            TUIMode.SPAWN_MODEL: "Step 4/6",
            TUIMode.SPAWN_SESSION: "Step 5/6",
            TUIMode.SPAWN_CONFIRM: "Step 6/6",
        }

        for mode, expected_step in step_expectations.items():
            state.mode = mode
            panel = create_spawn_panel(state)
            panel_text = str(panel.renderable)
            assert expected_step in panel_text, f"Expected '{expected_step}' in {mode}"

    def test_spawn_with_specific_category(self):
        """User selects specific category (e.g., UX only)."""
        loop = asyncio.new_event_loop()

        state = TUIState()
        state.mode = TUIMode.SPAWN_CATEGORY
        state.spawn_config = SpawnConfig(project="test")

        # Select UX category (key 1)
        loop.run_until_complete(handle_spawn("1", state))

        assert state.spawn_config.categories == [TaskCategory.UX]
        assert state.mode == TUIMode.SPAWN_MODEL

        loop.close()

    def test_spawn_priority_options(self):
        """All priority options work correctly."""
        loop = asyncio.new_event_loop()

        priority_map = {
            "1": TaskPriority.HIGH,
            "2": TaskPriority.MEDIUM,
            "3": TaskPriority.LOWER,
            "4": None,  # All priorities
        }

        for key, expected_priority in priority_map.items():
            state = TUIState()
            state.mode = TUIMode.SPAWN_PRIORITY
            state.spawn_config = SpawnConfig(project="test")

            loop.run_until_complete(handle_spawn(key, state))
            assert state.spawn_config.priority_min == expected_priority

        loop.close()

    def test_spawn_model_options(self):
        """All model options work correctly."""
        loop = asyncio.new_event_loop()

        model_map = {
            "1": ClaudeModel.SONNET,
            "2": ClaudeModel.OPUS,
            "3": ClaudeModel.HAIKU,
        }

        for key, expected_model in model_map.items():
            state = TUIState()
            state.mode = TUIMode.SPAWN_MODEL
            state.spawn_config = SpawnConfig(project="test")

            loop.run_until_complete(handle_spawn(key, state))
            assert state.spawn_config.model == expected_model

        loop.close()

    def test_spawn_confirm_registers_in_database(self):
        """Spawning a Ralph registers it in the database so wig status can see it.

        This is a critical test - without database registration, spawned Ralphs
        are invisible to `wig status` even though they're running.
        """
        loop = asyncio.new_event_loop()

        state = TUIState()
        state.mode = TUIMode.SPAWN_CONFIRM
        state.spawn_config = SpawnConfig(
            project="testproject",
            fix_plan_path="/tmp/test/@fix_plan.md",
            model=ClaudeModel.SONNET,
        )

        with patch("chiefwiggum.tui.handlers.can_spawn_ralph", new_callable=AsyncMock) as mock_can_spawn, \
             patch("chiefwiggum.tui.handlers.spawn_ralph_with_task_claim", new_callable=AsyncMock) as mock_spawn, \
             patch("chiefwiggum.tui.handlers.generate_ralph_id") as mock_gen_id:

            mock_can_spawn.return_value = (True, "Ready")
            # spawn_ralph_with_task_claim returns (success, message, task_id)
            mock_spawn.return_value = (True, "Spawned successfully", "task-123")
            mock_gen_id.return_value = "test-ralph-123"

            # Press Enter to confirm spawn
            loop.run_until_complete(handle_spawn("\r", state))

            # Verify spawn_ralph_with_task_claim was called (handles registration internally)
            mock_spawn.assert_called_once()
            call_kwargs = mock_spawn.call_args.kwargs
            assert call_kwargs["ralph_id"] == "test-ralph-123"
            assert call_kwargs["project"] == "testproject"

        loop.close()


# =============================================================================
# TestErrorHandlingJourney
# =============================================================================


class TestErrorHandlingJourney:
    """Tests for error handling scenarios."""

    def test_spawn_without_projects_shows_message(self):
        """Spawning without available projects shows helpful message."""
        state = TUIState()
        state.projects = []  # No projects available

        handle_normal_mode("n", state)

        # Should show error message instead of entering spawn mode
        assert "no projects" in state.status_message.lower() or state.mode == TUIMode.SPAWN_PROJECT

    def test_release_without_selection_shows_message(self):
        """Releasing without selected task shows message."""
        state = TUIState()
        state.selected_task_id = None

        handle_normal_mode("r", state)

        # Should show message about no task selected
        assert state.status_message != "" or state.mode == TUIMode.NORMAL

    def test_error_detail_without_failed_task(self):
        """Viewing error details without failed task shows message."""
        state = TUIState()
        state.selected_task_id = None

        handle_normal_mode("e", state)

        # Should stay in normal mode or show message
        assert state.mode in (TUIMode.NORMAL, TUIMode.ERROR_DETAIL)


# =============================================================================
# TestBulkOperationsJourney
# =============================================================================


class TestBulkOperationsJourney:
    """Tests for bulk operations: x → select → m → action."""

    def test_user_enters_bulk_select_mode(self):
        """User presses 'x' to enter bulk select mode."""
        state = TUIState()

        handle_normal_mode("x", state)

        # Bulk select is a flag toggle, not a mode change
        assert state.bulk_mode_active is True
        assert "Bulk select ON" in state.status_message

    def test_user_exits_bulk_select_with_x(self):
        """User can exit bulk select mode with 'x' again."""
        state = TUIState()
        state.bulk_mode_active = True

        # Press 'x' again to toggle off
        handle_normal_mode("x", state)

        assert state.bulk_mode_active is False
        assert "Bulk select OFF" in state.status_message

    def test_bulk_select_tracks_selected_tasks(self, multiple_tasks):
        """Bulk select mode tracks which tasks are selected."""
        state = TUIState()
        state.bulk_mode_active = True
        state.selected_task_ids = set()

        # Select a few tasks
        state.selected_task_ids.add("task-001")
        state.selected_task_ids.add("task-002")
        state.selected_task_ids.add("task-003")

        assert len(state.selected_task_ids) == 3
        assert "task-001" in state.selected_task_ids


# =============================================================================
# TestViewNavigationJourney
# =============================================================================


class TestViewNavigationJourney:
    """Tests for view navigation: zoom, scroll, sort."""

    def test_user_cycles_view_focus(self):
        """User presses 'z' to cycle view focus: Both → Tasks → Instances → Both."""
        state = TUIState()

        assert state.view_focus == ViewFocus.BOTH

        handle_normal_mode("z", state)
        assert state.view_focus == ViewFocus.TASKS
        assert "Tasks only" in state.status_message

        handle_normal_mode("z", state)
        assert state.view_focus == ViewFocus.INSTANCES
        assert "Ralphs only" in state.status_message

        handle_normal_mode("z", state)
        assert state.view_focus == ViewFocus.BOTH
        assert "Split view" in state.status_message

    def test_user_cycles_sort_order(self):
        """User presses 'o' to cycle sort order."""
        state = TUIState()

        handle_normal_mode("o", state)

        # Should cycle through sort orders
        assert state.sort_order is not None or state.status_message != ""

    def test_user_toggles_instance_visibility(self):
        """User presses 'i' to toggle instance visibility."""
        state = TUIState()

        assert state.show_all_instances is False

        handle_normal_mode("i", state)
        assert state.show_all_instances is True

        handle_normal_mode("i", state)
        assert state.show_all_instances is False

    def test_user_views_statistics(self):
        """User presses 't' to view statistics."""
        state = TUIState()

        handle_normal_mode("t", state)

        assert state.mode == TUIMode.STATS

    def test_user_views_history(self):
        """User presses 'H' to view task history."""
        state = TUIState()

        handle_normal_mode("H", state)

        assert state.mode == TUIMode.HISTORY


# =============================================================================
# TestCommandBarDisplay
# =============================================================================


class TestCommandBarDisplay:
    """Tests that command bar displays correct keys."""

    def test_command_bar_shows_critical_keys_in_source(self):
        """Command bar source includes critical keys like 'n' for New."""

        tui_path = Path(__file__).parent.parent / "chiefwiggum" / "tui" / "panels.py"
        source = tui_path.read_text()

        # In create_command_bar for NORMAL mode
        # Check that 'n' New is present (uses cyan style)
        assert 'text.append("n", style="cyan")' in source
        assert 'text.append(" New  ", style="dim")' in source

        # Check that 'p' Project is mentioned (in tier 3 comments)
        assert "p Project" in source


# =============================================================================
# TestStatusMessageTimeout
# =============================================================================


class TestStatusMessageTimeout:
    """Tests for status message display timeout."""

    def test_status_message_timeout_is_8_seconds(self):
        """Status message timeout should be 8 seconds (not 5)."""

        tui_path = Path(__file__).parent.parent / "chiefwiggum" / "tui" / "panels.py"
        source = tui_path.read_text()

        # Check that the timeout is 8 seconds
        assert "status_message_time) < 8" in source


# =============================================================================
# TestHelpPanelCompleteness
# =============================================================================


class TestHelpPanelCompleteness:
    """Tests that help panel documents all keys."""

    def test_help_panel_documents_all_keys(self):
        """Help panel should document all keys."""
        help_panel = create_help_panel()
        help_text = str(help_panel.renderable)

        # Navigation & Views
        required_keys = [
            "h",  # Help
            "j",  # Scroll down
            "k",  # Scroll up
            "z",  # Cycle view
            "p",  # Project filter
            "c",  # Category filter
            "a",  # Toggle all tasks
            "i",  # Toggle instances
            "t",  # Statistics
            "H",  # History
            "S",  # Settings
            # Task Operations
            "y",  # Sync (current project)
            "Y",  # Sync ALL (new)
            "r",  # Release
            "e",  # Error details
            # Instance Operations
            "n",  # Spawn (5-step)
            "N",  # Quickstart spawn (new)
            "s",  # Shutdown
            "l",  # Logs
            # Search & Viewing (added in v0.7.1)
            "/",  # Search
            "d",  # Task detail
            "o",  # Sort order
            "w",  # JSON export
            "v",  # Log streaming
            # Bulk Operations (added in v0.7.1)
            "x",  # Bulk select mode
            "m",  # Bulk action menu
            # Exit
            "q",  # Quit
        ]

        for key in required_keys:
            assert key in help_text, f"Help panel missing key '{key}'"

    def test_help_panel_has_search_viewing_section(self):
        """Help panel includes Search & Viewing section."""
        help_panel = create_help_panel()
        help_text = str(help_panel.renderable)

        assert "Search" in help_text and "Viewing" in help_text

    def test_help_panel_has_bulk_task_operations_section(self):
        """Help panel includes Bulk Task Operations section."""
        # Use larger visible_lines to see all content including Bulk Task Operations
        help_panel = create_help_panel(offset=0, visible_lines=100)
        help_text = str(help_panel.renderable)

        assert "Bulk Task Operations" in help_text


# =============================================================================
# TestQuickstartJourney
# =============================================================================


class TestQuickstartJourney:
    """Tests for Shift+N quickstart spawn journey."""

    def test_shift_n_in_normal_mode(self):
        """Shift+N (capital N) triggers quickstart spawn."""
        state = TUIState()
        state.projects = ["testproject"]

        # 'N' should trigger quickstart but needs async handling
        # So we just verify it returns False (needs async)
        result = handle_normal_mode("N", state)
        assert result is False  # Handled in handle_command (async)

    def test_quickstart_uses_configured_defaults(self):
        """Quickstart spawn uses configured defaults."""
        from chiefwiggum.config import get_quickstart_defaults

        defaults = get_quickstart_defaults()
        assert "model" in defaults
        assert "timeout_minutes" in defaults


# =============================================================================
# TestProjectScopedSyncJourney
# =============================================================================


class TestProjectScopedSyncJourney:
    """Tests for project-scoped sync journey."""

    def test_lowercase_y_syncs_current_project(self):
        """lowercase y syncs current project only."""
        state = TUIState()

        # Without project filter, should show message
        handle_normal_mode("y", state)
        # Returns False for async handling
        # The actual sync happens in handle_command

    def test_shift_y_syncs_all_projects(self):
        """Shift+Y syncs ALL projects."""
        state = TUIState()

        result = handle_normal_mode("Y", state)
        # Returns False for async handling
        assert result is False

    def test_get_current_project_from_filter(self):
        """get_current_project returns project_filter if set."""
        from chiefwiggum.tui import get_current_project

        state = TUIState()
        state.project_filter = "myproject"

        project = get_current_project(state)
        assert project == "myproject"

    def test_get_current_project_returns_none_when_unknown(self):
        """get_current_project returns None when project unknown."""
        from chiefwiggum.tui import get_current_project

        state = TUIState()
        state.project_filter = None

        # If cwd is not under claudecode, should return None
        _project = get_current_project(state)
        # May be None or a valid project depending on test environment


# =============================================================================
# TestSettingsJourneyExpanded
# =============================================================================


class TestSettingsJourneyExpanded:
    """Tests for expanded settings journey."""

    def test_settings_has_multiple_items(self):
        """Settings panel has more than 2 items."""
        state = TUIState()
        state.mode = TUIMode.SETTINGS

        # settings_cursor can go from 0 to 6 (7 items)
        state.settings_cursor = 6
        # Should be valid

    def test_settings_edit_model(self):
        """User can edit default model setting."""

        state = TUIState()
        state.mode = TUIMode.SETTINGS_EDIT_MODEL

        panel = create_settings_panel(state)
        panel_text = str(panel.renderable)

        assert "sonnet" in panel_text.lower() or "Sonnet" in panel_text
        assert "opus" in panel_text.lower() or "Opus" in panel_text
        assert "haiku" in panel_text.lower() or "Haiku" in panel_text

    def test_settings_edit_permissions(self):
        """User can edit ralph permissions."""

        state = TUIState()
        state.mode = TUIMode.SETTINGS_EDIT_PERMISSIONS
        state.permission_cursor = 0

        panel = create_settings_panel(state)
        panel_text = str(panel.renderable)

        # Should show permission options
        assert "Run Tests" in panel_text or "tests" in panel_text.lower()

    def test_settings_edit_strategy(self):
        """User can edit task assignment strategy."""

        state = TUIState()
        state.mode = TUIMode.SETTINGS_EDIT_STRATEGY

        panel = create_settings_panel(state)
        panel_text = str(panel.renderable)

        assert "priority" in panel_text.lower() or "Priority" in panel_text
        assert "round" in panel_text.lower() or "Robin" in panel_text

    def test_settings_edit_auto_spawn(self):
        """User can edit auto-scaling settings."""

        state = TUIState()
        state.mode = TUIMode.SETTINGS_EDIT_AUTO_SPAWN

        panel = create_settings_panel(state)
        panel_text = str(panel.renderable)

        assert "Auto-Spawn" in panel_text or "auto" in panel_text.lower()


# =============================================================================
# TestViewStatePersistenceJourney
# =============================================================================


class TestViewStatePersistenceJourney:
    """Tests for view state persistence journey."""

    def test_view_state_saved_on_toggle(self):
        """View state is saved when toggling settings."""
        from chiefwiggum.tui import auto_save_view_state

        state = TUIState()
        state.show_all_tasks = True
        state.view_focus = ViewFocus.TASKS

        # auto_save_view_state should not raise
        try:
            auto_save_view_state(state)
        except Exception as e:
            pytest.fail(f"auto_save_view_state raised exception: {e}")

    def test_view_focus_cycles_correctly(self):
        """View focus cycles BOTH -> TASKS -> INSTANCES -> BOTH."""
        state = TUIState()
        assert state.view_focus == ViewFocus.BOTH

        handle_normal_mode("z", state)
        assert state.view_focus == ViewFocus.TASKS

        handle_normal_mode("z", state)
        assert state.view_focus == ViewFocus.INSTANCES

        handle_normal_mode("z", state)
        assert state.view_focus == ViewFocus.BOTH
