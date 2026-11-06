"""Starting the air fryer the way Mijia does, and reservations ("预约") the unit itself can't do.

What the unit does (learned on the real one, MIoT service 2 of careli.fryer.maf09a):
  * writing the mode (select) starts cooking at once, with that mode's own defaults (temperature, time, ...)
  * temperature / time / weight can be changed while it cooks (the remaining time follows)
  * the 开始烹饪 button starts with whatever mode and numbers are set now
  * a number written to the reservation property does nothing by itself
So: to start a mode with the person's own numbers, write the mode, then write the numbers a moment later.
A reservation is kept here and started by the hub at that time.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .home_service import Home
    from .store import Store

log = logging.getLogger("kitchen")
TZ = timezone(timedelta(hours=8))
KEY = "kitchen_resv"
BUSY = ("烹饪中", "脆烤中", "嫩烤中", "保温中", "暂停中", "预热中", "预约中")


class Kitchen:
    def __init__(self, home: "Home", store: "Store"):
        self.home, self.store = home, store

    def _dev(self, device_id: str, kind: str) -> dict:
        d = next((d for d in self.home.model["devices"] if d["id"] == device_id and d["kind"] == kind), None)
        if not d or not d.get("kx"):
            raise ValueError("找不到这台设备")
        return d

    def status(self, d: dict) -> str:
        return (self.home.states.get(d["kx"].get("status")) or {}).get("state", "")

    async def _num(self, d: dict, key: str, value):
        e = d["kx"].get(key)
        if e and value is not None:
            await self.home._write(e, e.split(".")[0], "set_value", {"value": value})

    async def _switch(self, d: dict, key: str, on):
        e = d["kx"].get(key)
        if e and on is not None:
            await self.home._write(e, "switch", "turn_on" if on else "turn_off", {})

    async def start_fryer(self, device_id: str, p: dict):
        """p: mode (option name), temp, time, weight, keep (bool), flip (bool). Starts cooking now."""
        d = self._dev(device_id, "fryer")
        kx = d["kx"]
        mode_state = self.home.states.get(kx["mode"]) or {}
        if p.get("mode") not in (mode_state.get("attributes", {}).get("options") or []):
            raise ValueError("不认识这个模式")
        await self._switch(d, "keep", p.get("keep"))
        await self._switch(d, "flip", p.get("flip"))
        await self.home._write(kx["mode"], "select", "select_option", {"option": p["mode"]})    # starts cooking with the mode's defaults
        await asyncio.sleep(1.2)
        for key in ("temp", "time", "weight"):
            await self._num(d, key, p.get(key))
        for key in ("portion", "texture"):
            e = kx.get(key)
            if e and p.get(key) in ((self.home.states.get(e) or {}).get("attributes", {}).get("options") or []):
                await self.home._write(e, "select", "select_option", {"option": p[key]})
        log.info("fryer %s started: %s", d["name"], p)

    # ---- reservations
    def reservations(self) -> dict:
        return self.store.get_setting(KEY, {})

    def reserve(self, device_id: str, at: datetime, p: dict) -> dict:
        self._dev(device_id, "fryer")
        if at <= datetime.now(TZ) or at > datetime.now(TZ) + timedelta(hours=48):
            raise ValueError("预约时间要在未来 48 小时内")
        data = self.reservations()
        data[device_id] = {"at": at.isoformat(), "params": p}
        self.store.set_setting(KEY, data)
        return data[device_id]

    def cancel_reservation(self, device_id: str):
        data = self.reservations()
        data.pop(device_id, None)
        self.store.set_setting(KEY, data)

    async def tick(self):
        data = self.reservations()
        now = datetime.now(TZ)
        for device_id, r in list(data.items()):
            if datetime.fromisoformat(r["at"]) > now:
                continue
            data.pop(device_id, None)
            self.store.set_setting(KEY, data)
            try:
                d = self._dev(device_id, "fryer")
                if self.status(d) in BUSY:
                    log.warning("reservation of %s skipped: already busy", d["name"])
                    continue
                await self.start_fryer(device_id, r["params"])
            except Exception:
                log.exception("reservation of %s", device_id)

    async def run(self):
        while True:
            await asyncio.sleep(15)
            if self.home.model.get("devices"):
                await self.tick()
