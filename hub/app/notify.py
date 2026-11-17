"""Phone notifications ("提醒通知").

The hub keeps a numbered list of notices. Every phone running the HomeHub app asks for the ones after the last number it has
seen (GET /api/notices?after=N): the app does that in the background every ~15 minutes (Android JobScheduler) and holds a
long poll open while the app is in use, then shows each notice as an Android notification. Each phone keeps its own cursor,
so every phone gets every notice once. No Google / Xiaomi push service is involved.

Anything can raise a notice with Notifier.push(kind, title, body). A kind can be switched off by the person (the switch
sits on that device's page); KINDS lists them and their default. Rules watch HA state changes and push when a device enters
a state, e.g. the water heater's 防冻 status going Normal -> Antifreeze (Mijia: 防冻功能开始工作时，发送提醒通知).
A washer-finished rule can be added to RULES the same way.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("notify")

# kind -> what the switch is called and whether it is on before anyone touched it
KINDS: dict[str, dict] = {
    "antifreeze": {"label": "防冻提醒", "default": True},
}

KEEP = 200            # notices kept on the hub
DEAD = ("unavailable", "unknown", "", None)


@dataclass
class Rule:
    """Push `kind` when an entity whose id ends with `suffix` goes into `state` from any other real state."""
    kind: str
    suffix: str
    state: str
    title: Callable[[str], str]      # device name -> title
    body: str


RULES = [
    Rule("antifreeze", "_anti_icing_status_p_2_9", "Antifreeze",
         lambda name: f"{name}防冻功能开始工作", "天冷了，热水器正在自动加热，防止管路结冰"),
]


class Notifier:
    def __init__(self, store, name_of: Callable[[str], str] | None = None):
        self.store = store
        self.db = store.db
        self.name_of = name_of or (lambda e: "热水器")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS notices (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
                title TEXT NOT NULL, body TEXT NOT NULL);
        """)
        self.changed = asyncio.Event()

    # ---- settings: which kinds are on --------------------------------------------------

    def settings(self) -> dict:
        saved = self.store.get_setting("notify", {}) or {}
        return {k: bool(saved.get(k, v["default"])) for k, v in KINDS.items()}

    def set(self, kind: str, on: bool) -> dict:
        if kind not in KINDS:
            raise ValueError(f"没有这种提醒：{kind}")
        saved = self.store.get_setting("notify", {}) or {}
        saved[kind] = bool(on)
        self.store.set_setting("notify", saved)
        return self.settings()

    # ---- the list ------------------------------------------------------------------------

    def push(self, kind: str, title: str, body: str, force: bool = False) -> int | None:
        """Add a notice for the phones; None when this kind is switched off (force = a test notice)."""
        if not force and not self.settings().get(kind, True):
            log.info("%s switched off, not sending: %s", kind, title)
            return None
        cur = self.db.execute("INSERT INTO notices (ts, kind, title, body) VALUES (?, ?, ?, ?)",
                              (datetime.now(timezone.utc).isoformat(timespec="seconds"), kind, title, body))
        self.db.execute("DELETE FROM notices WHERE id <= ?", (cur.lastrowid - KEEP,))
        self.db.commit()
        log.info("notice %s [%s] %s - %s", cur.lastrowid, kind, title, body)
        self.changed.set()
        self.changed = asyncio.Event()
        return cur.lastrowid

    def latest(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(id), 0) FROM notices").fetchone()[0]

    def since(self, after: int) -> list[dict]:
        rows = self.db.execute("SELECT id, ts, kind, title, body FROM notices WHERE id > ? ORDER BY id LIMIT 20", (after,))
        return [{"id": i, "ts": ts, "kind": k, "title": t, "body": b} for i, ts, k, t, b in rows]

    async def wait(self, after: int, timeout: float) -> list[dict]:
        """Long poll: the notices after `after`, waiting up to `timeout` s for one to arrive."""
        got = self.since(after)
        if got or timeout <= 0:
            return got
        try:
            await asyncio.wait_for(self.changed.wait(), timeout)
        except asyncio.TimeoutError:
            return []
        return self.since(after)

    # ---- rules on HA state changes ------------------------------------------------------

    def _last(self) -> dict:
        return self.store.get_setting("notify_last", {}) or {}

    def on_change(self, entity_id: str, new: dict | None):
        state = (new or {}).get("state")
        if state in DEAD:
            return            # a dropout says nothing; the state before it still counts
        for r in RULES:
            if not entity_id.endswith(r.suffix):
                continue
            last = self._last()
            prev = last.get(entity_id)
            if prev == state:
                return
            last[entity_id] = state
            self.store.set_setting("notify_last", last)
            # prev None = first sight (fresh install): remember it, but it is not a change we saw happen
            if state == r.state and prev is not None:
                self.push(r.kind, r.title(self.name_of(entity_id)), r.body)
            return

    def resync(self, states: dict):
        """After (re)connecting to HA: catch a change that happened while the hub was not listening."""
        for e, s in states.items():
            if any(e.endswith(r.suffix) for r in RULES):
                self.on_change(e, s)
