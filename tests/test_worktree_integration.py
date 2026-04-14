"""Integration tests for git worktree workflow.

Tests end-to-end worktree lifecycle: creation, work, merge, and cleanup.
Uses real git repositories and commands (not mocks) for accurate validation.
"""

import asyncio
import subprocess
import tempfile
from pathlib import Path

import pytest

from chiefwiggum.git_merge import attempt_merge
from chiefwiggum.worktree_manager import (
    cleanup_stale_worktrees,
    cleanup_worktree,
    create_worktree,
    get_worktree_branch_name,
    list_active_worktrees,
)


@pytest.fixture
def temp_git_repo():
    """Create a temporary git repository for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo_path, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_path, check=True, capture_output=True
        )
        # Disable GPG signing for tests
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=repo_path, check=True, capture_output=True
        )

        # Create initial commit on main
        (repo_path / "README.md").write_text("# Test Repo\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Initial commit"],
            cwd=repo_path, check=True, capture_output=True
        )

        # Rename branch to main if needed
        subprocess.run(
            ["git", "branch", "-M", "main"],
            cwd=repo_path, check=True, capture_output=True
        )

        yield repo_path


# ============================================================================
# Integration Tests: End-to-End Worktree Workflows
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_task_claim_worktree_merge_cleanup(temp_git_repo):
    """Test 1: End-to-end workflow: Task claim → worktree created → work → merge → cleanup.

    This validates the complete happy path for a Ralph working on a task.
    """
    ralph_id = "ralph-1"
    task_id = "task-1-fix-bug"

    # Step 1: Create worktree for task
    success, msg, worktree_path = await create_worktree(
        project_path=temp_git_repo,
        ralph_id=ralph_id,
        task_id=task_id,
        base_branch="main"
    )
    assert success is True
    assert worktree_path is not None
    assert worktree_path.exists()
    assert worktree_path == temp_git_repo / ".worktrees" / ralph_id

    # Step 2: Verify branch was created
    branch_name = get_worktree_branch_name(ralph_id, task_id)
    result = subprocess.run(
        ["git", "branch", "--list", branch_name],
        cwd=temp_git_repo,
        capture_output=True,
        text=True
    )
    assert branch_name in result.stdout

    # Step 3: Do work in the worktree
    work_file = worktree_path / "feature.txt"
    work_file.write_text("This is a new feature")
    subprocess.run(
        ["git", "add", "feature.txt"],
        cwd=worktree_path,
        check=True,
        capture_output=True
    )
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Add feature"],
        cwd=worktree_path,
        check=True,
        capture_output=True
    )

    # Step 4: Merge back to main
    merge_result = await attempt_merge(
        worktree_branch=branch_name,
        target_branch="main",
        strategy="auto",
        repo_path=temp_git_repo
    )
    assert merge_result.success is True
    assert merge_result.has_conflicts is False
    assert merge_result.merge_sha is not None

    # Step 5: Verify merge in main branch
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    assert (temp_git_repo / "feature.txt").exists()
    assert (temp_git_repo / "feature.txt").read_text() == "This is a new feature"

    # Step 6: Cleanup worktree
    cleanup_success, cleanup_msg = await cleanup_worktree(worktree_path, force=True)
    assert cleanup_success is True
    assert not worktree_path.exists()

    # Step 7: Verify branch was deleted
    result = subprocess.run(
        ["git", "branch", "--list", branch_name],
        cwd=temp_git_repo,
        capture_output=True,
        text=True
    )
    assert branch_name not in result.stdout


@pytest.mark.asyncio
async def test_e2e_task_completion_auto_merge_success(temp_git_repo):
    """Test 2: End-to-end: Task completion → auto-merge attempt → success.

    Simulates Ralph completing a task successfully with automatic merge.
    """
    ralph_id = "ralph-2"
    task_id = "task-2-add-tests"

    # Create worktree and do work
    success, msg, worktree_path = await create_worktree(
        project_path=temp_git_repo,
        ralph_id=ralph_id,
        task_id=task_id,
        base_branch="main"
    )
    assert success is True

    # Add multiple commits (simulate real work)
    for i in range(3):
        test_file = worktree_path / f"test_{i}.py"
        test_file.write_text(f"def test_{i}():\n    assert True\n")
        subprocess.run(
            ["git", "add", f"test_{i}.py"],
            cwd=worktree_path,
            check=True,
            capture_output=True
        )
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", f"Add test {i}"],
            cwd=worktree_path,
            check=True,
            capture_output=True
        )

    # Auto-merge with squash strategy (clean history)
    branch_name = get_worktree_branch_name(ralph_id, task_id)
    merge_result = await attempt_merge(
        worktree_branch=branch_name,
        target_branch="main",
        strategy="squash",
        repo_path=temp_git_repo
    )

    assert merge_result.success is True
    assert merge_result.strategy_used == "squash"
    assert merge_result.has_conflicts is False

    # Verify all test files made it to main
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    for i in range(3):
        assert (temp_git_repo / f"test_{i}.py").exists()

    # Cleanup
    await cleanup_worktree(worktree_path, force=True)


@pytest.mark.asyncio
async def test_e2e_merge_conflict_abort_task_released(temp_git_repo):
    """Test 3: End-to-end: Merge conflict → abort → task released back to queue.

    Validates conflict detection and graceful handling.
    """
    ralph_id = "ralph-3"
    task_id = "task-3-update-docs"

    # Create worktree
    success, msg, worktree_path = await create_worktree(
        project_path=temp_git_repo,
        ralph_id=ralph_id,
        task_id=task_id,
        base_branch="main"
    )
    assert success is True

    # Ralph modifies README in worktree
    readme_path = worktree_path / "README.md"
    readme_path.write_text("# Ralph's Version\nRalph was here\n")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=worktree_path,
        check=True,
        capture_output=True
    )
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Update README"],
        cwd=worktree_path,
        check=True,
        capture_output=True
    )

    # Meanwhile, someone else modifies README in main
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    main_readme = temp_git_repo / "README.md"
    main_readme.write_text("# Main Version\nMain branch update\n")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Update README on main"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )

    # Attempt merge - should detect conflict
    branch_name = get_worktree_branch_name(ralph_id, task_id)
    merge_result = await attempt_merge(
        worktree_branch=branch_name,
        target_branch="main",
        strategy="auto",
        repo_path=temp_git_repo
    )

    # Verify conflict was detected
    assert merge_result.success is False
    assert merge_result.has_conflicts is True
    assert "README.md" in merge_result.conflicted_files
    assert len(merge_result.conflicted_files) > 0

    # Verify merge was aborted (main should be clean, ignoring .worktrees)
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True
    )
    # Filter out .worktrees directory (it's expected to be untracked)
    status_lines = [line for line in result.stdout.strip().split('\n') if line and '.worktrees' not in line]
    assert len(status_lines) == 0, f"Unexpected changes: {status_lines}"

    # Cleanup worktree
    await cleanup_worktree(worktree_path, force=True)


@pytest.mark.asyncio
async def test_e2e_multiple_ralphs_concurrent_worktrees(temp_git_repo):
    """Test 4: Multiple Ralphs: 3 Ralphs create worktrees simultaneously (no collision).

    Tests concurrent worktree creation with different Ralph IDs.
    """
    ralph_ids = ["ralph-1", "ralph-2", "ralph-3"]
    task_ids = ["task-1", "task-2", "task-3"]

    # Create worktrees concurrently
    create_tasks = [
        create_worktree(temp_git_repo, ralph_id, task_id, "main")
        for ralph_id, task_id in zip(ralph_ids, task_ids)
    ]
    results = await asyncio.gather(*create_tasks)

    # Verify all succeeded
    for i, (success, msg, wt_path) in enumerate(results):
        assert success is True, f"Ralph {i+1} failed: {msg}"
        assert wt_path is not None
        assert wt_path.exists()

    # Verify unique paths for each Ralph
    paths = [result[2] for result in results]
    assert len(set(paths)) == 3, "Worktree paths should be unique"

    # Verify all worktrees are listed
    worktrees = await list_active_worktrees(temp_git_repo)
    worktree_paths = [wt["path"] for wt in worktrees]

    for ralph_id in ralph_ids:
        expected_path = temp_git_repo / ".worktrees" / ralph_id
        # Use resolve() to normalize paths (handles /var vs /private/var on macOS)
        assert any(expected_path.resolve() == p.resolve() for p in worktree_paths), \
            f"Expected {expected_path.resolve()} not found in {[p.resolve() for p in worktree_paths]}"

    # Do work in each worktree concurrently
    async def do_work(ralph_id: str, wt_path: Path):
        work_file = wt_path / f"{ralph_id}.txt"
        work_file.write_text(f"Work by {ralph_id}")
        subprocess.run(
            ["git", "add", f"{ralph_id}.txt"],
            cwd=wt_path,
            check=True,
            capture_output=True
        )
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", f"Work by {ralph_id}"],
            cwd=wt_path,
            check=True,
            capture_output=True
        )

    work_tasks = [
        do_work(ralph_id, results[i][2])
        for i, ralph_id in enumerate(ralph_ids)
    ]
    await asyncio.gather(*work_tasks)

    # Merge all branches sequentially (to avoid conflicts)
    for i, ralph_id in enumerate(ralph_ids):
        branch_name = get_worktree_branch_name(ralph_id, task_ids[i])
        merge_result = await attempt_merge(
            worktree_branch=branch_name,
            target_branch="main",
            strategy="auto",
            repo_path=temp_git_repo
        )
        assert merge_result.success is True

    # Verify all work made it to main
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    for ralph_id in ralph_ids:
        assert (temp_git_repo / f"{ralph_id}.txt").exists()

    # Cleanup all worktrees
    for result in results:
        await cleanup_worktree(result[2], force=True)


@pytest.mark.asyncio
async def test_e2e_ralph_crash_worktree_cleanup(temp_git_repo):
    """Test 5: Crash scenario: Ralph crashes → worktree cleanup triggered.

    Simulates Ralph crash and validates stale worktree cleanup.
    """
    # Ralph starts working
    ralph_id = "ralph-crash"
    task_id = "task-incomplete"

    success, msg, worktree_path = await create_worktree(
        project_path=temp_git_repo,
        ralph_id=ralph_id,
        task_id=task_id,
        base_branch="main"
    )
    assert success is True
    assert worktree_path.exists()

    # Ralph does some work
    work_file = worktree_path / "incomplete.txt"
    work_file.write_text("Partial work before crash")
    subprocess.run(
        ["git", "add", "incomplete.txt"],
        cwd=worktree_path,
        check=True,
        capture_output=True
    )
    # Note: No commit - Ralph crashed!

    # Verify worktree exists with uncommitted changes
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
        capture_output=True,
        text=True
    )
    assert "incomplete.txt" in result.stdout

    # ChiefWiggum detects crash and cleans up stale worktrees
    # (ralph_id is NOT in active list)
    active_ralphs = ["ralph-1", "ralph-2"]  # Not including crashed Ralph
    cleanup_results = await cleanup_stale_worktrees(temp_git_repo, active_ralphs)

    # Verify crashed Ralph's worktree was cleaned up
    assert len(cleanup_results) == 1
    cleaned_path, cleanup_success, cleanup_msg = cleanup_results[0]
    assert cleanup_success is True
    assert cleaned_path == worktree_path
    assert not worktree_path.exists()


@pytest.mark.asyncio
async def test_e2e_stale_worktree_detection_and_cleanup(temp_git_repo):
    """Test 6: Stale worktree: Detect and cleanup worktrees older than threshold.

    Tests cleanup of worktrees from stopped/inactive Ralphs.
    """
    # Create worktrees for 3 Ralphs
    ralph_1 = "ralph-active"
    ralph_2 = "ralph-stale-1"
    ralph_3 = "ralph-stale-2"

    # Create all worktrees
    await create_worktree(temp_git_repo, ralph_1, "task-1")
    await create_worktree(temp_git_repo, ralph_2, "task-2")
    await create_worktree(temp_git_repo, ralph_3, "task-3")

    # Verify all exist
    worktrees = await list_active_worktrees(temp_git_repo)
    assert len(worktrees) == 4  # main + 3 ralphs

    # Only ralph-active is still running
    active_ralphs = [ralph_1]
    cleanup_results = await cleanup_stale_worktrees(temp_git_repo, active_ralphs)

    # Should have cleaned up 2 stale worktrees
    assert len(cleanup_results) == 2

    # Verify they're all successful cleanups
    for path, success, msg in cleanup_results:
        assert success is True

    # Verify only active Ralph's worktree remains
    worktrees = await list_active_worktrees(temp_git_repo)
    assert len(worktrees) == 2  # main + ralph-active

    # Verify stale worktrees are gone
    assert not (temp_git_repo / ".worktrees" / ralph_2).exists()
    assert not (temp_git_repo / ".worktrees" / ralph_3).exists()
    assert (temp_git_repo / ".worktrees" / ralph_1).exists()


@pytest.mark.asyncio
async def test_e2e_branch_naming_uniqueness(temp_git_repo):
    """Test 7: Branch naming: Verify unique branch names per Ralph/task.

    Ensures branch names don't collide even with similar inputs.
    """
    test_cases = [
        ("ralph-1", "task-123", "ralph-ralph-1-task-123"),
        ("ralph-2", "task-123", "ralph-ralph-2-task-123"),  # Same task, different Ralph
        ("ralph-1", "task-456", "ralph-ralph-1-task-456"),  # Same Ralph, different task
        ("ralph@foo", "task#123", None),  # Special chars should be sanitized
    ]

    created_branches = []
    worktree_paths = []

    for ralph_id, task_id, expected_branch in test_cases:
        success, msg, wt_path = await create_worktree(
            temp_git_repo, ralph_id, task_id, "main"
        )
        assert success is True

        # Get actual branch name
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=wt_path,
            capture_output=True,
            text=True
        )
        actual_branch = result.stdout.strip()
        created_branches.append(actual_branch)
        worktree_paths.append(wt_path)

        # Verify expected branch name (if provided)
        if expected_branch:
            assert actual_branch == expected_branch

        # Verify no special characters in branch name
        assert "@" not in actual_branch
        assert "#" not in actual_branch

    # Verify all branch names are unique
    assert len(set(created_branches)) == len(created_branches)

    # Cleanup
    for wt_path in worktree_paths:
        await cleanup_worktree(wt_path, force=True)


# ============================================================================
# Extended Unit Tests for Merge Strategies
# ============================================================================


@pytest.mark.asyncio
async def test_merge_strategy_fast_forward_linear_history(temp_git_repo):
    """Test 8: Merge strategy: fast-forward merge (linear history).

    Validates fast-forward merge when no divergence exists.
    """
    # Create feature branch
    subprocess.run(
        ["git", "checkout", "-b", "feature-ff"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )

    # Add commits on feature branch (linear from main)
    for i in range(3):
        file_path = temp_git_repo / f"ff_{i}.txt"
        file_path.write_text(f"Fast forward commit {i}")
        subprocess.run(
            ["git", "add", f"ff_{i}.txt"],
            cwd=temp_git_repo,
            check=True,
            capture_output=True
        )
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", f"FF commit {i}"],
            cwd=temp_git_repo,
            check=True,
            capture_output=True
        )

    # Attempt auto merge (should use fast-forward)
    merge_result = await attempt_merge(
        worktree_branch="feature-ff",
        target_branch="main",
        strategy="auto",
        repo_path=temp_git_repo
    )

    assert merge_result.success is True
    assert merge_result.strategy_used == "fast-forward"
    assert merge_result.has_conflicts is False
    assert merge_result.merge_sha is not None

    # Verify main now has all commits
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    for i in range(3):
        assert (temp_git_repo / f"ff_{i}.txt").exists()


@pytest.mark.asyncio
async def test_merge_strategy_squash_multiple_commits(temp_git_repo):
    """Test 9: Merge strategy: squash merge (multiple commits).

    Validates squash merge collapses multiple commits into one.
    """
    # Create feature branch
    subprocess.run(
        ["git", "checkout", "-b", "feature-squash"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )

    # Add multiple commits
    commit_count = 5
    for i in range(commit_count):
        file_path = temp_git_repo / f"squash_{i}.txt"
        file_path.write_text(f"Commit {i}")
        subprocess.run(
            ["git", "add", f"squash_{i}.txt"],
            cwd=temp_git_repo,
            check=True,
            capture_output=True
        )
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", f"Squash commit {i}"],
            cwd=temp_git_repo,
            check=True,
            capture_output=True
        )

    # Get commit count before merge
    result = subprocess.run(
        ["git", "rev-list", "--count", "main"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True
    )
    commits_before = int(result.stdout.strip())

    # Squash merge
    merge_result = await attempt_merge(
        worktree_branch="feature-squash",
        target_branch="main",
        strategy="squash",
        repo_path=temp_git_repo
    )

    assert merge_result.success is True
    assert merge_result.strategy_used == "squash"
    assert merge_result.merge_sha is not None

    # Verify only one new commit on main
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    result = subprocess.run(
        ["git", "rev-list", "--count", "main"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True
    )
    commits_after = int(result.stdout.strip())
    assert commits_after == commits_before + 1  # Only 1 new commit, not 5

    # Verify all files made it
    for i in range(commit_count):
        assert (temp_git_repo / f"squash_{i}.txt").exists()


@pytest.mark.asyncio
async def test_merge_strategy_auto_fallback_to_regular(temp_git_repo):
    """Test 10: Merge strategy: auto (tries fast-forward, falls back to regular).

    Validates auto strategy fallback behavior.
    """
    # Create feature branch
    subprocess.run(
        ["git", "checkout", "-b", "feature-auto"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )

    # Add commit on feature
    (temp_git_repo / "feature.txt").write_text("feature work")
    subprocess.run(
        ["git", "add", "feature.txt"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Feature work"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )

    # Go back to main and add divergent commit
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    (temp_git_repo / "main.txt").write_text("main work")
    subprocess.run(
        ["git", "add", "main.txt"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Main work"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )

    # Attempt auto merge (fast-forward will fail, should fallback to regular)
    merge_result = await attempt_merge(
        worktree_branch="feature-auto",
        target_branch="main",
        strategy="auto",
        repo_path=temp_git_repo
    )

    assert merge_result.success is True
    assert merge_result.strategy_used == "regular"  # Fell back to regular
    assert merge_result.has_conflicts is False
    assert merge_result.merge_sha is not None

    # Verify both files exist
    assert (temp_git_repo / "feature.txt").exists()
    assert (temp_git_repo / "main.txt").exists()


@pytest.mark.asyncio
async def test_conflict_detection_identifies_files(temp_git_repo):
    """Test 11: Conflict detection: Correctly identifies conflicted files.

    Validates accurate conflict file detection.
    """
    # Create two branches with conflicting changes
    # Branch 1: modify README and file1.txt
    subprocess.run(
        ["git", "checkout", "-b", "branch-1"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    (temp_git_repo / "README.md").write_text("# Branch 1 README\n")
    (temp_git_repo / "file1.txt").write_text("Branch 1 content")
    subprocess.run(
        ["git", "add", "README.md", "file1.txt"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Branch 1 changes"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )

    # Branch 2: modify README and file1.txt differently
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    subprocess.run(
        ["git", "checkout", "-b", "branch-2"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    (temp_git_repo / "README.md").write_text("# Branch 2 README\n")
    (temp_git_repo / "file1.txt").write_text("Branch 2 content")
    (temp_git_repo / "file2.txt").write_text("Branch 2 only")  # No conflict
    subprocess.run(
        ["git", "add", "README.md", "file1.txt", "file2.txt"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Branch 2 changes"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )

    # Merge branch-1 into main first
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    subprocess.run(
        ["git", "merge", "branch-1"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )

    # Now try to merge branch-2 (should conflict on README.md and file1.txt)
    merge_result = await attempt_merge(
        worktree_branch="branch-2",
        target_branch="main",
        strategy="auto",
        repo_path=temp_git_repo
    )

    assert merge_result.success is False
    assert merge_result.has_conflicts is True
    assert len(merge_result.conflicted_files) == 2
    assert "README.md" in merge_result.conflicted_files
    assert "file1.txt" in merge_result.conflicted_files
    assert "file2.txt" not in merge_result.conflicted_files  # Should not conflict


@pytest.mark.asyncio
async def test_worktree_paths_correct_generation(temp_git_repo):
    """Test 12: Worktree paths: Correct path generation for .worktrees/{ralph_id}/.

    Validates worktree path structure and uniqueness.
    """
    test_cases = [
        ("ralph-1", "task-1"),
        ("ralph-2", "task-2"),
        ("ralph-alpha", "task-beta"),
        ("ralph@special", "task#chars"),
    ]

    for ralph_id, task_id in test_cases:
        success, msg, wt_path = await create_worktree(
            temp_git_repo, ralph_id, task_id, "main"
        )
        assert success is True

        # Verify path structure
        assert wt_path.parent.name == ".worktrees"
        # Use resolve() to compare paths (handles symlinks on macOS)
        assert wt_path.parent.parent.resolve() == temp_git_repo.resolve()

        # Note: The worktree_manager uses ralph_id directly in the path
        # (not sanitized), but the branch name is sanitized
        # This is actually correct behavior - the directory name can have special chars
        # but the branch name cannot
        assert ralph_id in str(wt_path) or ralph_id.replace("@", "-").replace("#", "-") in str(wt_path)

        # Verify path is absolute
        assert wt_path.is_absolute()

        # Verify directory was actually created
        assert wt_path.exists()
        assert wt_path.is_dir()

        # Verify .git exists in worktree
        assert (wt_path / ".git").exists()

    # Cleanup
    worktree_base = temp_git_repo / ".worktrees"
    for ralph_dir in worktree_base.iterdir():
        await cleanup_worktree(ralph_dir, force=True)


# ============================================================================
# Additional Edge Case Tests
# ============================================================================


@pytest.mark.asyncio
async def test_worktree_creation_with_existing_uncommitted_changes(temp_git_repo):
    """Test creating new worktree when old one has uncommitted changes."""
    ralph_id = "ralph-dirty"

    # Create first worktree
    success, msg, wt_path1 = await create_worktree(
        temp_git_repo, ralph_id, "task-1", "main"
    )
    assert success is True

    # Leave uncommitted changes
    (wt_path1 / "dirty.txt").write_text("uncommitted")

    # Create second worktree with same Ralph ID (should cleanup first)
    success, msg, wt_path2 = await create_worktree(
        temp_git_repo, ralph_id, "task-2", "main"
    )
    assert success is True
    assert wt_path2 == wt_path1  # Same path

    # Verify dirty file is gone (cleanup happened)
    assert not (wt_path2 / "dirty.txt").exists()


@pytest.mark.asyncio
async def test_concurrent_merge_attempts_sequential_success(temp_git_repo):
    """Test that multiple Ralphs can merge sequentially without conflicts."""
    ralphs = [("ralph-1", "task-1"), ("ralph-2", "task-2"), ("ralph-3", "task-3")]

    # Create worktrees and do independent work
    for ralph_id, task_id in ralphs:
        success, msg, wt_path = await create_worktree(
            temp_git_repo, ralph_id, task_id, "main"
        )
        assert success is True

        # Each Ralph works on a different file (no conflicts)
        work_file = wt_path / f"{ralph_id}_work.txt"
        work_file.write_text(f"Work by {ralph_id}")
        subprocess.run(
            ["git", "add", f"{ralph_id}_work.txt"],
            cwd=wt_path,
            check=True,
            capture_output=True
        )
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", f"Work by {ralph_id}"],
            cwd=wt_path,
            check=True,
            capture_output=True
        )

    # Merge sequentially
    for ralph_id, task_id in ralphs:
        branch_name = get_worktree_branch_name(ralph_id, task_id)
        merge_result = await attempt_merge(
            worktree_branch=branch_name,
            target_branch="main",
            strategy="auto",
            repo_path=temp_git_repo
        )
        assert merge_result.success is True

    # Verify all work is in main
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True
    )
    for ralph_id, _ in ralphs:
        assert (temp_git_repo / f"{ralph_id}_work.txt").exists()
