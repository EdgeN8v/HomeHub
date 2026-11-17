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
import uuid
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
        self.flips: dict[str, dict] = {}       # device -> {after, t0, done}: a flip-over reminder armed for the run in progress
        self.warms: dict[str, dict] = {}       # device -> {minutes, t0}: a hub-side keep-warm stop armed for the run in progress

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
        # The 自动保温 switch (keep / auto_keep_warm_p_2_6) is only a default; selecting a preset mode starts the cook
        # with that preset's own per-cook keep-warm, which for presets like 红薯 is off (seen on the real unit
        # 2026-11-15: ran 30 min, went 烹饪完成 -> 待机 without ever entering 保温中, then auto-powered-off ~5 min later).
        # So set the per-cook switch (keep_now / current_keep_warm_p_2_7) to match the person's choice after the cook has
        # started. Guarded: a failed keep-warm write must not abort a cook that is already running.
        keep = bool(p.get("keep"))
        if kx.get("keep_now"):
            try:
                await self._switch(d, "keep_now", keep)
            except Exception:
                log.warning("fryer %s: could not set the per-cook keep-warm", d["name"])
        # The unit has no settable keep-warm duration (MIoT service 2 of careli.fryer.maf09a has on/off switches
        # p_2_6 / p_2_7 and a keep_warm_finished event, but no keep-warm-time property), and left on it keeps warm for a
        # long time. So keep 20 min the hub's way: once the cook finishes and the unit enters 保温中, _watch_warms stops
        # the keep-warm after this many minutes.
        self.warms.pop(device_id, None)
        if keep:
            km = int(p.get("keep_min") or 20)
            if km > 0:
                self.warms[device_id] = {"minutes": km, "t0": None}
        self.flips.pop(device_id, None)
        after = int(p.get("flip_at") or 0)
        if 0 < after < int(p.get("time") or 0):
            self.flips[device_id] = {"after": after, "t0": asyncio.get_running_loop().time(), "done": False}
        log.info("fryer %s started: %s", d["name"], p)

    # ---- your own recipes (扩展模式): kept here, shared by the household, started as 手动模式 with their numbers
    def recipes(self) -> dict:
        return self.store.get_setting("kitchen_recipes", {})

    def save_recipe(self, device_id: str, r: dict) -> dict:
        self._dev(device_id, "fryer")
        name = str(r.get("name", "")).strip()
        if not 1 <= len(name) <= 10:
            raise ValueError("名字要 1 到 10 个字")
        temp, minutes = int(r.get("temp", 180)), int(r.get("time", 20))
        if not 40 <= temp <= 230 or not 1 <= minutes <= 1440:
            raise ValueError("温度 40–230，时间 1 分钟以上")
        data = self.recipes()
        lst = data.setdefault(device_id, [])
        rid = str(r.get("id") or "") or uuid.uuid4().hex[:8]
        rec = {"id": rid, "name": name, "temp": temp, "time": minutes, "keep": bool(r.get("keep", True))}
        flip_at = int(r.get("flip_at") or 0)
        if flip_at:
            if not 1 <= flip_at < minutes:
                raise ValueError("翻面时间要在烹饪时间之内")
            rec["flip_at"] = flip_at
        for i, x in enumerate(lst):
            if x["id"] == rid:
                lst[i] = rec
                break
        else:
            lst.append(rec)
        self.store.set_setting("kitchen_recipes", data)
        return data

    def delete_recipe(self, device_id: str, rid: str) -> dict:
        data = self.recipes()
        data[device_id] = [x for x in data.get(device_id, []) if x["id"] != rid]
        self.store.set_setting("kitchen_recipes", data)
        return data

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

    def flip_waiting(self) -> list:
        return [d for d, f in self.flips.items() if f.get("done") and f.get("waiting")]

    async def _watch_flips(self):
        """Half-way flip-over: at the minute the recipe says, pause the fryer (it stops heating and beeps); the person flips
        the food and presses 继续. The paused minutes do not count, the unit keeps its own clock."""
        for device_id, f in list(self.flips.items()):
            d = next((x for x in self.home.model["devices"] if x["id"] == device_id), None)
            if not d:
                self.flips.pop(device_id, None)
                continue
            s = self.status(d)
            if f["done"]:
                if "暂停" in s:
                    f["waiting"] = True
                elif f.get("waiting") or not any(b in s for b in BUSY):
                    self.flips.pop(device_id, None)                  # resumed, or finished / stopped
                continue
            if not any(b in s for b in BUSY) and asyncio.get_running_loop().time() - f["t0"] > 30:
                self.flips.pop(device_id, None)
                continue
            if "暂停" in s or asyncio.get_running_loop().time() - f["t0"] < 20:
                continue
            try:
                left = int(float((self.home.states.get(d["kx"]["left"]) or {}).get("state")))
                total = int(float((self.home.states.get(d["kx"]["time"]) or {}).get("state")))
            except (TypeError, ValueError):
                continue
            if 0 < left <= total - f["after"]:
                f["done"] = True
                await self.home._write(d["kx"]["pause"], "button", "press", {})
                log.info("fryer %s paused for the flip-over (%s left)", d["name"], left)

    async def _watch_warms(self):
        """Keep warm for a set number of minutes, the hub's way: once the cook finishes and the unit enters 保温中,
        start the clock; when it runs out, turn the per-cook keep-warm off so the unit leaves 保温中 (it then idles and
        powers itself off a few minutes later, as it does after any cook)."""
        for device_id, w in list(self.warms.items()):
            d = next((x for x in self.home.model["devices"] if x["id"] == device_id), None)
            if not d:
                self.warms.pop(device_id, None)
                continue
            s = self.status(d)
            # Real sample (2026-11-05): a cook goes 烹饪中 -> 保温中, and during keep-warm the status flips
            # 保温中 <-> 抽锅暂停 <-> 暂停 as the basket is pulled, then lands on 待机 / 关机 when the keep-warm ends.
            # The unit exposes a keep_warm_finished event and a 保温中 state but NO settable keep-warm duration, and the
            # natural length is still unobserved, so the hub caps it: count wall time from the first 保温中; if the unit
            # ends keep-warm on its own first, just stop watching.
            if "保温" in s:
                now = asyncio.get_running_loop().time()
                if w["t0"] is None:
                    w["t0"] = now
                    log.info("fryer %s: keeping warm, will stop after %s min", d["name"], w["minutes"])
                elif now - w["t0"] >= w["minutes"] * 60:
                    self.warms.pop(device_id, None)
                    try:
                        await self._switch(d, "keep_now", False)
                        log.info("fryer %s: keep-warm stopped after %s min", d["name"], w["minutes"])
                    except Exception:
                        log.warning("fryer %s: could not stop keep-warm", d["name"])
            elif w["t0"] is not None:
                if "待机" in s or "关机" in s:
                    self.warms.pop(device_id, None)      # keep-warm ended (by the unit or the person) before 20 min
                # else 抽锅暂停 / 暂停: a transient during keep-warm; keep the clock running
            elif not any(b in s for b in BUSY):
                self.warms.pop(device_id, None)          # cook ended without ever keeping warm (stopped, or keep was off on the unit)

    async def tick(self):
        await self._watch_flips()
        await self._watch_warms()
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
            await asyncio.sleep(5)
            if self.home.model.get("devices"):
                await self.tick()
