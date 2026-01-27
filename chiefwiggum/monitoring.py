"""Production monitoring for task completion detection.

This module provides metrics and monitoring for tracking how well ChiefWiggum
detects task completions from Ralph instances.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from chiefwiggum.database import get_connection

logger = logging.getLogger(__name__)


@dataclass
class CompletionMetrics:
    """Metrics for task completion detection."""

    total_completions: int = 0
    explicit_ralph_status: int = 0  # Detected via RALPH_STATUS block
    legacy_markers: int = 0  # Detected via TASK_COMPLETE
    auto_recovery: int = 0  # Detected via commit parsing
    failed_detections: int = 0  # No marker found

    avg_confidence_score: float = 0.0

    def detection_rate(self) -> float:
        """Percentage of successful detections.

        Returns:
            Detection rate as a percentage (0-100)
        """
        if self.total_completions == 0:
            return 0.0
        success = self.explicit_ralph_status + self.legacy_markers + self.auto_recovery
        return (success / self.total_completions) * 100

    def explicit_rate(self) -> float:
        """Percentage of completions using explicit RALPH_STATUS blocks.

        This is the preferred method.
        """
        if self.total_completions == 0:
            return 0.0
        return (self.explicit_ralph_status / self.total_completions) * 100

    def auto_recovery_rate(self) -> float:
        """Percentage of completions detected via auto-recovery.

        High auto-recovery rate indicates Ralphs aren't outputting RALPH_STATUS blocks.
        """
        if self.total_completions == 0:
            return 0.0
        return (self.auto_recovery / self.total_completions) * 100


async def get_completion_metrics(hours: int = 24) -> CompletionMetrics:
    """Get completion detection metrics for the last N hours.

    Args:
        hours: Number of hours to look back (default: 24)

    Returns:
        CompletionMetrics object with aggregated statistics
    """
    conn = await get_connection()
    since = datetime.now() - timedelta(hours=hours)

    # Query task_claims for completions
    cursor = await conn.execute(
        """
        SELECT completion_message, error_message
        FROM task_claims
        WHERE completed_at >= ?
        AND status = 'completed'
        """,
        (since,),
    )

    metrics = CompletionMetrics()

    for row in await cursor.fetchall():
        metrics.total_completions += 1
        message = row[0] or ""

        # Classify detection method based on completion message
        if "RALPH_STATUS" in message:
            metrics.explicit_ralph_status += 1
        elif "Auto-recovered" in message or "auto-recovery" in message.lower():
            metrics.auto_recovery += 1
        elif "TASK_COMPLETE" in message:
            metrics.legacy_markers += 1
        # If no clear indicator, count as successful detection
        # (we don't track failed_detections in current schema)

    return metrics


async def get_detection_stats(since_date: Optional[datetime] = None) -> dict:
    """Get detailed statistics about task completion detection.

    Args:
        since_date: Start date for statistics (default: last 7 days)

    Returns:
        Dictionary with detection statistics
    """
    if since_date is None:
        since_date = datetime.now() - timedelta(days=7)

    conn = await get_connection()

    # Get overall completion stats
    cursor = await conn.execute(
        """
        SELECT
            COUNT(*) as total,
            COUNT(CASE WHEN git_commit_sha IS NOT NULL THEN 1 END) as with_commit,
            COUNT(CASE WHEN git_commit_sha IS NULL THEN 1 END) as without_commit,
            AVG(CAST((julianday(completed_at) - julianday(claimed_at)) * 24 * 60 AS REAL)) as avg_duration_minutes
        FROM task_claims
        WHERE status = 'completed'
        AND completed_at >= ?
        """,
        (since_date,),
    )

    row = await cursor.fetchone()

    return {
        "total_completions": row[0] or 0,
        "with_commit_sha": row[1] or 0,
        "without_commit_sha": row[2] or 0,
        "avg_duration_minutes": round(row[3], 2) if row[3] else 0,
        "commit_sha_rate": round((row[1] / row[0] * 100), 2) if row[0] > 0 else 0,
    }


async def get_ralph_completion_stats(ralph_id: str, hours: int = 24) -> dict:
    """Get completion statistics for a specific Ralph instance.

    Args:
        ralph_id: Ralph instance ID
        hours: Number of hours to look back

    Returns:
        Dictionary with Ralph-specific completion stats
    """
    conn = await get_connection()
    since = datetime.now() - timedelta(hours=hours)

    # Get task completions by this Ralph
    cursor = await conn.execute(
        """
        SELECT
            COUNT(*) as total,
            COUNT(CASE WHEN git_commit_sha IS NOT NULL THEN 1 END) as with_commit,
            MIN(completed_at) as first_completion,
            MAX(completed_at) as last_completion
        FROM task_claims
        WHERE claimed_by_ralph_id = ?
        AND status = 'completed'
        AND completed_at >= ?
        """,
        (ralph_id, since),
    )

    row = await cursor.fetchone()

    total = row[0] or 0
    with_commit = row[1] or 0

    return {
        "ralph_id": ralph_id,
        "total_completions": total,
        "with_commit_sha": with_commit,
        "without_commit_sha": total - with_commit,
        "first_completion": row[2],
        "last_completion": row[3],
        "commit_sha_rate": round((with_commit / total * 100), 2) if total > 0 else 0,
    }


async def get_recent_completions(limit: int = 10) -> list[dict]:
    """Get the most recent task completions.

    Args:
        limit: Number of completions to return

    Returns:
        List of completion dictionaries
    """
    conn = await get_connection()

    cursor = await conn.execute(
        """
        SELECT
            task_id,
            task_title,
            claimed_by_ralph_id,
            completed_at,
            git_commit_sha,
            completion_message
        FROM task_claims
        WHERE status = 'completed'
        ORDER BY completed_at DESC
        LIMIT ?
        """,
        (limit,),
    )

    completions = []
    for row in await cursor.fetchall():
        completions.append(
            {
                "task_id": row[0],
                "task_title": row[1],
                "ralph_id": row[2],
                "completed_at": row[3],
                "git_commit_sha": row[4],
                "completion_message": row[5],
                "has_commit": row[4] is not None,
            }
        )

    return completions


async def get_completion_rate_by_hour() -> dict[int, int]:
    """Get task completion rate grouped by hour of day.

    Returns:
        Dictionary mapping hour (0-23) to completion count
    """
    conn = await get_connection()

    # Get completions from last 7 days grouped by hour
    since = datetime.now() - timedelta(days=7)

    cursor = await conn.execute(
        """
        SELECT
            CAST(strftime('%H', completed_at) AS INTEGER) as hour,
            COUNT(*) as count
        FROM task_claims
        WHERE status = 'completed'
        AND completed_at >= ?
        GROUP BY hour
        ORDER BY hour
        """,
        (since,),
    )

    hourly_counts = {hour: 0 for hour in range(24)}

    for row in await cursor.fetchall():
        hour = row[0]
        count = row[1]
        hourly_counts[hour] = count

    return hourly_counts


async def get_ralph_performance_leaderboard(hours: int = 24) -> list[dict]:
    """Get leaderboard of Ralph instances by task completion.

    Args:
        hours: Number of hours to look back

    Returns:
        List of Ralph stats sorted by completion count
    """
    conn = await get_connection()
    since = datetime.now() - timedelta(hours=hours)

    cursor = await conn.execute(
        """
        SELECT
            claimed_by_ralph_id,
            COUNT(*) as completions,
            COUNT(CASE WHEN git_commit_sha IS NOT NULL THEN 1 END) as with_commit,
            AVG(CAST((julianday(completed_at) - julianday(claimed_at)) * 24 * 60 AS REAL)) as avg_minutes
        FROM task_claims
        WHERE status = 'completed'
        AND completed_at >= ?
        GROUP BY claimed_by_ralph_id
        ORDER BY completions DESC
        """,
        (since,),
    )

    leaderboard = []
    for row in await cursor.fetchall():
        total = row[1]
        with_commit = row[2]
        leaderboard.append(
            {
                "ralph_id": row[0],
                "completions": total,
                "with_commit_sha": with_commit,
                "commit_rate": round((with_commit / total * 100), 2) if total > 0 else 0,
                "avg_duration_minutes": round(row[3], 2) if row[3] else 0,
            }
        )

    return leaderboard


def format_metrics_summary(metrics: CompletionMetrics) -> str:
    """Format metrics as a human-readable summary.

    Args:
        metrics: CompletionMetrics object

    Returns:
        Formatted string summary
    """
    lines = [
        f"Total Completions: {metrics.total_completions}",
        f"Detection Rate: {metrics.detection_rate():.1f}%",
        "",
        "Detection Methods:",
        f"  RALPH_STATUS blocks: {metrics.explicit_ralph_status} ({metrics.explicit_rate():.1f}%)",
        f"  Legacy markers: {metrics.legacy_markers}",
        f"  Auto-recovery: {metrics.auto_recovery} ({metrics.auto_recovery_rate():.1f}%)",
    ]

    if metrics.failed_detections > 0:
        lines.append(f"  Failed detections: {metrics.failed_detections}")

    return "\n".join(lines)


def track_jq_validation_failure(ralph_id: str, variable_name: str, value: str) -> None:
    """Track jq validation failures for monitoring.

    This function logs when variables fail validation before being passed to jq --argjson.
    Useful for identifying patterns in jq crashes and validation issues.

    Args:
        ralph_id: The Ralph instance ID where the failure occurred
        variable_name: Name of the variable that failed validation
        value: The invalid value that was provided

    Example:
        track_jq_validation_failure("ralph-123", "loop_number", "")
    """
    logger.warning(
        f"jq validation failure: {ralph_id} - {variable_name}={repr(value)}",
        extra={
            "ralph_id": ralph_id,
            "variable_name": variable_name,
            "invalid_value": value,
            "failure_type": "jq_validation",
        },
    )


async def get_jq_validation_failures(hours: int = 24) -> dict[str, int]:
    """Get statistics on jq validation failures.

    This would require log parsing or a dedicated metrics table.
    For now, returns placeholder data structure.

    Args:
        hours: Number of hours to look back

    Returns:
        Dictionary mapping variable names to failure counts
    """
    # TODO: Implement if we add a metrics table for validation failures
    return {
        "loop_number": 0,
        "files_modified": 0,
        "confidence_score": 0,
        "exit_signal": 0,
        "has_completion_signal": 0,
    }
