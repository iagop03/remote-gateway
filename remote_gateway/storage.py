import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# A session in one of these statuses no longer occupies a concurrency "slot" —
# shared by the MAX_CONCURRENT_SESSIONS/ALLOW_MULTIPLE_SESSIONS_SAME_DRIVER
# checks, the inactivity reaper, and the /metrics sessions_active count, so
# "active" means the same thing everywhere.
_TERMINAL_STATUSES = ("completed", "error", "expired")


class Storage:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, driver TEXT NOT NULL, model TEXT NOT NULL,
                working_directory TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL,
                last_activity TEXT NOT NULL, message_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL, timestamp TEXT NOT NULL, type TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                client_id TEXT NOT NULL, driver TEXT NOT NULL, model TEXT NOT NULL,
                session_id TEXT, input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0, origin_ip TEXT,
                status TEXT NOT NULL DEFAULT 'ok'
            );
            CREATE INDEX IF NOT EXISTS idx_audit_log_client_id ON audit_log(client_id);
            """
        )
        try:
            self.connection.execute("ALTER TABLE sessions ADD COLUMN driver_session_id TEXT")
        except sqlite3.OperationalError:
            pass
        self.connection.commit()

    def create_session(self, session: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO sessions (id, driver, model, working_directory, status, created_at, "
            "last_activity, message_count, driver_session_id) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (session["id"], session["driver"], session["model"], session.get("working_directory"),
             session["status"], session["created_at"], session["last_activity"], session.get("driver_session_id")),
        )
        self.connection.commit()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def update_session(self, session_id: str, **values: Any) -> None:
        if not values:
            return
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.connection.execute(f"UPDATE sessions SET {assignments} WHERE id = ?", (*values.values(), session_id))
        self.connection.commit()

    def add_event(self, event: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO events (event_id, session_id, timestamp, type, data) VALUES (?, ?, ?, ?, ?)",
            (event["event_id"], event["session_id"], event["timestamp"], event["type"], json.dumps(event["data"])),
        )
        self.connection.commit()

    def events(self, session_id: str, limit: int = 50, after: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT event_id, session_id, timestamp, type, data FROM events WHERE session_id = ?"
        params: list[Any] = [session_id]
        if after:
            query += " AND id > COALESCE((SELECT id FROM events WHERE event_id = ?), 0)"
            params.append(after)
        query += " ORDER BY id LIMIT ?"
        params.append(min(max(limit, 1), 500))
        rows = self.connection.execute(query, params).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def add_audit_entry(self, entry: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO audit_log (timestamp, client_id, driver, model, session_id, "
            "input_tokens, output_tokens, origin_ip, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry["timestamp"], entry["client_id"], entry["driver"], entry["model"], entry.get("session_id"),
             entry.get("input_tokens", 0), entry.get("output_tokens", 0), entry.get("origin_ip"),
             entry.get("status", "ok")),
        )
        self.connection.commit()

    def audit_entries(
        self, limit: int = 100, client_id: str | None = None, since: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM audit_log WHERE 1=1"
        params: list[Any] = []
        if client_id:
            query += " AND client_id = ?"
            params.append(client_id)
        if since:
            query += " AND timestamp >= ?"
            params.append(since)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(min(max(limit, 1), 1000))
        rows = self.connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def count_active_sessions(self, driver: str | None = None) -> int:
        placeholders = ",".join("?" * len(_TERMINAL_STATUSES))
        query = f"SELECT COUNT(*) FROM sessions WHERE status NOT IN ({placeholders})"
        params: list[Any] = list(_TERMINAL_STATUSES)
        if driver:
            query += " AND driver = ?"
            params.append(driver)
        return self.connection.execute(query, params).fetchone()[0]

    def session_stats(self, driver: str) -> dict[str, int]:
        total_messages = self.connection.execute(
            "SELECT COALESCE(SUM(message_count), 0) FROM sessions WHERE driver = ?", (driver,)
        ).fetchone()[0]
        return {"sessions_active": self.count_active_sessions(driver), "messages_total": total_messages}

    def expire_stale_sessions(self, cutoff: str) -> int:
        """Mark sessions inactive since before `cutoff` as expired. Returns how many."""
        placeholders = ",".join("?" * len(_TERMINAL_STATUSES))
        cursor = self.connection.execute(
            f"UPDATE sessions SET status = 'expired' WHERE last_activity < ? AND status NOT IN ({placeholders})",
            (cutoff, *_TERMINAL_STATUSES),
        )
        self.connection.commit()
        return cursor.rowcount
