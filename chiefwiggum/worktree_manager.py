"""Git Worktree Management for ChiefWiggum

Manages isolated git worktrees for each Ralph instance to prevent merge conflicts.
Each worktree operates in a separate directory with its own branch.
"""

import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def get_worktree_branch_name(ralph_id: str, task_id: str) -> str:
    """Generate branch name for worktree.

    Format: ralph-{ralph_id}-{task_id}

    Args:
        ralph_id: Ralph instance ID
        task_id: Task ID

    Returns:
        Branch name string
    """
    # Sanitize IDs to be git-safe
    safe_ralph = re.sub(r'[^a-zA-Z0-9_-]', '-', ralph_id)
    safe_task = re.sub(r'[^a-zA-Z0-9_-]', '-', task_id)
    return f"ralph-{safe_ralph}-{safe_task}"


async def create_worktree(
    project_path: Path,
    ralph_id: str,
    task_id: str,
    base_branch: str = "main"
) -> tuple[bool, str, Path | None]:
    """Create isolated git worktree for task.

    Creates worktree at: {project}/.worktrees/{ralph_id}/
    Branch name: ralph-{ralph_id}-{task_id}

    Args:
        project_path: Path to the git repository
        ralph_id: Ralph instance ID
        task_id: Task ID
        base_branch: Base branch to create worktree from (default: main)

    Returns:
        Tuple of (success, message, worktree_path)
    """
    try:
        # Ensure project_path is a git repository
        if not (project_path / ".git").exists():
            return False, f"Not a git repository: {project_path}", None

        # Create worktree directory
        worktree_base = project_path / ".worktrees"
        worktree_path = worktree_base / ralph_id

        # Check if worktree already exists
        if worktree_path.exists():
            logger.warning(f"Worktree already exists: {worktree_path}")
            # Clean up existing worktree first
            cleanup_success, cleanup_msg = await cleanup_worktree(worktree_path, force=True)
            if not cleanup_success:
                return False, f"Failed to cleanup existing worktree: {cleanup_msg}", None

        # Generate branch name
        branch_name = get_worktree_branch_name(ralph_id, task_id)

        # Create worktree with new branch from base_branch
        # git worktree add -b {branch_name} {worktree_path} {base_branch}
        result = subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, str(worktree_path), base_branch],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            # Check if branch already exists
            if "already exists" in result.stderr.lower():
                # Try without -b flag (checkout existing branch)
                result = subprocess.run(
                    ["git", "worktree", "add", str(worktree_path), branch_name],
                    cwd=str(project_path),
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                if result.returncode != 0:
                    return False, f"Git worktree add failed: {result.stderr}", None
            else:
                return False, f"Git worktree add failed: {result.stderr}", None

        logger.info(f"Created worktree for {ralph_id} at {worktree_path} (branch: {branch_name})")
        return True, f"Worktree created at {worktree_path}", worktree_path

    except subprocess.TimeoutExpired:
        return False, "Git worktree add timed out", None
    except Exception as e:
        logger.exception(f"Error creating worktree for {ralph_id}")
        return False, f"Exception: {str(e)}", None


async def cleanup_worktree(
    worktree_path: Path,
    force: bool = False
) -> tuple[bool, str]:
    """Remove worktree and delete branch.

    Checks for uncommitted changes unless force=True.

    Args:
        worktree_path: Path to the worktree directory
        force: If True, force cleanup even with uncommitted changes

    Returns:
        Tuple of (success, message)
    """
    try:
        if not worktree_path.exists():
            return True, "Worktree already removed"

        # Get the git repository root (parent of .worktrees)
        repo_path = worktree_path.parent.parent

        # Check for uncommitted changes unless force=True
        if not force:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0 and result.stdout.strip():
                return False, "Worktree has uncommitted changes (use force=True to override)"

        # Get branch name from worktree
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=10
        )
        branch_name = result.stdout.strip() if result.returncode == 0 else None

        # Remove worktree
        result = subprocess.run(
            ["git", "worktree", "remove", str(worktree_path)] + (["--force"] if force else []),
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            logger.warning(f"Failed to remove worktree {worktree_path}: {result.stderr}")
            # Try to prune it anyway
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=10
            )
            return False, f"Failed to remove worktree: {result.stderr}"

        # Delete the branch if we got it
        if branch_name and branch_name != "HEAD":
            result = subprocess.run(
                ["git", "branch", "-D", branch_name],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                logger.warning(f"Failed to delete branch {branch_name}: {result.stderr}")
                # Not critical, continue

        logger.info(f"Cleaned up worktree: {worktree_path}")
        return True, "Worktree cleaned up successfully"

    except subprocess.TimeoutExpired:
        return False, "Git worktree cleanup timed out"
    except Exception as e:
        logger.exception(f"Error cleaning up worktree {worktree_path}")
        return False, f"Exception: {str(e)}"


async def list_active_worktrees(project_path: Path) -> list[dict]:
    """Parse `git worktree list --porcelain` output.

    Args:
        project_path: Path to the git repository

    Returns:
        List of dicts with worktree info:
        - path: Path to worktree
        - branch: Branch name
        - commit: HEAD commit SHA
    """
    try:
        if not (project_path / ".git").exists():
            return []

        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            logger.warning(f"Failed to list worktrees: {result.stderr}")
            return []

        # Parse porcelain output
        worktrees = []
        current_wt = {}

        for line in result.stdout.split('\n'):
            line = line.strip()
            if not line:
                if current_wt:
                    worktrees.append(current_wt)
                    current_wt = {}
                continue

            if line.startswith("worktree "):
                current_wt["path"] = Path(line.split(maxsplit=1)[1])
            elif line.startswith("HEAD "):
                current_wt["commit"] = line.split(maxsplit=1)[1]
            elif line.startswith("branch "):
                current_wt["branch"] = line.split(maxsplit=1)[1]

        if current_wt:
            worktrees.append(current_wt)

        return worktrees

    except subprocess.TimeoutExpired:
        logger.warning("Git worktree list timed out")
        return []
    except Exception as e:
        logger.exception("Error listing worktrees")
        return []


async def cleanup_stale_worktrees(
    project_path: Path,
    active_ralph_ids: list[str]
) -> list[tuple[Path, bool, str]]:
    """Cleanup worktrees from crashed/stopped Ralphs.

    Args:
        project_path: Path to the git repository
        active_ralph_ids: List of currently active Ralph IDs

    Returns:
        List of tuples: (worktree_path, success, message)
    """
    results = []

    try:
        worktree_base = project_path / ".worktrees"
        if not worktree_base.exists():
            return results

        # Get all worktree directories
        for ralph_dir in worktree_base.iterdir():
            if not ralph_dir.is_dir():
                continue

            ralph_id = ralph_dir.name

            # If this Ralph is still active, skip
            if ralph_id in active_ralph_ids:
                continue

            # Cleanup stale worktree
            logger.info(f"Cleaning up stale worktree for inactive Ralph: {ralph_id}")
            success, msg = await cleanup_worktree(ralph_dir, force=True)
            results.append((ralph_dir, success, msg))

    except Exception as e:
        logger.exception("Error cleaning up stale worktrees")

    return results


async def get_worktree_status(worktree_path: Path) -> dict | None:
    """Get status information for a worktree.

    Args:
        worktree_path: Path to the worktree

    Returns:
        Dict with status info or None if error:
        - branch: Current branch name
        - has_changes: Whether there are uncommitted changes
        - ahead: Number of commits ahead of base
        - behind: Number of commits behind base
    """
    try:
        if not worktree_path.exists():
            return None

        # Get current branch
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=10
        )
        branch = result.stdout.strip() if result.returncode == 0 else "unknown"

        # Check for uncommitted changes
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=10
        )
        has_changes = bool(result.stdout.strip()) if result.returncode == 0 else False

        # Get ahead/behind info
        ahead = 0
        behind = 0
        result = subprocess.run(
            ["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split()
            if len(parts) == 2:
                ahead = int(parts[0])
                behind = int(parts[1])

        return {
            "branch": branch,
            "has_changes": has_changes,
            "ahead": ahead,
            "behind": behind
        }

    except Exception as e:
        logger.exception(f"Error getting worktree status for {worktree_path}")
        return None
