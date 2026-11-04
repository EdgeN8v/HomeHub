"""Home service: keeps the device model and a live state cache, streams changes to open pages,
and runs the controls they send - only on entities that belong to a device in the model."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid

from aiohttp import web

from .ha import HA
from .home import build_home

log = logging.getLogger("home")
MODEL_TTL = 600   # rebuild the device model every 10 min to pick up new devices / renames

# domain -> services the page may call
ALLOWED = {
    "vacuum": {"start", "pause", "stop", "return_to_base", "locate", "set_fan_speed"},
    "switch": {"turn_on", "turn_off", "toggle"},
    "light": {"turn_on", "turn_off", "toggle"},
    "fan": {"turn_on", "turn_off", "toggle", "set_percentage", "set_preset_mode", "oscillate"},
    "climate": {"turn_on", "turn_off", "set_temperature", "set_hvac_mode", "set_fan_mode", "set_preset_mode", "set_swing_mode"},
    "text": {"set_value"},
    "humidifier": {"turn_on", "turn_off", "toggle", "set_humidity", "set_mode"},
    "water_heater": {"turn_on", "turn_off", "set_temperature", "set_operation_mode"},
    "media_player": {"turn_on", "turn_off", "volume_set", "volume_mute", "media_play_pause"},
    "select": {"select_option"},
    "number": {"set_value"},
    "button": {"press"},
    "automation": {"turn_on", "turn_off", "trigger"},
}
DROP_ATTRS = {"friendly_name", "icon", "entity_picture", "supported_features", "attribution"}


def slim(st: dict) -> dict:
    return {"s": st["state"], "a": {k: v for k, v in st.get("attributes", {}).items() if k not in DROP_ATTRS}}



def _plain(s: str | None) -> str:
    """HA's setup texts are markdown: keep the words, drop links and emphasis."""
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"", s or "")
    return re.sub(r"[*_`]{1,3}", "", s).strip()


def _field(f: dict) -> dict | None:
    """One input of an HA config-flow form (voluptuous_serialize output) as the page draws it."""
    name = f.get("name")
    sel = f.get("selector") or {}
    t = f.get("type")
    out = {"name": name, "required": bool(f.get("required")), "kind": "text"}
    if "default" in f:
        out["default"] = f["default"]
    if "text" in sel:
        out["kind"] = "password" if sel["text"].get("type") == "password" else "text"
    elif "number" in sel or t in ("integer", "float"):
        out["kind"] = "number"
    elif "boolean" in sel or t == "boolean":
        out["kind"] = "bool"
    elif "select" in sel:
        opts = sel["select"].get("options") or []
        out.update(kind="select", options=[[o, o] if isinstance(o, str) else [o.get("value"), o.get("label", o.get("value"))] for o in opts])
    elif t == "select":
        opts = f.get("options") or []
        out.update(kind="select", options=[[o[0], o[1]] if isinstance(o, (list, tuple)) else [o, o] for o in (opts.items() if isinstance(opts, dict) else opts)])
    elif t == "constant":
        return None
    elif t == "string" or not t:
        out["kind"] = "password" if "password" in (name or "") else "text"
    else:
        return None
    return out


def _flow_input(raw: dict) -> dict:
    """Form values from the page: numbers arrive as numbers or digit strings; empty optional fields are dropped."""
    out = {}
    for k, v in raw.items():
        if v is None or v == "":
            continue
        out[k] = v
    return out

class Home:
    def __init__(self, ha: HA):
        self.ha = ha
        self.model: dict = {"homes": [], "devices": []}
        self.entities: set[str] = set()
        self.states: dict[str, dict] = {}
        self.built_at = 0.0
        self._tr_cache: dict[str, dict] = {}   # integration -> HA's setup-screen wording
        self.areas: dict[str, str] = {}
        self.device_ids: set[str] = set()
        self.event_ids: set[str] = set()
        self.subscribers: set[asyncio.Queue] = set()

    async def refresh(self):
        r = await self.ha.ws_commands(["config/device_registry/list", "config/entity_registry/list",
                                       "config/area_registry/list", "config_entries/get", "get_states"])
        self.states = {s["entity_id"]: s for s in r["get_states"]}
        self.model = build_home(r["config/device_registry/list"], r["config/entity_registry/list"],
                                r["config/area_registry/list"], self.states,
                                {e["entry_id"]: e["domain"] for e in r["config_entries/get"]})
        self.entities = {e for d in self.model["devices"] for e in d["entities"]}
        self.areas = {a["name"]: a["area_id"] for a in r["config/area_registry/list"]}
        self.device_ids = {d["id"] for d in self.model["devices"]}
        self.event_ids = {ev["entity_id"] for d in self.model["devices"] for ev in d["events"]}
        # Outdoor weather for the header (HA's default Met.no forecast entity).
        self.model["weather"] = next((e for e in sorted(self.states) if e.startswith("weather.")), None)
        if self.model["weather"]:
            self.entities.add(self.model["weather"])
        # Automations live in HA (they keep running without internet); the 智能 page lists and switches them.
        self.model["automations"] = sorted(e for e in self.states if e.startswith("automation."))
        self.model["automation_names"] = {e: self.states[e]["attributes"].get("friendly_name", e)
                                          for e in self.model["automations"]}
        # The plain-language "如果…→…" line kept in each automation's description, for the 智能 page.
        descs = {}
        for e in self.model["automations"]:
            aid = self.states[e]["attributes"].get("id")
            cfg = await self.ha.automation_config(aid) if aid else None
            if cfg and cfg.get("description"):
                descs[e] = cfg["description"]
        self.model["automation_desc"] = descs
        self.entities.update(self.model["automations"])
        self.built_at = time.monotonic()
        log.info("home model: %d devices, %d entities", len(self.model["devices"]), len(self.entities))

    def check_devices(self, ids: list[str]):
        if not ids or any(i not in self.device_ids for i in ids):
            raise web.HTTPBadRequest(text="unknown device")

    async def rename(self, device_id: str, name: str):
        """Renames in HA only (name_by_user); Mijia keeps its own name."""
        self.check_devices([device_id])
        name = name.strip()
        if not name or len(name) > 40:
            raise web.HTTPBadRequest(text="name must be 1-40 characters")
        await self.ha.ws_commands([{"type": "config/device_registry/update", "device_id": device_id, "name_by_user": name}])
        await self.refresh()

    async def move(self, device_ids: list[str], home: str, room: str):
        """Moves devices to another room of the same home (HA areas are named "<home> <room>")."""
        self.check_devices(device_ids)
        area = self.areas.get(f"{home} {room}".strip())
        if not area:
            raise web.HTTPBadRequest(text=f"no room {home} {room}")
        await self.ha.ws_commands([{"type": "config/device_registry/update", "device_id": d, "area_id": area}
                                   for d in device_ids])
        await self.refresh()

    # ---- automations (the 智能 page's editor)

    def automation_id(self, entity_id: str) -> str:
        aid = self.states.get(entity_id, {}).get("attributes", {}).get("id")
        if entity_id not in self.model.get("automations", []) or not aid:
            raise web.HTTPNotFound(text="no such automation")
        return aid

    async def get_automation(self, entity_id: str) -> dict:
        cfg = await self.ha.automation_config(self.automation_id(entity_id))
        if cfg is None:
            raise web.HTTPNotFound(text="automation not editable (not made in the UI)")
        return cfg

    def check_automation(self, cfg: dict) -> dict:
        """Only what the editor builds: state / numeric_state / time triggers, a time-window condition,
        device actions on this home's entities (whitelisted services) and delays."""
        known = self.entities | self.event_ids

        def ent_ok(e):
            ids = e if isinstance(e, list) else [e]
            return bool(ids) and all(isinstance(i, str) and i in known for i in ids)

        alias = str(cfg.get("alias", "")).strip()
        if not 1 <= len(alias) <= 60:
            raise web.HTTPBadRequest(text="名字要 1-60 个字")
        triggers, conditions, actions = cfg.get("triggers") or [], cfg.get("conditions") or [], cfg.get("actions") or []
        if not triggers or not actions:
            raise web.HTTPBadRequest(text="至少要一个条件和一个动作")
        for t in triggers:
            kind = t.get("trigger")
            if kind in ("state", "numeric_state"):
                if not ent_ok(t.get("entity_id")):
                    raise web.HTTPBadRequest(text="条件里有不认识的设备")
            elif kind != "time":
                raise web.HTTPBadRequest(text=f"不支持的条件 {kind}")
        for c in conditions:
            if c.get("condition") != "time":
                raise web.HTTPBadRequest(text="只支持生效时间段")
        for a in actions:
            if "delay" in a:
                continue
            domain, _, service = str(a.get("action", "")).partition(".")
            if service not in ALLOWED.get(domain, ()) or domain == "automation":
                raise web.HTTPBadRequest(text=f"不支持的动作 {a.get('action')}")
            if not ent_ok((a.get("target") or {}).get("entity_id")):
                raise web.HTTPBadRequest(text="动作里有不认识的设备")
        return {"alias": alias, "description": str(cfg.get("description", ""))[:300], "mode": "single",
                "triggers": triggers, "conditions": conditions, "actions": actions}

    async def save_automation(self, entity_id: str | None, cfg: dict) -> str:
        cfg = self.check_automation(cfg)
        aid = self.automation_id(entity_id) if entity_id else "hh_" + uuid.uuid4().hex[:12]
        await self.ha.save_automation_config(aid, cfg)
        await asyncio.sleep(2)   # HA reloads automations
        await self.refresh()
        return next((e for e in self.model["automations"] if self.states[e]["attributes"].get("id") == aid), "")

    async def delete_automation(self, entity_id: str):
        await self.ha.delete_automation_config(self.automation_id(entity_id))
        await asyncio.sleep(2)
        await self.refresh()

    # ---- adding devices

    async def discovered(self) -> list[dict]:
        """Things HA found on the network but nobody has added yet (its "已发现" list)."""
        r = await self.ha.ws_commands(["config_entries/flow/progress"])
        out = []
        for f in r["config_entries/flow/progress"]:
            ctx = f.get("context", {})
            if ctx.get("source") in ("user", "reauth", "reconfigure"):
                continue   # a setup someone started by hand, not a discovery
            title = (ctx.get("title_placeholders") or {}).get("name") or f["handler"]
            out.append({"flow_id": f["flow_id"], "domain": f["handler"], "title": title})
        return out

    # ---- adding a device = walking one of HA's config flows -------------------------------------
    _LABELS = {"username": "账号（邮箱或手机号）", "password": "密码", "email": "邮箱", "host": "地址（IP 或域名）",
               "port": "端口", "pin": "配对码", "pairing_code": "配对码", "code": "验证码", "api_key": "API Key",
               "token": "令牌", "name": "名称"}

    async def flow(self, flow_id: str, user_input: dict | None = None) -> dict:
        """One step of adding a discovered device, in a shape the page can draw as a form.
        user_input=None opens the flow: a bare confirmation is accepted at once (as before), anything that
        asks for something (account, code, pairing code ...) is returned as a form for the person to fill."""
        r = await self.ha.ws_commands(["config_entries/flow/progress"])
        if flow_id not in {f["flow_id"] for f in r["config_entries/flow/progress"]}:
            raise web.HTTPBadRequest(text="这个设备已经不在待添加列表里了，刷新一下")
        errors: dict = {}
        if user_input is None:
            step = await self.ha.flow_get(flow_id)
            if step.get("type") == "form" and not step.get("data_schema"):
                status, step = await self.ha.flow_post(flow_id, {})   # just "do you want to set this up?"
        else:
            status, step = await self.ha.flow_post(flow_id, _flow_input(user_input))
            errors = step.get("errors") if status == 400 else {}
            if status == 400 and not errors:
                return {"type": "error", "message": f"提交没成功（{step.get('message', status)}）"}
            if status == 400:
                step = await self.ha.flow_get(flow_id)   # the same form again, with the errors shown
        return await self._flow_json(flow_id, step, errors)

    async def _flow_json(self, flow_id: str, step: dict, errors: dict | None = None) -> dict:
        domain = step.get("handler") or ""
        tr = await self._flow_translations(domain)
        t = lambda key: tr.get(f"component.{domain}.config.{key}")   # noqa: E731
        ph = {k: str(v) for k, v in (step.get("description_placeholders") or {}).items()}
        fill = lambda s: re.sub(r"\{(\w+)\}", lambda m: ph.get(m.group(1), ""), s or "")   # noqa: E731
        kind = step.get("type")
        if kind == "create_entry":
            await asyncio.sleep(3)
            await self.refresh()
            return {"type": "done", "message": f"已添加：{step.get('title') or domain}"}
        if kind == "abort":
            reason = step.get("reason", "")
            return {"type": "abort", "message": _plain(fill(t(f"abort.{reason}"))) or f"没有添加（{reason}）"}
        if kind == "menu":
            opts = step.get("menu_options") or []
            names = {o: _plain(t(f"step.{step.get('step_id')}.menu_options.{o}")) or o for o in (opts if isinstance(opts, list) else opts)}
            return {"type": "menu", "flow_id": flow_id, "step_id": step.get("step_id"), "options": [[o, names[o]] for o in names],
                    "title": _plain(t(f"step.{step.get('step_id')}.title")) or domain}
        if kind != "form":
            return {"type": "abort", "message": f"这个设备的添加方式（{kind}）HomeHub 还不支持，可以先在 HA 里添加"}
        sid = step.get("step_id")
        fields = []
        for f in step.get("data_schema") or []:
            fd = _field(f)
            if not fd:
                continue
            fd["label"] = _plain(t(f"step.{sid}.data.{fd['name']}")) or self._LABELS.get(fd["name"], fd["name"])
            hint = _plain(t(f"step.{sid}.data_description.{fd['name']}"))
            if hint:
                fd["hint"] = fill(hint)
            fields.append(fd)
        errs = {k: (_plain(fill(t(f"error.{v}"))) or str(v)) for k, v in (errors or {}).items()}
        return {"type": "form", "flow_id": flow_id, "step_id": sid, "domain": domain, "fields": fields, "errors": errs,
                "title": _plain(fill(t(f"step.{sid}.title"))) or domain,
                "description": _plain(fill(t(f"step.{sid}.description")))}

    async def _flow_translations(self, domain: str) -> dict:
        if domain not in self._tr_cache:
            try:
                self._tr_cache[domain] = await self.ha.config_translations(domain)
            except Exception:   # noqa: BLE001 - labels are a nicety; the form still works with field names
                return {}
        return self._tr_cache[domain]

    async def sync_xiaomi(self) -> int:
        """Reload the Xiaomi integration so devices just paired in Mijia show up now, not in 10 minutes."""
        r = await self.ha.ws_commands(["config_entries/get"])
        for e in r["config_entries/get"]:
            if e["domain"] == "xiaomi_home":
                await self.ha.call("homeassistant", "reload_config_entry", {"entry_id": e["entry_id"]})
        before = len(self.model["devices"])
        await asyncio.sleep(15)   # the integration fetches the device list from Xiaomi again
        await self.refresh()
        return len(self.model["devices"]) - before

    async def on_change(self, entity_id: str, new: dict):
        self.states[entity_id] = new
        if entity_id in self.entities:
            msg = json.dumps({entity_id: slim(new)}, ensure_ascii=False)
            for q in list(self.subscribers):
                if q.qsize() < 500:
                    q.put_nowait(msg)

    def states_of(self, ids: list[str]) -> dict:
        """A few entities' current state, for the page to poll right after a control: the live
        stream can arrive late inside the app."""
        return {e: slim(self.states[e]) for e in ids if e in self.entities and e in self.states}

    async def snapshot(self) -> dict:
        if time.monotonic() - self.built_at > MODEL_TTL or not self.model["devices"]:
            await self.refresh()
        return {**self.model, "states": {e: slim(self.states[e]) for e in self.entities if e in self.states}}

    async def call(self, entity_id: str, service: str, data: dict):
        domain = entity_id.split(".", 1)[0]
        if entity_id not in self.entities or service not in ALLOWED.get(domain, ()):
            raise web.HTTPForbidden(text=f"{domain}.{service} on {entity_id} not allowed")
        data = {k: v for k, v in (data or {}).items() if k != "entity_id"}
        await self.ha.call(domain, service, {"entity_id": entity_id, **data})

    async def stream(self, request: web.Request) -> web.StreamResponse:
        """Server-sent events: one JSON {entity_id: {s, a}} per change, a comment every 20 s."""
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        try:
            await resp.write(b": hello\n\n")
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20)
                    await resp.write(f"data: {msg}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.subscribers.discard(q)
        return resp
