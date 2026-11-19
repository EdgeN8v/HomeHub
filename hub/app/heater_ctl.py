"""恒温 for the baseboard heaters: the room's thermometer decides, not the heater's own sensor.

The heater measures at its own grille, where the air is warmer than in the room (its number ran up to 28 while the room was
22), so on its own it overshoots. Here the person's wish (e.g. 24) is kept in the hub and compared with the room thermometer;
the heater itself only ever gets its set-point and its heat level changed, never power:

    room far below the wish  -> set-point at the heater's maximum, normal heat        ("heat")
    room close to the wish   -> same set-point, low heat (less overshoot)             ("low")
    room at / above the wish -> set-point at the heater's minimum: it stops heating   ("hold")
    room cools again         -> back to low / heat

No usable thermometer in the room (none, or silent for an hour) -> the heater follows the wish with its own sensor ("own").
A change made on the heater or in Mijia (a set-point that is not one of ours) becomes the new wish.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .comfort import Comfort
    from .home_service import Home
    from .store import Store

log = logging.getLogger("heater")

KEY = "heater"
TICK_S = 30
STOP_AT = 0.0         # stop heating once the room reaches the wish
RESUME_BELOW = 0.5    # ... and start again when it is this far below
LOW_WITHIN = 0.8      # heat gently when the room is this close to the wish
FULL_BELOW = 1.3      # heat fully again when the room is this far below it
STALE_MIN = 60
WRITE_GAP_S = 20      # do not hammer a device that refuses
ADOPT_AFTER_S = 90    # a set-point we did not write, seen this long after our last write, is the person's new wish


def next_phase(prev: str, room: float, wish: float) -> str:
    err = wish - room                      # > 0: room is below the wish
    if err <= STOP_AT:
        return "hold"
    if prev == "hold":
        if err < RESUME_BELOW:
            return "hold"
        return "heat" if err >= FULL_BELOW else "low"
    if prev == "heat":
        return "heat" if err >= LOW_WITHIN else "low"
    return "heat" if err >= FULL_BELOW else "low"      # prev == "low" / "own" / anything else


class HeaterCtl:
    def __init__(self, home: Home, store: Store, comfort: Comfort):
        self.home, self.store, self.comfort = home, store, comfort
        self.phase: dict[str, str] = {}
        self.sp: dict[str, tuple[float, float]] = {}       # entity -> (set-point we last wrote, monotonic time)
        self.wrote: dict[str, float] = {}                  # entity -> monotonic time of our last write attempt
        self.room: dict[str, float | None] = {}
        self.unsynced: set[str] = set()                    # a new wish the heater has not been given yet

    # ---- config: {entity: {"target": wish}}
    def config(self) -> dict:
        return self.store.get_setting(KEY, {})

    def snapshot(self) -> dict:
        cfg = self.config()
        return {e: {"target": c.get("target"), "phase": self.phase.get(e), "room": self.room.get(e)} for e, c in cfg.items()}

    def _device(self, entity_id: str) -> dict | None:
        return next((d for d in self.home.model["devices"] if d["roles"].get("main") == entity_id and d["kind"] == "heater"), None)

    def set_target(self, entity_id: str, target: float) -> dict:
        dev = self._device(entity_id)
        if not dev:
            raise ValueError("不是踢脚线")
        a = (self.home.states.get(entity_id) or {}).get("attributes", {})
        lo, hi = float(a.get("min_temp", 16)), float(a.get("max_temp", 28))
        if not lo <= float(target) <= hi:
            raise ValueError(f"目标温度要在 {lo:g} 到 {hi:g} 之间")
        cfg = self.config()
        cfg[entity_id] = {"target": float(target)}
        self.store.set_setting(KEY, cfg)
        self.wrote.pop(entity_id, None)
        self.sp.pop(entity_id, None)
        self.unsynced.add(entity_id)
        return self.snapshot().get(entity_id, {})

    def _reading(self, dev: dict) -> float | None:
        sensors = self.comfort.thermometers(dev)
        vals = []
        for e in sensors:
            s = self.home.states.get(e)
            try:
                v = float(s["state"])
                ts = datetime.fromisoformat(s.get("last_reported") or s["last_updated"])
            except (TypeError, ValueError, KeyError):
                continue
            if (datetime.now(timezone.utc) - ts).total_seconds() <= STALE_MIN * 60:
                vals.append(v)
        return sum(vals) / len(vals) if vals else None

    async def tick(self):
        for dev in [d for d in self.home.model["devices"] if d["kind"] == "heater"]:
            eid = dev["roles"].get("main")
            try:
                if eid:
                    await self._one(eid, dev)
            except Exception:
                log.exception("heater %s", eid)

    async def _one(self, eid: str, dev: dict):
        s = self.home.states.get(eid)
        if not s or s["state"] in ("unavailable", "unknown"):
            self.phase.pop(eid, None)
            return
        a = s["attributes"]
        lo, hi = float(a.get("min_temp", 16)), float(a.get("max_temp", 28))
        cfg = self.config()
        c = cfg.get(eid)
        now = time.monotonic()
        sp_now = a.get("temperature")
        mine = self.sp.get(eid)
        # a set-point that is neither ours nor an end stop, well after our last write: the person (or Mijia) changed it -> new wish
        if sp_now is not None and s["state"] != "off" and eid not in self.unsynced:
            spf = float(sp_now)
            ours = mine is not None and abs(spf - mine[0]) <= 0.01
            recent = mine is not None and now - mine[1] <= ADOPT_AFTER_S
            if lo < spf < hi and not ours and not recent and (c is None or abs(spf - c["target"]) > 0.01):
                cfg[eid] = c = {"target": spf}
                self.store.set_setting(KEY, cfg)
                log.info("heater %s: new wish %.0f taken from the heater itself", eid, spf)
        if c is None:
            sp0 = float(sp_now) if sp_now is not None and lo < float(sp_now) < hi else 24.0
            cfg[eid] = c = {"target": min(hi, max(lo, sp0))}
            self.store.set_setting(KEY, cfg)
        if s["state"] == "off":
            self.phase.pop(eid, None)
            return
        wish = c["target"]
        room = self._reading(dev)
        self.room[eid] = room
        if room is None:
            phase, want_sp, want_level = "own", wish, 0
        else:
            phase = next_phase(self.phase.get(eid, "heat"), room, wish)
            want_sp = lo if phase == "hold" else hi
            want_level = 1 if phase == "low" else 0 if phase == "heat" else None      # in "hold" the level does not matter
        if phase != self.phase.get(eid):
            log.info("heater %s: room %s, wish %.1f -> %s", eid, "?" if room is None else f"{room:.1f}", wish, phase)
        self.phase[eid] = phase
        if now - self.wrote.get(eid, -1e9) < WRITE_GAP_S:
            return
        if sp_now is not None and abs(float(sp_now) - want_sp) <= 0.01:
            self.unsynced.discard(eid)
        else:
            self.wrote[eid] = now
            self.sp[eid] = (want_sp, now)
            self.unsynced.discard(eid)
            await self.home._write(eid, "climate", "set_temperature", {"temperature": want_sp})
        presets = a.get("preset_modes") or []
        if want_level is not None and len(presets) > want_level and a.get("preset_mode") != presets[want_level]:
            self.wrote[eid] = now
            await self.home._write(eid, "climate", "set_preset_mode", {"preset_mode": presets[want_level]})

    async def run(self):
        while True:
            await asyncio.sleep(TICK_S)
            if self.home.model.get("devices"):
                await self.tick()
