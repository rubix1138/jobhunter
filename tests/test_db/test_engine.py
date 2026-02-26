"""Tests for DB engine initialization and migrations."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from jobhunter.db.engine import get_connection, init_db, run_migrations


@pytest.fixture
def tmp_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = init_db(db_path)
    yield conn, db_path
    conn.close()


def test_get_connection_creates_file(tmp_path):
    db_path = str(tmp_path / "new.db")
    conn = get_connection(db_path)
    assert Path(db_path).exists()
    conn.close()


def test_wal_mode_enabled(tmp_db):
    conn, _ = tmp_db
    row = conn.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


def test_foreign_keys_enabled(tmp_db):
    conn, _ = tmp_db
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    assert row[0] == 1


def test_all_tables_created(tmp_db):
    conn, _ = tmp_db
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    expected = {"jobs", "applications", "credentials", "email_log", "agent_runs", "llm_usage"}
    assert expected.issubset(tables)


def test_migrations_are_idempotent(tmp_db):
    conn, _ = tmp_db
    # Running migrations twice should not raise
    run_migrations(conn)
    run_migrations(conn)


def test_row_factory_returns_sqlite_row(tmp_db):
    conn, _ = tmp_db
    conn.execute(
        "INSERT INTO jobs (linkedin_job_id, title, company, job_url) VALUES (?,?,?,?)",
        ("jid1", "Engineer", "Acme", "https://example.com"),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM jobs WHERE linkedin_job_id = 'jid1'").fetchone()
    assert isinstance(row, sqlite3.Row)
    assert row["title"] == "Engineer"
