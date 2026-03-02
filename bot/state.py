"""
Dialog state: pending category choice, rate limiting, last notes.
Persists in SQLite so state survives bot restarts.
"""
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Rate limit: 30 requests per minute per user
RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW_SEC = 60

# Pending state: user is choosing category for uncertain classification
PENDING_KEY = "pending_category"
# Last N note titles per user (for "перенеси последнюю заметку")
LAST_NOTES_KEY = "last_notes"
LAST_NOTES_MAX = 20


def _db_path() -> Path:
    import os
    if os.getenv("STATE_DB_PATH"):
        return Path(os.environ["STATE_DB_PATH"])
    return Path(__file__).resolve().parent.parent / "state.db"


def _get_conn() -> sqlite3.Connection:
    path = _db_path()
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _get_conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
                user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated REAL NOT NULL,
                PRIMARY KEY (user_id, key)
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS rate (
                user_id INTEGER PRIMARY KEY,
                count INTEGER NOT NULL,
                window_start REAL NOT NULL
            )
            """
        )


def _get_json(user_id: int, key: str) -> Any | None:
    _init_db()
    with _get_conn() as c:
        row = c.execute(
            "SELECT value FROM kv WHERE user_id = ? AND key = ?",
            (user_id, key),
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return None


def _set_json(user_id: int, key: str, value: Any) -> None:
    _init_db()
    with _get_conn() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO kv (user_id, key, value, updated)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, key, json.dumps(value), time.time()),
        )


def _delete(user_id: int, key: str) -> None:
    _init_db()
    with _get_conn() as c:
        c.execute("DELETE FROM kv WHERE user_id = ? AND key = ?", (user_id, key))


# --- Public API ---


def check_rate_limit(user_id: int) -> bool:
    """Returns True if request is allowed, False if over limit."""
    _init_db()
    now = time.time()
    with _get_conn() as c:
        row = c.execute(
            "SELECT count, window_start FROM rate WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            c.execute(
                "INSERT OR REPLACE INTO rate (user_id, count, window_start) VALUES (?, 1, ?)",
                (user_id, now),
            )
            return True
        count, window_start = row["count"], row["window_start"]
        if now - window_start >= RATE_LIMIT_WINDOW_SEC:
            c.execute(
                "UPDATE rate SET count = 1, window_start = ? WHERE user_id = ?",
                (now, user_id),
            )
            return True
        if count >= RATE_LIMIT_REQUESTS:
            return False
        c.execute(
            "UPDATE rate SET count = count + 1 WHERE user_id = ?", (user_id,)
        )
        return True


def get_pending_category(user_id: int) -> dict | None:
    """Returns pending state: { "text", "options" } or None."""
    return _get_json(user_id, PENDING_KEY)


def set_pending_category(user_id: int, text: str, options: list[str]) -> None:
    _set_json(user_id, PENDING_KEY, {"text": text, "options": options})


def clear_pending_category(user_id: int) -> None:
    _delete(user_id, PENDING_KEY)


def get_last_notes(user_id: int) -> list[dict]:
    """List of { "page_id", "title", "database_id", "database_title" }."""
    return _get_json(user_id, LAST_NOTES_KEY) or []


def append_last_note(
    user_id: int,
    page_id: str,
    title: str,
    database_id: str,
    database_title: str,
) -> None:
    last = get_last_notes(user_id)
    last.insert(
        0,
        {
            "page_id": page_id,
            "title": title,
            "database_id": database_id,
            "database_title": database_title,
        },
    )
    _set_json(user_id, LAST_NOTES_KEY, last[:LAST_NOTES_MAX])


def remove_last_note_by_page_id(user_id: int, page_id: str) -> None:
    last = [n for n in get_last_notes(user_id) if n["page_id"] != page_id]
    _set_json(user_id, LAST_NOTES_KEY, last)
