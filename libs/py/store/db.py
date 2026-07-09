"""SQLite connection management + migration runner.

One writer at a time, many readers — WAL mode makes that safe across the
Python jobs and the Node API reading the same file.
"""

import sqlite3
from importlib import resources
from pathlib import Path

from core.config import settings


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else settings().gtl_db_path
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending numbered migrations; returns the names applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied = []
    mig_dir = resources.files("store").joinpath("migrations")
    for f in sorted(mig_dir.iterdir(), key=lambda p: p.name):
        if not f.name.endswith(".sql") or f.name in done:
            continue
        with conn:  # one transaction per migration
            conn.executescript(f.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (f.name,))
        applied.append(f.name)
    return applied
