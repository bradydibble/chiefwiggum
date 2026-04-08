"""Tests for git commit verification and caching."""

import asyncio
import subprocess

import pytest

from chiefwiggum.git_verifier import (
    _verification_cache,
    clear_cache,
    verify_commit_in_repo,
)


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo with one commit. Returns (repo_path, commit_sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True
        )

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    git("config", "commit.gpgsign", "false")

    (repo / "README.md").write_text("hello")
    git("add", ".")
    git("commit", "-m", "Initial commit")

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    sha = result.stdout.strip()
    return repo, sha


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear verification cache before and after each test."""
    clear_cache()
    yield
    clear_cache()


async def test_verify_existing_commit_returns_true(git_repo):
    repo, sha = git_repo
    exists, message = await verify_commit_in_repo(sha, repo_path=repo)
    assert exists is True
    assert message is not None
    assert "Initial commit" in message


async def test_verify_nonexistent_sha_returns_false(git_repo):
    repo, _ = git_repo
    fake_sha = "a" * 40
    exists, message = await verify_commit_in_repo(fake_sha, repo_path=repo)
    assert exists is False
    assert message is None


async def test_verify_missing_repo_returns_false(tmp_path):
    nonexistent = tmp_path / "no_such_repo"
    exists, message = await verify_commit_in_repo("abc123", repo_path=nonexistent)
    assert exists is False
    assert message is None


async def test_cache_prevents_duplicate_git_calls(git_repo, monkeypatch):
    repo, sha = git_repo

    call_count = 0
    original_exec = asyncio.create_subprocess_exec

    async def counting_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return await original_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting_exec)

    # First call — subprocess is used
    exists1, msg1 = await verify_commit_in_repo(sha, repo_path=repo)
    calls_after_first = call_count
    assert calls_after_first > 0

    # Second call with same SHA — served from cache, no new subprocess calls
    exists2, msg2 = await verify_commit_in_repo(sha, repo_path=repo)
    assert call_count == calls_after_first

    assert exists1 == exists2 is True
    assert msg1 == msg2


async def test_clear_cache_forces_fresh_lookup(git_repo, monkeypatch):
    repo, sha = git_repo

    call_count = 0
    original_exec = asyncio.create_subprocess_exec

    async def counting_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return await original_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting_exec)

    await verify_commit_in_repo(sha, repo_path=repo)
    calls_after_first = call_count

    # Cache cleared — next verify must hit subprocess again
    clear_cache()
    await verify_commit_in_repo(sha, repo_path=repo)
    assert call_count > calls_after_first


async def test_result_is_cached_after_verify(git_repo):
    repo, sha = git_repo

    assert sha not in _verification_cache
    await verify_commit_in_repo(sha, repo_path=repo)
    assert sha in _verification_cache

    cached_exists, cached_msg, _ = _verification_cache[sha]
    assert cached_exists is True
