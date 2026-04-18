"""ChiefWiggum daemon — the reconciler.

A long-running process that keeps ralphs running so the TUI can die freely.
It reads desired state from the database (spawn_requests, cancel_requests,
ralph_instances) and acts on it every tick:

  1. mark stale ralph_instances as crashed
  2. execute pending spawn_requests (spawn the ralph process)
  3. execute pending cancel_requests (kill the ralph process)

A single daemon holds an fcntl lock on `<state>/daemon.lock` so only one
instance runs at a time. Intended to be launched under launchd / systemd with
KeepAlive=true so the OS handles restart-on-crash.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from chiefwiggum.coordination import (
    count_pending_intents,
    fetch_pending_cancel_requests,
    fetch_pending_spawn_requests,
    mark_cancel_request_consumed,
    mark_spawn_request_consumed,
    mark_stale_instances_crashed,
)
from chiefwiggum.database import init_db
from chiefwiggum.paths import get_paths
from chiefwiggum.spawner import spawn_ralph_with_task_claim, stop_ralph_daemon

logger = logging.getLogger("chiefwiggum.daemon")

DEFAULT_TICK_SECONDS = 15


@dataclass
class DaemonStats:
    started_at: datetime = field(default_factory=datetime.now)
    ticks: int = 0
    spawns_executed: int = 0
    cancels_executed: int = 0
    stale_marked_crashed: int = 0
    last_tick_at: datetime | None = None
    last_error: str | None = None


def _daemon_pid_path() -> Path:
    paths = get_paths()
    paths.ensure_dirs()
    return paths.state_dir / "daemon.pid"


def _daemon_lock_path() -> Path:
    paths = get_paths()
    paths.ensure_dirs()
    return paths.state_dir / "daemon.lock"


def _daemon_log_path() -> Path:
    paths = get_paths()
    paths.ensure_dirs()
    return paths.state_dir / "daemon.log"


def is_daemon_running() -> tuple[bool, int | None]:
    """Return (running, pid). Reads the daemon pid file and verifies the process."""
    pid_path = _daemon_pid_path()
    if not pid_path.exists():
        return (False, None)
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return (False, None)
    try:
        os.kill(pid, 0)
        return (True, pid)
    except OSError:
        return (False, pid)


def _acquire_lock() -> int | None:
    """Acquire an exclusive non-blocking lock on the daemon lock file.

    Returns the held fd on success, or None if another daemon holds the lock.
    Caller must keep the fd open for the lifetime of the process.
    """
    lock_path = _daemon_lock_path()
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _write_pid_file() -> None:
    _daemon_pid_path().write_text(f"{os.getpid()}\n")


def _remove_pid_file() -> None:
    try:
        _daemon_pid_path().unlink()
    except FileNotFoundError:
        pass


async def _process_spawn_requests(stats: DaemonStats) -> None:
    """Consume pending spawn_requests and actually spawn the ralphs."""
    from chiefwiggum.cli import generate_ralph_id  # local import: avoid cycle at import time

    requests = await fetch_pending_spawn_requests(limit=10)
    for req in requests:
        project = req["project_path"]
        fix_plan_path = req["fix_plan_path"]
        if not fix_plan_path:
            fix_plan_path = str(Path(project) / "fix_plan.md")

        ralph_id = generate_ralph_id(Path(project).name)
        logger.info(
            "[DAEMON] Consuming spawn_request id=%s project=%s task_id=%s → ralph=%s",
            req["id"], project, req["task_id"], ralph_id,
        )
        try:
            success, message, task_id = await spawn_ralph_with_task_claim(
                ralph_id=ralph_id,
                project=project,
                fix_plan_path=fix_plan_path,
            )
            if success:
                stats.spawns_executed += 1
                await mark_spawn_request_consumed(
                    req["id"], spawned_ralph_id=ralph_id, error=None,
                )
                logger.info("[DAEMON] spawn_request id=%s ok (task_id=%s)", req["id"], task_id)
            else:
                await mark_spawn_request_consumed(
                    req["id"], spawned_ralph_id=None, error=message,
                )
                logger.warning(
                    "[DAEMON] spawn_request id=%s failed: %s", req["id"], message,
                )
        except Exception as e:
            logger.exception("[DAEMON] spawn_request id=%s raised", req["id"])
            await mark_spawn_request_consumed(
                req["id"], spawned_ralph_id=None, error=repr(e),
            )


async def _process_cancel_requests(stats: DaemonStats) -> None:
    """Consume pending cancel_requests by killing the named ralph processes."""
    requests = await fetch_pending_cancel_requests(limit=20)
    for req in requests:
        ralph_id = req["ralph_id"]
        logger.info("[DAEMON] Consuming cancel_request id=%s ralph=%s", req["id"], ralph_id)
        try:
            success, message = stop_ralph_daemon(ralph_id, force=False)
            if success:
                stats.cancels_executed += 1
                await mark_cancel_request_consumed(req["id"], error=None)
                logger.info("[DAEMON] cancel_request id=%s ok", req["id"])
            else:
                await mark_cancel_request_consumed(req["id"], error=message)
                logger.info(
                    "[DAEMON] cancel_request id=%s no-op (%s)", req["id"], message,
                )
        except Exception as e:
            logger.exception("[DAEMON] cancel_request id=%s raised", req["id"])
            await mark_cancel_request_consumed(req["id"], error=repr(e))


async def _tick(stats: DaemonStats) -> None:
    stats.ticks += 1
    stats.last_tick_at = datetime.now()
    try:
        n = await mark_stale_instances_crashed()
        if n:
            stats.stale_marked_crashed += n
            logger.info("[DAEMON] Marked %s stale instance(s) crashed", n)
        await _process_spawn_requests(stats)
        await _process_cancel_requests(stats)
        stats.last_error = None
    except Exception as e:
        stats.last_error = repr(e)
        logger.exception("[DAEMON] tick failed")


async def run_forever(tick_seconds: int = DEFAULT_TICK_SECONDS) -> None:
    """Main reconcile loop. Returns on SIGTERM/SIGINT."""
    stats = DaemonStats()
    stop_event = asyncio.Event()

    def _on_signal(signum: int) -> None:
        logger.info("[DAEMON] Received signal %s, shutting down", signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except NotImplementedError:
            # Windows: add_signal_handler isn't supported; rely on KeyboardInterrupt.
            pass

    await init_db()
    logger.info("[DAEMON] Started (pid=%s, tick=%ss)", os.getpid(), tick_seconds)

    while not stop_event.is_set():
        await _tick(stats)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass

    logger.info(
        "[DAEMON] Stopped. ticks=%s spawns=%s cancels=%s",
        stats.ticks, stats.spawns_executed, stats.cancels_executed,
    )


def _configure_logging(foreground: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if foreground:
        handler: logging.Handler = logging.StreamHandler(sys.stderr)
    else:
        # Simple append-mode file handler. launchd / logrotate handles rotation.
        handler = logging.FileHandler(_daemon_log_path(), mode="a")
    handler.setFormatter(formatter)
    # Avoid duplicate handlers if called twice.
    root.handlers = [handler]


def _daemonize() -> None:
    """POSIX double-fork so the daemon detaches from the controlling terminal.

    Only used when invoked without --foreground and not already under launchd
    (launchd runs the daemon in --foreground mode).
    """
    if os.name != "posix":
        raise RuntimeError("daemonize() is only supported on POSIX systems")

    # First fork
    pid = os.fork()
    if pid > 0:
        # Parent exits immediately — let the caller return.
        os._exit(0)

    os.setsid()

    # Second fork — reparent to init so we can't reacquire a controlling tty.
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    # Redirect std streams to /dev/null.
    with open(os.devnull, "rb", 0) as f_null_in:
        os.dup2(f_null_in.fileno(), 0)
    with open(os.devnull, "ab", 0) as f_null_out:
        os.dup2(f_null_out.fileno(), 1)
        os.dup2(f_null_out.fileno(), 2)


def start_daemon(foreground: bool = False, tick_seconds: int = DEFAULT_TICK_SECONDS) -> int:
    """Start the daemon. Returns a process exit code.

    When `foreground=True`, runs in the current process (suitable for launchd
    and for `wig daemon start --foreground` debugging). When `foreground=False`,
    double-forks first, then runs the reconcile loop in the grandchild.
    """
    running, pid = is_daemon_running()
    if running:
        print(f"chiefwiggum daemon already running (pid={pid})", file=sys.stderr)
        return 1

    # Clean up a stale pid file if the previous daemon crashed.
    if _daemon_pid_path().exists():
        _remove_pid_file()

    if not foreground:
        _daemonize()

    lock_fd = _acquire_lock()
    if lock_fd is None:
        # Another daemon grabbed the lock between our check and now.
        print("chiefwiggum daemon lock held by another process", file=sys.stderr)
        return 1

    _configure_logging(foreground=foreground)
    _write_pid_file()
    try:
        asyncio.run(run_forever(tick_seconds=tick_seconds))
    except KeyboardInterrupt:
        pass
    finally:
        _remove_pid_file()
        try:
            os.close(lock_fd)
        except OSError:
            pass
    return 0


def stop_daemon(timeout_seconds: float = 10.0) -> tuple[bool, str]:
    """Send SIGTERM to the running daemon and wait for it to exit."""
    running, pid = is_daemon_running()
    if not running or pid is None:
        return (False, "daemon not running")

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return (False, f"kill failed: {e}")

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return (True, f"stopped pid={pid}")
        time.sleep(0.2)

    # Escalate to SIGKILL if it didn't stop in time.
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    _remove_pid_file()
    return (True, f"forced stop pid={pid}")


async def daemon_status() -> dict[str, object]:
    """Return a dict describing daemon + queue state, safe to JSON-serialize."""
    running, pid = is_daemon_running()
    intents = await count_pending_intents()
    return {
        "running": running,
        "pid": pid,
        "pid_file": str(_daemon_pid_path()),
        "log_file": str(_daemon_log_path()),
        "pending_spawn_requests": intents["spawn"],
        "pending_cancel_requests": intents["cancel"],
    }
