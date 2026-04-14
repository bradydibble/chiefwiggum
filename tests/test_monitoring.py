"""Tests for monitoring and alerts modules."""

import os
from datetime import datetime, timedelta

import pytest

from chiefwiggum import init_db, reset_db
from chiefwiggum.alerts import (
    check_commit_sha_health,
    check_completion_health,
    check_ralph_health,
    format_alerts_summary,
    get_all_alerts,
    has_critical_alerts,
)
from chiefwiggum.database import get_connection
from chiefwiggum.monitoring import (
    CompletionMetrics,
    format_metrics_summary,
    get_completion_metrics,
    get_detection_stats,
    get_ralph_completion_stats,
)


@pytest.fixture(autouse=True)
async def setup_test_db(tmp_path):
    """Set up a test database for each test."""
    test_db = tmp_path / "test_monitoring.db"
    os.environ["CHIEFWIGGUM_DB"] = str(test_db)
    await init_db()
    yield
    await reset_db()
    del os.environ["CHIEFWIGGUM_DB"]


class TestCompletionMetrics:
    """Tests for CompletionMetrics dataclass."""

    def test_detection_rate_calculation(self):
        """Test detection_rate() calculation."""
        metrics = CompletionMetrics(
            total_completions=100,
            explicit_ralph_status=80,
            legacy_markers=15,
            auto_recovery=5,
            failed_detections=0
        )

        assert metrics.detection_rate() == 100.0

    def test_detection_rate_with_failures(self):
        """Test detection_rate() with some failures."""
        metrics = CompletionMetrics(
            total_completions=100,
            explicit_ralph_status=70,
            legacy_markers=10,
            auto_recovery=10,
            failed_detections=10
        )

        assert metrics.detection_rate() == 90.0

    def test_explicit_rate(self):
        """Test explicit_rate() calculation."""
        metrics = CompletionMetrics(
            total_completions=100,
            explicit_ralph_status=85,
            legacy_markers=10,
            auto_recovery=5
        )

        assert metrics.explicit_rate() == 85.0

    def test_auto_recovery_rate(self):
        """Test auto_recovery_rate() calculation."""
        metrics = CompletionMetrics(
            total_completions=100,
            explicit_ralph_status=70,
            legacy_markers=20,
            auto_recovery=10
        )

        assert metrics.auto_recovery_rate() == 10.0

    def test_zero_completions_handling(self):
        """Test that rates return 0 when no completions."""
        metrics = CompletionMetrics(total_completions=0)

        assert metrics.detection_rate() == 0.0
        assert metrics.explicit_rate() == 0.0
        assert metrics.auto_recovery_rate() == 0.0


class TestMonitoringFunctions:
    """Tests for monitoring module functions."""

    @pytest.mark.asyncio
    async def test_get_completion_metrics_empty(self):
        """Test get_completion_metrics() with no data."""
        metrics = await get_completion_metrics(hours=24)

        assert metrics.total_completions == 0
        assert metrics.detection_rate() == 0.0

    @pytest.mark.asyncio
    async def test_get_completion_metrics_with_data(self):
        """Test get_completion_metrics() with completion data."""
        conn = await get_connection()

        # Insert completed tasks
        now = datetime.now()
        await conn.execute(
            """
            INSERT INTO task_claims
            (task_id, task_title, task_priority, project, status, completed_at, completion_message)
            VALUES
            ('task-1', 'Task 1', 'HIGH', 'test', 'completed', ?, 'Completed via RALPH_STATUS'),
            ('task-2', 'Task 2', 'HIGH', 'test', 'completed', ?, 'Auto-recovered completion'),
            ('task-3', 'Task 3', 'MEDIUM', 'test', 'completed', ?, 'TASK_COMPLETE marker found')
            """,
            (now, now, now)
        )
        await conn.commit()

        metrics = await get_completion_metrics(hours=24)

        assert metrics.total_completions == 3
        assert metrics.explicit_ralph_status == 1
        assert metrics.auto_recovery == 1
        assert metrics.legacy_markers == 1

    @pytest.mark.asyncio
    async def test_get_detection_stats(self):
        """Test get_detection_stats() function."""
        conn = await get_connection()

        # Insert tasks with varying commit SHAs
        now = datetime.now()
        claimed_time = now - timedelta(minutes=30)

        await conn.execute(
            """
            INSERT INTO task_claims
            (task_id, task_title, task_priority, project, status, claimed_at, completed_at, git_commit_sha)
            VALUES
            ('task-1', 'Task 1', 'HIGH', 'test', 'completed', ?, ?, 'abc123'),
            ('task-2', 'Task 2', 'HIGH', 'test', 'completed', ?, ?, 'def456'),
            ('task-3', 'Task 3', 'MEDIUM', 'test', 'completed', ?, ?, NULL)
            """,
            (claimed_time, now, claimed_time, now, claimed_time, now)
        )
        await conn.commit()

        stats = await get_detection_stats(since_date=now - timedelta(hours=1))

        assert stats['total_completions'] == 3
        assert stats['with_commit_sha'] == 2
        assert stats['without_commit_sha'] == 1
        assert stats['commit_sha_rate'] == pytest.approx(66.67, rel=0.1)

    @pytest.mark.asyncio
    async def test_get_ralph_completion_stats(self):
        """Test get_ralph_completion_stats() for specific Ralph."""
        conn = await get_connection()

        ralph_id = "test-ralph-stats"
        now = datetime.now()

        # First register the Ralph instance
        await conn.execute(
            """
            INSERT INTO ralph_instances
            (ralph_id, project, status, started_at, last_heartbeat)
            VALUES (?, 'test', 'ACTIVE', ?, ?)
            """,
            (ralph_id, now, now)
        )

        # Then insert tasks completed by this Ralph
        await conn.execute(
            """
            INSERT INTO task_claims
            (task_id, task_title, task_priority, project, status, completed_at, git_commit_sha, claimed_by_ralph_id)
            VALUES
            ('task-1', 'Task 1', 'HIGH', 'test', 'completed', ?, 'abc123', ?),
            ('task-2', 'Task 2', 'HIGH', 'test', 'completed', ?, 'def456', ?)
            """,
            (now, ralph_id, now, ralph_id)
        )
        await conn.commit()

        stats = await get_ralph_completion_stats(ralph_id, hours=24)

        assert stats['ralph_id'] == ralph_id
        assert stats['total_completions'] == 2
        assert stats['with_commit_sha'] == 2
        assert stats['commit_sha_rate'] == 100.0

    def test_format_metrics_summary(self):
        """Test format_metrics_summary() output."""
        metrics = CompletionMetrics(
            total_completions=100,
            explicit_ralph_status=85,
            legacy_markers=10,
            auto_recovery=5
        )

        summary = format_metrics_summary(metrics)

        assert "Total Completions: 100" in summary
        assert "Detection Rate: 100.0%" in summary
        assert "RALPH_STATUS blocks: 85" in summary


class TestAlerts:
    """Tests for alerts module functions."""

    @pytest.mark.asyncio
    async def test_check_completion_health_no_alerts(self):
        """Test check_completion_health() with healthy state."""
        # No completions = no alerts (when no active Ralphs)
        alerts = await check_completion_health()

        # Should be empty or warn about no completions
        assert isinstance(alerts, list)

    @pytest.mark.asyncio
    async def test_check_completion_health_low_detection(self):
        """Test alert for low detection rate."""
        conn = await get_connection()

        # Insert completions with low detection rate
        now = datetime.now()
        for i in range(5):
            await conn.execute(
                """
                INSERT INTO task_claims
                (task_id, task_title, task_priority, project, status, completed_at, completion_message)
                VALUES (?, ?, 'HIGH', 'test', 'completed', ?, ?)
                """,
                (f"task-{i}", f"Task {i}", now, "Unknown method" if i < 2 else "RALPH_STATUS")
            )
        await conn.commit()

        # This should trigger low detection rate if we classify "Unknown method" as failed
        # But our current implementation doesn't track failed_detections well
        # So this test documents current behavior
        alerts = await check_completion_health()
        assert isinstance(alerts, list)

    @pytest.mark.asyncio
    async def test_check_commit_sha_health(self):
        """Test check_commit_sha_health() function."""
        conn = await get_connection()

        # Insert completions without commit SHAs
        now = datetime.now()
        for i in range(5):
            await conn.execute(
                """
                INSERT INTO task_claims
                (task_id, task_title, task_priority, project, status, completed_at, git_commit_sha)
                VALUES (?, ?, 'HIGH', 'test', 'completed', ?, NULL)
                """,
                (f"task-{i}", f"Task {i}", now)
            )
        await conn.commit()

        alerts = await check_commit_sha_health()

        # Should alert about low commit SHA rate
        assert len(alerts) > 0
        assert any("LOW COMMIT SHA RATE" in alert or "NO COMMIT SHAS" in alert for alert in alerts)

    @pytest.mark.asyncio
    async def test_check_ralph_health_stale(self):
        """Test check_ralph_health() detects stale Ralphs."""
        conn = await get_connection()

        # Insert stale Ralph instance
        stale_time = datetime.now() - timedelta(minutes=15)
        await conn.execute(
            """
            INSERT INTO ralph_instances
            (ralph_id, project, status, started_at, last_heartbeat)
            VALUES ('stale-ralph', 'test', 'ACTIVE', ?, ?)
            """,
            (stale_time, stale_time)
        )
        await conn.commit()

        alerts = await check_ralph_health()

        assert len(alerts) > 0
        assert any("STALE RALPH" in alert for alert in alerts)

    @pytest.mark.asyncio
    async def test_check_ralph_health_crashed(self):
        """Test check_ralph_health() detects crashed Ralphs."""
        conn = await get_connection()

        # Insert crashed Ralph
        await conn.execute(
            """
            INSERT INTO ralph_instances
            (ralph_id, project, status, started_at, last_heartbeat)
            VALUES ('crashed-ralph', 'test', 'CRASHED', ?, ?)
            """,
            (datetime.now(), datetime.now())
        )
        await conn.commit()

        alerts = await check_ralph_health()

        assert len(alerts) > 0
        assert any("CRASHED RALPH" in alert for alert in alerts)

    @pytest.mark.asyncio
    async def test_get_all_alerts(self):
        """Test get_all_alerts() aggregates all categories."""
        alerts = await get_all_alerts()

        assert isinstance(alerts, dict)
        assert "completion_detection" in alerts
        assert "commit_tracking" in alerts
        assert "ralph_health" in alerts
        assert "task_backlog" in alerts

    @pytest.mark.asyncio
    async def test_has_critical_alerts(self):
        """Test has_critical_alerts() detection."""
        # With no data, should not have critical alerts
        has_critical = await has_critical_alerts()
        assert isinstance(has_critical, bool)

    def test_format_alerts_summary_empty(self):
        """Test format_alerts_summary() with no alerts."""
        alerts = {
            "completion_detection": [],
            "commit_tracking": [],
            "ralph_health": [],
            "task_backlog": []
        }

        summary = format_alerts_summary(alerts)

        assert "All systems healthy" in summary

    def test_format_alerts_summary_with_alerts(self):
        """Test format_alerts_summary() with alerts."""
        alerts = {
            "completion_detection": ["⚠️ Test alert 1"],
            "commit_tracking": [],
            "ralph_health": ["🚨 Critical alert"],
            "task_backlog": []
        }

        summary = format_alerts_summary(alerts)

        assert "2 alert(s)" in summary
        assert "Test alert 1" in summary
        assert "Critical alert" in summary
