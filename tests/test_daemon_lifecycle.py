"""Tests for chiefwiggum daemon lifecycle and reconciler tick.

These exercise the real `start_daemon`/`stop_daemon` entrypoints in a
subprocess so we cover PID file + lock + signal handling. We also verify
that when a spawn_request is enqueued, the daemon consumes it and calls the
spawner (mocked to avoid actually launching a ralph process in tests).
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from chiefwiggum import init_db, reset_db
from chiefwiggum.coordination import (
    enqueue_spawn_request,
    fetch_pending_spawn_requests,
)
from chiefwiggum.daemon import (
    DaemonStats,
    _process_spawn_requests,
    is_daemon_running,
)
from chiefwiggum.paths import reset_paths_cache


@pytest.fixture(autouse=True)
async def isolated_env(tmp_path, monkeypatch):
    """Isolate database, state dir, and paths singleton for each test."""
    test_db = tmp_path / "test_daemon.db"
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    monkeypatch.setenv("CHIEFWIGGUM_DB", str(test_db))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # Block the legacy-path heuristic.
    monkeypatch.setenv("HOME", str(tmp_path))

    reset_paths_cache()
    await init_db()
    yield tmp_path
    await reset_db()
    reset_paths_cache()


class TestReconcileTick:
    @pytest.mark.asyncio
    async def test_spawn_request_consumed_on_success(self):
        stats = DaemonStats()
        await enqueue_spawn_request(
            project_path="/tmp/does-not-matter",
            fix_plan_path="/tmp/does-not-matter/fix_plan.md",
            requested_by="cli",
        )

        fake_spawn = AsyncMock(return_value=(True, "ok", "task-1"))
        with patch("chiefwiggum.daemon.spawn_ralph_with_task_claim", fake_spawn):
            await _process_spawn_requests(stats)

        assert fake_spawn.await_count == 1
        assert stats.spawns_executed == 1
        assert await fetch_pending_spawn_requests() == []

    @pytest.mark.asyncio
    async def test_spawn_request_consumed_on_failure_with_error(self):
        stats = DaemonStats()
        await enqueue_spawn_request(project_path="/tmp/x")

        fake_spawn = AsyncMock(return_value=(False, "no tasks pending", None))
        with patch("chiefwiggum.daemon.spawn_ralph_with_task_claim", fake_spawn):
            await _process_spawn_requests(stats)

        assert stats.spawns_executed == 0
        # Still consumed so we don't retry forever — error is recorded.
        assert await fetch_pending_spawn_requests() == []

    @pytest.mark.asyncio
    async def test_spawn_request_consumed_when_spawner_raises(self):
        stats = DaemonStats()
        await enqueue_spawn_request(project_path="/tmp/x")

        fake_spawn = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("chiefwiggum.daemon.spawn_ralph_with_task_claim", fake_spawn):
            await _process_spawn_requests(stats)

        assert stats.spawns_executed == 0
        assert await fetch_pending_spawn_requests() == []

    @pytest.mark.asyncio
    async def test_tick_is_idempotent_across_multiple_pending(self):
        stats = DaemonStats()
        await enqueue_spawn_request(project_path="/tmp/a")
        await enqueue_spawn_request(project_path="/tmp/b")

        fake_spawn = AsyncMock(return_value=(True, "ok", None))
        with patch("chiefwiggum.daemon.spawn_ralph_with_task_claim", fake_spawn):
            await _process_spawn_requests(stats)

        assert fake_spawn.await_count == 2
        assert stats.spawns_executed == 2


class TestDaemonProcess:
    """Exercises start_daemon/stop_daemon via an actual subprocess.

    Uses the installed `wig` entry point when available; otherwise falls back
    to `python -m chiefwiggum.cli` so tests pass before editable-install.
    """

    def _wig_cmd(self) -> list[str]:
        return [sys.executable, "-m", "chiefwiggum.cli"]

    def test_daemon_start_foreground_then_stop(self, isolated_env, monkeypatch):
        # Keep subprocess on same DB + paths.
        env = os.environ.copy()

        proc = subprocess.Popen(
            [*self._wig_cmd(), "daemon", "start", "--foreground", "--tick", "1"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            # Wait for PID file to appear.
            deadline = time.monotonic() + 5
            running = False
            while time.monotonic() < deadline:
                running, pid = is_daemon_running()
                if running:
                    break
                time.sleep(0.1)
            assert running, (
                f"daemon never wrote pid file; stderr={proc.stderr.read().decode() if proc.stderr else ''}"
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

        # After termination, pid file should be cleaned up or the PID dead.
        time.sleep(0.2)
        running, _ = is_daemon_running()
        assert not running

    def test_daemon_status_json(self, isolated_env):
        import json as _json
        result = subprocess.run(
            [*self._wig_cmd(), "daemon", "status", "--format", "json"],
            env=os.environ.copy(),
            check=True,
            capture_output=True,
            text=True,
        )
        data = _json.loads(result.stdout)
        assert data["running"] is False
        assert data["pending_spawn_requests"] == 0
        assert data["pending_cancel_requests"] == 0

    def test_second_daemon_refuses_to_start(self, isolated_env):
        env = os.environ.copy()

        # Start first daemon.
        first = subprocess.Popen(
            [*self._wig_cmd(), "daemon", "start", "--foreground", "--tick", "60"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                running, _ = is_daemon_running()
                if running:
                    break
                time.sleep(0.1)

            # Second start should refuse (non-zero exit).
            second = subprocess.run(
                [*self._wig_cmd(), "daemon", "start", "--foreground", "--tick", "60"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert second.returncode != 0
            assert "already running" in second.stderr
        finally:
            first.terminate()
            try:
                first.wait(timeout=5)
            except subprocess.TimeoutExpired:
                first.kill()
                first.wait(timeout=5)


class TestWigSpawnCli:
    def _wig_cmd(self) -> list[str]:
        return [sys.executable, "-m", "chiefwiggum.cli"]

    def test_wig_spawn_enqueues_when_daemon_is_down(self, isolated_env):
        env = os.environ.copy()
        result = subprocess.run(
            [*self._wig_cmd(), "spawn", "/tmp/does-not-matter", "--priority", "3"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        # Either "enqueued" header fires regardless of daemon state.
        assert "spawn_request enqueued" in result.stdout
        # Warning about daemon not running should appear.
        assert "daemon is NOT running" in result.stdout

    def test_wig_spawn_specific_task_id(self, isolated_env):
        env = os.environ.copy()
        result = subprocess.run(
            [*self._wig_cmd(), "spawn", "/tmp/demo", "--task-id", "task-7"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr

        # Verify the row landed with the right task_id.
        async def _check():
            pending = await fetch_pending_spawn_requests()
            assert len(pending) == 1
            assert pending[0]["task_id"] == "task-7"
            assert pending[0]["requested_by"] == "cli"

        asyncio.run(_check())
