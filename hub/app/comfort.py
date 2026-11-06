"""一键恒温: keep the air where the person is (the room's thermometer on the desk) at the wanted
temperature by moving the air conditioner's own set-point. An AC measures at its intake high up on the
wall, where the air is warmer than at desk height, so its number and what you feel differ.

Only the target temperature is ever written - never power, mode, fan or the display light - so the AC keeps
running its compressor by itself, and if this service dies it just stays at the last set-point.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .home_service import Home
    from .store import Store

log = logging.getLogger("comfort")

KEY = "comfort"
TICK_S = 60
BAND = 0.5            # how far the desk may drift from the target before the set-point moves
BAND_DARK = 1.0       # AC display dark (someone is asleep): tolerate more, change less
WAIT_MIN = 15         # minutes to let the room answer one change before the next
WAIT_MIN_DARK = 30
MAX_DRIFT = 6.0       # the set-point never strays further than this from the target


def decide(read: float, target: float, setpoint: float, step: float, lo: float, hi: float, dark: bool,
           minutes_since_change: float, fresh_reading: bool) -> float | None:
    """The new set-point, or None to leave it. read = desk thermometer."""
    err = read - target
    if abs(err) < (BAND_DARK if dark else BAND):
        return None
    if not fresh_reading or minutes_since_change < (WAIT_MIN_DARK if dark else WAIT_MIN):
        return None
    move = 1.0 if abs(err) >= 1.5 else 0.5
    move = max(move, step)
    new = setpoint + (-move if err > 0 else move)
    new = min(hi, target + MAX_DRIFT, max(lo, target - MAX_DRIFT, new))
    new = round(new / step) * step
    return None if abs(new - setpoint) < 1e-6 else new


def _num(s: dict | None) -> float | None:
    try:
        return float(s["state"])
    except (TypeError, ValueError, KeyError):
        return None


class Comfort:
    def __init__(self, home: Home, store: Store):
        self.home, self.store = home, store
        self.last: dict[str, float] = {}          # entity -> monotonic time of our last change
        self.last_change_ts: dict[str, datetime] = {}

    def config(self) -> dict:
        return self.store.get_setting(KEY, {})

    def _device(self, entity_id: str) -> dict | None:
        return next((d for d in self.home.model["devices"] if d["roles"].get("main") == entity_id and d["kind"] in ("ac", "heater")), None)

    def thermometers(self, dev: dict) -> list[str]:
        """Temperature sensors of the thermometers in the same room."""
        return [d["roles"]["temperature"] for d in self.home.model["devices"]
                if d["kind"] == "thermo" and d["home"] == dev["home"] and d["room"] == dev["room"] and "temperature" in d["roles"]]

    def reading(self, dev: dict) -> tuple[float, datetime] | None:
        vals = []
        for e in self.thermometers(dev):
            s = self.home.states.get(e)
            v = _num(s)
            if v is not None:
                vals.append((v, datetime.fromisoformat(s["last_updated"])))
        if not vals:
            return None
        return sum(v for v, _ in vals) / len(vals), max(t for _, t in vals)

    def set(self, entity_id: str, on: bool, target: float | None = None) -> dict:
        dev = self._device(entity_id)
        if not dev or not self.thermometers(dev):
            raise ValueError("这个房间没有温湿度计")
        cfg = self.config()
        cur = cfg.get(entity_id) or {}
        if on:
            a = (self.home.states.get(entity_id) or {}).get("attributes", {})
            t = target if target is not None else cur.get("target") if cur.get("on") else a.get("temperature")
            if t is None:
                raise ValueError("空调没有设定温度")
            cfg[entity_id] = {"on": True, "target": float(t)}
        else:
            cfg.pop(entity_id, None)
        self.store.set_setting(KEY, cfg)
        self.last.pop(entity_id, None)
        return cfg.get(entity_id, {"on": False})

    async def tick(self):
        cfg = self.config()
        for entity_id, c in list(cfg.items()):
            try:
                await self._one(entity_id, c, cfg)
            except Exception:
                log.exception("comfort %s", entity_id)

    async def _one(self, entity_id: str, c: dict, cfg: dict):
        s = self.home.states.get(entity_id)
        dev = self._device(entity_id)
        if not s or not dev:
            return
        if s["state"] == "off":                 # switched off: the next time it is started, it starts normal
            cfg.pop(entity_id, None)
            self.store.set_setting(KEY, cfg)
            return
        if s["state"] not in ("cool", "heat", "auto", "heat_cool"):
            return
        r = self.reading(dev)
        a = s["attributes"]
        if r is None or a.get("temperature") is None:
            return
        read, read_ts = r
        dark = any(e.endswith("indicator_light") and (self.home.states.get(e) or {}).get("state") == "off" for e in dev["entities"])
        since = (time.monotonic() - self.last[entity_id]) / 60 if entity_id in self.last else 1e9
        lc = self.last_change_ts.get(entity_id)
        new = decide(read, c["target"], float(a["temperature"]), float(a.get("target_temp_step") or 0.5),
                     float(a.get("min_temp", 16)), float(a.get("max_temp", 31)), dark, since,
                     lc is None or read_ts > lc)
        if new is None:
            return
        log.info("comfort %s: desk %.1f, target %.1f, set-point %.1f -> %.1f%s", entity_id, read, c["target"],
                 float(a["temperature"]), new, " (dark)" if dark else "")
        await self.home._write(entity_id, "climate", "set_temperature", {"temperature": new})
        self.last[entity_id] = time.monotonic()
        self.last_change_ts[entity_id] = datetime.now(timezone.utc)

    async def run(self):
        while True:
            await asyncio.sleep(TICK_S)
            if self.home.model.get("devices"):
                await self.tick()
