"""SQLite connection factory with WAL mode and automatic schema migrations."""

import os
import sqlite3
from pathlib import Path


_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Return a SQLite connection with WAL mode and row factory enabled."""
    if db_path is None:
        db_path = os.environ.get("DB_PATH", "data/jobhunter.db")

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Enable WAL mode for concurrent reads
    conn.execute("PRAGMA journal_mode=WAL")
    # Enforce foreign key constraints
    conn.execute("PRAGMA foreign_keys=ON")
    # Improve performance
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")  # 64 MB

    return conn


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply schema.sql DDL (idempotent — uses CREATE TABLE IF NOT EXISTS)."""
    schema = _SCHEMA_PATH.read_text()
    conn.executescript(schema)
    conn.commit()


def init_db(db_path: str | None = None) -> sqlite3.Connection:
    """Open DB connection and run migrations. Returns ready-to-use connection."""
    conn = get_connection(db_path)
    run_migrations(conn)
    return conn
