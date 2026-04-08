"""Tests for ChiefWiggum database schema and operations."""

import os

import aiosqlite
import pytest

from chiefwiggum.database import (
    _run_migrations,
    get_connection,
    get_setting,
    init_db,
    reset_db,
    set_setting,
)


@pytest.fixture(autouse=True)
async def db(tmp_path):
    """Isolated test database for each test."""
    os.environ["CHIEFWIGGUM_DB"] = str(tmp_path / "test.db")
    await init_db()
    yield
    await reset_db()
    del os.environ["CHIEFWIGGUM_DB"]


async def test_schema_has_last_error_column():
    conn = await get_connection()
    try:
        cursor = await conn.execute("PRAGMA table_info(ralph_instances)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "last_error" in columns
    finally:
        await conn.close()


async def test_schema_task_history_has_cost_columns():
    conn = await get_connection()
    try:
        cursor = await conn.execute("PRAGMA table_info(task_history)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "input_tokens" in columns
        assert "estimated_cost_usd" in columns
    finally:
        await conn.close()


async def test_init_db_is_idempotent():
    # Called once by fixture; call again — must not raise
    await init_db()
    await init_db()


async def test_wal_mode_enabled():
    conn = await get_connection()
    try:
        cursor = await conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        assert row[0] == "wal"
    finally:
        await conn.close()


async def test_get_set_setting_roundtrip():
    await set_setting("test_key", "hello_world")
    result = await get_setting("test_key")
    assert result == "hello_world"


async def test_get_setting_default_when_missing():
    result = await get_setting("nonexistent_key", default="fallback")
    assert result == "fallback"


async def test_reset_db_clears_all_tables():
    await set_setting("should_disappear", "yes")
    await reset_db()
    result = await get_setting("should_disappear")
    assert result is None


async def test_migration_adds_missing_columns(tmp_path):
    """Simulate an old database missing migration columns, verify _run_migrations adds them."""
    migration_db = tmp_path / "migration_test.db"
    conn = await aiosqlite.connect(str(migration_db))
    try:
        await conn.execute("PRAGMA journal_mode = WAL")

        # Minimal ralph_instances WITHOUT last_error, worktree_base_path, etc.
        await conn.execute("""
            CREATE TABLE ralph_instances (
                ralph_id TEXT PRIMARY KEY,
                hostname TEXT,
                started_at TIMESTAMP NOT NULL,
                last_heartbeat TIMESTAMP NOT NULL,
                status TEXT DEFAULT 'active'
            )
        """)

        # Minimal task_claims WITHOUT category, worktree_path, etc.
        await conn.execute("""
            CREATE TABLE task_claims (
                task_id TEXT PRIMARY KEY,
                task_title TEXT NOT NULL,
                task_priority TEXT NOT NULL,
                status TEXT DEFAULT 'pending'
            )
        """)

        # Minimal task_history WITHOUT cost columns
        await conn.execute("""
            CREATE TABLE task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                task_title TEXT NOT NULL,
                ralph_id TEXT NOT NULL,
                started_at TIMESTAMP NOT NULL,
                completed_at TIMESTAMP NOT NULL,
                duration_seconds REAL NOT NULL,
                status TEXT NOT NULL
            )
        """)
        await conn.commit()

        # Run migrations on this bare-bones DB
        await _run_migrations(conn)
        await conn.commit()

        # Verify last_error added to ralph_instances
        cursor = await conn.execute("PRAGMA table_info(ralph_instances)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "last_error" in cols

        # Verify worktree_path added to task_claims
        cursor = await conn.execute("PRAGMA table_info(task_claims)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "worktree_path" in cols

        # Verify cost columns added to task_history
        cursor = await conn.execute("PRAGMA table_info(task_history)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "estimated_cost_usd" in cols
        assert "input_tokens" in cols

    finally:
        await conn.close()
