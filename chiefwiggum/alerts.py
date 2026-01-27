"""Health check alerts for task completion monitoring.

This module provides alerting functionality to detect issues with task completion
detection and Ralph health.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from chiefwiggum.monitoring import get_completion_metrics, get_detection_stats
from chiefwiggum.database import get_connection

logger = logging.getLogger(__name__)


async def check_completion_health() -> list[str]:
    """Check for completion detection issues and return alerts.

    Returns:
        List of alert messages (empty list if all healthy)
    """
    alerts = []

    # Get metrics for last 6 hours
    metrics = await get_completion_metrics(hours=6)

    # Alert: Detection rate too low
    if metrics.total_completions > 0 and metrics.detection_rate() < 90.0:
        alerts.append(
            f"⚠️ LOW DETECTION RATE: {metrics.detection_rate():.1f}% "
            f"({metrics.failed_detections} failed in last 6 hours)"
        )

    # Alert: Too many auto-recoveries (indicates RALPH_STATUS not being output)
    if metrics.auto_recovery > metrics.explicit_ralph_status:
        alerts.append(
            f"⚠️ HIGH AUTO-RECOVERY RATE: {metrics.auto_recovery} auto-recoveries vs "
            f"{metrics.explicit_ralph_status} explicit completions. "
            "Ralphs may not be outputting RALPH_STATUS blocks correctly."
        )

    # Alert: No completions in 2 hours (if Ralphs are running)
    if metrics.total_completions == 0:
        # Check if any Ralphs are active
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM ralph_instances WHERE status = 'ACTIVE'"
        )
        active_count = (await cursor.fetchone())[0]

        if active_count > 0:
            alerts.append(
                f"🚨 NO COMPLETIONS in last 6 hours despite {active_count} active Ralph(s). "
                "Check if Ralphs are stuck!"
            )

    return alerts


async def check_commit_sha_health() -> list[str]:
    """Check for issues with commit SHA tracking.

    Returns:
        List of alert messages
    """
    alerts = []

    # Get detection stats for last 24 hours
    stats = await get_detection_stats(since_date=datetime.now() - timedelta(hours=24))

    total = stats["total_completions"]
    with_commit = stats["with_commit_sha"]
    commit_rate = stats["commit_sha_rate"]

    # Alert: Low commit SHA rate
    if total > 0 and commit_rate < 80.0:
        alerts.append(
            f"⚠️ LOW COMMIT SHA RATE: Only {commit_rate:.1f}% of completions have commit SHAs "
            f"({with_commit}/{total}). Verify Ralphs are committing changes."
        )

    # Alert: No commit SHAs at all
    if total > 5 and with_commit == 0:
        alerts.append(
            "🚨 NO COMMIT SHAS: None of the last {total} completions have commit SHAs! "
            "Check git configuration and Ralph prompts."
        )

    return alerts


async def check_ralph_health() -> list[str]:
    """Check for issues with Ralph instances.

    Returns:
        List of alert messages
    """
    alerts = []
    conn = await get_connection()

    # Check for stale Ralph instances (heartbeat > 10 minutes old)
    stale_threshold = datetime.now() - timedelta(minutes=10)
    cursor = await conn.execute(
        """
        SELECT ralph_id, last_heartbeat, current_task_id
        FROM ralph_instances
        WHERE status = 'ACTIVE'
        AND last_heartbeat < ?
        """,
        (stale_threshold,),
    )

    stale_ralphs = await cursor.fetchall()
    if stale_ralphs:
        for ralph_id, last_heartbeat, task_id in stale_ralphs:
            alerts.append(
                f"⚠️ STALE RALPH: {ralph_id} (last heartbeat: {last_heartbeat}, "
                f"working on: {task_id or 'no task'})"
            )

    # Check for Ralphs stuck on same task for too long (> 2 hours)
    stuck_threshold = datetime.now() - timedelta(hours=2)
    cursor = await conn.execute(
        """
        SELECT ri.ralph_id, ri.current_task_id, tc.claimed_at, tc.task_title
        FROM ralph_instances ri
        JOIN task_claims tc ON ri.current_task_id = tc.task_id
        WHERE ri.status = 'ACTIVE'
        AND tc.status = 'in_progress'
        AND tc.claimed_at < ?
        """,
        (stuck_threshold,),
    )

    stuck_ralphs = await cursor.fetchall()
    if stuck_ralphs:
        for ralph_id, task_id, claimed_at, task_title in stuck_ralphs:
            duration = datetime.now() - datetime.fromisoformat(claimed_at)
            hours = duration.total_seconds() / 3600
            alerts.append(
                f"⚠️ STUCK RALPH: {ralph_id} working on '{task_title}' "
                f"for {hours:.1f} hours (claimed: {claimed_at})"
            )

    # Check for crashed Ralph instances
    cursor = await conn.execute(
        """
        SELECT ralph_id
        FROM ralph_instances
        WHERE status = 'CRASHED'
        AND started_at > ?
        """,
        (datetime.now() - timedelta(hours=24),),
    )

    crashed_ralphs = await cursor.fetchall()
    if crashed_ralphs:
        for (ralph_id,) in crashed_ralphs:
            alerts.append(
                f"🚨 CRASHED RALPH: {ralph_id}"
            )

    return alerts


async def check_task_backlog() -> list[str]:
    """Check for task backlog issues.

    Returns:
        List of alert messages
    """
    alerts = []
    conn = await get_connection()

    # Check for high number of pending HIGH priority tasks
    cursor = await conn.execute(
        """
        SELECT COUNT(*)
        FROM task_claims
        WHERE status = 'pending'
        AND task_priority = 'HIGH'
        """
    )
    high_priority_count = (await cursor.fetchone())[0]

    if high_priority_count > 10:
        alerts.append(
            f"⚠️ HIGH PRIORITY BACKLOG: {high_priority_count} HIGH priority tasks pending. "
            "Consider spawning more Ralphs."
        )

    # Check for tasks stuck in in_progress for > 4 hours
    stuck_threshold = datetime.now() - timedelta(hours=4)
    cursor = await conn.execute(
        """
        SELECT COUNT(*)
        FROM task_claims
        WHERE status = 'in_progress'
        AND claimed_at < ?
        """,
        (stuck_threshold,),
    )
    stuck_task_count = (await cursor.fetchone())[0]

    if stuck_task_count > 0:
        alerts.append(
            f"⚠️ STUCK TASKS: {stuck_task_count} tasks in_progress for > 4 hours. "
            "Check if Ralphs are making progress."
        )

    return alerts


async def get_all_alerts() -> dict[str, list[str]]:
    """Get all health check alerts grouped by category.

    Returns:
        Dictionary mapping category to list of alerts
    """
    return {
        "completion_detection": await check_completion_health(),
        "commit_tracking": await check_commit_sha_health(),
        "ralph_health": await check_ralph_health(),
        "task_backlog": await check_task_backlog(),
    }


async def has_critical_alerts() -> bool:
    """Check if there are any critical alerts.

    Returns:
        True if there are any alerts with 🚨 (critical) marker
    """
    all_alerts = await get_all_alerts()

    for category_alerts in all_alerts.values():
        for alert in category_alerts:
            if "🚨" in alert:
                return True

    return False


def format_alerts_summary(alerts_dict: dict[str, list[str]]) -> str:
    """Format all alerts as a human-readable summary.

    Args:
        alerts_dict: Dictionary from get_all_alerts()

    Returns:
        Formatted string summary
    """
    lines = ["ChiefWiggum Health Check", "=" * 50, ""]

    total_alerts = sum(len(alerts) for alerts in alerts_dict.values())

    if total_alerts == 0:
        lines.append("✅ All systems healthy - no alerts")
        return "\n".join(lines)

    lines.append(f"⚠️  Found {total_alerts} alert(s):")
    lines.append("")

    for category, alerts in alerts_dict.items():
        if alerts:
            lines.append(f"{category.replace('_', ' ').title()}:")
            for alert in alerts:
                lines.append(f"  {alert}")
            lines.append("")

    return "\n".join(lines)


async def log_alerts_to_file(log_file: Optional[str] = None) -> None:
    """Log all alerts to a file.

    Args:
        log_file: Path to log file (default: ~/.chiefwiggum/alerts.log)
    """
    if log_file is None:
        from chiefwiggum.paths import get_paths
        log_file = str(get_paths().base_dir / "alerts.log")

    alerts = await get_all_alerts()
    summary = format_alerts_summary(alerts)

    # Append to log file with timestamp
    with open(log_file, "a") as f:
        f.write(f"\n{'=' * 60}\n")
        f.write(f"Alert Check: {datetime.now().isoformat()}\n")
        f.write(f"{'=' * 60}\n")
        f.write(summary)
        f.write("\n")

    # Also log to Python logger
    if any(alerts.values()):
        logger.warning(f"Health check found {sum(len(a) for a in alerts.values())} alert(s)")
        for category, category_alerts in alerts.items():
            for alert in category_alerts:
                logger.warning(f"[{category}] {alert}")
