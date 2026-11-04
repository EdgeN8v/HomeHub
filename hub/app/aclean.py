"""Air conditioner extras the cloud app keeps on its own servers and HA does not: how dirty the unit is
(a progress bar from how much it ran, like the washer's), the self-clean itself, and daily electricity / running
hours that survive HA's 10-day history.

Self-clean, learned from a real run on the h55h00 (property 9.1, a string):
  write "1"  -> the unit starts and reports  "1,<progress %>,<stage 1-5>,1"  while it runs (about 65 minutes)
  write "0"  -> it winds down ("1,90,5,0" for a minute) and ends at "0,100,5,1", the idle value
  stages: 1 内机凝露 2 内机结霜 3 内机化霜 4 内机烘干 5 外机清洁
A run counts as a clean only when it got to the end; one stopped early does not.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ha import HA
    from .home_service import Home
    from .store import Store

log = logging.getLogger("aclean")
TZ = timezone(timedelta(hours=8))
# How soon it needs cleaning: running time, not the calendar. Cooling and drying leave the coil wet (mould),
# heating mostly just moves dust, so they weigh differently; a unit that sits unused for a long time still
# gets a calendar limit.
WEIGHT = {"cool": 1.5, "dry": 1.5, "heat": 1.0, "heat_cool": 1.0, "auto": 1.0, "fan_only": 0.5}
LIMIT_HOURS, LIMIT_DAYS = 200, 180
FINISHED = 95              # a run that reached this progress was a full clean
CLEAN_KEY, DAILY_KEY, LOG_KEY = "ac_clean", "ac_daily", "ac_clean_log"
IDLE = ("off", "unavailable", "unknown")


def clean_info(rec: dict | None, now: datetime, weighted: float = 0.0) -> dict:
    """What the page shows. progress = the larger of weighted running hours / LIMIT_HOURS and days / LIMIT_DAYS."""
    rec = rec or {}
    last = datetime.fromisoformat(rec.get("last") or rec.get("baseline") or now.isoformat())
    days = max(0.0, (now - last).total_seconds() / 86400)
    by_hours, by_days = weighted / LIMIT_HOURS, days / LIMIT_DAYS
    p = min(1.0, max(by_hours, by_days))
    by = "hours" if by_hours >= by_days else "days"
    text = f"{round(weighted)}/{LIMIT_HOURS} 小时" if by == "hours" else f"{int(days)}/{LIMIT_DAYS} 天"
    return {"progress": round(p, 3), "days_since": int(days), "weighted_hours": round(weighted, 1), "driver": by, "text": text,
            "due": p >= 1.0, "limit_hours": LIMIT_HOURS, "limit_days": LIMIT_DAYS,
            "from_baseline": not rec.get("last"), "last": rec.get("last"),
            "label": "该清洁了" if p >= 1.0 else f"{round(p * 100)}%"}


def parse_run(value: str | None) -> dict | None:
    """"1,35,2,1" -> running, 35 %, stage 2; idle/other values -> None-ish."""
    p = (value or "").split(",")
    if len(p) != 4 or not all(x.lstrip("-").isdigit() for x in p):
        return None
    return {"running": p[0] == "1", "progress": int(p[1]), "stage": int(p[2]), "winding_down": p[3] == "0"}


def daily_kwh(points: list[tuple[datetime, float]]) -> dict[str, float]:
    """Cumulative kWh readings -> kWh used per local day."""
    out: dict[str, float] = {}
    last = None
    for t, v in points:
        if last is not None and v >= last:
            day = t.astimezone(TZ).strftime("%Y-%m-%d")
            out[day] = round(out.get(day, 0) + (v - last), 3)
        last = v
    return out


def daily_hours(states: list[tuple[datetime, str]], until: datetime) -> dict[str, dict[str, float]]:
    """State changes of the climate entity -> hours switched on per local day and mode (an interval is cut at midnight)."""
    out: dict[str, dict[str, float]] = {}
    for (t, s), nxt in zip(states, [t for t, _ in states[1:]] + [until]):
        if s in IDLE:
            continue
        a = t.astimezone(TZ)
        b = nxt.astimezone(TZ)
        while a < b:
            end = min(b, (a + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
            day = out.setdefault(a.strftime("%Y-%m-%d"), {})
            day[s] = round(day.get(s, 0) + (end - a).total_seconds() / 3600, 3)
            a = end
    return out


def weighted_hours(daily: dict, since_day: str) -> float:
    return sum(WEIGHT.get(m, 1.0) * h for day, v in daily.items() if day >= since_day for m, h in (v.get("modes") or {}).items())


class AcExtras:
    def __init__(self, ha: "HA", home: "Home", store: "Store"):
        self.ha, self.home, self.store = ha, home, store

    def _acs(self) -> list[dict]:
        return [d for d in self.home.model.get("devices", []) if d["kind"] == "ac" and d.get("ac")]

    # ---- how dirty
    def clean_all(self) -> dict:
        now = datetime.now(TZ)
        data = self.store.get_setting(CLEAN_KEY, {})
        daily = self.store.get_setting(DAILY_KEY, {})
        changed = False
        for d in self._acs():
            eid = d["roles"]["main"]
            if eid not in data:
                data[eid] = {"baseline": now.isoformat()}
                changed = True
        if changed:
            self.store.set_setting(CLEAN_KEY, data)
        out = {}
        for e, rec in data.items():
            since = (rec.get("last") or rec.get("baseline") or now.isoformat())[:10]
            out[e] = clean_info(rec, now, weighted_hours(daily.get(e, {}), since))
        return out

    def mark_cleaned(self, entity_id: str, when: datetime | None = None):
        data = self.store.get_setting(CLEAN_KEY, {})
        rec = data.setdefault(entity_id, {})
        rec["last"] = (when or datetime.now(TZ)).isoformat()
        rec.pop("max_progress", None)
        self.store.set_setting(CLEAN_KEY, data)

    def note_change(self, entity_id: str, new: dict):
        """Follow the unit's cleaning report: remember how far a run got, and count it as a clean when it ran to the end."""
        if "_clean_p_9_1" not in entity_id and "_clean_result_e_9_4" not in entity_id:
            return
        log_ = self.store.get_setting(LOG_KEY, [])
        log_.append({"t": datetime.now(TZ).isoformat(timespec="seconds"), "e": entity_id.split("_h55h00_")[-1], "s": new.get("state")})
        self.store.set_setting(LOG_KEY, log_[-100:])
        if "_clean_p_9_1" not in entity_id:
            return
        eid = next((d["roles"]["main"] for d in self._acs() if d["ac"].get("clean") == entity_id), None)
        run = parse_run(new.get("state"))
        if not eid or not run:
            return
        data = self.store.get_setting(CLEAN_KEY, {})
        rec = data.setdefault(eid, {})
        if run["running"] and not run["winding_down"]:
            if run["progress"] > rec.get("max_progress", 0):
                rec["max_progress"] = run["progress"]
                self.store.set_setting(CLEAN_KEY, data)
        elif not run["running"] and rec.get("max_progress") is not None:
            done = rec["max_progress"] >= FINISHED
            log.info("self-clean of %s ended at %s%%: %s", eid, rec["max_progress"], "counted" if done else "stopped early")
            if done:
                self.mark_cleaned(eid)
            else:
                rec.pop("max_progress", None)
                self.store.set_setting(CLEAN_KEY, data)

    # ---- long-term daily numbers
    async def collect(self):
        """Merge the last 10 days of HA history into the stored per-day totals (they only ever grow)."""
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=10)
        store = self.store.get_setting(DAILY_KEY, {})
        first = (start + timedelta(days=1)).astimezone(TZ).strftime("%Y-%m-%d")   # the oldest day is only partly in the window
        for d in self._acs():
            eid = d["roles"]["main"]
            rec = store.setdefault(eid, {})
            kwh_id = d["ac"].get("kwh")
            kwh = daily_kwh(await self.ha.history_series(start, kwh_id)) if kwh_id else {}
            rows = [(datetime.fromisoformat(ts), st) for _, st, ts in await self.ha.history(start, [eid])]
            modes = daily_hours(sorted(rows), now)
            for day in sorted(set(kwh) | set(modes)):
                if day < first:
                    continue
                rec[day] = {"kwh": kwh.get(day, 0), "hours": round(sum(modes.get(day, {}).values()), 3), "modes": modes.get(day, {})}
        self.store.set_setting(DAILY_KEY, store)

    def daily(self, entity_id: str) -> dict:
        return self.store.get_setting(DAILY_KEY, {}).get(entity_id, {})
