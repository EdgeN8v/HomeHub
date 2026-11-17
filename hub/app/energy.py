"""Electricity use per device.

What can be measured in this house (found in Home Assistant, see the README of the energy page):
  * the metered plug (酷控插座) in the living room: real-time watts. The fridge, the washer (and later the floor
    washer) are all plugged into it, so its watts are shared out between them (see _share).
  * the two air conditioners and the water heater report their own running total in kWh.
Everything is summed up by day and by price band (Shanghai bills 峰 06:00-22:00 and 谷 22:00-06:00 differently).
Samples are taken every 30 s; a day's numbers are kept as watt-hours per (day, who, band) so the table stays tiny.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .washer_logic import DRUMS, PAUSED, WORKING, drum_entities

if TYPE_CHECKING:
    from .home_service import Home
    from .store import Store

log = logging.getLogger("energy")
TZ = timezone(timedelta(hours=8))
STEP = 30                                    # seconds between samples
PRICES = {"peak": 0.617, "valley": 0.307, "peak_from": 6, "peak_to": 22}      # 元/kWh, 上海居民一户一表峰谷电价
FRIDGE_W0 = 45.0                             # fridge's average draw until it has been measured


def band_price(b: str, prices: dict) -> float:
    return prices["peak"] if b == "peak" else prices["valley"] if b == "valley" else (prices["peak"] + prices["valley"]) / 2


def band(dt: datetime, prices: dict) -> str:
    return "peak" if prices["peak_from"] <= dt.hour < prices["peak_to"] else "valley"


class Energy:
    def __init__(self, home: "Home", store: "Store"):
        self.home, self.store = home, store
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS energy_day (
                day TEXT NOT NULL, who TEXT NOT NULL, band TEXT NOT NULL, wh REAL NOT NULL,
                PRIMARY KEY (day, who, band));
        """)
        self.last_t: float | None = None
        self.counters: dict[str, float] = store.get_setting("energy_counters", {})
        self.fridge_w: float = store.get_setting("energy_fridge_w", FRIDGE_W0)
        self.saved_at = 0.0

    # ---- settings
    def prices(self) -> dict:
        return {**PRICES, **self.store.get_setting("energy_prices", {})}

    def bill_day(self) -> int:
        return int(self.store.get_setting("energy_bill_day", 1))

    def set_settings(self, prices: dict | None, bill_day: int | None):
        if prices:
            p = {k: float(prices[k]) for k in ("peak", "valley") if k in prices}
            if any(not 0 < v < 10 for v in p.values()):
                raise ValueError("电价要在 0 到 10 元之间")
            self.store.set_setting("energy_prices", {**self.store.get_setting("energy_prices", {}), **p})
        if bill_day is not None:
            if not 1 <= int(bill_day) <= 28:
                raise ValueError("起算日是 1 到 28 号")
            self.store.set_setting("energy_bill_day", int(bill_day))

    # ---- what is where
    def _meters(self):
        """(metered plug device id, its watts entity) and [(device id, kWh counter entity)] of everything that counts itself."""
        plug, counters = None, []
        for d in self.home.model.get("devices", []):
            for e in d.get("entities", []):
                a = (self.home.states.get(e) or {}).get("attributes", {})
                if a.get("device_class") == "power" and a.get("unit_of_measurement") == "W" and e.startswith("sensor."):
                    plug = plug or (d["id"], e)
                elif a.get("device_class") == "energy" and a.get("unit_of_measurement") == "kWh" and e.startswith("sensor."):
                    counters.append((d["id"], e))
        # a metered plug also has an energy counter of its own: the watts are used for it, so leave it out of the counters
        counters = [(i, e) for i, e in counters if not (plug and i == plug[0])]
        return plug, counters

    def meter_plug_id(self) -> str | None:
        plug, _ = self._meters()
        return plug[0] if plug else None

    def _washer_running(self) -> bool:
        for drum in DRUMS:
            s = (self.home.states.get(drum_entities(drum)["status"]) or {}).get("state")
            if s in (WORKING, PAUSED):
                return True
        return False

    @staticmethod
    def _num(state) -> float | None:
        try:
            return float(state)
        except (TypeError, ValueError):
            return None

    def _share(self, watts: float, washer_on: bool) -> dict[str, float]:
        """The plug's watts -> who used them. The fridge runs all the time, so while the washer is off everything is the
        fridge (and its average is learnt from that); while the washer runs the fridge keeps its average and the rest is
        the washer's."""
        if washer_on:
            fridge = min(watts, self.fridge_w)
            return {"fridge": fridge, "washer": watts - fridge}
        if 2 <= watts < 400:
            self.fridge_w += (watts - self.fridge_w) * (STEP / 21600)      # a 6-hour moving average
        return {"fridge": watts}

    # ---- sampling
    def _add(self, buf: dict, now: datetime, who: str, wh: float, prices: dict):
        if wh > 0:
            k = (now.strftime("%Y-%m-%d"), who, band(now, prices))
            buf[k] = buf.get(k, 0.0) + wh

    async def tick(self):
        t = time.time()
        dt = min(t - self.last_t, 120) if self.last_t else 0
        self.last_t = t
        now = datetime.now(TZ)
        prices = self.prices()
        plug, counters = self._meters()
        buf: dict = {}
        if plug and dt:
            w = self._num((self.home.states.get(plug[1]) or {}).get("state"))
            if w is not None and w >= 0:
                for who, ww in self._share(w, self._washer_running()).items():
                    self._add(buf, now, who, ww * dt / 3600, prices)
        for dev, e in counters:
            v = self._num((self.home.states.get(e) or {}).get("state"))
            if v is None:
                continue
            last = self.counters.get(e)
            if last is not None and 0 < v - last < 30:
                self._add(buf, now, dev, (v - last) * 1000, prices)
            if last is None or v != last:
                self.counters[e] = v
        if buf:
            self.store.db.executemany(
                "INSERT INTO energy_day VALUES (?, ?, ?, ?) ON CONFLICT(day, who, band) DO UPDATE SET wh = wh + excluded.wh",
                [(d, w, b, wh) for (d, w, b), wh in buf.items()])
            self.store.db.commit()
        if t - self.saved_at > 300:
            self.saved_at = t
            self.store.set_setting("energy_counters", self.counters)
            self.store.set_setting("energy_fridge_w", round(self.fridge_w, 1))

    def backfill(self):
        """The air conditioners' per-day kWh that AcExtras already keeps (from HA history) for the days before this
        started counting. Those days have no time of day, so they are priced at the average of 峰 and 谷."""
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        daily = self.store.get_setting("ac_daily", {})
        have = {(d, w) for d, w in self.store.db.execute("SELECT day, who FROM energy_day")}
        rows = []
        for d in self.home.model.get("devices", []):
            if d.get("kind") != "ac":
                continue
            for day, v in (daily.get((d.get("roles") or {}).get("main"), {}) or {}).items():
                if day < today and (day, d["id"]) not in have and v.get("kwh"):
                    rows.append((day, d["id"], "unk", v["kwh"] * 1000))
        if rows:
            self.store.db.executemany("INSERT OR IGNORE INTO energy_day VALUES (?, ?, ?, ?)", rows)
            self.store.db.commit()
            log.info("energy: %d past air conditioner days added", len(rows))

    async def run(self):
        n = 0
        while True:
            if n % 120 == 0 and self.home.model.get("devices"):
                try:
                    self.backfill()
                except Exception:
                    log.exception("energy backfill")
            n += 1
            await asyncio.sleep(STEP)
            try:
                if self.home.model.get("devices"):
                    await self.tick()
            except Exception:
                log.exception("energy sample")

    # ---- reading
    def summary(self) -> dict:
        prices = self.prices()
        rows = self.store.db.execute("SELECT day, who, band, wh FROM energy_day ORDER BY day").fetchall()
        days: dict[str, dict] = {}
        for day, who, b, wh in rows:
            d = days.setdefault(day, {"day": day, "who": {}})
            w = d["who"].setdefault(who, {"kwh": 0.0, "cost": 0.0})
            w["kwh"] += wh / 1000
            w["cost"] += wh / 1000 * band_price(b, prices)
        for d in days.values():
            d["kwh"] = sum(w["kwh"] for w in d["who"].values())
            d["cost"] = sum(w["cost"] for w in d["who"].values())
        today = datetime.now(TZ).date()
        bd = self.bill_day()
        cycles = {}
        for day, d in days.items():
            y, m, dd = map(int, day.split("-"))
            k = date(y, m, 1)
            if dd < bd:
                k = (k - timedelta(days=1)).replace(day=1)
            c = cycles.setdefault(k.isoformat(), {"start": date(k.year, k.month, bd).isoformat(), "kwh": 0.0, "cost": 0.0, "who": {}, "days": 0})
            c["days"] += 1
            c["kwh"] += d["kwh"]
            c["cost"] += d["cost"]
            for who, w in d["who"].items():
                x = c["who"].setdefault(who, {"kwh": 0.0, "cost": 0.0})
                x["kwh"] += w["kwh"]
                x["cost"] += w["cost"]
        keep = sorted(days)[-62:]
        return {"today": days.get(today.isoformat(), {"day": today.isoformat(), "kwh": 0, "cost": 0, "who": {}}),
                "days": [days[k] for k in keep], "cycles": [dict(cycles[k], key=k) for k in sorted(cycles)][-12:],
                "prices": prices, "bill_day": bd, "fridge_w": round(self.fridge_w, 1),
                "since": min(days) if days else None, "meter": self.meter_plug_id()}
