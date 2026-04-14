"""Git Auto-Merge for ChiefWiggum

Handles automatic merging of worktree branches back to main with conflict detection.
"""

import logging
import subprocess
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class MergeResult(BaseModel):
    """Result of a merge operation."""

    success: bool
    has_conflicts: bool = False
    conflicted_files: list[str] = []
    merge_sha: str | None = None
    strategy_used: str  # 'fast-forward', 'regular', 'squash'
    error_message: str | None = None
    merged_at: datetime


async def detect_conflicts(repo_path: Path) -> list[str]:
    """Get list of conflicted files.

    Uses: git diff --name-only --diff-filter=U

    Args:
        repo_path: Path to the git repository

    Returns:
        List of conflicted file paths
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            return [f.strip() for f in result.stdout.split('\n') if f.strip()]

        return []

    except subprocess.TimeoutExpired:
        logger.warning("Git diff timed out while detecting conflicts")
        return []
    except Exception:
        logger.exception("Error detecting conflicts")
        return []


async def attempt_merge(
    worktree_branch: str,
    target_branch: str,
    strategy: str,
    repo_path: Path
) -> MergeResult:
    """Attempt merge with automatic fallback.

    Strategy: 'auto' (recommended)
      1. Try fast-forward merge (--ff-only)
      2. If fails, try regular merge (--no-ff)
      3. Detect conflicts
      4. Return detailed result

    Args:
        worktree_branch: Branch to merge from
        target_branch: Branch to merge into (usually 'main')
        strategy: 'auto', 'fast-forward', 'regular', or 'squash'
        repo_path: Path to the git repository

    Returns:
        MergeResult with success status and details
    """
    merged_at = datetime.now()

    try:
        # Ensure we're on the target branch
        result = subprocess.run(
            ["git", "checkout", target_branch],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            return MergeResult(
                success=False,
                has_conflicts=False,
                strategy_used="none",
                error_message=f"Failed to checkout {target_branch}: {result.stderr}",
                merged_at=merged_at
            )

        # Update target branch to latest
        result = subprocess.run(
            ["git", "pull", "origin", target_branch],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30
        )
        # Continue even if pull fails (might be working offline)

        # Try merge based on strategy
        if strategy == "auto":
            # Try fast-forward first
            logger.info(f"Attempting fast-forward merge of {worktree_branch} into {target_branch}")
            result = subprocess.run(
                ["git", "merge", "--ff-only", worktree_branch],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                # Fast-forward succeeded!
                merge_sha = await _get_head_sha(repo_path)
                logger.info(f"Fast-forward merge succeeded: {merge_sha}")
                return MergeResult(
                    success=True,
                    has_conflicts=False,
                    merge_sha=merge_sha,
                    strategy_used="fast-forward",
                    merged_at=merged_at
                )

            # Fast-forward failed, try regular merge
            logger.info("Fast-forward failed, attempting regular merge")
            result = subprocess.run(
                ["git", "merge", "--no-ff", worktree_branch, "-m",
                 f"Merge {worktree_branch} into {target_branch}"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                merge_sha = await _get_head_sha(repo_path)
                logger.info(f"Regular merge succeeded: {merge_sha}")
                return MergeResult(
                    success=True,
                    has_conflicts=False,
                    merge_sha=merge_sha,
                    strategy_used="regular",
                    merged_at=merged_at
                )

            # Check for conflicts
            conflicted_files = await detect_conflicts(repo_path)

            if conflicted_files:
                # Abort the merge
                subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    timeout=10
                )

                logger.warning(f"Merge conflicts detected: {conflicted_files}")
                return MergeResult(
                    success=False,
                    has_conflicts=True,
                    conflicted_files=conflicted_files,
                    strategy_used="regular",
                    error_message=f"Merge conflicts in {len(conflicted_files)} files",
                    merged_at=merged_at
                )

            # Some other error
            return MergeResult(
                success=False,
                has_conflicts=False,
                strategy_used="regular",
                error_message=f"Merge failed: {result.stderr}",
                merged_at=merged_at
            )

        elif strategy == "fast-forward":
            # Only try fast-forward
            result = subprocess.run(
                ["git", "merge", "--ff-only", worktree_branch],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                merge_sha = await _get_head_sha(repo_path)
                return MergeResult(
                    success=True,
                    has_conflicts=False,
                    merge_sha=merge_sha,
                    strategy_used="fast-forward",
                    merged_at=merged_at
                )

            return MergeResult(
                success=False,
                has_conflicts=False,
                strategy_used="fast-forward",
                error_message=f"Fast-forward not possible: {result.stderr}",
                merged_at=merged_at
            )

        elif strategy == "regular":
            # Regular merge with merge commit
            result = subprocess.run(
                ["git", "merge", "--no-ff", worktree_branch, "-m",
                 f"Merge {worktree_branch} into {target_branch}"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                merge_sha = await _get_head_sha(repo_path)
                return MergeResult(
                    success=True,
                    has_conflicts=False,
                    merge_sha=merge_sha,
                    strategy_used="regular",
                    merged_at=merged_at
                )

            # Check for conflicts
            conflicted_files = await detect_conflicts(repo_path)

            if conflicted_files:
                subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    timeout=10
                )

                return MergeResult(
                    success=False,
                    has_conflicts=True,
                    conflicted_files=conflicted_files,
                    strategy_used="regular",
                    error_message=f"Merge conflicts in {len(conflicted_files)} files",
                    merged_at=merged_at
                )

            return MergeResult(
                success=False,
                has_conflicts=False,
                strategy_used="regular",
                error_message=f"Merge failed: {result.stderr}",
                merged_at=merged_at
            )

        elif strategy == "squash":
            # Squash merge
            result = subprocess.run(
                ["git", "merge", "--squash", worktree_branch],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                # Need to commit after squash
                commit_result = subprocess.run(
                    ["git", "commit", "-m", f"Squash merge {worktree_branch} into {target_branch}"],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                if commit_result.returncode == 0:
                    merge_sha = await _get_head_sha(repo_path)
                    return MergeResult(
                        success=True,
                        has_conflicts=False,
                        merge_sha=merge_sha,
                        strategy_used="squash",
                        merged_at=merged_at
                    )

                # Commit failed after squash — check for "nothing to commit" (already merged)
                combined_output = (commit_result.stdout + commit_result.stderr).lower()
                if "nothing to commit" in combined_output:
                    merge_sha = await _get_head_sha(repo_path)
                    return MergeResult(
                        success=True,
                        has_conflicts=False,
                        merge_sha=merge_sha,
                        strategy_used="squash",
                        merged_at=merged_at
                    )

                # Commit failed for another reason — reset staged changes to leave repo clean
                subprocess.run(
                    ["git", "reset", "HEAD"],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                return MergeResult(
                    success=False,
                    has_conflicts=False,
                    strategy_used="squash",
                    error_message=f"Squash commit failed: {commit_result.stderr}",
                    merged_at=merged_at
                )

            # git merge --squash itself failed — check for conflicts
            conflicted_files = await detect_conflicts(repo_path)

            if conflicted_files:
                # Squash doesn't create MERGE_HEAD, so reset staged changes instead
                subprocess.run(
                    ["git", "reset", "HEAD"],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    timeout=10
                )

                return MergeResult(
                    success=False,
                    has_conflicts=True,
                    conflicted_files=conflicted_files,
                    strategy_used="squash",
                    error_message=f"Merge conflicts in {len(conflicted_files)} files",
                    merged_at=merged_at
                )

            return MergeResult(
                success=False,
                has_conflicts=False,
                strategy_used="squash",
                error_message=f"Squash merge failed: {result.stderr}",
                merged_at=merged_at
            )

        else:
            return MergeResult(
                success=False,
                has_conflicts=False,
                strategy_used="unknown",
                error_message=f"Unknown strategy: {strategy}",
                merged_at=merged_at
            )

    except subprocess.TimeoutExpired:
        return MergeResult(
            success=False,
            has_conflicts=False,
            strategy_used=strategy,
            error_message="Merge operation timed out",
            merged_at=merged_at
        )
    except Exception as e:
        logger.exception(f"Error during merge of {worktree_branch}")
        return MergeResult(
            success=False,
            has_conflicts=False,
            strategy_used=strategy,
            error_message=f"Exception: {str(e)}",
            merged_at=merged_at
        )


async def _get_head_sha(repo_path: Path) -> str | None:
    """Get the current HEAD commit SHA.

    Args:
        repo_path: Path to the git repository

    Returns:
        Commit SHA or None if error
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            return result.stdout.strip()

        return None

    except Exception:
        logger.exception("Error getting HEAD SHA")
        return None
