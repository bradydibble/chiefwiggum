"""Tests for ChiefWiggum TUI module.

Tests cover:
- discover_fix_plan_projects() helper function
- Mode transitions (all TUIMode values handled)
- Keyboard handling (z, c, y, spawn workflow keys)
- State management (TUIState defaults and lifecycle)
- Data filtering (category/project filters)
- Status messages (user feedback)
"""

import time
from pathlib import Path


from chiefwiggum.models import (
    ClaudeModel,
    TaskCategory,
    TaskPriority,
)
from chiefwiggum.tui import (
    SpawnConfig,
    TUIMode,
    TUIState,
    ViewFocus,
    discover_fix_plan_projects,
    handle_normal_mode,
    handle_spawn,
)


# =============================================================================
# TestDiscoverFixPlanProjects
# =============================================================================


class TestDiscoverFixPlanProjects:
    """Tests for the discover_fix_plan_projects() helper function."""

    def test_discovers_projects_in_claudecode_dir(self, tmp_path, monkeypatch):
        """Should find @fix_plan.md files in ~/claudecode/*/"""
        # Set up fake home directory with claudecode structure
        claudecode_dir = tmp_path / "claudecode"
        claudecode_dir.mkdir()

        # Create project directories with fix plans
        project1 = claudecode_dir / "project1"
        project1.mkdir()
        (project1 / "@fix_plan.md").write_text("# Fix Plan 1")

        project2 = claudecode_dir / "project2"
        project2.mkdir()
        (project2 / "@fix_plan.md").write_text("# Fix Plan 2")

        # Project without fix plan - should be ignored
        project3 = claudecode_dir / "project3"
        project3.mkdir()

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Ensure cwd doesn't interfere
        monkeypatch.setattr(Path, "cwd", lambda: tmp_path / "somewhere_else")

        projects = discover_fix_plan_projects()

        assert len(projects) == 2
        project_names = [p[0] for p in projects]
        assert "project1" in project_names
        assert "project2" in project_names
        assert "project3" not in project_names

    def test_includes_cwd_fix_plan(self, tmp_path, monkeypatch):
        """Should include @fix_plan.md in current directory."""
        # Set up empty claudecode dir
        claudecode_dir = tmp_path / "claudecode"
        claudecode_dir.mkdir()

        # Set up cwd with fix plan (not in claudecode)
        cwd = tmp_path / "my_project"
        cwd.mkdir()
        (cwd / "@fix_plan.md").write_text("# CWD Fix Plan")

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(Path, "cwd", lambda: cwd)

        projects = discover_fix_plan_projects()

        assert len(projects) == 1
        assert projects[0][0] == "my_project"
        assert projects[0][1] == cwd / "@fix_plan.md"

    def test_no_duplicates_when_cwd_is_in_claudecode(self, tmp_path, monkeypatch):
        """Should not duplicate if cwd is already in claudecode dir."""
        claudecode_dir = tmp_path / "claudecode"
        claudecode_dir.mkdir()

        project1 = claudecode_dir / "project1"
        project1.mkdir()
        (project1 / "@fix_plan.md").write_text("# Fix Plan 1")

        # cwd IS the project inside claudecode
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(Path, "cwd", lambda: project1)

        projects = discover_fix_plan_projects()

        # Should only appear once
        project_names = [p[0] for p in projects]
        assert project_names.count("project1") == 1
        assert len(projects) == 1

    def test_empty_when_no_fix_plans_exist(self, tmp_path, monkeypatch):
        """Should return empty list when no @fix_plan.md files found."""
        claudecode_dir = tmp_path / "claudecode"
        claudecode_dir.mkdir()

        # Empty project directory
        project1 = claudecode_dir / "project1"
        project1.mkdir()

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(Path, "cwd", lambda: tmp_path)  # No fix plan in cwd either

        projects = discover_fix_plan_projects()

        assert projects == []

    def test_ignores_non_directory_entries(self, tmp_path, monkeypatch):
        """Should skip files in claudecode dir, only check directories."""
        claudecode_dir = tmp_path / "claudecode"
        claudecode_dir.mkdir()

        # Create a file (not directory) in claudecode
        (claudecode_dir / "some_file.txt").write_text("Not a directory")

        # Create a valid project
        project1 = claudecode_dir / "project1"
        project1.mkdir()
        (project1 / "@fix_plan.md").write_text("# Fix Plan")

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

        projects = discover_fix_plan_projects()

        assert len(projects) == 1
        assert projects[0][0] == "project1"

    def test_claudecode_dir_does_not_exist(self, tmp_path, monkeypatch):
        """Should handle missing claudecode directory gracefully."""
        # No claudecode dir at all
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

        projects = discover_fix_plan_projects()

        assert projects == []


# =============================================================================
# TestModeTransitions
# =============================================================================


class TestModeTransitions:
    """Tests that all TUIMode values are handled properly."""

    def test_all_spawn_modes_in_update_dashboard_tuple(self):
        """SPAWN_CATEGORY must be in the mode tuple - catches the bug we fixed."""
        # Read the source file and parse to find the tuple
        tui_path = Path(__file__).parent.parent / "chiefwiggum" / "tui" / "dashboard.py"
        source = tui_path.read_text()

        # The critical line is the elif that handles spawn modes in update_dashboard
        # It should include SPAWN_CATEGORY

        # Find the specific pattern for update_dashboard spawn modes check
        # This appears around line 1220: elif state.mode in (TUIMode.SPAWN_PROJECT, ...)
        assert "TUIMode.SPAWN_CATEGORY" in source, "SPAWN_CATEGORY missing from source"

        # Check that all spawn modes appear together in at least one tuple
        # Look for the pattern "state.mode in (TUIMode.SPAWN_"
        import re

        pattern = r"state\.mode\s+in\s+\(TUIMode\.SPAWN_PROJECT.*?TUIMode\.SPAWN_CONFIRM\)"
        matches = re.findall(pattern, source, re.DOTALL)

        # At least one match should contain SPAWN_CATEGORY
        found_complete_tuple = False
        for match in matches:
            if "SPAWN_CATEGORY" in match:
                found_complete_tuple = True
                break

        assert found_complete_tuple, "SPAWN_CATEGORY not in any spawn modes tuple"

    def test_all_spawn_modes_in_command_bar_tuple(self):
        """All spawn modes must be in command bar handler."""
        tui_path = Path(__file__).parent.parent / "chiefwiggum" / "tui" / "panels.py"
        source = tui_path.read_text()

        # In create_command_bar, there's a check for spawn modes
        # Should include SPAWN_CATEGORY in the tuple
        # Find the elif in create_command_bar for spawn modes
        import re

        # Look for the pattern in create_command_bar function
        # The check is: elif state.mode in (TUIMode.SPAWN_PROJECT, ..., TUIMode.SPAWN_CONFIRM):
        pattern = r"elif state\.mode in \(TUIMode\.SPAWN_PROJECT.*?TUIMode\.SPAWN_CONFIRM\)"
        matches = re.findall(pattern, source)

        assert len(matches) >= 1, "No spawn mode tuple found in command bar"

        for match in matches:
            assert "SPAWN_CATEGORY" in match, f"SPAWN_CATEGORY missing from tuple: {match}"

    def test_all_spawn_modes_in_handle_command_tuple(self):
        """All spawn modes must be in handle_command."""
        tui_path = Path(__file__).parent.parent / "chiefwiggum" / "tui" / "handlers.py"
        source = tui_path.read_text()

        # In handle_command, spawn modes are handled together
        import re

        pattern = r"elif state\.mode in \(TUIMode\.SPAWN_PROJECT.*?TUIMode\.SPAWN_CONFIRM\)"
        matches = re.findall(pattern, source)

        # Should have at least one match with SPAWN_CATEGORY
        found = False
        for match in matches:
            if "SPAWN_CATEGORY" in match:
                found = True
                break

        assert found, "SPAWN_CATEGORY not in handle_command spawn modes tuple"

    def test_spawn_mode_progression(self):
        """PROJECT -> PRIORITY -> CATEGORY -> MODEL -> SESSION -> CONFIRM."""
        state = TUIState()
        state.mode = TUIMode.SPAWN_PROJECT
        state.projects = ["testproject"]
        state.spawn_config = SpawnConfig()

        # Test that pressing keys progresses through spawn modes correctly
        # This tests the handle_spawn function's mode transitions

        # Start at SPAWN_PROJECT, select project 1
        import asyncio

        loop = asyncio.new_event_loop()

        # Project selection
        state.mode = TUIMode.SPAWN_PROJECT
        loop.run_until_complete(handle_spawn("1", state))
        assert state.mode == TUIMode.SPAWN_PRIORITY

        # Priority selection (1 = HIGH)
        loop.run_until_complete(handle_spawn("1", state))
        assert state.mode == TUIMode.SPAWN_CATEGORY

        # Category selection (0 = All)
        loop.run_until_complete(handle_spawn("0", state))
        assert state.mode == TUIMode.SPAWN_MODEL

        # Model selection (1 = Sonnet)
        loop.run_until_complete(handle_spawn("1", state))
        assert state.mode == TUIMode.SPAWN_SESSION

        # Session settings (Enter to continue)
        loop.run_until_complete(handle_spawn("\r", state))
        assert state.mode == TUIMode.SPAWN_CONFIRM

        loop.close()

    def test_escape_returns_to_normal_from_all_modes(self):
        """ESC should work from any non-NORMAL mode."""
        import asyncio

        loop = asyncio.new_event_loop()

        escape_modes = [
            TUIMode.SPAWN_PROJECT,
            TUIMode.SPAWN_PRIORITY,
            TUIMode.SPAWN_CATEGORY,
            TUIMode.SPAWN_MODEL,
            TUIMode.SPAWN_SESSION,
            TUIMode.SPAWN_CONFIRM,
        ]

        for mode in escape_modes:
            state = TUIState()
            state.mode = mode
            state.projects = ["test"]
            state.spawn_config = SpawnConfig()

            loop.run_until_complete(handle_spawn("ESCAPE", state))
            assert state.mode == TUIMode.NORMAL, f"ESC from {mode} didn't return to NORMAL"

        loop.close()


# =============================================================================
# TestKeyboardHandling
# =============================================================================


class TestKeyboardHandling:
    """Tests for key handlers."""

    def test_z_cycles_view_focus(self):
        """z key: BOTH -> TASKS -> INSTANCES -> BOTH."""
        state = TUIState()
        assert state.view_focus == ViewFocus.BOTH

        # First z: BOTH -> TASKS
        handle_normal_mode("z", state)
        assert state.view_focus == ViewFocus.TASKS
        assert "Tasks only" in state.status_message

        # Second z: TASKS -> INSTANCES
        handle_normal_mode("z", state)
        assert state.view_focus == ViewFocus.INSTANCES
        assert "Ralphs only" in state.status_message

        # Third z: INSTANCES -> BOTH
        handle_normal_mode("z", state)
        assert state.view_focus == ViewFocus.BOTH
        assert "Split view" in state.status_message

    def test_c_cycles_category_filter(self):
        """c key: None -> UX -> API -> TESTING -> DATABASE -> INFRA -> None."""
        state = TUIState()
        assert state.category_filter is None

        expected_cycle = [
            TaskCategory.UX,
            TaskCategory.API,
            TaskCategory.TESTING,
            TaskCategory.DATABASE,
            TaskCategory.INFRA,
            None,  # Back to all
        ]

        for expected in expected_cycle:
            handle_normal_mode("c", state)
            assert state.category_filter == expected
            if expected:
                assert expected.value in state.status_message
            else:
                assert "All" in state.status_message

    def test_y_triggers_sync_with_status_message(self):
        """y key should show syncing status message."""
        state = TUIState()

        # y in handle_normal_mode sets the status message
        # (actual sync happens in handle_command which is async)
        handle_normal_mode("y", state)

        assert state.status_message == "Syncing tasks..."
        assert state.status_message_time > 0

    def test_spawn_project_selection(self):
        """1-9 keys select project in SPAWN_PROJECT mode."""
        import asyncio

        loop = asyncio.new_event_loop()

        state = TUIState()
        state.mode = TUIMode.SPAWN_PROJECT
        state.projects = ["alpha", "beta", "gamma"]
        state.spawn_config = SpawnConfig()

        # Key 1 selects first project
        loop.run_until_complete(handle_spawn("1", state))
        assert state.spawn_config.project == "alpha"
        assert state.mode == TUIMode.SPAWN_PRIORITY

        # Reset and test key 2
        state.mode = TUIMode.SPAWN_PROJECT
        state.spawn_config = SpawnConfig()
        loop.run_until_complete(handle_spawn("2", state))
        assert state.spawn_config.project == "beta"

        # Reset and test key 3
        state.mode = TUIMode.SPAWN_PROJECT
        state.spawn_config = SpawnConfig()
        loop.run_until_complete(handle_spawn("3", state))
        assert state.spawn_config.project == "gamma"

        loop.close()

    def test_spawn_priority_selection(self):
        """1-4 keys select priority in SPAWN_PRIORITY mode."""
        import asyncio

        loop = asyncio.new_event_loop()

        # Test key 1 = HIGH
        state = TUIState()
        state.mode = TUIMode.SPAWN_PRIORITY
        state.spawn_config = SpawnConfig(project="test")
        loop.run_until_complete(handle_spawn("1", state))
        assert state.spawn_config.priority_min == TaskPriority.HIGH
        assert state.mode == TUIMode.SPAWN_CATEGORY

        # Test key 2 = MEDIUM
        state = TUIState()
        state.mode = TUIMode.SPAWN_PRIORITY
        state.spawn_config = SpawnConfig(project="test")
        loop.run_until_complete(handle_spawn("2", state))
        assert state.spawn_config.priority_min == TaskPriority.MEDIUM

        # Test key 3 = LOWER
        state = TUIState()
        state.mode = TUIMode.SPAWN_PRIORITY
        state.spawn_config = SpawnConfig(project="test")
        loop.run_until_complete(handle_spawn("3", state))
        assert state.spawn_config.priority_min == TaskPriority.LOWER

        # Test key 4 = All (None)
        state = TUIState()
        state.mode = TUIMode.SPAWN_PRIORITY
        state.spawn_config = SpawnConfig(project="test")
        loop.run_until_complete(handle_spawn("4", state))
        assert state.spawn_config.priority_min is None

        loop.close()

    def test_spawn_category_selection(self):
        """0-5 keys select category in SPAWN_CATEGORY mode."""
        import asyncio

        loop = asyncio.new_event_loop()

        category_keys = {
            "0": [],  # All categories
            "1": [TaskCategory.UX],
            "2": [TaskCategory.API],
            "3": [TaskCategory.TESTING],
            "4": [TaskCategory.DATABASE],
            "5": [TaskCategory.INFRA],
        }

        for key, expected_categories in category_keys.items():
            state = TUIState()
            state.mode = TUIMode.SPAWN_CATEGORY
            state.spawn_config = SpawnConfig(project="test")

            loop.run_until_complete(handle_spawn(key, state))
            assert state.spawn_config.categories == expected_categories
            assert state.mode == TUIMode.SPAWN_MODEL

        loop.close()

    def test_spawn_model_selection(self):
        """1-3 keys select model in SPAWN_MODEL mode and advance to SPAWN_SESSION."""
        import asyncio

        loop = asyncio.new_event_loop()

        model_keys = {
            "1": ClaudeModel.SONNET,
            "2": ClaudeModel.OPUS,
            "3": ClaudeModel.HAIKU,
        }

        for key, expected_model in model_keys.items():
            state = TUIState()
            state.mode = TUIMode.SPAWN_MODEL
            state.spawn_config = SpawnConfig(project="test")

            loop.run_until_complete(handle_spawn(key, state))
            assert state.spawn_config.model == expected_model
            assert state.mode == TUIMode.SPAWN_SESSION  # Now goes to SESSION, not CONFIRM

        loop.close()

    def test_q_returns_quit_signal(self):
        """q key should return True to signal quit."""
        state = TUIState()

        result = handle_normal_mode("q", state)

        assert result is True

    def test_h_opens_help(self):
        """h key should open help mode."""
        state = TUIState()

        handle_normal_mode("h", state)

        assert state.mode == TUIMode.HELP

    def test_question_mark_opens_help(self):
        """? key should also open help mode."""
        state = TUIState()

        handle_normal_mode("?", state)

        assert state.mode == TUIMode.HELP

    def test_j_scrolls_down(self):
        """j key should scroll tasks down."""
        state = TUIState()
        state.selected_task_idx = 0

        # With enough tasks, j should move selection down
        handle_normal_mode("j", state, tasks_count=100)

        assert state.selected_task_idx == 1  # Moves selection by 1

    def test_k_moves_selection_up(self):
        """k key should move task selection up."""
        state = TUIState()
        state.selected_task_idx = 10

        handle_normal_mode("k", state, tasks_count=100)

        assert state.selected_task_idx == 9  # Moves selection up by 1

    def test_k_does_not_go_negative(self):
        """k key should not move selection below 0."""
        state = TUIState()
        state.selected_task_idx = 0

        handle_normal_mode("k", state, tasks_count=100)

        assert state.selected_task_idx == 0  # Stays at 0

    def test_a_toggles_all_tasks(self):
        """a key should toggle show_all_tasks."""
        state = TUIState()
        assert state.show_all_tasks is False

        handle_normal_mode("a", state)
        assert state.show_all_tasks is True
        assert "all tasks" in state.status_message.lower()

        handle_normal_mode("a", state)
        assert state.show_all_tasks is False
        assert "active" in state.status_message.lower()

    def test_i_toggles_all_instances(self):
        """i key should toggle show_all_instances."""
        state = TUIState()
        assert state.show_all_instances is False

        handle_normal_mode("i", state)
        assert state.show_all_instances is True
        assert "all instances" in state.status_message.lower()

        handle_normal_mode("i", state)
        assert state.show_all_instances is False
        assert "active" in state.status_message.lower()


# =============================================================================
# TestStateManagement
# =============================================================================


class TestStateManagement:
    """Tests for TUIState."""

    def test_initial_state_defaults(self):
        """TUIState should have correct defaults."""
        state = TUIState()

        assert state.mode == TUIMode.NORMAL
        assert state.project_filter is None
        assert state.show_all_tasks is False
        assert state.show_all_instances is False
        assert state.status_message == ""
        assert state.status_message_time == 0
        assert state.projects == []
        assert state.instances == []
        assert state.task_scroll_offset == 0
        assert state.instance_scroll_offset == 0
        assert state.tasks_per_page == 20

    def test_view_focus_default_is_both(self):
        """Default view_focus should be BOTH."""
        state = TUIState()

        assert state.view_focus == ViewFocus.BOTH

    def test_category_filter_default_is_none(self):
        """Default category_filter should be None (all categories)."""
        state = TUIState()

        assert state.category_filter is None

    def test_scroll_offset_resets_on_filter_change(self):
        """Scroll offset should reset when category changes."""
        state = TUIState()
        state.task_scroll_offset = 50

        # Changing category via 'c' key should reset scroll
        handle_normal_mode("c", state)

        assert state.task_scroll_offset == 0

    def test_scroll_offset_resets_on_toggle_all_tasks(self):
        """Scroll offset should reset when toggling all tasks."""
        state = TUIState()
        state.task_scroll_offset = 50

        handle_normal_mode("a", state)

        assert state.task_scroll_offset == 0

    def test_spawn_config_initialized_on_spawn(self):
        """SpawnConfig should be fresh when entering spawn mode."""
        state = TUIState()
        state.projects = ["test"]

        # Trigger spawn with 'n' key
        handle_normal_mode("n", state)

        assert state.mode == TUIMode.SPAWN_PROJECT
        assert state.spawn_config.project == ""
        assert state.spawn_config.fix_plan_path == ""
        assert state.spawn_config.priority_min is None
        assert state.spawn_config.categories == []
        assert state.spawn_config.model == ClaudeModel.SONNET

    def test_bulk_mode_state(self):
        """Bulk mode state should be tracked correctly."""
        state = TUIState()

        assert state.bulk_mode_active is False
        assert state.selected_task_ids == set()

        # Toggle bulk mode on
        handle_normal_mode("x", state)
        assert state.bulk_mode_active is True

        # Toggle bulk mode off clears selection
        state.selected_task_ids = {"task-1", "task-2"}
        handle_normal_mode("x", state)
        assert state.bulk_mode_active is False
        assert state.selected_task_ids == set()


# =============================================================================
# TestDataFiltering
# =============================================================================


class TestDataFiltering:
    """Tests for filter logic."""

    def test_category_filter_cycles_through_all_categories(self):
        """Category filter should cycle through all TaskCategory values plus None."""
        state = TUIState()

        # Get all categories from the cycle
        seen_categories = []
        for _ in range(7):  # None + 5 categories + back to None
            handle_normal_mode("c", state)
            seen_categories.append(state.category_filter)

        # Should have: UX, API, TESTING, DATABASE, INFRA, None
        assert TaskCategory.UX in seen_categories
        assert TaskCategory.API in seen_categories
        assert TaskCategory.TESTING in seen_categories
        assert TaskCategory.DATABASE in seen_categories
        assert TaskCategory.INFRA in seen_categories
        assert None in seen_categories

    def test_project_filter_mode_requires_projects(self):
        """Project filter mode should not activate without projects."""
        state = TUIState()
        state.projects = []

        handle_normal_mode("p", state)

        assert state.mode == TUIMode.NORMAL
        assert "No projects" in state.status_message

    def test_project_filter_mode_activates_with_projects(self):
        """Project filter mode should activate when projects exist."""
        state = TUIState()
        state.projects = ["project1", "project2"]

        handle_normal_mode("p", state)

        assert state.mode == TUIMode.PROJECT_FILTER


# =============================================================================
# TestStatusMessages
# =============================================================================


class TestStatusMessages:
    """Tests for user feedback messages."""

    def test_zoom_status_message_content(self):
        """Zoom should show appropriate status messages."""
        state = TUIState()

        # BOTH -> TASKS
        handle_normal_mode("z", state)
        assert "Tasks only" in state.status_message
        assert "z to cycle" in state.status_message

        # TASKS -> INSTANCES
        handle_normal_mode("z", state)
        assert "Ralphs only" in state.status_message

        # INSTANCES -> BOTH
        handle_normal_mode("z", state)
        assert "Split view" in state.status_message

    def test_category_status_message_content(self):
        """Category should show appropriate status messages."""
        state = TUIState()

        # To UX
        handle_normal_mode("c", state)
        assert "ux" in state.status_message.lower()
        assert "c to cycle" in state.status_message

        # Cycle through to None
        for _ in range(5):
            handle_normal_mode("c", state)

        assert "All" in state.status_message

    def test_status_message_timestamp(self):
        """Status messages should have timestamps set."""
        state = TUIState()
        before = time.time()

        handle_normal_mode("z", state)

        after = time.time()
        assert state.status_message_time >= before
        assert state.status_message_time <= after

    def test_no_projects_spawn_message(self):
        """Spawn should show error when no projects available."""
        state = TUIState()
        state.projects = []

        handle_normal_mode("n", state)

        assert state.mode == TUIMode.NORMAL
        assert "No projects" in state.status_message
        assert "sync tasks" in state.status_message.lower()

    def test_no_instances_shutdown_message(self):
        """Shutdown should show error when no instances."""
        state = TUIState()
        state.instances = []

        handle_normal_mode("s", state)

        assert state.mode == TUIMode.NORMAL
        assert "No active instances" in state.status_message

    def test_no_failed_tasks_error_detail_message(self):
        """Error detail should show message when no failed tasks."""
        state = TUIState()
        state.failed_tasks = []

        handle_normal_mode("e", state)

        assert state.mode == TUIMode.NORMAL
        assert "No failed tasks" in state.status_message

    def test_no_in_progress_tasks_release_message(self):
        """Release should show message when no in-progress tasks."""
        state = TUIState()
        state.in_progress_tasks = []

        handle_normal_mode("r", state)

        assert state.mode == TUIMode.NORMAL
        assert "No in-progress tasks" in state.status_message


# =============================================================================
# TestSpawnConfig
# =============================================================================


class TestSpawnConfig:
    """Tests for SpawnConfig dataclass."""

    def test_spawn_config_defaults(self):
        """SpawnConfig should have correct defaults."""
        config = SpawnConfig()

        assert config.project == ""
        assert config.fix_plan_path == ""
        assert config.priority_min is None
        assert config.categories == []
        assert config.model == ClaudeModel.SONNET
        assert config.no_continue is True  # Default changed to stop after one task
        assert config.max_loops is None

    def test_spawn_config_can_be_modified(self):
        """SpawnConfig fields should be modifiable."""
        config = SpawnConfig()

        config.project = "myproject"
        config.priority_min = TaskPriority.HIGH
        config.categories = [TaskCategory.UX, TaskCategory.API]
        config.model = ClaudeModel.OPUS

        assert config.project == "myproject"
        assert config.priority_min == TaskPriority.HIGH
        assert config.categories == [TaskCategory.UX, TaskCategory.API]
        assert config.model == ClaudeModel.OPUS


# =============================================================================
# TestViewFocus
# =============================================================================


class TestViewFocus:
    """Tests for ViewFocus enum."""

    def test_view_focus_values(self):
        """ViewFocus should have BOTH, TASKS, INSTANCES values."""
        assert ViewFocus.BOTH is not None
        assert ViewFocus.TASKS is not None
        assert ViewFocus.INSTANCES is not None

    def test_view_focus_distinct(self):
        """ViewFocus values should be distinct."""
        assert ViewFocus.BOTH != ViewFocus.TASKS
        assert ViewFocus.BOTH != ViewFocus.INSTANCES
        assert ViewFocus.TASKS != ViewFocus.INSTANCES


# =============================================================================
# TestTUIMode
# =============================================================================


class TestTUIMode:
    """Tests for TUIMode enum completeness."""

    def test_all_modes_exist(self):
        """All expected TUI modes should exist."""
        expected_modes = [
            "NORMAL",
            "HELP",
            "PROJECT_FILTER",
            "SHUTDOWN",
            "RELEASE",
            "SETTINGS",
            "SPAWN_PROJECT",
            "SPAWN_PRIORITY",
            "SPAWN_CATEGORY",
            "SPAWN_MODEL",
            "SPAWN_CONFIRM",
            "ERROR_DETAIL",
            "STATS",
            "CONFIRM_BULK_STOP",
            "CONFIRM_BULK_PAUSE",
            "LOG_VIEW",
            "HISTORY",
            "SEARCH",
            "TASK_DETAIL",
            "BULK_SELECT",
            "BULK_ACTION",
            "LOG_STREAM",
        ]

        actual_modes = [m.name for m in TUIMode]

        for expected in expected_modes:
            assert expected in actual_modes, f"Missing mode: {expected}"

    def test_spawn_modes_sequential(self):
        """Spawn modes should be defined (for tuple membership checking)."""
        spawn_modes = [
            TUIMode.SPAWN_PROJECT,
            TUIMode.SPAWN_PRIORITY,
            TUIMode.SPAWN_CATEGORY,
            TUIMode.SPAWN_MODEL,
            TUIMode.SPAWN_CONFIRM,
        ]

        # All should be valid TUIMode values
        for mode in spawn_modes:
            assert isinstance(mode, TUIMode)


# =============================================================================
# TestOverlayToggleBehavior
# =============================================================================


class TestOverlayToggleBehavior:
    """Tests for overlay toggle key behavior (Phase 1 consistency)."""

    def test_help_closes_with_h_key(self):
        """h should close help mode (toggle behavior)."""
        import asyncio
        from chiefwiggum.tui import handle_command

        loop = asyncio.new_event_loop()
        state = TUIState()
        state.mode = TUIMode.HELP

        loop.run_until_complete(handle_command("h", state))

        assert state.mode == TUIMode.NORMAL
        loop.close()

    def test_help_closes_with_question_mark(self):
        """? should close help mode (toggle behavior)."""
        import asyncio
        from chiefwiggum.tui import handle_command

        loop = asyncio.new_event_loop()
        state = TUIState()
        state.mode = TUIMode.HELP

        loop.run_until_complete(handle_command("?", state))

        assert state.mode == TUIMode.NORMAL
        loop.close()

    def test_stats_closes_with_t_key(self):
        """t should close stats mode (toggle behavior)."""
        import asyncio
        from chiefwiggum.tui import handle_command

        loop = asyncio.new_event_loop()
        state = TUIState()
        state.mode = TUIMode.STATS

        loop.run_until_complete(handle_command("t", state))

        assert state.mode == TUIMode.NORMAL
        loop.close()

    def test_error_detail_closes_with_e_key(self):
        """e should close error detail mode (toggle behavior)."""
        import asyncio
        from chiefwiggum.tui import handle_command

        loop = asyncio.new_event_loop()
        state = TUIState()
        state.mode = TUIMode.ERROR_DETAIL

        loop.run_until_complete(handle_command("e", state))

        assert state.mode == TUIMode.NORMAL
        loop.close()

    def test_log_view_closes_with_l_key(self):
        """l should close log view mode (toggle behavior)."""
        import asyncio
        from chiefwiggum.tui import handle_command

        loop = asyncio.new_event_loop()
        state = TUIState()
        state.mode = TUIMode.LOG_VIEW

        loop.run_until_complete(handle_command("l", state))

        assert state.mode == TUIMode.NORMAL
        loop.close()

    def test_history_closes_with_H_key(self):
        """H should close history mode (toggle behavior)."""
        import asyncio
        from chiefwiggum.tui import handle_command

        loop = asyncio.new_event_loop()
        state = TUIState()
        state.mode = TUIMode.HISTORY

        loop.run_until_complete(handle_command("H", state))

        assert state.mode == TUIMode.NORMAL
        loop.close()

    def test_task_detail_closes_with_d_key(self):
        """d should close task detail mode (toggle behavior)."""
        import asyncio
        from chiefwiggum.tui import handle_command

        loop = asyncio.new_event_loop()
        state = TUIState()
        state.mode = TUIMode.TASK_DETAIL

        loop.run_until_complete(handle_command("d", state))

        assert state.mode == TUIMode.NORMAL
        loop.close()

    def test_log_stream_closes_with_v_key(self):
        """v should close log stream mode (toggle behavior)."""
        import asyncio
        from chiefwiggum.tui import handle_command

        loop = asyncio.new_event_loop()
        state = TUIState()
        state.mode = TUIMode.LOG_STREAM

        loop.run_until_complete(handle_command("v", state))

        assert state.mode == TUIMode.NORMAL
        loop.close()

    def test_overlays_close_with_q_key(self):
        """q should close all overlay modes."""
        import asyncio
        from chiefwiggum.tui import handle_command

        loop = asyncio.new_event_loop()

        overlay_modes = [
            TUIMode.STATS,
            TUIMode.ERROR_DETAIL,
            TUIMode.LOG_VIEW,
            TUIMode.LOG_STREAM,
            TUIMode.HISTORY,
            TUIMode.TASK_DETAIL,
        ]

        for mode in overlay_modes:
            state = TUIState()
            state.mode = mode
            loop.run_until_complete(handle_command("q", state))
            assert state.mode == TUIMode.NORMAL, f"q didn't close {mode}"

        loop.close()

    def test_overlays_close_with_escape(self):
        """Escape should close all overlay modes."""
        import asyncio
        from chiefwiggum.tui import handle_command

        loop = asyncio.new_event_loop()

        overlay_modes = [
            TUIMode.STATS,
            TUIMode.ERROR_DETAIL,
            TUIMode.LOG_VIEW,
            TUIMode.LOG_STREAM,
            TUIMode.HISTORY,
            TUIMode.TASK_DETAIL,
        ]

        for mode in overlay_modes:
            state = TUIState()
            state.mode = mode
            loop.run_until_complete(handle_command("ESCAPE", state))
            assert state.mode == TUIMode.NORMAL, f"Escape didn't close {mode}"

        loop.close()

    def test_overlays_ignore_other_keys(self):
        """Other keys should be ignored in overlay modes (not close them)."""
        import asyncio
        from chiefwiggum.tui import handle_command

        loop = asyncio.new_event_loop()

        # Test that random keys don't close overlays
        test_cases = [
            (TUIMode.STATS, "x"),  # x shouldn't close stats
            (TUIMode.ERROR_DETAIL, "z"),  # z shouldn't close error detail
            (TUIMode.LOG_VIEW, "n"),  # n shouldn't close log view
            (TUIMode.HISTORY, "a"),  # a shouldn't close history
            (TUIMode.TASK_DETAIL, "b"),  # b shouldn't close task detail
        ]

        for mode, key in test_cases:
            state = TUIState()
            state.mode = mode
            loop.run_until_complete(handle_command(key, state))
            assert state.mode == mode, f"Key '{key}' should not close {mode}"

        loop.close()
