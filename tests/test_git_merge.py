"""Tests for git_merge.py

Tests git auto-merge with conflict detection.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest

from chiefwiggum.git_merge import attempt_merge, detect_conflicts


@pytest.fixture
def temp_git_repo():
    """Create a temporary git repository with main branch."""
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


@pytest.mark.asyncio
async def test_fast_forward_merge(temp_git_repo):
    """Test successful fast-forward merge."""
    # Create a feature branch
    subprocess.run(
        ["git", "checkout", "-b", "feature-1"],
        cwd=temp_git_repo, check=True, capture_output=True
    )

    # Add commit on feature branch
    (temp_git_repo / "feature.txt").write_text("new feature")
    subprocess.run(["git", "add", "feature.txt"], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Add feature"],
        cwd=temp_git_repo, check=True, capture_output=True
    )

    # Attempt merge with auto strategy (should use fast-forward)
    result = await attempt_merge(
        worktree_branch="feature-1",
        target_branch="main",
        strategy="auto",
        repo_path=temp_git_repo
    )

    assert result.success is True
    assert result.has_conflicts is False
    assert result.strategy_used == "fast-forward"
    assert result.merge_sha is not None


@pytest.mark.asyncio
async def test_regular_merge_with_divergent_branches(temp_git_repo):
    """Test regular merge when fast-forward is not possible."""
    # Create a feature branch
    subprocess.run(
        ["git", "checkout", "-b", "feature-1"],
        cwd=temp_git_repo, check=True, capture_output=True
    )

    # Add commit on feature branch
    (temp_git_repo / "feature.txt").write_text("new feature")
    subprocess.run(["git", "add", "feature.txt"], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Add feature"],
        cwd=temp_git_repo, check=True, capture_output=True
    )

    # Go back to main and add a different commit
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=temp_git_repo, check=True, capture_output=True
    )
    (temp_git_repo / "main.txt").write_text("main work")
    subprocess.run(["git", "add", "main.txt"], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Main work"],
        cwd=temp_git_repo, check=True, capture_output=True
    )

    # Attempt merge with auto strategy (should fallback to regular merge)
    result = await attempt_merge(
        worktree_branch="feature-1",
        target_branch="main",
        strategy="auto",
        repo_path=temp_git_repo
    )

    assert result.success is True
    assert result.has_conflicts is False
    assert result.strategy_used == "regular"
    assert result.merge_sha is not None


@pytest.mark.asyncio
async def test_merge_with_conflicts(temp_git_repo):
    """Test merge that results in conflicts."""
    # Create a feature branch
    subprocess.run(
        ["git", "checkout", "-b", "feature-1"],
        cwd=temp_git_repo, check=True, capture_output=True
    )

    # Modify README on feature branch
    (temp_git_repo / "README.md").write_text("# Feature Version\n")
    subprocess.run(["git", "add", "README.md"], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Update README"],
        cwd=temp_git_repo, check=True, capture_output=True
    )

    # Go back to main and modify README differently
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=temp_git_repo, check=True, capture_output=True
    )
    (temp_git_repo / "README.md").write_text("# Main Version\n")
    subprocess.run(["git", "add", "README.md"], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Update README on main"],
        cwd=temp_git_repo, check=True, capture_output=True
    )

    # Attempt merge (should detect conflict)
    result = await attempt_merge(
        worktree_branch="feature-1",
        target_branch="main",
        strategy="auto",
        repo_path=temp_git_repo
    )

    assert result.success is False
    assert result.has_conflicts is True
    assert len(result.conflicted_files) > 0
    assert "README.md" in result.conflicted_files
    assert result.error_message is not None


@pytest.mark.asyncio
async def test_detect_conflicts(temp_git_repo):
    """Test conflict detection."""
    # No conflicts initially
    conflicts = await detect_conflicts(temp_git_repo)
    assert len(conflicts) == 0

    # Create conflicting scenario
    subprocess.run(
        ["git", "checkout", "-b", "feature-1"],
        cwd=temp_git_repo, check=True, capture_output=True
    )
    (temp_git_repo / "README.md").write_text("# Feature\n")
    subprocess.run(["git", "add", "README.md"], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Update"],
        cwd=temp_git_repo, check=True, capture_output=True
    )

    subprocess.run(
        ["git", "checkout", "main"],
        cwd=temp_git_repo, check=True, capture_output=True
    )
    (temp_git_repo / "README.md").write_text("# Main\n")
    subprocess.run(["git", "add", "README.md"], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Update main"],
        cwd=temp_git_repo, check=True, capture_output=True
    )

    # Start merge but don't complete it
    subprocess.run(
        ["git", "merge", "feature-1"],
        cwd=temp_git_repo,
        capture_output=True
    )
    # Merge should fail with conflicts

    # Detect conflicts
    conflicts = await detect_conflicts(temp_git_repo)
    assert len(conflicts) > 0
    assert "README.md" in conflicts

    # Abort merge
    subprocess.run(
        ["git", "merge", "--abort"],
        cwd=temp_git_repo,
        capture_output=True
    )


@pytest.mark.asyncio
async def test_fast_forward_only_strategy(temp_git_repo):
    """Test fast-forward only strategy fails when not possible."""
    # Create divergent branches
    subprocess.run(
        ["git", "checkout", "-b", "feature-1"],
        cwd=temp_git_repo, check=True, capture_output=True
    )
    (temp_git_repo / "feature.txt").write_text("feature")
    subprocess.run(["git", "add", "feature.txt"], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Add feature"],
        cwd=temp_git_repo, check=True, capture_output=True
    )

    subprocess.run(
        ["git", "checkout", "main"],
        cwd=temp_git_repo, check=True, capture_output=True
    )
    (temp_git_repo / "main.txt").write_text("main")
    subprocess.run(["git", "add", "main.txt"], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Main work"],
        cwd=temp_git_repo, check=True, capture_output=True
    )

    # Try fast-forward only (should fail)
    result = await attempt_merge(
        worktree_branch="feature-1",
        target_branch="main",
        strategy="fast-forward",
        repo_path=temp_git_repo
    )

    assert result.success is False
    assert result.strategy_used == "fast-forward"
    assert "not possible" in result.error_message.lower()


@pytest.mark.asyncio
async def test_squash_merge(temp_git_repo):
    """Test squash merge strategy."""
    # Create feature branch with multiple commits
    subprocess.run(
        ["git", "checkout", "-b", "feature-1"],
        cwd=temp_git_repo, check=True, capture_output=True
    )

    (temp_git_repo / "file1.txt").write_text("file1")
    subprocess.run(["git", "add", "file1.txt"], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Add file1"],
        cwd=temp_git_repo, check=True, capture_output=True
    )

    (temp_git_repo / "file2.txt").write_text("file2")
    subprocess.run(["git", "add", "file2.txt"], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Add file2"],
        cwd=temp_git_repo, check=True, capture_output=True
    )

    # Squash merge
    result = await attempt_merge(
        worktree_branch="feature-1",
        target_branch="main",
        strategy="squash",
        repo_path=temp_git_repo
    )

    assert result.success is True
    assert result.strategy_used == "squash"
    assert result.merge_sha is not None
