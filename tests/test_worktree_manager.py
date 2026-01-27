"""Tests for worktree_manager.py

Tests git worktree lifecycle management.
"""

import pytest
import subprocess
import tempfile
from pathlib import Path

from chiefwiggum.worktree_manager import (
    cleanup_stale_worktrees,
    cleanup_worktree,
    create_worktree,
    get_worktree_branch_name,
    get_worktree_status,
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

        # Create initial commit
        (repo_path / "README.md").write_text("# Test Repo\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Initial commit"],
            cwd=repo_path, check=True, capture_output=True
        )

        # Rename branch to main
        subprocess.run(
            ["git", "branch", "-M", "main"],
            cwd=repo_path, check=True, capture_output=True
        )

        yield repo_path


def test_get_worktree_branch_name():
    """Test branch name generation."""
    branch = get_worktree_branch_name("ralph-1", "task-123-fix-bug")
    assert branch == "ralph-ralph-1-task-123-fix-bug"

    # Test with special characters
    branch = get_worktree_branch_name("ralph@foo", "task#456")
    assert "@" not in branch
    assert "#" not in branch


@pytest.mark.asyncio
async def test_create_worktree_success(temp_git_repo):
    """Test successful worktree creation."""
    success, msg, wt_path = await create_worktree(
        project_path=temp_git_repo,
        ralph_id="ralph-1",
        task_id="task-123",
        base_branch="main"
    )

    assert success is True
    assert wt_path is not None
    assert wt_path.exists()
    assert wt_path == temp_git_repo / ".worktrees" / "ralph-1"

    # Verify branch exists
    result = subprocess.run(
        ["git", "branch", "--list", "ralph-ralph-1-task-123"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True
    )
    assert "ralph-ralph-1-task-123" in result.stdout


@pytest.mark.asyncio
async def test_create_worktree_not_git_repo():
    """Test worktree creation fails in non-git directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        success, msg, wt_path = await create_worktree(
            project_path=Path(tmpdir),
            ralph_id="ralph-1",
            task_id="task-123"
        )

        assert success is False
        assert "Not a git repository" in msg
        assert wt_path is None


@pytest.mark.asyncio
async def test_cleanup_worktree_success(temp_git_repo):
    """Test successful worktree cleanup."""
    # Create worktree first
    success, msg, wt_path = await create_worktree(
        project_path=temp_git_repo,
        ralph_id="ralph-1",
        task_id="task-123"
    )
    assert success is True

    # Cleanup worktree
    success, msg = await cleanup_worktree(wt_path)
    assert success is True
    assert not wt_path.exists()

    # Verify branch is deleted
    result = subprocess.run(
        ["git", "branch", "--list", "ralph-ralph-1-task-123"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True
    )
    assert "ralph-ralph-1-task-123" not in result.stdout


@pytest.mark.asyncio
async def test_cleanup_worktree_with_changes(temp_git_repo):
    """Test worktree cleanup with uncommitted changes."""
    # Create worktree
    success, msg, wt_path = await create_worktree(
        project_path=temp_git_repo,
        ralph_id="ralph-1",
        task_id="task-123"
    )
    assert success is True

    # Add uncommitted changes
    (wt_path / "test.txt").write_text("uncommitted change")

    # Cleanup should fail without force
    success, msg = await cleanup_worktree(wt_path, force=False)
    assert success is False
    assert "uncommitted changes" in msg.lower()

    # Cleanup with force should succeed
    success, msg = await cleanup_worktree(wt_path, force=True)
    assert success is True
    assert not wt_path.exists()


@pytest.mark.asyncio
async def test_list_active_worktrees(temp_git_repo):
    """Test listing active worktrees."""
    # No worktrees initially (except main)
    worktrees = await list_active_worktrees(temp_git_repo)
    assert len(worktrees) == 1  # Just the main worktree

    # Create a worktree
    await create_worktree(temp_git_repo, "ralph-1", "task-123")

    # List should show 2 worktrees now
    worktrees = await list_active_worktrees(temp_git_repo)
    assert len(worktrees) == 2

    # Check worktree info
    wt = next((w for w in worktrees if ".worktrees" in str(w.get("path", ""))), None)
    assert wt is not None
    assert "branch" in wt
    assert wt["branch"] == "refs/heads/ralph-ralph-1-task-123"


@pytest.mark.asyncio
async def test_cleanup_stale_worktrees(temp_git_repo):
    """Test cleanup of stale worktrees."""
    # Create worktrees for ralph-1 and ralph-2
    await create_worktree(temp_git_repo, "ralph-1", "task-123")
    await create_worktree(temp_git_repo, "ralph-2", "task-456")

    # Both worktrees exist
    worktrees = await list_active_worktrees(temp_git_repo)
    assert len(worktrees) == 3  # main + ralph-1 + ralph-2

    # Cleanup stale worktrees (only ralph-1 is active)
    results = await cleanup_stale_worktrees(temp_git_repo, ["ralph-1"])

    # Should have cleaned up ralph-2
    assert len(results) == 1
    assert results[0][1] is True  # success

    # Only ralph-1 worktree should remain
    worktrees = await list_active_worktrees(temp_git_repo)
    assert len(worktrees) == 2  # main + ralph-1


@pytest.mark.asyncio
async def test_get_worktree_status(temp_git_repo):
    """Test getting worktree status."""
    # Create worktree
    success, msg, wt_path = await create_worktree(
        temp_git_repo, "ralph-1", "task-123"
    )
    assert success is True

    # Get status
    status = await get_worktree_status(wt_path)
    assert status is not None
    assert status["branch"] == "ralph-ralph-1-task-123"
    assert status["has_changes"] is False

    # Add changes
    (wt_path / "test.txt").write_text("change")

    status = await get_worktree_status(wt_path)
    assert status["has_changes"] is True


@pytest.mark.asyncio
async def test_create_worktree_duplicate(temp_git_repo):
    """Test creating worktree with existing directory."""
    # Create first worktree
    success1, msg1, wt_path1 = await create_worktree(
        temp_git_repo, "ralph-1", "task-123"
    )
    assert success1 is True

    # Try to create another worktree with same ralph_id
    # (should cleanup existing and create new)
    success2, msg2, wt_path2 = await create_worktree(
        temp_git_repo, "ralph-1", "task-456"
    )
    assert success2 is True
    assert wt_path2 == wt_path1  # Same path

    # Verify new branch exists
    result = subprocess.run(
        ["git", "branch", "--list", "ralph-ralph-1-task-456"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True
    )
    assert "ralph-ralph-1-task-456" in result.stdout


@pytest.mark.asyncio
async def test_worktree_path_generation_structure(temp_git_repo):
    """Test worktree path follows .worktrees/{ralph_id}/ structure."""
    ralph_id = "ralph-test"
    task_id = "task-path-test"

    success, msg, wt_path = await create_worktree(
        temp_git_repo, ralph_id, task_id
    )

    assert success is True
    assert wt_path is not None

    # Verify path structure
    assert wt_path == temp_git_repo / ".worktrees" / ralph_id
    assert wt_path.parent.name == ".worktrees"
    assert wt_path.parent.parent == temp_git_repo

    # Verify directory exists
    assert wt_path.exists()
    assert wt_path.is_dir()

    # Cleanup
    await cleanup_worktree(wt_path, force=True)


@pytest.mark.asyncio
async def test_worktree_path_with_special_characters(temp_git_repo):
    """Test worktree path generation with special characters.

    Note: The worktree_manager uses ralph_id directly in the path (not sanitized),
    so some special characters will be present. This is acceptable as the branch
    name is properly sanitized (which is what matters for git operations).
    """
    test_cases = [
        ("ralph@host", "task#123"),
        ("ralph_safe", "task-safe"),  # Safe case
        ("ralph with spaces", "task-test"),
    ]

    for ralph_id, task_id in test_cases:
        success, msg, wt_path = await create_worktree(
            temp_git_repo, ralph_id, task_id
        )

        assert success is True, f"Failed to create worktree for {ralph_id}: {msg}"
        assert wt_path is not None

        # Path should exist
        assert wt_path.exists()

        # The path should be under .worktrees (may have nested dirs if ralph_id has /)
        # Find the .worktrees parent
        current = wt_path
        found_worktrees = False
        while current != temp_git_repo:
            if current.name == ".worktrees":
                found_worktrees = True
                break
            current = current.parent

        assert found_worktrees, f"Path {wt_path} is not under .worktrees"

        # Verify branch name is sanitized (most important)
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=wt_path,
            capture_output=True,
            text=True
        )
        branch_name = result.stdout.strip()
        # Branch names should not have special chars
        assert "@" not in branch_name
        assert "#" not in branch_name
        assert " " not in branch_name

        # Cleanup
        await cleanup_worktree(wt_path, force=True)


@pytest.mark.asyncio
async def test_list_active_worktrees_with_multiple(temp_git_repo):
    """Test listing multiple active worktrees with details."""
    # Create multiple worktrees
    ralph_ids = ["ralph-1", "ralph-2", "ralph-3"]
    task_ids = ["task-a", "task-b", "task-c"]

    for ralph_id, task_id in zip(ralph_ids, task_ids):
        await create_worktree(temp_git_repo, ralph_id, task_id)

    # List worktrees
    worktrees = await list_active_worktrees(temp_git_repo)

    # Should have main + 3 ralphs
    assert len(worktrees) >= 4

    # Verify each Ralph's worktree is listed
    ralph_worktrees = [
        wt for wt in worktrees
        if ".worktrees" in str(wt.get("path", ""))
    ]
    assert len(ralph_worktrees) == 3

    # Verify each has required fields
    for wt in ralph_worktrees:
        assert "path" in wt
        assert "branch" in wt
        assert "commit" in wt
        assert isinstance(wt["path"], Path)

    # Cleanup
    for ralph_id in ralph_ids:
        wt_path = temp_git_repo / ".worktrees" / ralph_id
        await cleanup_worktree(wt_path, force=True)


@pytest.mark.asyncio
async def test_cleanup_stale_worktrees_preserves_active(temp_git_repo):
    """Test stale cleanup preserves active Ralph worktrees."""
    # Create worktrees for 4 Ralphs
    all_ralphs = ["ralph-1", "ralph-2", "ralph-3", "ralph-4"]
    active_ralphs = ["ralph-1", "ralph-3"]  # Only 2 are active

    for ralph_id in all_ralphs:
        await create_worktree(temp_git_repo, ralph_id, f"task-{ralph_id}")

    # Cleanup stale worktrees
    results = await cleanup_stale_worktrees(temp_git_repo, active_ralphs)

    # Should have cleaned up 2 stale worktrees
    assert len(results) == 2

    # Verify results
    cleaned_paths = [str(path) for path, _, _ in results]
    for ralph_id in all_ralphs:
        wt_path = temp_git_repo / ".worktrees" / ralph_id
        if ralph_id in active_ralphs:
            # Active worktrees should NOT be in cleanup results
            assert str(wt_path) not in cleaned_paths
            assert wt_path.exists()
        else:
            # Stale worktrees should be in cleanup results
            assert str(wt_path) in cleaned_paths
            assert not wt_path.exists()

    # Cleanup remaining
    for ralph_id in active_ralphs:
        wt_path = temp_git_repo / ".worktrees" / ralph_id
        await cleanup_worktree(wt_path, force=True)


@pytest.mark.asyncio
async def test_get_worktree_status_detailed(temp_git_repo):
    """Test detailed worktree status information."""
    # Create worktree
    success, msg, wt_path = await create_worktree(
        temp_git_repo, "ralph-status", "task-status"
    )
    assert success is True

    # Get initial status
    status = await get_worktree_status(wt_path)
    assert status is not None
    assert "branch" in status
    assert "has_changes" in status
    assert "ahead" in status
    assert "behind" in status

    # Initially should be clean
    assert status["has_changes"] is False
    assert status["branch"] == "ralph-ralph-status-task-status"

    # Add tracked changes
    (wt_path / "tracked.txt").write_text("tracked file")
    subprocess.run(
        ["git", "add", "tracked.txt"],
        cwd=wt_path,
        check=True,
        capture_output=True
    )

    status = await get_worktree_status(wt_path)
    assert status["has_changes"] is True

    # Commit changes
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Add tracked file"],
        cwd=wt_path,
        check=True,
        capture_output=True
    )

    status = await get_worktree_status(wt_path)
    assert status["has_changes"] is False  # No uncommitted changes

    # Cleanup
    await cleanup_worktree(wt_path, force=True)
