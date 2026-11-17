"""The operations service: what needs the Orange Pi's own disks and docker - moving services between the machines, the image
registry and its copy on the NAS, the nightly copies back to the NAS, the saved container list.

It runs as its own container (homehub-ops) that is always on the Pi, whichever machine currently runs the hub. The hub - on the Pi or
on the NAS - serves the same pages and simply passes these requests on to it (see `proxy_to_ops` in main.py), so the phone
shows the same thing either way. If the Pi is down this is down too, which is right: none of it can work without the Pi."""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import ssl
from pathlib import Path

from aiohttp import web

from . import machines as mc
from .failback import Failback
from .joiner import Joiner
from .mirror import FolderMirror
from .migrate import Migrator
from .monitor import Monitor
from .registry import Registry
from .replica import Replicator
from .specs import Specs
from .store import Store
from .transfer import Transfer

log = logging.getLogger("ops")


class PhysStore(Store):
    """Settings are kept in PHYSICAL terms ("pi" = the Pi) so every program on either machine reads them the same way. An ops process that
    runs on the NAS has the engine roles swapped (see machines.py), so the few settings that name a machine are swapped on the way in and out."""

    @staticmethod
    def _swap(key, value):
        if not mc.SWAPPED or not isinstance(value, list):
            return value
        flip = lambda v: {"pi": "nas", "nas": "pi"}.get(v, v)
        if key == "replicas":
            return [{**r, "pi": r.get("nas"), "nas": r.get("pi")} for r in value]
        if key == "migrations":
            return [{**m, "from": flip(m.get("from")), "to": flip(m.get("to"))} for m in value]
        return value

    def get_setting(self, key: str, default=None):
        return self._swap(key, super().get_setting(key, default))

    def set_setting(self, key: str, value):
        super().set_setting(key, self._swap(key, value))

DATA = Path(os.environ.get("OPS_DATA", "/data"))
HUB_DATA = Path(mc.get("pi", "hub_data"))                 # the hub's own data: certificate, and the old home of these settings
KEYS = ("migrations", "replicas", "registry_mirror", "replica_status")


@web.middleware
async def auth(request: web.Request, handler):
    given = request.headers.get("Authorization", "")
    token = os.environ.get("HUB_TOKEN", "")
    if token and given.startswith("Bearer ") and hmac.compare_digest(given[7:], token):
        return await handler(request)
    return web.Response(status=401, text="token needed")


async def registry_list(request: web.Request):
    out = dict(await request.app["registry"].listing())
    out["specs"] = request.app["specs"].summary()
    out["replica"] = request.app["replica"].state()
    return web.json_response(mc.localize(out))


async def registry_sync(request: web.Request):
    reg = request.app["registry"]
    items = await reg.wanted()
    return web.json_response({"job": reg.start(items), "count": len(items)})


async def registry_mirror(request: web.Request):
    return web.json_response({"job": request.app["registry"].start_mirror()})


async def registry_rollback(request: web.Request):
    b = await request.json()
    return web.json_response({"job": request.app["registry"].start_rollback(str(b["repo"]), str(b["tag"]))})


async def registry_job(request: web.Request):
    job = request.app["registry"].jobs.get(request.match_info["job_id"])
    if job is None:
        raise web.HTTPNotFound(text="no such job")
    return web.json_response(job)


async def transfer_plan(request: web.Request):
    return web.json_response({"groups": await request.app["transfer"].plan(request.query.get("from", ""))})


async def transfer_start(request: web.Request):
    b = await request.json()
    try:
        return web.json_response({"job": await request.app["transfer"].start(str(b.get("from")), [str(x) for x in b.get("skip", [])])})
    except ValueError as ex:
        raise web.HTTPBadRequest(text=str(ex))


async def transfer_job(request: web.Request):
    job = request.app["transfer"].jobs.get(request.match_info["job_id"])
    if job is None:
        raise web.HTTPNotFound(text="no such job")
    return web.json_response(mc.localize(job))


async def join_candidates(request: web.Request):
    return web.json_response({"machines": request.app["joiner"].candidates(include_known=request.query.get("all") == "1")})


async def join_claim(request: web.Request):
    b = await request.json()
    try:
        return web.json_response({"job": request.app["joiner"].start_claim(str(b.get("id")), str(b.get("role")))})
    except ValueError as ex:
        raise web.HTTPBadRequest(text=str(ex))


async def join_job(request: web.Request):
    job = request.app["joiner"].jobs.get(request.match_info["job_id"])
    if job is None:
        raise web.HTTPNotFound(text="no such job")
    return web.json_response(job)


async def specs_refresh(request: web.Request):
    await request.app["specs"].refresh()
    return web.json_response(request.app["specs"].summary())


async def replica_run(request: web.Request):
    """Copy the moved services' folders back to the NAS now (otherwise at 03:00)."""
    await request.app["replica"].run()
    return web.json_response(request.app["replica"].state())


async def failback_start(request: web.Request):
    """Give the services back to the Pi with the data the NAS produced while it stood in."""
    return web.json_response({"job": request.app["failback"].start()})


async def failback_job(request: web.Request):
    job = request.app["failback"].jobs.get(request.match_info["job_id"])
    if job is None:
        raise web.HTTPNotFound(text="no such job")
    return web.json_response(mc.localize(job))


async def migrate_plan(request: web.Request):
    # the phone names a PHYSICAL machine; the engines know roles ("pi" = where this runs)
    return web.json_response(mc.localize(await request.app["migrator"].plan(mc.phys(request.query.get("host", "")), request.query.get("name", ""))))


async def migrate_start(request: web.Request):
    b = await request.json()
    mig = request.app["migrator"]
    plan = await mig.plan(mc.phys(str(b.get("host"))), str(b.get("name")))
    if not plan["ok"]:
        raise web.HTTPBadRequest(text="；".join(plan["reasons"]))
    return web.json_response({"job": mig.start(plan["name"], plan)})


async def migrate_active(request: web.Request):
    return web.json_response(mc.localize(request.app["migrator"].active()))


async def migrate_job(request: web.Request):
    job = request.app["migrator"].jobs.get(request.match_info["job_id"])
    if job is None:
        raise web.HTTPNotFound(text="no such job")
    return web.json_response(mc.localize(job))


def import_old_settings(store: Store):
    """First start: take the settings these features had in the hub's database (the list of moved services and what is copied back)."""
    if store.get_setting("ops_imported") or mc.SWAPPED:
        return
    try:
        import sqlite3
        db = sqlite3.connect(f"file:{HUB_DATA / 'hub.db'}?mode=ro", uri=True)
        for k in KEYS:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (k,)).fetchone()
            if row:
                import json
                store.set_setting(k, json.loads(row[0]))
                log.info("imported setting %s", k)
        db.close()
    except Exception as ex:
        log.warning("no old settings to import: %s", ex)
    store.set_setting("ops_imported", True)


async def on_startup(app: web.Application):
    store = app["store"] = PhysStore(str(DATA / "ops.db"))
    import_old_settings(store)
    monitor = app["monitor"] = Monitor()
    monitor.start()
    app["migrator"] = Migrator(monitor, store)
    app["failback"] = Failback(monitor, store, app["migrator"])
    app["registry"] = Registry(monitor, store)
    app["replica"] = Replicator(monitor, store)
    app["specs"] = Specs(monitor)
    app["joiner"] = Joiner(monitor, store)
    app["transfer"] = Transfer(app["migrator"], monitor)
    await app["joiner"].start()
    if not os.environ.get("OPS_PASSIVE"):               # a test instance next to the real one must not run the nightly jobs twice
        asyncio.create_task(app["registry"].nightly())
        asyncio.create_task(app["registry"].housekeeping())
        asyncio.create_task(app["replica"].nightly())
        asyncio.create_task(app["specs"].loop())
        asyncio.create_task(FolderMirror(monitor).loop())


async def on_cleanup(app: web.Application):
    app["monitor"].stop()


def make_app() -> web.Application:
    app = web.Application(middlewares=[auth])
    app.add_routes([
        web.get("/api/registry", registry_list),
        web.post("/api/registry/sync", registry_sync),
        web.post("/api/registry/mirror", registry_mirror),
        web.post("/api/registry/rollback", registry_rollback),
        web.get("/api/registry/job/{job_id}", registry_job),
        web.post("/api/specs/refresh", specs_refresh),
        web.get("/api/transfer/plan", transfer_plan),
        web.post("/api/transfer/start", transfer_start),
        web.get("/api/transfer/job/{job_id}", transfer_job),
        web.get("/api/join/candidates", join_candidates),
        web.post("/api/join/claim", join_claim),
        web.get("/api/join/job/{job_id}", join_job),
        web.post("/api/replica/run", replica_run),
        web.post("/api/recovery/failback-data", failback_start),
        web.get("/api/recovery/job/{job_id}", failback_job),
        web.get("/api/monitor/migrate/plan", migrate_plan),
        web.post("/api/monitor/migrate", migrate_start),
        web.get("/api/monitor/migrate/active", migrate_active),
        web.get("/api/monitor/migrate/job/{job_id}", migrate_job),
    ])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(HUB_DATA / "tls" / "server.crt", HUB_DATA / "tls" / "server.key")
    web.run_app(make_app(), port=int(os.environ.get("OPS_PORT", "8220")), ssl_context=ctx)


if __name__ == "__main__":
    main()
