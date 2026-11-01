"""SQLite store: raw state changes of the watched entities (the source of truth for wash runs,
recomputed on demand) and a log of every booking we made."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .washer_logic import Event


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                entity_id TEXT NOT NULL, state TEXT NOT NULL, ts TEXT NOT NULL,
                PRIMARY KEY (entity_id, ts));
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, drum TEXT NOT NULL,
                dry_run INTEGER NOT NULL, ok INTEGER NOT NULL, detail TEXT NOT NULL);
        """)

    def add_events(self, rows: list[tuple[str, str, str]]) -> int:
        cur = self.db.executemany("INSERT OR IGNORE INTO events VALUES (?, ?, ?)",
                                  [(e, s, _utc(ts)) for e, s, ts in rows])
        self.db.commit()
        return cur.rowcount

    def events(self, since: datetime | None = None) -> list[Event]:
        q, args = "SELECT entity_id, state, ts FROM events", ()
        if since:
            q, args = q + " WHERE ts >= ?", (_utc(since.isoformat()),)
        return [Event(e, s, datetime.fromisoformat(ts)) for e, s, ts in self.db.execute(q + " ORDER BY ts", args)]

    def first_event_ts(self) -> str | None:
        return self.db.execute("SELECT MIN(ts) FROM events").fetchone()[0]

    def get_setting(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value):
        self.db.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    def add_booking(self, ts: datetime, drum: str, dry_run: bool, ok: bool, detail: dict):
        self.db.execute("INSERT INTO bookings (ts, drum, dry_run, ok, detail) VALUES (?, ?, ?, ?, ?)",
                        (ts.isoformat(), drum, int(dry_run), int(ok), json.dumps(detail, ensure_ascii=False)))
        self.db.commit()

    def bookings(self, limit: int = 20) -> list[dict]:
        rows = self.db.execute("SELECT ts, drum, dry_run, ok, detail FROM bookings ORDER BY id DESC LIMIT ?", (limit,))
        return [{"ts": ts, "drum": d, "dry_run": bool(dr), "ok": bool(ok), "detail": json.loads(det)}
                for ts, d, dr, ok, det in rows]


def _utc(ts: str) -> str:
    """Normalise to one ISO form so the primary key dedupes HA history and live events alike."""
    return datetime.fromisoformat(ts).astimezone(timezone.utc).isoformat(timespec="milliseconds")
