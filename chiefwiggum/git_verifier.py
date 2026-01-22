"""Git commit verification across repositories.

This module provides functionality to verify that commits exist in target
repositories, which is critical for ensuring that completed tasks actually
have their changes committed to the project.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Cache for commit verification results (SHA -> (exists, message, timestamp))
_verification_cache: dict[str, tuple[bool, Optional[str], datetime]] = {}
_CACHE_TTL = timedelta(minutes=5)


def _is_cache_valid(timestamp: datetime) -> bool:
    """Check if cache entry is still valid."""
    return datetime.now() - timestamp < _CACHE_TTL


async def verify_commit_in_repo(
    commit_sha: str, repo_path: str | Path = "../tian"
) -> tuple[bool, Optional[str]]:
    """Verify if a commit exists in the specified repository.

    Args:
        commit_sha: The commit SHA to verify
        repo_path: Path to the git repository (relative or absolute)

    Returns:
        Tuple of (exists: bool, commit_message: Optional[str])
        - If commit exists: (True, commit_message)
        - If commit doesn't exist or repo not found: (False, None)

    Note:
        Results are cached for 5 minutes since commit SHAs are immutable.
    """
    # Check cache first
    if commit_sha in _verification_cache:
        exists, message, timestamp = _verification_cache[commit_sha]
        if _is_cache_valid(timestamp):
            logger.debug(f"Cache hit for commit {commit_sha[:8]}")
            return exists, message

    repo_path = Path(repo_path).resolve()

    # Check if repository exists
    if not repo_path.exists() or not (repo_path / ".git").exists():
        logger.warning(f"Repository not found at {repo_path}")
        result = (False, None)
        _verification_cache[commit_sha] = (*result, datetime.now())
        return result

    try:
        # Verify commit exists using git cat-file
        # The ^{commit} syntax ensures it's a commit object
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo_path),
            "cat-file",
            "-e",
            f"{commit_sha}^{{commit}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.error(f"Timeout verifying commit {commit_sha[:8]} in {repo_path}")
            process.kill()
            result = (False, None)
            _verification_cache[commit_sha] = (*result, datetime.now())
            return result

        if process.returncode != 0:
            logger.debug(f"Commit {commit_sha[:8]} not found in {repo_path}")
            result = (False, None)
            _verification_cache[commit_sha] = (*result, datetime.now())
            return result

        # Commit exists, get the message
        message = await get_commit_message(commit_sha, repo_path)
        result = (True, message)
        _verification_cache[commit_sha] = (*result, datetime.now())
        logger.info(f"Verified commit {commit_sha[:8]} in {repo_path}")
        return result

    except Exception as e:
        logger.error(f"Error verifying commit {commit_sha[:8]}: {e}")
        result = (False, None)
        _verification_cache[commit_sha] = (*result, datetime.now())
        return result


async def get_commit_message(
    commit_sha: str, repo_path: str | Path = "../tian"
) -> Optional[str]:
    """Extract the commit message for a given SHA.

    Args:
        commit_sha: The commit SHA
        repo_path: Path to the git repository

    Returns:
        The commit message, or None if extraction failed
    """
    repo_path = Path(repo_path).resolve()

    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo_path),
            "log",
            "-1",
            "--format=%B",
            commit_sha,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=5.0
            )
        except asyncio.TimeoutError:
            logger.error(
                f"Timeout getting commit message for {commit_sha[:8]} in {repo_path}"
            )
            process.kill()
            return None

        if process.returncode != 0:
            logger.warning(
                f"Failed to get commit message for {commit_sha[:8]}: {stderr.decode().strip()}"
            )
            return None

        message = stdout.decode().strip()
        return message if message else None

    except Exception as e:
        logger.error(f"Error getting commit message for {commit_sha[:8]}: {e}")
        return None


def clear_cache():
    """Clear the verification cache. Useful for testing."""
    global _verification_cache
    _verification_cache.clear()
