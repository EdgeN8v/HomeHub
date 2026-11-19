"""Sends the hub's writes straight to the device on the LAN instead of through Home Assistant and the Xiaomi cloud.

Every write of the hub goes through HA.call(domain, service, data). The Xiaomi integration names its entities after what they
are on the device:  select.mibx5_cn_860976874_x13_mode_p_13_3  = device 860976874, service 13, property 3
(buttons: ..._a_13_2 = action 2 of service 13). So an entity id is enough to know *what* to write, the device's cached
spec (value lists, number formats) says *how*, and miot_local does it with the device's token.

Safety:
  * only devices listed as "local" (setting local_dids, changed with /api/local/enable) are sent directly, one by one, as each
    has been tried out
  * anything this cannot translate (lights, climate, ...) and any local failure falls back to Home Assistant
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import aiohttp

from . import miot_local as ml

log = logging.getLogger("local_bridge")
ENT = re.compile(r"^(?P<dom>[a-z_]+)\.[a-z0-9]+_cn_(?P<did>\d+)_.*?_(?P<kind>[pa])_(?P<s>\d+)_(?P<i>\d+)$")
# the main light of a device: service 2, switched with property 1, brightness is property 2 (percent)
LIGHT = re.compile(r"^light\.[a-z0-9]+_cn_(?P<did>\d+)_[a-z0-9]+_s_2(?:_light)?$")
# the air conditioner: one climate entity per device; service 2 = the unit, service 3 = the fan / swing
CLIMATE = re.compile(r"^climate\.[a-z0-9]+_cn_(?P<did>\d+)_[a-z0-9]+$")
HVAC = {"cool": 2, "dry": 3, "fan_only": 4, "heat": 5}
SWING = {"off": (False, False), "horizontal": (True, False), "vertical": (False, True), "both": (True, True)}   # (horizontal 3/3, vertical 3/4)


def why_code(code: int, off_hint: bool = False) -> str:
    if code == -5000:
        return "设备在现在的状态下不接受这个设置" + ("，空调要先开机" if off_hint else "")
    return f"设备返回 {code}"


class DirectFailed(aiohttp.ClientResponseError):
    """The device did not take a direct command. With local_strict on (the default) this is an error for the phone, not a
    silent detour through Home Assistant: a direct device is meant to work without it."""

    def __init__(self, message: str):
        super().__init__(request_info=None, history=(), status=502, message=message)

    def __str__(self) -> str:       # ClientResponseError.__str__ needs request_info, which a direct write does not have
        return self.message


# a fan: one fan entity per device, service N = the fan; property 1 on, 3 mode (preset), 5 speed in percent
FAN = re.compile(r"^fan\.[a-z0-9]+_cn_(?P<did>\d+)_[a-z0-9]+_s_(?P<s>\d+)_fan$")


DOMAINS = ("sensor", "number", "select", "switch", "binary_sensor")


def shown(domain: str, prop: dict, value, options: list | None):
    """The text Home Assistant would show for the device's value, or None when we cannot tell."""
    if isinstance(value, bool):
        return ("on" if value else "off") if domain in ("switch", "binary_sensor") else None
    vl = {x["value"]: (x.get("description_trans") or x.get("description")) for x in prop.get("value_list") or []}
    if domain == "select":
        text = str(vl.get(value, value))
        return text if options and text in options else None
    if vl:
        return str(vl[value]) if value in vl and domain == "sensor" else None
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    return None


class Spec:
    """What the device says about its own properties: format, value list, range."""

    def __init__(self, raw: dict):
        self.props: dict[tuple[int, int], dict] = {}
        for svc in raw.get("services", []):
            for p in svc.get("properties", []):
                self.props[(svc["iid"], p["iid"])] = p

    def choice(self, s: int, i: int, text: str):
        p = self.props.get((s, i)) or {}
        for item in p.get("value_list") or []:
            if text in (item.get("description_trans"), item.get("description"), item.get("name"), str(item.get("value"))):
                return item["value"]
        raise KeyError(text)


def load_specs(folder: Path) -> list[tuple[str, Spec]]:
    out = []
    for f in sorted(folder.glob("*.dict")) if folder.is_dir() else []:
        try:
            b = f.read_bytes()
            raw = json.loads(b[: b.rfind(b"}") + 1].decode("utf-8"))      # the integration's cache files end with a 32-byte checksum
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if isinstance(raw, dict) and "services" in raw and raw.get("urn"):
            out.append((raw["urn"].split(":")[5] if raw["urn"].count(":") >= 5 else "", Spec(raw)))
    return out


class Bridge:
    def __init__(self, data: Path, store):
        self.store = store
        self.devs = ml.load_tokens(data / "mi_tokens.json") if (data / "mi_tokens.json").exists() else {}
        self.specs = load_specs(data / "miot_specs")
        self.used = {"local": 0, "fallback": 0}
        self.stats: dict[str, dict] = {}        # did -> {"local": n, "fallback": n, "at": last time}
        self.account = self._account(data / "mi_tokens.json")

    @staticmethod
    def _account(f: Path) -> dict:
        """What the token dump holds: all devices of the account, those with a token, the Bluetooth ones with a key."""
        out = {"devices": 0, "tokens": 0, "ble": []}
        try:
            for srv in json.loads(f.read_text(encoding="utf-8")):
                for home in srv.get("homes", []):
                    for d in home.get("devices", []):
                        out["devices"] += 1
                        out["tokens"] += bool(d.get("token"))
                        if d.get("BLE_DATA"):
                            out["ble"].append({"name": d.get("name", ""), "model": d.get("model", "")})
        except (OSError, ValueError):
            pass
        return out

    def _note(self, did: str, key: str):
        import time
        st = self.stats.setdefault(did, {"local": 0, "fallback": 0, "at": 0})
        st[key] += 1
        st["at"] = time.time()
        self.used[key] += 1

    def enabled(self) -> list[str]:
        return list(self.store.get_setting("local_dids", []))

    def enable(self, dids: list[str]):
        self.store.set_setting("local_dids", sorted({str(d) for d in dids if str(d) in self.devs}))

    def strict(self) -> bool:
        return bool(self.store.get_setting("local_strict", True))

    def _failed(self, dev, eid: str, why) -> bool:
        """A device failure: error for the phone when strict, else let Home Assistant try."""
        log.warning("direct write of %s to %s failed (%s)", eid, dev.name, why)
        self._note(dev.did, "fallback")
        if self.strict():
            raise DirectFailed(f"{dev.name}没有接受指令（{why}）")
        return False

    def has_spec(self, model: str) -> bool:
        return self._spec(model) is not None

    def _spec(self, model: str) -> Spec | None:
        parts = model.split(".")          # careli.fryer.maf09a -> the spec says careli-maf09a
        key = f"{parts[0]}-{parts[-1]}" if len(parts) >= 3 else model.replace(".", "-")
        for urn_model, spec in self.specs:
            if urn_model == key:
                return spec
        return None

    def _value(self, dev: ml.Device, dom: str, service: str, data: dict, s: int, i: int):
        spec = self._spec(dev.model)
        p = (spec.props.get((s, i)) if spec else None) or {}
        if dom == "select" and service == "select_option":
            return spec.choice(s, i, str(data["option"]))
        if dom == "number" and service == "set_value":
            v = float(data["value"])
            return int(round(v)) if p.get("format", "int").startswith(("int", "uint")) else v
        if dom == "switch" and service in ("turn_on", "turn_off"):
            return service == "turn_on"
        raise KeyError(f"{dom}.{service}")

    async def try_call(self, domain: str, service: str, data: dict) -> bool:
        """True when the device took it directly; False = let Home Assistant do it."""
        eid = str(data.get("entity_id", ""))
        fm = FAN.match(eid) if domain == "fan" else None
        if fm:
            return await self._fan(fm["did"], int(fm["s"]), eid, service, data)
        cm = CLIMATE.match(eid) if domain == "climate" else None
        if cm:
            return await self._climate(cm["did"], eid, service, data)
        lm = LIGHT.match(eid) if domain == "light" else None
        if lm:
            return await self._light(lm["did"], eid, service, data)
        m = ENT.match(eid)
        if not m or m["did"] not in self.enabled() or m["did"] not in self.devs:
            return False
        dev, s, i = self.devs[m["did"]], int(m["s"]), int(m["i"])
        try:
            if domain == "button" and service == "press" and m["kind"] == "a":
                code = await dev.action(s, i)
            elif m["kind"] == "p":
                code = await dev.set(s, i, self._value(dev, domain, service, data, s, i))
            else:
                return False
        except ml.LocalError as e:
            return self._failed(dev, eid, e)
        except (KeyError, ValueError, TypeError) as e:        # this call is not translated yet: Home Assistant does it
            log.warning("cannot send %s directly (%s), using Home Assistant", eid, e)
            return False
        if code != 0:
            return self._failed(dev, eid, why_code(code))
        self._note(m["did"], "local")
        return True

    async def _light(self, did: str, eid: str, service: str, data: dict) -> bool:
        """light.turn_on / turn_off with at most a brightness; colour temperature, colour, transition ... stay with Home Assistant."""
        if did not in self.enabled() or did not in self.devs:
            return False
        dev = self.devs[did]
        if dev.model.startswith("yeelink.light."):
            return await self._yeelight(dev, eid, service, data)
        extra = set(data) - {"entity_id", "brightness_pct"}
        if service == "turn_off" and not extra:
            writes = [(1, False)]
        elif service == "turn_on" and not extra:
            writes = [(1, True)] + ([(2, max(1, min(100, int(data["brightness_pct"]))))] if "brightness_pct" in data else [])
        else:
            return False
        try:
            for piid, value in writes:
                code = await dev.set(2, piid, value)
                if code != 0:
                    raise ml.LocalError(why_code(code))
        except ml.LocalError as e:
            return self._failed(dev, eid, e)
        except (ValueError, TypeError) as e:
            log.warning("cannot send %s directly (%s), using Home Assistant", eid, e)
            return False
        self._note(did, "local")
        return True

    async def _yeelight(self, dev: ml.Device, eid: str, service: str, data: dict) -> bool:
        """Yeelink lights speak the older Yeelight commands (set_power / set_bright / set_ct_abx), not MIoT properties."""
        extra = set(data) - {"entity_id", "brightness_pct", "color_temp_kelvin"}
        if extra or service not in ("turn_on", "turn_off"):
            return False
        calls: list[tuple[str, list]] = []
        if service == "turn_off":
            calls.append(("set_power", ["off", "smooth", 500]))
        else:
            calls.append(("set_power", ["on", "smooth", 500]))
            if "brightness_pct" in data:
                calls.append(("set_bright", [max(1, min(100, int(data["brightness_pct"]))), "smooth", 500]))
            if "color_temp_kelvin" in data:
                calls.append(("set_ct_abx", [max(2700, min(6500, int(data["color_temp_kelvin"]))), "smooth", 500]))
        try:
            for method, params in calls:
                if await dev.call(method, params) != ["ok"]:
                    raise ml.LocalError(f"{method} refused")
        except ml.LocalError as e:
            return self._failed(dev, eid, e)
        except (ValueError, TypeError) as e:
            log.warning("cannot send %s directly (%s), using Home Assistant", eid, e)
            return False
        self._note(dev.did, "local")
        return True

    async def _climate(self, did: str, eid: str, service: str, data: dict) -> bool:
        """climate.turn_on/off, set_hvac_mode, set_temperature, set_fan_mode, set_swing_mode; anything else stays with Home Assistant."""
        if did not in self.enabled() or did not in self.devs:
            return False
        dev = self.devs[did]
        try:
            writes: list[tuple[int, int, object]] = []
            if service in ("turn_on", "turn_off") and set(data) == {"entity_id"}:
                writes = [(2, 1, service == "turn_on")]
            elif service == "set_hvac_mode" and set(data) <= {"entity_id", "hvac_mode"}:
                writes = self._mode(data["hvac_mode"])
            elif service == "set_temperature" and set(data) <= {"entity_id", "temperature", "hvac_mode"}:
                if "hvac_mode" in data:
                    writes += self._mode(data["hvac_mode"])
                # the target temperature is 2/4 on the air conditioner but 2/5 on the baseboard heater (2/4 is not a property there: -4002):
                # ask the spec which one it is
                spec = self._spec(dev.model)
                tp = next((i for (sv, i), pr in (spec.props.items() if spec else []) if sv == 2 and pr.get("name") == "target-temperature"), 4)
                writes.append((2, tp, float(data["temperature"])))
            elif service == "set_fan_mode" and set(data) == {"entity_id", "fan_mode"}:
                spec = self._spec(dev.model)
                writes = [(3, 2, spec.choice(3, 2, str(data["fan_mode"])))]
            elif service == "set_swing_mode" and set(data) == {"entity_id", "swing_mode"}:
                h, v = SWING[data["swing_mode"]]
                writes = [(3, 3, h), (3, 4, v)]
            else:
                return False
            for s, i, value in writes:
                code = await dev.set(s, i, value)
                if code != 0:
                    raise ml.LocalError(why_code(code, off_hint=True))
        except ml.LocalError as e:
            return self._failed(dev, eid, e)
        except (KeyError, ValueError, TypeError) as e:
            log.warning("cannot send %s directly (%s), using Home Assistant", eid, e)
            return False
        self._note(did, "local")
        return True

    @staticmethod
    def _mode(hvac: str) -> list[tuple[int, int, object]]:
        if hvac == "off":
            return [(2, 1, False)]
        return [(2, 1, True), (2, 2, HVAC[hvac])]

    async def _fan(self, did: str, siid: int, eid: str, service: str, data: dict) -> bool:
        """fan.turn_on / turn_off / set_percentage / set_preset_mode; oscillation and the rest stay with Home Assistant."""
        if did not in self.enabled() or did not in self.devs:
            return False
        dev = self.devs[did]
        try:
            extra = set(data) - {"entity_id"}
            if service == "turn_off" and not extra:
                writes = [(siid, 1, False)]
            elif service == "turn_on" and extra <= {"percentage", "preset_mode"}:
                writes = [(siid, 1, True)]
                if "preset_mode" in data:
                    writes.append((siid, 3, self._spec(dev.model).choice(siid, 3, str(data["preset_mode"]))))
                if "percentage" in data:
                    writes.append((siid, 5, max(1, min(100, int(data["percentage"])))))
            elif service == "set_percentage" and extra == {"percentage"}:
                pct = int(data["percentage"])
                writes = [(siid, 1, False)] if pct <= 0 else [(siid, 1, True), (siid, 5, min(100, pct))]
            elif service == "set_preset_mode" and extra == {"preset_mode"}:
                writes = [(siid, 3, self._spec(dev.model).choice(siid, 3, str(data["preset_mode"])))]
            else:
                return False
            for s, i, value in writes:
                code = await dev.set(s, i, value)
                if code != 0:
                    raise ml.LocalError(why_code(code))
        except ml.LocalError as e:
            return self._failed(dev, eid, e)
        except (KeyError, ValueError, TypeError) as e:
            log.warning("cannot send %s directly (%s), using Home Assistant", eid, e)
            return False
        self._note(did, "local")
        return True

    def reads(self) -> set[str]:
        """Devices whose state is taken from the device itself: the direct ones plus those listed for reading only."""
        return set(self.enabled()) | set(self.store.get_setting("local_read_dids", []))

    async def overlay(self, states: dict[str, dict]) -> dict[str, dict]:
        """Put the device's own answer over Home Assistant's (stale) state for entities of directly read devices."""
        want: dict[str, dict[tuple[int, int], list[str]]] = {}
        for eid in states:
            m = ENT.match(eid)
            if m and m["kind"] == "p" and eid.split(".")[0] in DOMAINS and m["did"] in self.devs and m["did"] in self.reads():
                want.setdefault(m["did"], {}).setdefault((int(m["s"]), int(m["i"])), []).append(eid)
        for did, props in want.items():
            dev, spec = self.devs[did], self._spec(self.devs[did].model)
            if spec is None:
                continue
            keys, vals = sorted(props), {}
            for i in range(0, len(keys), 6):
                try:
                    vals.update(await dev.get(keys[i:i + 6]))
                except ml.LocalError:
                    break
            for k, v in vals.items():
                for eid in props[k]:
                    old = states[eid]
                    text = shown(eid.split(".")[0], spec.props.get(k) or {}, v, (old.get("attributes") or {}).get("options"))
                    if text is not None and old.get("state") not in ("unknown", "unavailable"):
                        states[eid] = {**old, "state": text}
        return states
