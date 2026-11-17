"""HomeHub backend: our own layer above Home Assistant. Serves the phone page and the API that the
page (and later 小爱 / our App) call. Everything device-specific goes through HA.

Reachable from the internet (NAS forwards public :8095 here), so it speaks HTTPS with a self-signed
cert the phone app pins, and every request except the icons needs the token: as a Bearer header,
or as the hub_token cookie (the app's WebView, or a browser after visiting /login?token=...)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import ssl
from datetime import datetime, time
from pathlib import Path

import aiohttp
from aiohttp import web

from . import machines

from .aclean import AcExtras
from .comfort import Comfort
from .ha import HA
from .kitchen import Kitchen, TZ as KITCHEN_TZ
from .energy import Energy
from .home_service import Home
from .endpoints import Endpoints
from .monitor import Monitor
from .notify import Notifier, KINDS as NOTIFY_KINDS, RULES as NOTIFY_RULES
from .store import Store
from .local_bridge import Bridge as LocalBridge
from .local_reader import Reader
from .ble_listen import BleListener
from . import hub1_link
from .router import Router, RouterError
from .camera import cam_proxy
from . import camrec
from . import journey
from .provision import Provisioner
from .washer import Washer
from .washer_logic import DEFAULT_FINISH_BY, DRUMS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"
DATA = Path(os.environ.get("HUB_DATA", "/data"))
UPDATES = DATA / "updates"
TOKEN = os.environ["HUB_TOKEN"]
COOKIE = "hub_token"
ENDPOINTS_TOKEN = os.environ.get("ENDPOINTS_TOKEN", "")      # may only add / change service entries in the address table (tools/register_service.py)
PUBLIC_PATHS = ("/static/icon.svg", "/static/manifest.json", "/static/idea.svg", "/static/idea_manifest.json")
IDEA_TOKEN = os.environ.get("IDEA_TOKEN", "")             # the Idea phone app: may only use the Idea page and /api/journey/*
IDEA_PATHS = ("/idea", "/journey", "/static/", "/api/journey")


def _authorized(request: web.Request) -> bool:
    given = request.cookies.get(COOKIE, "")
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        given = auth[len("Bearer "):]
    return bool(given) and hmac.compare_digest(given.encode(), TOKEN.encode())


def _idea_app(request: web.Request) -> bool:
    given = request.cookies.get(COOKIE, "")
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        given = auth[len("Bearer "):]
    return (bool(IDEA_TOKEN) and bool(given) and request.path.startswith(IDEA_PATHS)
            and hmac.compare_digest(given.encode(), IDEA_TOKEN.encode()))


def _table_writer(request: web.Request) -> bool:
    auth = request.headers.get("Authorization", "")
    return (bool(ENDPOINTS_TOKEN) and request.method == "POST" and request.path == "/api/endpoints" and auth.startswith("Bearer ")
            and hmac.compare_digest(auth[7:].encode(), ENDPOINTS_TOKEN.encode()))


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if _table_writer(request):
        request["narrow"] = True
    if request.get("narrow") or request.path in PUBLIC_PATHS or request.path == "/login" or (request.path == "/api/endpoints" and request.method == "GET") or request.path.startswith("/idea-dl/") or _authorized(request) or _idea_app(request):
        resp = await handler(request)
        if request.path.startswith("/static/"):
            # Versioned links (?v=<hash>, see page()) never change content: cache for a year. Anything
            # else is revalidated each time (304 when unchanged).
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if "v" in request.query else "no-cache"
        return resp
    if request.path.startswith("/api/"):
        raise web.HTTPUnauthorized(text="token required")
    return web.Response(status=401, content_type="text/html",
                        text="<meta charset=utf-8><p style='font:16px sans-serif;padding:24px'>请用 HomeHub App 打开。</p>")


async def login(request: web.Request):
    """Browser access: /login?token=... sets the cookie once and goes to the page."""
    given = request.query.get("token", "")
    if not (given and hmac.compare_digest(given.encode(), TOKEN.encode())):
        raise web.HTTPUnauthorized(text="wrong token")
    nxt = request.query.get("next", "/")                     # e.g. /login?token=...&next=/journey
    resp = web.HTTPFound(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
    resp.set_cookie(COOKIE, TOKEN, max_age=10 * 365 * 86400, secure=True, httponly=True, samesite="Strict")
    raise resp


def _phone(value) -> str | None:
    """Which phone is asking (its model, from the WebView user agent), so each person keeps their own
    card order. Anything odd is treated as no phone, i.e. the shared order."""
    return value if isinstance(value, str) and re.fullmatch(r"[\w .+-]{1,40}", value) else None


async def home_snapshot(request: web.Request):
    if request.query.get("fresh"):          # pull-to-refresh: rebuild the model from HA now (new devices, renames)
        await request.app["home"].refresh()
    snap = await request.app["home"].snapshot()
    store = request.app["store"]
    phone = _phone(request.query.get("phone"))
    # this phone's own order per room, falling back to the shared one for rooms it never rearranged
    snap["order"] = {**store.get_setting("card_order", {}), **(store.get_setting(f"card_order:{phone}", {}) if phone else {})}
    snap["hidden"] = store.get_setting("hidden_devices", [])
    snap["comfort"] = request.app["comfort"].config()
    snap["ac_clean"] = request.app["acx"].clean_all()
    snap["kitchen_resv"] = request.app["kitchen"].reservations()
    snap["kitchen_recipes"] = request.app["kitchen"].recipes()
    snap["energy_meter"] = request.app["energy"].meter_plug_id()
    resp = web.json_response(snap, dumps=_dumps)
    resp.enable_compression()   # ~180 KB of JSON -> a few dozen KB over 5G
    return resp


async def save_order(request: web.Request):
    """Card order the user dragged into place: {"key": "<home>/<room>", "ids": [device ids], "phone": model}.
    Kept per phone; a request without a phone (older page) writes the shared order new phones start from."""
    body = await request.json()
    key, ids = body.get("key"), body.get("ids")
    if not isinstance(key, str) or not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise web.HTTPBadRequest(text="need key and ids")
    phone = _phone(body.get("phone"))
    setting = f"card_order:{phone}" if phone else "card_order"
    order = request.app["store"].get_setting(setting, {})
    order[key] = ids
    request.app["store"].set_setting(setting, order)
    return web.json_response({"ok": True})


async def home_states(request: web.Request):
    ids = [i for i in request.query.get("ids", "").split(",") if i][:60]
    return web.json_response(request.app["home"].states_of(ids), dumps=_dumps)


async def home_stream(request: web.Request):
    return await request.app["home"].stream(request)


async def home_call(request: web.Request):
    body = await request.json()
    logging.getLogger("home").info("call %s %s %s", body.get("entity_id", ""), body.get("service", ""), body.get("data") or {})
    meter = request.app["energy"].meter_plug_id()
    if meter and body.get("service") in ("turn_off", "toggle") and any(
            body.get("entity_id") in d.get("entities", []) for d in request.app["home"].model["devices"] if d["id"] == meter):
        raise web.HTTPBadRequest(text="这个插座上接着冰箱，要一直供电，所以不能关")
    if "stop_washing" in body.get("entity_id", ""):
        request.app["washer"].note_stop(body["entity_id"])
    try:
        await request.app["home"].call(body.get("entity_id", ""), body.get("service", ""), body.get("data") or {})
    except aiohttp.ClientResponseError as e:
        msg = getattr(e, "message", "") or "设备没有接受指令"
        # 停止洗涤 while nothing is running is refused with -4404: it is already stopped
        if "-4404" in msg and "stop_washing" in body.get("entity_id", ""):
            return web.json_response({"ok": True, "note": "已经是停止状态"})
        raise web.HTTPBadGateway(text=msg)
    return web.json_response({"ok": True})


def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


async def ac_history(request: web.Request):
    """The air conditioner page's 室内环境 / 电量统计: the unit's room temperature over the last day, and the
    per-day kWh and running hours HomeHub has been keeping (HA itself only holds ~10 days)."""
    from datetime import timedelta, timezone
    home = request.app["home"]
    eid = request.query.get("entity_id")
    dev = next((d for d in home.model["devices"] if d["roles"].get("main") == eid), None)
    if not dev:
        raise web.HTTPNotFound()
    temps = await request.app["ha"].history_series(datetime.now(timezone.utc) - timedelta(hours=24), eid, "current_temperature")
    return web.json_response({"temp": [[t.isoformat(), v] for t, v in temps], "daily": request.app["acx"].daily(eid)})


async def ac_clean(request: web.Request):
    """Self-clean of an air conditioner: "start" (the unit must be off), "stop", or "done" (mark a clean as
    finished, e.g. one done from Mijia). The unit's protocol is described in aclean.py."""
    body = await request.json()
    acx, home, eid = request.app["acx"], request.app["home"], str(body.get("entity_id", ""))
    dev = next((d for d in home.model["devices"] if d["roles"].get("main") == eid), None)
    if not dev or not dev.get("ac", {}).get("clean"):
        raise web.HTTPNotFound()
    action = body.get("action")
    if action == "done":
        acx.mark_cleaned(eid)
    elif action == "start":
        if (home.states.get(eid) or {}).get("state") not in ("off",):
            raise web.HTTPConflict(text="先把空调关掉，再开始自清洁")
        await home.call(dev["ac"]["clean"], "set_value", {"value": "1"})
    elif action == "stop":
        await home.call(dev["ac"]["clean"], "set_value", {"value": "0"})
    else:
        raise web.HTTPBadRequest(text="unknown action")
    return web.json_response({"ok": True, "ac_clean": acx.clean_all()})


async def kitchen_fryer(request: web.Request):
    """Air fryer: {"device", "action": "start" | "reserve" | "cancel_resv", "params": {mode, temp, time, weight, keep, flip}, "at": "YYYY-MM-DDTHH:MM"}"""
    body = await request.json()
    k, device, params = request.app["kitchen"], str(body.get("device", "")), body.get("params") or {}
    try:
        if body.get("action") == "start":
            await k.start_fryer(device, params)
        elif body.get("action") == "reserve":
            from datetime import datetime as _dt
            k.reserve(device, _dt.fromisoformat(str(body.get("at"))).replace(tzinfo=KITCHEN_TZ), params)
        elif body.get("action") == "cancel_resv":
            k.cancel_reservation(device)
        else:
            raise ValueError("unknown action")
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"ok": True, "kitchen_resv": k.reservations(), "flip_waiting": k.flip_waiting()})


async def kitchen_flip(request: web.Request):
    return web.json_response({"flip_waiting": request.app["kitchen"].flip_waiting()})


async def kitchen_recipe(request: web.Request):
    """Your own air fryer recipes: {"device", "action": "save" | "delete", "recipe": {id?, name, temp, time, keep}}"""
    body = await request.json()
    k, device, r = request.app["kitchen"], str(body.get("device", "")), body.get("recipe") or {}
    try:
        if body.get("action") == "save":
            data = k.save_recipe(device, r)
        elif body.get("action") == "delete":
            data = k.delete_recipe(device, str(r.get("id", "")))
        else:
            raise ValueError("unknown action")
    except (ValueError, TypeError) as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"ok": True, "kitchen_recipes": data})


async def comfort_set(request: web.Request):
    """一键恒温 on/off for an air conditioner, or a new desk target while it is on."""
    body = await request.json()
    try:
        cfg = request.app["comfort"].set(str(body.get("entity_id", "")), bool(body.get("on")), body.get("target"))
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"ok": True, "comfort": cfg})


_WX = {"at": 0.0, "data": None}


async def weather_get(request: web.Request):
    """The weather page: now, the next 24 hours and the next days, from HA's weather entity (met.no). Cached 10 minutes."""
    import time as _t
    home, ha = request.app["home"], request.app["ha"]
    eid = home.model.get("weather")
    if not eid:
        raise web.HTTPNotFound()
    if _WX["data"] is None or _t.time() - _WX["at"] > 600:
        s = await ha.state(eid)
        hourly, daily = await ha.forecast(eid, "hourly"), await ha.forecast(eid, "daily")
        a = s["attributes"]
        pick = lambda f, *ks: {k: f.get(k) for k in ks}
        _WX["data"] = {
            "now": {"condition": s["state"], **pick(a, "temperature", "humidity", "pressure", "wind_speed", "wind_bearing", "cloud_coverage", "uv_index", "dew_point")},
            "hourly": [pick(f, "datetime", "condition", "temperature", "precipitation", "wind_speed", "humidity") for f in hourly[:36]],
            "daily": [pick(f, "datetime", "condition", "temperature", "templow", "precipitation", "wind_speed", "uv_index") for f in daily[:7]],
            "source": a.get("attribution", ""), "at": _t.time()}
        _WX["at"] = _t.time()
    return web.json_response(_WX["data"])


_LOCAL = {"at": 0.0, "ok": {}}


def _away(dev) -> str:
    """Why a device cannot be reached over this home's Wi-Fi/LAN: it is in another home, or it is not on the Wi-Fi at all (the
    account has no Wi-Fi record and a public address for it, e.g. a wall switch that is controlled through the gateway)."""
    import ipaddress
    if dev.home > 0 or "liaoyang" in dev.name.lower():
        return "另一个家"
    try:
        if not ipaddress.ip_address(dev.ip).is_private:
            return "不在 Wi-Fi 上（蓝牙或经网关）"
    except ValueError:
        if ":" in dev.ip:               # an IPv6 address (the dump even cuts some short): not a Wi-Fi LAN address
            return "不在 Wi-Fi 上（蓝牙或经网关）"
    return ""


async def local_status(request: web.Request):
    """Which devices answer a direct local call right now (no Home Assistant, no Xiaomi cloud in between), which are switched to
    direct control, and how much was sent directly. The probe is cached for 30 s."""
    import time
    from . import miot_local
    bridge = request.app["local"]
    if not bridge.devs:
        return web.json_response({"error": "no mi_tokens.json in the data folder"}, status=404)
    async def refresh():
        _LOCAL["ok"] = await miot_local.probe(bridge.devs)
        _LOCAL["at"] = time.time()
        _LOCAL["busy"] = False
    stale = time.time() - _LOCAL["at"] > 30
    if request.query.get("fresh") or not _LOCAL["ok"]:
        await refresh()
    elif stale and not _LOCAL.get("busy"):
        _LOCAL["busy"] = True                      # answer with what is known, the probe runs behind it
        asyncio.create_task(refresh())
    ok, on = _LOCAL["ok"], bridge.enabled()
    rows = [{"did": d, "name": v.name, "model": v.model, "ip": v.ip, "local": ok.get(d, False), "direct": d in on,
             "spec": bridge.has_spec(v.model), "stats": bridge.stats.get(d), "away": _away(v), "no_props": v.model.startswith("xiaomi.router.")}
            for d, v in bridge.devs.items()]
    return web.json_response({"account": bridge.account, "devices": rows, "enabled": on, "used": bridge.used,
                              "local_count": sum(ok.values()), "total": len(rows), "probed_at": _LOCAL["at"]})


async def local_enable(request: web.Request):
    """{"dids": ["944295092", ...]}: the devices whose writes go straight to them (everything else still through Home Assistant)."""
    body = await request.json()
    request.app["local"].enable(list(body.get("dids") or []))
    return web.json_response({"enabled": request.app["local"].enabled()})


async def ble_status(request: web.Request):
    """The Xiaomi BLE sensors the hub hears directly off the air (MiBeacon, decrypted with the owner's own bind key) —
    no gateway, no Xiaomi cloud. Latest value of each; a device with no value yet has not broadcast since start."""
    return web.json_response(request.app["ble"].snapshot())


async def hub1_devlist(request: web.Request):
    """On-demand: list what the Xiaomi central hub (xiaomi.gateway.hub1) exposes on its own local channel.
    Body {"code": "123456"} — the 6-digit login code from the Mijia app's geek page. The code is single-use: the hub
    rotates it after one login, so this is a look-up tool (one call per fresh code), not a stored credential."""
    code = str((await request.json()).get("code") or "").strip()
    if not code.isdigit() or len(code) != 6:
        return web.json_response({"error": "need the 6-digit login code from the Mijia app geek page"}, status=400)
    ip = hub1_link.gateway_ip(DATA / "mi_tokens.json")
    if not ip:
        return web.json_response({"error": "no xiaomi.gateway.hub1 with a local address in mi_tokens.json"}, status=404)
    try:
        async with hub1_link.HubLink(ip, code) as hub:
            dl = await hub.call("getDevList")
    except PermissionError:
        return web.json_response({"error": "登录码不对或已失效（极客版的码用一次就换，请在米家 App 里重新读一个）"}, status=403)
    except Exception as e:
        return web.json_response({"error": f"连网关失败：{e}"}, status=502)
    devs = dl.get("devList", dl) if isinstance(dl, dict) else dl
    return web.json_response({"ip": ip, "count": len(devs) if hasattr(devs, "__len__") else 0, "devList": devs})


async def router_get(request: web.Request):
    """The router's status for the 直连 page: asked directly (its own web api), not through Home Assistant or the Xiaomi cloud."""
    r = request.app["router"]
    if not r.configured():
        return web.json_response({"configured": False})
    try:
        return web.json_response({"configured": True, **await r.overview()})
    except RouterError as e:
        return web.json_response({"configured": True, "error": str(e)})


async def router_password(request: web.Request):
    """{"password": "..."}: the router's admin password, kept only in the data folder; checked by logging in at once."""
    r = request.app["router"]
    pw = str((await request.json()).get("password") or "")
    if not pw:
        r.forget()
        return web.json_response({"configured": False})
    r.set_password(pw)
    try:
        await r.login()
    except RouterError as e:
        r.forget()
        return web.json_response({"configured": False, "error": str(e)}, status=400)
    return web.json_response({"configured": True})


async def router_raw(request: web.Request):
    """Read-only look at one router api path (GET), for finding what a firmware offers: /api/router/raw?path=misystem/status"""
    path = request.query.get("path", "")
    if not path.startswith(("misystem/", "xqnetwork/", "xqsystem/", "xqdatacenter/")) or "set" in path.lower() or "reboot" in path.lower():
        return web.json_response({"error": "read-only paths only"}, status=400)
    try:
        return web.json_response(await request.app["router"].api(path))
    except RouterError as e:
        return web.json_response({"error": str(e)}, status=502)


async def energy_get(request: web.Request):
    return web.json_response(request.app["energy"].summary())


async def energy_settings(request: web.Request):
    """{"prices": {"peak": 0.617, "valley": 0.307}, "bill_day": 1}"""
    body = await request.json()
    try:
        request.app["energy"].set_settings(body.get("prices"), body.get("bill_day"))
    except (ValueError, TypeError) as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response(request.app["energy"].summary())


async def device_rename(request: web.Request):
    body = await request.json()
    await request.app["home"].rename(body.get("device_id", ""), str(body.get("name", "")))
    return web.json_response({"ok": True})


async def device_move(request: web.Request):
    body = await request.json()
    await request.app["home"].move(list(body.get("device_ids") or []), str(body.get("home", "")), str(body.get("room", "")))
    return web.json_response({"ok": True})


async def device_hide(request: web.Request):
    """"删除设备" in the app: drop from the home page (shared by all phones). Unbinding a Xiaomi device
    can only be done in Mijia; {"hidden": false} brings it back."""
    body = await request.json()
    ids = list(body.get("device_ids") or [])
    request.app["home"].check_devices(ids)
    store = request.app["store"]
    hidden = set(store.get_setting("hidden_devices", []))
    hidden = hidden | set(ids) if body.get("hidden", True) else hidden - set(ids)
    store.set_setting("hidden_devices", sorted(hidden))
    return web.json_response({"ok": True})


async def automation_get(request: web.Request):
    return web.json_response(await request.app["home"].get_automation(request.match_info["entity_id"]), dumps=_dumps)


async def automation_save(request: web.Request):
    """POST /api/automations/new or /api/automations/<entity_id> with the HA automation config."""
    eid = request.match_info["entity_id"]
    saved = await request.app["home"].save_automation(None if eid == "new" else eid, await request.json())
    return web.json_response({"ok": True, "entity_id": saved})


async def automation_delete(request: web.Request):
    await request.app["home"].delete_automation(request.match_info["entity_id"])
    return web.json_response({"ok": True})


async def devices_discovered(request: web.Request):
    return web.json_response(await request.app["home"].discovered(), dumps=_dumps)


async def devices_add(request: web.Request):
    """Add a discovered device: {flow_id} opens it, {flow_id, input} answers the form it showed,
    {flow_id, input: {next_step_id}} picks a menu entry. Replies with the next form or the outcome."""
    body = await request.json()
    return web.json_response(await request.app["home"].flow(str(body.get("flow_id", "")), body.get("input")), dumps=_dumps)


async def devices_sync(request: web.Request):
    return web.json_response({"ok": True, "added": await request.app["home"].sync_xiaomi()})


async def provision_start(request: web.Request):
    return web.json_response(request.app["provision"].start())


async def provision_step(request: web.Request):
    body = await request.json()
    return web.json_response(request.app["provision"].step(str(body.get("session", "")), body.get("reply"), body.get("wifi")))


async def provision_find(request: web.Request):
    body = await request.json()
    return web.json_response(await request.app["provision"].find(str(body.get("session", ""))))


async def washer_clean(request: web.Request):
    """Start 筒清洁 on one drum: {drum, tonight}. Background job like the other bookings."""
    body = await request.json()
    drum = body.get("drum")
    if drum not in DRUMS:
        raise web.HTTPBadRequest(text="need drum")
    washer = request.app["washer"]
    return web.json_response({"job": washer.in_background(drum, lambda cb: washer.clean(drum, bool(body.get("tonight")), cb))})


async def washer_clean_settings(request: web.Request):
    body = await request.json()
    try:
        cfg = request.app["washer"].save_clean_settings(body)
    except (ValueError, TypeError) as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response(cfg)


async def washer_status(request: web.Request):
    return web.json_response(await request.app["washer"].status())


async def washer_book(request: web.Request):
    body = await request.json() if request.can_read_body else {}
    drums = body.get("drums") or list(DRUMS)
    if any(d not in DRUMS for d in drums):
        raise web.HTTPBadRequest(text=f"drums must be from {list(DRUMS)}")
    try:
        hh, mm = (body.get("finish_by") or DEFAULT_FINISH_BY.strftime("%H:%M")).split(":")
        finish_by = time(int(hh), int(mm))
    except ValueError:
        raise web.HTTPBadRequest(text="finish_by must be HH:MM")
    results = await request.app["washer"].book(drums, finish_by, bool(body.get("dry_run")))
    return web.json_response({"ok": all(r["ok"] for r in results), "results": results})


async def washer_quick_book(request: web.Request):
    """Home-page card button: book one drum on its 自定义 program, in the background."""
    body = await request.json()
    drum = body.get("drum")
    if drum not in DRUMS:
        raise web.HTTPBadRequest(text="need drum")
    washer = request.app["washer"]

    async def book(on_booked):
        return (await washer.book([drum], DEFAULT_FINISH_BY, False, on_booked=on_booked))[0]
    return web.json_response({"job": washer.in_background(drum, book)})


async def washer_job(request: web.Request):
    job = request.app["washer"].job(request.match_info["job_id"])
    if job is None:
        raise web.HTTPNotFound(text="no such job")
    return web.json_response(job)


async def washer_start(request: web.Request):
    """One drum, one mode: {drum, mode, program, finish_at: ISO time or null for 立即开始}."""
    body = await request.json()
    drum, mode = body.get("drum"), str(body.get("mode") or "")
    if drum not in DRUMS:
        raise web.HTTPBadRequest(text="need drum")
    finish_at = None
    if body.get("finish_at"):
        try:
            finish_at = datetime.fromisoformat(body["finish_at"])
        except ValueError:
            raise web.HTTPBadRequest(text="finish_at must be ISO time")
        if finish_at.tzinfo is None:
            raise web.HTTPBadRequest(text="finish_at needs a timezone")
    washer = request.app["washer"]
    try:
        await washer.check_mode(drum, mode)
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"job": washer.in_background(drum, lambda cb: washer.start(drum, mode, body.get("program"), finish_at, on_booked=cb))})


async def washer_program(request: web.Request):
    body = await request.json()
    drum = body.get("drum")
    if drum not in DRUMS or not isinstance(body.get("program"), dict):
        raise web.HTTPBadRequest(text="need drum and program")
    try:
        prog = request.app["washer"].save_program(drum, body["program"])
    except (ValueError, TypeError) as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"ok": True, "program": prog})


async def washer_modeparams(request: web.Request):
    """{drum, mode, params}: what the mode page was last set to. Opened again (on any phone) it shows exactly this."""
    body = await request.json()
    try:
        saved = request.app["washer"].save_mode_params(body.get("drum"), str(body.get("mode") or ""), body.get("params") or {})
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="need drum, mode and params")
    return web.json_response({"ok": True, "params": saved})


async def update_info(_):
    """version.json written by tools/publish_app.ps1: {versionCode, versionName, apk, size, notes}."""
    f = UPDATES / "version.json"
    if not f.exists():
        raise web.HTTPNotFound(text="no release published")
    return web.json_response(json.loads(f.read_text(encoding="utf-8")))


async def update_apk(request: web.Request):
    name = request.match_info["name"]
    f = UPDATES / name
    if "/" in name or not name.endswith(".apk") or not f.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(f, headers={"Content-Type": "application/vnd.android.package-archive"})


def asset_version(name: str) -> str:
    return hashlib.sha1((STATIC / name).read_bytes()).hexdigest()[:10]


def page(name: str):
    """HTML pages: always revalidated, with their /static/*.js|css links stamped ?v=<content hash> so
    the phone can keep those files cached for good and only refetch when a deploy changes them."""
    async def handler(_):
        html = (STATIC / name).read_text(encoding="utf-8")
        html = re.sub(r'(/static/([\w.-]+\.(?:js|css)))"', lambda m: f'{m.group(1)}?v={asset_version(m.group(2))}"', html)
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})
    return handler


async def monitor_status(request: web.Request):
    return web.json_response(request.app["monitor"].snapshot())


async def monitor_container(request: web.Request):
    b = await request.json()
    try:
        return web.json_response(await request.app["monitor"].control(str(b.get("host")), str(b.get("name")), str(b.get("action"))))
    except ValueError as ex:
        raise web.HTTPBadRequest(text=str(ex))
    except Exception as ex:
        raise web.HTTPBadGateway(text=str(ex)[:200])


async def monitor_all(request: web.Request):
    b = await request.json()
    try:
        return web.json_response(await request.app["monitor"].control_all(str(b.get("host")), str(b.get("action"))))
    except ValueError as ex:
        raise web.HTTPBadRequest(text=str(ex))


async def proxy_to_ops(request: web.Request):
    """Moving services, the image registry, the copies back to the NAS: done by the ops service, which is always on the Pi. This hub - on
    the Pi or on the NAS - serves the same pages and passes these requests on, so the phone sees the same thing from either machine."""
    url = os.environ.get("OPS_URL", "https://127.0.0.1:8220")
    ctx = _OPS_SSL
    body = await request.read() if request.can_read_body else None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
            async with s.request(request.method, url + request.path_qs, data=body, ssl=ctx,
                                 headers={"Authorization": "Bearer " + os.environ.get("HUB_TOKEN", ""), "Content-Type": request.content_type or "application/json"}) as r:
                return web.Response(status=r.status, body=await r.read(), content_type=r.content_type)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response({"error": "香橙派上的运维服务没有回应：迁移、镜像仓库同步需要香橙派在线"}, status=503)


def _ops_ssl() -> ssl.SSLContext:
    try:                                                     # trust exactly the hub's own certificate, which the ops service also uses
        ctx = ssl.create_default_context(cafile=str(DATA / "tls" / "server.crt"))
        ctx.check_hostname = False
    except Exception:
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    return ctx


_OPS_SSL = _ops_ssl()


async def _sentinel(method: str, path: str, body=None):
    """The sentinel on this machine (decides which machine runs the hub + HomeAssistant). It answers for the other one too."""
    url, token = os.environ.get("SENTINEL_URL"), os.environ.get("SENTINEL_TOKEN")
    if not url or not token:
        raise web.HTTPServiceUnavailable(text="sentinel not configured")
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE        # loopback, token-protected
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.request(method, url + path, json=body, headers={"Authorization": "Bearer " + token}, ssl=ctx) as r:
                return r.status, await r.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
        raise web.HTTPBadGateway(text=f"sentinel: {type(ex).__name__}")


async def _nas_sentinel(method: str, path: str, body=None):
    """The sentinel on the NAS (it holds the disaster-recovery plan). Reached by its LAN address from either machine."""
    token = os.environ.get("SENTINEL_TOKEN")
    if not token:
        raise web.HTTPServiceUnavailable(text="sentinel not configured")
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
            async with s.request(method, os.environ.get("NAS_SENTINEL_URL", machines.sentinel_url("nas")) + path, json=body,
                                 headers={"Authorization": "Bearer " + token}, ssl=ctx) as r:
                return r.status, await r.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
        raise web.HTTPBadGateway(text=f"NAS sentinel: {type(ex).__name__}")


async def recovery_get(request: web.Request):
    """What the NAS would start if the Pi were gone, how much memory that needs, and what it is running in its place right now."""
    _, plan = await _nas_sentinel("POST", "/recovery/plan", {})
    _, status = await _nas_sentinel("GET", "/status")
    plan.update(recovered=status.get("recovered", []), recovered_at=status.get("recovered_at", 0), nas_primary=status.get("primary"),
                peer_ok=(status.get("peer") or {}).get("ok"), auto=status.get("auto"))
    return web.json_response(plan)


async def recovery_failback(request: web.Request):
    status, data = await _nas_sentinel("POST", "/recovery/failback", {})
    return web.json_response(data, status=status)


async def primary_get(request: web.Request):
    status, data = await _sentinel("GET", "/status")
    return web.json_response(data, status=status)


async def primary_switch(request: web.Request):
    body = await request.json()
    if body.get("target") not in ("pi", "nas"):
        raise web.HTTPBadRequest(text="target must be pi or nas")
    status, data = await _sentinel("POST", "/switch", {"target": body["target"]})
    return web.json_response(data, status=status)


async def primary_job(request: web.Request):
    status, data = await _sentinel("GET", "/job/" + request.match_info["job_id"])
    return web.json_response(data, status=status)


async def primary_auto(request: web.Request):
    body = await request.json()
    status, data = await _sentinel("POST", "/auto", {"on": bool(body.get("on"))})
    return web.json_response(data, status=status)


async def endpoints_get(request: web.Request):
    return web.json_response(request.app["endpoints"].get())


async def endpoints_audit(request: web.Request):
    return web.json_response(await request.app["endpoints"].audit())


async def endpoints_register(request: web.Request):
    try:
        b = await request.json()
        e = request.app["endpoints"]
        items = b["items"] if "items" in b else [b]
        for it in items:
            e.register(str(it["container"]), str(it["name"]), int(it["port"]))
        await e.reconcile()
    except (ValueError, KeyError, TypeError) as ex:
        return web.json_response({"error": str(ex)}, status=400)
    return web.json_response(await e.audit())


async def endpoints_edit(request: web.Request):
    e = request.app["endpoints"]
    try:
        b = await request.json()
        what = request.match_info["what"]
        if what == "forget":
            await e.forget(str(b["name"]))
        elif what == "rename":
            await e.rename(str(b["name"]), str(b["to"]))
        elif what == "refresh":
            await e.reconcile()
        else:
            return web.json_response({"error": "unknown"}, status=404)
    except (ValueError, KeyError, TypeError) as ex:
        return web.json_response({"error": str(ex)}, status=400)
    return web.json_response(await e.audit())


async def endpoints_probe(request: web.Request):
    return web.json_response(await request.app["endpoints"].probe(request.query.get("name", "")))


async def endpoints_put(request: web.Request):
    """Change the table (token needed): body {"services": {name: {check, expect, lan[], wan[]}}} - listed services are replaced."""
    try:
        body = await request.json()
        if request.get("narrow") and "homehub" in body["services"]:
            return web.json_response({"error": "这个令牌不能改 homehub 这一项"}, status=403)
        t = await request.app["endpoints"].put(body["services"])
    except (ValueError, KeyError, TypeError) as ex:
        return web.json_response({"error": str(ex)}, status=400)
    return web.json_response(t)


async def notices_get(request: web.Request):
    """The phone app's notification poll: notices after ?after=N, held open up to ?wait=S seconds (max 50) until one arrives.
    after=-1 (a phone that never asked before) only learns the latest number, so it does not get old notices."""
    n = request.app["notifier"]
    try:
        after = int(request.query.get("after", "-1"))
        wait = min(max(float(request.query.get("wait", "0")), 0), 50)
    except ValueError:
        raise web.HTTPBadRequest(text="after / wait must be numbers")
    if after < 0:
        return web.json_response({"latest": n.latest(), "notices": []})
    got = await n.wait(after, wait)
    return web.json_response({"latest": max([after] + [x["id"] for x in got]), "notices": got}, dumps=_dumps)


async def notify_get(request: web.Request):
    n = request.app["notifier"]
    return web.json_response({"on": n.settings(), "labels": {k: v["label"] for k, v in NOTIFY_KINDS.items()}, "latest": n.latest()},
                             dumps=_dumps)


async def notify_set(request: web.Request):
    """{"kind": "antifreeze", "on": false}"""
    body = await request.json()
    try:
        on = request.app["notifier"].set(str(body.get("kind", "")), bool(body.get("on")))
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"on": on})


async def notify_test(request: web.Request):
    """{"kind": "antifreeze"}: send that rule's notice as if the device had just changed (obeys the switch).
    {"title": ..., "body": ...}: a plain test notice that is always sent."""
    body = await request.json()
    n = request.app["notifier"]
    kind = body.get("kind")
    if kind:
        r = next((r for r in NOTIFY_RULES if r.kind == kind), None)
        if not r:
            raise web.HTTPBadRequest(text=f"没有这种提醒：{kind}")
        e = next((e for e in request.app["home"].states if e.endswith(r.suffix)), "")
        nid = n.push(kind, r.title(_entity_device_name(request.app, e)), r.body)
    else:
        nid = n.push("test", str(body.get("title") or "HomeHub 测试通知"), str(body.get("body") or "能看到这条，手机通知就通了"), force=True)
    return web.json_response({"id": nid, "sent": nid is not None})


def _entity_device_name(app, entity_id: str) -> str:
    for d in app["home"].model.get("devices", []):
        if entity_id in d.get("entities", []):
            return d.get("name") or "热水器"
    return "热水器"


async def on_startup(app: web.Application):
    ha = HA(os.environ["HA_URL"], os.environ["HA_TOKEN"])
    await ha.start()
    app["ha"] = ha
    store = app["store"] = Store(str(DATA / "hub.db"))
    ha.local = app["local"] = LocalBridge(DATA, store)
    washer = app["washer"] = Washer(ha, store)
    app["provision"] = Provisioner(store)
    home = app["home"] = Home(ha)
    notifier = app["notifier"] = Notifier(store, lambda e: _entity_device_name(app, e))
    comfort = app["comfort"] = Comfort(home, store)
    app["comfort_task"] = asyncio.create_task(comfort.run())
    acx = app["acx"] = AcExtras(ha, home, store)
    kitchen = app["kitchen"] = Kitchen(home, store)
    app["kitchen_task"] = asyncio.create_task(kitchen.run())
    energy = app["energy"] = Energy(home, store)
    app["energy_task"] = asyncio.create_task(energy.run())
    monitor = app["monitor"] = Monitor()
    monitor.start()
    app["endpoints"] = Endpoints(store, monitor)
    app["endpoints_task"] = asyncio.create_task(app["endpoints"].loop())
    if os.environ.get("HUB_ROLE", "pi") != "nas":
        asyncio.create_task(app["endpoints"].copy_to_nas())

    app["router"] = Router(DATA)
    camrec.setup(app, DATA)
    app["reader"] = Reader(app["local"], home)
    app["reader_task"] = asyncio.create_task(app["reader"].run())
    app["ble"] = BleListener(DATA)
    app["ble_task"] = asyncio.create_task(app["ble"].run())

    async def collect_loop():
        await asyncio.sleep(60)
        while True:
            try:
                await acx.collect()
            except Exception:
                logging.getLogger("aclean").exception("collecting daily numbers")
            await asyncio.sleep(3600)
    app["acx_task"] = asyncio.create_task(collect_loop())

    async def on_change(entity_id: str, new: dict):
        await washer.on_change(entity_id, new)
        await home.on_change(entity_id, new)
        acx.note_change(entity_id, new)
        try:
            notifier.on_change(entity_id, new)
        except Exception:
            logging.getLogger("notify").exception("notice rule")

    async def on_connect():
        await ha.wait_running()
        await home.refresh()
        notifier.resync(home.states)
        await washer.backfill()

    app["listener"] = asyncio.create_task(ha.listen(on_change, on_connect))


async def on_cleanup(app: web.Application):
    app["listener"].cancel()
    app["comfort_task"].cancel()
    app["acx_task"].cancel()
    app["reader_task"].cancel()
    app["ble_task"].cancel()
    for t in app.get("camrec_tasks", []):
        t.cancel()
    app["kitchen_task"].cancel()
    app["energy_task"].cancel()
    app["monitor"].stop()
    await app["ha"].close()


def make_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.add_routes([
        web.get("/", page("index.html")),
        web.get("/washer", page("washer.html")),
        web.get("/journey", page("journey.html")),
        web.get("/idea", page("journey.html")),
        web.get("/api/monitor", monitor_status),
        web.post("/api/monitor/container", monitor_container),
        web.post("/api/monitor/all", monitor_all),
        web.get("/api/registry", proxy_to_ops),
        web.post("/api/registry/sync", proxy_to_ops),
        web.post("/api/registry/mirror", proxy_to_ops),
        web.post("/api/registry/rollback", proxy_to_ops),
        web.post("/api/specs/refresh", proxy_to_ops),
        web.get("/api/transfer/plan", proxy_to_ops),
        web.post("/api/transfer/start", proxy_to_ops),
        web.get("/api/transfer/job/{job_id}", proxy_to_ops),
        web.get("/api/join/candidates", proxy_to_ops),
        web.post("/api/join/claim", proxy_to_ops),
        web.get("/api/join/job/{job_id}", proxy_to_ops),
        web.post("/api/replica/run", proxy_to_ops),
        web.get("/api/recovery", recovery_get),
        web.post("/api/recovery/failback", recovery_failback),
        web.post("/api/recovery/failback-data", proxy_to_ops),
        web.get("/api/recovery/job/{job_id}", proxy_to_ops),
        web.get("/api/primary", primary_get),
        web.post("/api/primary/switch", primary_switch),
        web.get("/api/primary/job/{job_id}", primary_job),
        web.post("/api/primary/auto", primary_auto),
        web.get("/api/endpoints", endpoints_get),
        web.post("/api/endpoints", endpoints_put),
        web.get("/api/endpoints/audit", endpoints_audit),
        web.get("/api/endpoints/probe", endpoints_probe),
        web.post("/api/endpoints/register", endpoints_register),
        web.post("/api/endpoints/{what}", endpoints_edit),
        web.get("/api/registry/job/{job_id}", proxy_to_ops),
        web.get("/api/monitor/migrate/plan", proxy_to_ops),
        web.post("/api/monitor/migrate", proxy_to_ops),
        web.get("/api/monitor/migrate/active", proxy_to_ops),
        web.get("/api/monitor/migrate/job/{job_id}", proxy_to_ops),
        web.get("/login", login),
        web.get("/api/home", home_snapshot),
        web.get("/api/stream", home_stream),
        web.get("/api/states", home_states),
        web.post("/api/call", home_call),
        web.post("/api/order", save_order),
        web.post("/api/comfort", comfort_set),
        web.get("/api/ac/history", ac_history),
        web.post("/api/ac/clean", ac_clean),
        web.post("/api/kitchen/fryer", kitchen_fryer),
        web.post("/api/kitchen/recipe", kitchen_recipe),
        web.get("/api/kitchen/flip", kitchen_flip),
        web.get("/api/weather", weather_get),
        web.get("/api/energy", energy_get),
        web.post("/api/energy/settings", energy_settings),
        web.post("/api/device/rename", device_rename),
        web.post("/api/device/move", device_move),
        web.post("/api/device/hide", device_hide),
        web.get("/api/automations/{entity_id}", automation_get),
        web.post("/api/automations/{entity_id}", automation_save),
        # deleting is a POST too: the phone app relays POSTs (with X-HH-Body) but not DELETE
        web.post("/api/automations/{entity_id}/delete", automation_delete),
        web.get("/api/devices/discovered", devices_discovered),
        web.post("/api/devices/add", devices_add),
        web.post("/api/devices/sync", devices_sync),
        web.get("/api/washer", washer_status),
        web.post("/api/washer/book", washer_book),
        web.post("/api/washer/program", washer_program),
        web.post("/api/washer/modeparams", washer_modeparams),
        web.post("/api/washer/start", washer_start),
        web.post("/api/washer/clean", washer_clean),
        web.post("/api/washer/clean/settings", washer_clean_settings),
        web.post("/api/provision/start", provision_start),
        web.post("/api/provision/step", provision_step),
        web.post("/api/provision/find", provision_find),
        web.post("/api/washer/quick", washer_quick_book),
        web.get("/api/local", local_status),
        web.post("/api/local/enable", local_enable),
        web.get("/api/ble", ble_status),
        web.post("/api/hub1/devlist", hub1_devlist),
        web.get("/cam/{tail:.*}", cam_proxy),
        web.post("/api/cam/move", camrec.cam_move2),
        web.get("/api/cam/events", camrec.cam_events),
        web.get("/api/cam/prefs", camrec.cam_prefs),
        web.post("/api/cam/prefs", camrec.cam_prefs),
        web.get("/api/cam/stat", camrec.cam_stat),
        web.get("/api/cam/live", camrec.cam_live),
        web.get("/api/cam/settings", camrec.cam_settings),
        web.post("/api/cam/settings", camrec.cam_settings),
        web.post("/api/cam/talk", camrec.cam_talk),
        web.get("/api/cam/power", camrec.cam_power),
        web.post("/api/cam/power", camrec.cam_power),
        web.get("/api/cam/file/{name}", camrec.cam_file),
        web.get("/api/router", router_get),
        web.post("/api/router/password", router_password),
        web.get("/api/router/raw", router_raw),
        web.get("/api/washer/job/{job_id}", washer_job),
        web.get("/api/notices", notices_get),
        web.get("/api/notify", notify_get),
        web.post("/api/notify", notify_set),
        web.post("/api/notify/test", notify_test),
        web.get("/api/updates/version.json", update_info),
        web.get("/api/updates/{name}", update_apk),
        web.static("/static", STATIC),
    ])
    journey.setup(app, DATA)              # 开发闯关地图: /journey + /api/journey/*
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def tls() -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(DATA / "tls" / "server.crt", DATA / "tls" / "server.key")
    return ctx


if __name__ == "__main__":
    web.run_app(make_app(), port=int(os.environ.get("HUB_PORT", "8200")), ssl_context=tls())
