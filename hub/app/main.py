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

from aiohttp import web

from .aclean import AcExtras
from .comfort import Comfort
from .ha import HA
from .kitchen import Kitchen, TZ as KITCHEN_TZ
from .home_service import Home
from .store import Store
from .provision import Provisioner
from .washer import Washer
from .washer_logic import DEFAULT_FINISH_BY, DRUMS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"
DATA = Path(os.environ.get("HUB_DATA", "/data"))
UPDATES = DATA / "updates"
TOKEN = os.environ["HUB_TOKEN"]
COOKIE = "hub_token"
PUBLIC_PATHS = ("/static/icon.svg", "/static/manifest.json")


def _authorized(request: web.Request) -> bool:
    given = request.cookies.get(COOKIE, "")
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        given = auth[len("Bearer "):]
    return bool(given) and hmac.compare_digest(given.encode(), TOKEN.encode())


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path in PUBLIC_PATHS or request.path == "/login" or _authorized(request):
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
    resp = web.HTTPFound("/")
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
    await request.app["home"].call(body.get("entity_id", ""), body.get("service", ""), body.get("data") or {})
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
    return web.json_response({"ok": True, "kitchen_resv": k.reservations()})


async def comfort_set(request: web.Request):
    """一键恒温 on/off for an air conditioner, or a new desk target while it is on."""
    body = await request.json()
    try:
        cfg = request.app["comfort"].set(str(body.get("entity_id", "")), bool(body.get("on")), body.get("target"))
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"ok": True, "comfort": cfg})


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


async def on_startup(app: web.Application):
    ha = HA(os.environ["HA_URL"], os.environ["HA_TOKEN"])
    await ha.start()
    app["ha"] = ha
    store = app["store"] = Store(str(DATA / "hub.db"))
    washer = app["washer"] = Washer(ha, store)
    app["provision"] = Provisioner(store)
    home = app["home"] = Home(ha)
    comfort = app["comfort"] = Comfort(home, store)
    app["comfort_task"] = asyncio.create_task(comfort.run())
    acx = app["acx"] = AcExtras(ha, home, store)
    kitchen = app["kitchen"] = Kitchen(home, store)
    app["kitchen_task"] = asyncio.create_task(kitchen.run())

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

    async def on_connect():
        await ha.wait_running()
        await home.refresh()
        await washer.backfill()

    app["listener"] = asyncio.create_task(ha.listen(on_change, on_connect))


async def on_cleanup(app: web.Application):
    app["listener"].cancel()
    app["comfort_task"].cancel()
    app["acx_task"].cancel()
    app["kitchen_task"].cancel()
    await app["ha"].close()


def make_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.add_routes([
        web.get("/", page("index.html")),
        web.get("/washer", page("washer.html")),
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
        web.post("/api/washer/start", washer_start),
        web.post("/api/washer/clean", washer_clean),
        web.post("/api/washer/clean/settings", washer_clean_settings),
        web.post("/api/provision/start", provision_start),
        web.post("/api/provision/step", provision_step),
        web.post("/api/provision/find", provision_find),
        web.post("/api/washer/quick", washer_quick_book),
        web.get("/api/washer/job/{job_id}", washer_job),
        web.get("/api/updates/version.json", update_info),
        web.get("/api/updates/{name}", update_apk),
        web.static("/static", STATIC),
    ])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def tls() -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(DATA / "tls" / "server.crt", DATA / "tls" / "server.key")
    return ctx


if __name__ == "__main__":
    web.run_app(make_app(), port=int(os.environ.get("HUB_PORT", "8200")), ssl_context=tls())
