"""Turns HA's registries + states into a Mijia-like home model, with no I/O.

HA gives every Xiaomi device dozens of entities named "<device>  <service> <property>" (a "*" in
front of the service marks non-standard MIoT properties). Mijia shows only a few of them; this module
decides which: the device kind, the entities the card toggles, a few "roles" the page uses for the
status line and the main controls, the everyday features, and the rest tucked under 更多设置.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Properties too generic to stand alone as a label: "卧室氛围灯 开关" reads better than "开关".
GENERIC_PROPS = {"开关", "开关机", "温度", "湿度", "工作状态", "故障", "剩余时间", "亮度"}
# Services that just repeat what the device is; dropped from labels ("空调 ECO节能模式" -> "ECO节能模式").
KIND_SERVICES = {"空调", "灯", "风扇", "热水器", "加湿器", "电暖器", "洗碗机", "微波炉", "压力锅", "电磁炉",
                 "空气炸锅", "冰箱", "跑步机", "按摩仪", "音箱", "智能音箱", "网关", "路由器", "洗衣机", "开关"}
# Settings nobody touches day to day: go to 更多设置.
NOISE = ("提示音", "童锁", "物理控制锁", "指示灯", "防闪烁", "接线方式", "自检", "不使用", "开发者模式", "默认上电",
         "渐变", "渐亮", "渐灭", "DHCP", "mac地址", "IP地址", "WiFi网络名称", "音频ID", "弹窗", "接入方式", "喜好模式",
         "滤网类型", "食谱类型", "多段烹饪", "设备被重置", "P2P", "存储", "云服务", "会员分片", "电池插入", "水印",
         "畸变", "录制", "编码", "对讲", "分辨率", "UV指示灯", "提醒开关", "上电状态")
# Detail page order: things you switch, then pick, then adjust, then press, then just read.
DOMAIN_ORDER = {"switch": 0, "light": 0, "select": 1, "number": 2, "button": 3, "binary_sensor": 4, "sensor": 5}
# Card order inside a room: what you use most first, infrastructure last.
KIND_ORDER = ["ac", "heater", "light", "switch", "bath_heater", "fan", "humidifier", "water_heater", "plug", "lock",
              "camera", "washer", "dishwasher", "cooker", "vacuum", "fridge", "speaker", "treadmill", "massager", "thermo",
              "presence", "contact", "flood", "smoke", "gas", "remote", "other", "gateway", "router"]
SKIP_DOMAINS = {"event", "text", "notify", "update", "device_tracker", "tts", "conversation", "image"}

KIND_NAMES = {
    "ac": "空调", "heater": "取暖器", "light": "灯", "switch": "开关", "plug": "插座", "fan": "风扇",
    "humidifier": "加湿器", "water_heater": "热水器", "bath_heater": "浴霸", "lock": "门锁", "camera": "摄像机",
    "presence": "人体传感器", "contact": "门窗传感器", "flood": "水浸传感器", "smoke": "烟雾报警器",
    "gas": "燃气报警器", "thermo": "温湿度计", "fridge": "冰箱", "washer": "洗衣机", "dishwasher": "洗碗机",
    "cooker": "厨房电器", "speaker": "音箱", "treadmill": "跑步机", "gateway": "网关", "router": "路由器",
    "remote": "无线开关", "massager": "按摩仪", "vacuum": "扫地机", "other": "设备",
}


@dataclass
class Ent:
    entity_id: str
    friendly_name: str
    attributes: dict
    category: str | None = None   # HA entity_category: config / diagnostic / None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def device_class(self) -> str | None:
        return self.attributes.get("device_class")


def parse_name(friendly: str, device_name: str) -> tuple[str, str, bool]:
    """(service, property, nonstandard) from "<device>  <service> <property>"."""
    rest = friendly[len(device_name):] if friendly.startswith(device_name) else friendly
    rest = rest.strip()
    nonstandard = rest.startswith("*")
    rest = rest.lstrip("* ").strip()
    parts = rest.split(" ", 1)
    if len(parts) == 1:
        return parts[0], parts[0], nonstandard
    return parts[0], parts[1].strip(), nonstandard


def make_label(service: str, prop: str) -> str:
    # Some property names carry a long explanation after a comma / colon / paren; keep the head.
    prop = re.split(r"[，,：:（(]", prop, maxsplit=1)[0].strip() or prop
    if service == prop or prop not in GENERIC_PROPS:
        return prop
    if prop == "开关":
        return service
    return prop if service in KIND_SERVICES else f"{service}{prop}"


def is_noise(label: str, service: str) -> bool:
    return any(n in label or n in service for n in NOISE)


def classify(model: str, ents: list[Ent]) -> str:
    m = model or ""
    domains = {e.domain for e in ents if e.category is None}
    dcs = {e.device_class for e in ents}
    hvac = next((e.attributes.get("hvac_modes", []) for e in ents if e.domain == "climate"), [])
    for key, kind in ((".washer.", "washer"), (".dishwasher.", "dishwasher"), ("bhf_light", "bath_heater"),
                      (".lock.", "lock"), (".camera.", "camera"), ("sensor_smoke", "smoke"), ("sensor_gas", "gas"),
                      (".fridge.", "fridge"), ("treadmill", "treadmill"), (".gateway.", "gateway"),
                      (".router.", "router"), (".remote.", "remote"), ("magic_touch", "massager"),
                      ("sensor_ht", "thermo"), (".plug.", "plug")):
        if key in m:
            return kind
    if "vacuum" in domains:
        return "vacuum"
    if "climate" in domains:
        return "ac" if "cool" in hvac else "heater"
    for d in ("light", "fan", "humidifier", "water_heater"):
        if d in domains:
            return d
    if "media_player" in domains:
        return "speaker"
    if "moisture" in dcs:
        return "flood"
    if "occupancy" in dcs or "motion" in dcs:
        return "presence"
    if "door" in dcs or "window" in dcs:
        return "contact"
    if any(x in m for x in ("cooker", "microwave", "fryer", "oven", "kettle")):
        return "cooker"
    if ".switch." in m or "switch" in domains:
        return "switch"
    return "other"


def build_device(dev: dict, area_name: str | None, ents: list[Ent]) -> dict | None:
    """One device for the page, or None when it has nothing worth showing."""
    name = dev.get("name_by_user") or dev.get("name") or "设备"
    model = dev.get("model") or ""
    kind = classify(model, ents)
    rows = []
    events = []   # button presses, doorbell...: not shown on the page, but triggers for automations
    for e in ents:
        if e.domain == "event":
            service, prop, nonstandard = parse_name(e.friendly_name, name)
            if not nonstandard and e.category is None and not is_noise(prop, service):
                label = prop if service in prop or service in KIND_SERVICES or len(service) > 6 else f"{service}{prop}"
                events.append({"entity_id": e.entity_id, "label": label})
            continue
        if e.domain in SKIP_DOMAINS:
            continue
        service, prop, nonstandard = parse_name(e.friendly_name, name)
        if nonstandard:
            continue
        label = make_label(service, prop)
        rows.append({"e": e, "label": label, "service": service, "prop": prop,
                     "noise": e.category is not None or is_noise(label, service)})
    if not rows and kind != "washer":
        return None

    roles: dict[str, str] = {}

    def take(role: str, pred):
        for r in rows:
            if role not in roles and pred(r):
                roles[role] = r["e"].entity_id

    main_domain = {"ac": "climate", "heater": "climate", "light": "light", "fan": "fan", "humidifier": "humidifier",
                   "water_heater": "water_heater", "speaker": "media_player", "bath_heater": "light", "vacuum": "vacuum"}.get(kind)
    if main_domain:
        take("main", lambda r: r["e"].domain == main_domain and not r["noise"])
    take("temperature", lambda r: r["e"].domain == "sensor" and r["e"].device_class == "temperature" and not r["noise"])
    take("humidity", lambda r: r["e"].domain == "sensor" and r["e"].device_class == "humidity")
    take("battery", lambda r: r["e"].device_class == "battery")
    take("status", lambda r: r["e"].domain == "sensor" and r["prop"] in ("工作状态", "门状态"))
    take("left", lambda r: r["e"].domain == "sensor" and r["prop"] == "剩余时间")
    take("occupancy", lambda r: r["e"].device_class in ("occupancy", "motion"))
    take("door", lambda r: r["e"].device_class in ("door", "window"))
    take("moisture", lambda r: r["e"].device_class == "moisture")
    take("gas", lambda r: r["e"].domain == "sensor" and "浓度" in r["prop"])
    take("illuminance", lambda r: r["e"].device_class == "illuminance")
    take("power", lambda r: r["e"].device_class == "power")
    take("energy", lambda r: r["e"].device_class == "energy")
    take("fault", lambda r: r["e"].domain == "sensor" and r["prop"].startswith("故障") and not r["noise"])
    if kind == "fridge":
        for r in rows:
            if r["e"].device_class == "temperature":
                roles[f"zone:{r['service']}"] = r["e"].entity_id

    # What the card's power button(s) control: the main entity, or the plain "X 开关" switches
    # (wall-switch keys are named after what they light, e.g. "卧室氛围灯 开关").
    toggles = []
    if "main" in roles and kind in ("ac", "heater", "light", "fan", "humidifier", "water_heater"):
        toggles.append({"entity_id": roles["main"], "label": KIND_NAMES[kind]})
    if kind == "bath_heater":
        toggles += [{"entity_id": r["e"].entity_id, "label": r["label"]} for r in rows
                    if r["e"].domain in ("light", "switch") and not r["noise"]]
    elif not toggles and kind not in ("washer", "camera", "dishwasher"):
        toggles += [{"entity_id": r["e"].entity_id, "label": r["service"] if r["service"] not in KIND_SERVICES else name}
                    for r in rows if r["e"].domain == "switch" and r["prop"] in ("开关", "开关机") and not r["noise"]]
    toggles = toggles[:3]

    used = set(roles.values()) | {t["entity_id"] for t in toggles}
    features, more = [], []
    for r in sorted(rows, key=lambda r: DOMAIN_ORDER.get(r["e"].domain, 9)):
        if r["e"].entity_id in used:
            continue
        (more if r["noise"] else features).append({"entity_id": r["e"].entity_id, "label": r["label"]})

    room = area_name or ""
    home = ""
    if " " in room:
        home, room = room.split(" ", 1)
    return {
        "id": dev["id"], "name": name, "model": model, "kind": kind, "kind_name": KIND_NAMES[kind],
        "home": home, "room": room, "roles": roles, "toggles": toggles,
        "features": features, "more": more, "events": events,
        # every entity's short label, for the automation editor ("厕所传感器 · 有人无人状态")
        "labels": {**{r["e"].entity_id: r["label"] for r in rows}, **{ev["entity_id"]: ev["label"] for ev in events}},
        "entities": sorted({r["e"].entity_id for r in rows}),
    }


UNASSIGNED = "未分配"
# Integrations whose "devices" are HA's own plumbing, not things in the home (they never get a room).
INFRA_DOMAINS = {"bluetooth", "usb", "hassio", "homeassistant", "cloud", "backup", "sun", "met", "mobile_app",
                 "google_translate", "radio_browser", "shopping_list", "go2rtc"}


def build_home(devices: list[dict], entity_reg: list[dict], areas: list[dict], states: dict[str, dict],
               entry_domains: dict[str, str] | None = None) -> dict:
    """entry_domains: config entry id -> integration domain, to tell HA plumbing apart from new devices
    that just haven't been given a room yet (those land in 未分配 of the main home)."""
    area_names = {a["area_id"]: a["name"] for a in areas}
    entry_domains = entry_domains or {}
    by_dev: dict[str, list[Ent]] = {}
    for er in entity_reg:
        if er.get("disabled_by") or er.get("hidden_by") or not er.get("device_id"):
            continue
        st = states.get(er["entity_id"])
        if st is None:
            continue
        a = st.get("attributes", {})
        by_dev.setdefault(er["device_id"], []).append(
            Ent(er["entity_id"], a.get("friendly_name", er["entity_id"]), a, er.get("entity_category")))
    out = []
    for d in devices:
        if d.get("disabled_by") or d.get("entry_type") == "service" or d["id"] not in by_dev:
            continue
        area = area_names.get(d.get("area_id"))
        if area is None and any(entry_domains.get(c) in INFRA_DOMAINS for c in d.get("config_entries", [])):
            continue   # HA's own plumbing (Bluetooth adapter...) - services were dropped above
        dev = build_device(d, area, sorted(by_dev[d["id"]], key=lambda e: e.entity_id))
        if dev:
            if area is None:
                dev["room"] = UNASSIGNED
            out.append(dev)
    # Devices without a room belong to the home with the most devices.
    counts: dict[str, int] = {}
    for dev in out:
        if dev["room"] != UNASSIGNED:
            counts[dev["home"]] = counts.get(dev["home"], 0) + 1
    main_home = max(counts, key=counts.get) if counts else ""
    for dev in out:
        if dev["room"] == UNASSIGNED:
            dev["home"] = main_home
    homes: dict[str, list[str]] = {}
    for dev in out:
        rooms = homes.setdefault(dev["home"], [])
        if dev["room"] not in rooms:
            rooms.append(dev["room"])
    # 未分配 first, so a newly added device is the first thing seen.
    order = [UNASSIGNED, "客厅", "卧室", "主卧", "次卧", "厨房", "餐厅", "卫生间", "随身/卫生间", "阳台", "书房"]
    for rooms in homes.values():
        rooms.sort(key=lambda r: (order.index(r) if r in order else len(order), r))
    return {"homes": [{"name": h, "rooms": r} for h, r in sorted(homes.items(), key=lambda kv: -len(kv[1]))],
            "devices": sorted(out, key=lambda d: (d["home"], d["room"], KIND_ORDER.index(d["kind"]), d["name"]))}
