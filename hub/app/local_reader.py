"""Reads the state of directly-controlled devices from the device itself and puts it over Home Assistant's copy.

Home Assistant's Xiaomi integration keeps a few values stale (a washer that is idle still shows "246 minutes left" and last
cycle's temperature). For every device switched to direct control (and those listed in the setting local_read_dids, read-only) this asks the device (miIO get_properties, the same
properties the integration exposes as entities) and, when its answer differs from what Home Assistant says, updates the
hub's copy and tells the phones. The wording of a value (a select's option, an enum sensor) comes from the device's own
spec; a value whose wording cannot be matched to the entity's options is left as Home Assistant has it.
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import local_bridge as lb
from . import miot_local as ml

log = logging.getLogger("local_reader")
DOMAINS = lb.DOMAINS


class Reader:
    def __init__(self, bridge: lb.Bridge, home, every: float = 30.0):
        self.bridge, self.home, self.every = bridge, home, every
        self.fixed = 0
        self.last: dict[str, float] = {}      # did -> time of the last good read

    def _entities(self, did: str) -> dict[tuple[int, int], list[str]]:
        out: dict[tuple[int, int], list[str]] = {}
        for eid in self.home.states:
            if eid.split(".")[0] not in DOMAINS:
                continue
            m = lb.ENT.match(eid)
            if m and m["did"] == did and m["kind"] == "p":
                out.setdefault((int(m["s"]), int(m["i"])), []).append(eid)
        return out

    async def read_one(self, did: str) -> int:
        dev = self.bridge.devs.get(did)
        spec = self.bridge._spec(dev.model) if dev else None
        if dev is None or spec is None:
            return 0
        ents = self._entities(did)
        keys = sorted(ents)
        vals: dict = {}
        for i in range(0, len(keys), 6):
            try:
                vals.update(await dev.get(keys[i:i + 6]))
            except ml.LocalError:
                break
        if not vals:
            return 0
        self.last[did] = time.time()
        changed = 0
        for k, v in vals.items():
            for eid in ents[k]:
                old = self.home.states.get(eid)
                if not old or old.get("state") in ("unknown", "unavailable"):
                    continue
                text = lb.shown(eid.split(".")[0], spec.props.get(k) or {}, v, (old.get("attributes") or {}).get("options"))
                if text is None:
                    continue
                try:
                    a, b = float(text), float(old["state"])
                    same = abs(a - b) <= max(1.0, 0.03 * max(abs(a), abs(b)))      # a reading that only wobbles (power, energy) is not stale
                except (ValueError, TypeError):
                    same = text == old["state"]
                if same:
                    continue
                await self.home.on_change(eid, {**old, "state": text})
                changed += 1
        self.fixed += changed
        return changed

    async def refresh_addresses(self):
        prefix = ml.lan_prefix(self.bridge.devs)
        if not prefix:
            return
        for did, ip in (await ml.discover(prefix)).items():
            dev = self.bridge.devs.get(did)
            if dev and dev.ip != ip:
                log.info("%s moved: %s -> %s", dev.name, dev.ip, ip)
                dev.ip = ip

    async def run(self):
        await asyncio.sleep(45)          # let Home Assistant's states arrive first
        last_scan = 0.0
        while True:
            if time.time() - last_scan > 300:
                last_scan = time.time()
                try:
                    await self.refresh_addresses()
                except Exception:
                    log.exception("address scan")
            for did in sorted(set(self.bridge.enabled()) | set(self.bridge.store.get_setting("local_read_dids", []))):
                try:
                    n = await self.read_one(did)
                    if n:
                        log.info("%s: %d value(s) corrected from the device", self.bridge.devs[did].name, n)
                except Exception:
                    log.exception("direct read of %s", did)
            await asyncio.sleep(self.every)
