"""The address table: where each of the apps' servers can be reached.

Apps used to have their addresses baked in, so any change (a server moved, a domain retired) meant a new app build. Now the
table lives here and the apps fetch it (and keep the last copy they got); the addresses baked into an app are only the
first-start fallback.

Format (version goes up on every change, so apps can tell a newer table):
  {"version": 3, "updated": <unix>, "services": {"<service>": {"check": "/path", "expect": "text in the answer",
   "lan": [urls tried first, at home], "wan": [urls tried after, from outside]}}}
`check` is a cheap GET that only the right service answers; `expect` (optional) must appear in the answer, so a gateway's
error page does not count as "up".

Two copies are kept: here (GET /api/endpoints, no token - the table holds addresses only, nothing secret) and on the NAS,
next to the app update files, so apps can still read it when the Pi is down (http://<nas>:8099/endpoints.json)."""
from __future__ import annotations

import asyncio
import json
import re
import logging
import os
import time

log = logging.getLogger("endpoints")
from . import machines as mc

NAS_FILE = mc.get("nas", "data_root") + "/learnsync/music-update/endpoints.json"
PI, NAS = mc.addr("pi"), mc.addr("nas")
# Public (DDNS) names come from machines.json ("domain"), which is not in the repository.
def wan_urls(scheme: str, port_of, order) -> list[str]:
    return [f"{scheme}://{mc.get(k, 'domain')}:{port_of(k)}" for k in order if mc.get(k, "domain")]

# The hub itself is on the Pi (8200); the NAS reaches it through its forwarder on 8095.
DEFAULT = {"services": {
    "homehub": {"check": "/static/icon.svg", "expect": "<svg",
                "lan": [mc.hub_url("pi"), mc.hub_url("nas")],
                "wan": wan_urls("https", lambda k: mc.get(k, "hub_port"), ("pi", "nas"))},
}}


# Services that were registered by hand before the table could build itself: which container is the service, and on which port.
SEED = {"music": ("navidrome", 4533), "immich": ("immich_server", 2283), "ledger": ("ledger-api-1", 8000), "music-update": ("music-update", 8099)}


INFRA = re.compile(r"^(homehub-|registry|homeassistant|ddnsgo|jeessy_ddns|nas_rsyncd|immich_(redis|postgres|ml)|.*-(db|redis|postgres)(-\d+)?$|ledger-backup)")


class Endpoints:
    def __init__(self, store, monitor):
        self.store, self.m = store, monitor
        self.auto = {"at": 0, "changed": [], "error": None}

    # ---- which containers are which service: a label on the container (homehub.service) or an entry here (one tap in the app)
    def catalog(self) -> dict:
        c = self.store.get_setting("service_catalog")
        if not c:
            c = {k: list(v) for k, v in SEED.items()}
            self.store.set_setting("service_catalog", c)
        return {k: (v[0], int(v[1])) for k, v in c.items()}

    def register(self, container: str, name: str, port: int):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,30}", name) or not (0 < port < 65536):
            raise ValueError("名字只能用小写字母、数字、-、_")
        c = {k: list(v) for k, v in self.catalog().items()}
        c[name] = [container, port]
        self.store.set_setting("service_catalog", c)

    async def forget(self, name: str):
        """Take a service out of the table (and out of the list of entries; a container that carries a label will come back by itself)."""
        if name == "homehub":
            raise ValueError("homehub 这一项不能删")
        c = {k: list(v) for k, v in self.catalog().items()}
        c.pop(name, None)
        self.store.set_setting("service_catalog", c)
        t = self.get()
        if t["services"].pop(name, None) is not None:
            t["version"], t["updated"] = t["version"] + 1, int(time.time())
            self.store.set_setting("endpoints", t)
            await self.copy_to_nas()

    async def rename(self, name: str, to: str):
        cat = self.catalog()
        if name not in cat:
            raise ValueError("这一项是容器标签登记的，改容器标签才能改名")
        cont, port = cat[name]
        entry = self.get()["services"].get(name)
        self.register(cont, to, port)
        await self.forget(name)
        if entry:
            await self.put({to: entry})

    async def probe(self, name: str) -> dict:
        """Can the hub itself open the first home address of this service right now?"""
        import aiohttp
        e = self.get()["services"].get(name)
        if not e or not e.get("lan"):
            return {"ok": False, "why": "没有地址"}
        url = e["lan"][0].rstrip("/") + (e.get("check") or "/")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as http:
                async with http.get(url, ssl=False) as r:
                    body = (await r.content.read(4096)).decode("utf-8", "ignore")
                    ok = r.status < 500 and (not e.get("expect") or e["expect"] in body)
                    return {"ok": ok, "why": "" if ok else f"返回 {r.status}"}
        except Exception as ex:
            return {"ok": False, "why": type(ex).__name__}

    async def audit(self) -> dict:
        """Every container that listens on a port: is it in the address table (by label or by an entry), and who runs it now."""
        cat = self.catalog()
        by_cont = {cont: (svc, p) for svc, (cont, p) in cat.items()}
        rows, missing = [], []
        for key, host in self.m.hosts.items():
            try:
                lst = await host.labelled()
            except Exception:
                continue
            for c in lst:
                n, lb = c["name"], c["labels"]
                if INFRA.match(n) or n.endswith("-forward") or (not c["ports"] and not lb.get("homehub.service")):
                    continue
                if lb.get("homehub.service"):
                    rows.append({"name": lb["homehub.service"], "container": n, "on": key, "running": c["running"], "how": "label"})
                elif n in by_cont:
                    rows.append({"name": by_cont[n][0], "container": n, "on": key, "running": c["running"], "how": "entry"})
                elif c["running"] or not any(m["container"] == n for m in missing):
                    guess = re.sub(r"(-forward|_default|-server|-1)$", "", n).lower()
                    missing.append({"container": n, "on": key, "running": c["running"], "ports": c["ports"], "guess": guess})
        return {"rows": rows, "missing": missing, "table": self.get().get("services", {})}

    # ---- the table builds itself: a service's addresses are listed with the machine that runs it FIRST
    async def reconcile(self):
        t = self.get()
        found: dict[str, dict] = {}                       # service -> {"port", "scheme", "check", "expect", "on": {machine: running}}
        for key, host in self.m.hosts.items():
            try:
                rows = await host.labelled()
            except Exception as ex:                       # this machine could not be asked: its services keep the order they have
                log.info("endpoints: %s not asked: %s", key, ex)
                return
            for c in rows:
                lb = c["labels"]
                name = lb.get("homehub.service")
                port = int(lb["homehub.port"]) if lb.get("homehub.port") else (c["ports"][0] if c["ports"] else 0)
                if not name:
                    for svc, (cont, p) in self.catalog().items():
                        if c["name"] == cont:
                            name, port = svc, p
                if not name or not port:
                    continue
                f = found.setdefault(name, {"port": port, "scheme": lb.get("homehub.scheme", "http"), "check": lb.get("homehub.check"),
                                            "expect": lb.get("homehub.expect"), "on": {}})
                f["on"][key] = f["on"].get(key, False) or c["running"]
                for k, lk in (("check", "homehub.check"), ("expect", "homehub.expect")):
                    if lb.get(lk) is not None:
                        f[k] = lb[lk]
        changes = {}
        for name, f in found.items():
            where = [k for k, v in f["on"].items() if v]
            if len(where) != 1:                           # runs on both, or nowhere right now: do not guess
                continue
            first = where[0]
            order = [first] + [k for k in ("pi", "nas") if k != first]
            cur = t["services"].get(name, {})
            entry = {"check": f["check"] if f["check"] is not None else cur.get("check", "/"),
                     "expect": f["expect"] if f["expect"] is not None else cur.get("expect", ""),
                     "lan": [f"{f['scheme']}://{mc.addr(k)}:{f['port']}" for k in order],
                     "wan": wan_urls(f["scheme"], lambda k: f["port"], order) or cur.get("wan", [])}
            if any(cur.get(k) != entry[k] for k in entry):
                changes[name] = entry
        if changes:
            await self.put(changes)
            log.info("endpoints: updated from where the services run: %s", ", ".join(changes))
        self.auto.update(at=time.time(), changed=list(changes), error=None)
        try:                                                                  # the directory page follows along (only written when it changed)
            md = (await self.directory_md()).encode()
            if md != getattr(self, "_dir_md", None):
                await asyncio.to_thread(self._write_static, md, "directory.md")
                try:
                    await asyncio.to_thread(self._copy_named, md, "directory.md")
                except Exception as ex:
                    log.info("directory.md NAS copy: %s", ex)
                self._dir_md = md
        except Exception as ex:
            log.warning("directory page: %s", ex)

    async def loop(self):
        await asyncio.sleep(90)
        while True:
            try:
                await self.reconcile()
            except Exception as ex:
                self.auto["error"] = str(ex)[:120]
                log.warning("endpoints reconcile: %s", ex)
            await asyncio.sleep(60)

    def get(self) -> dict:
        t = self.store.get_setting("endpoints")
        ips = {"pi": mc.addr("pi"), "nas": mc.addr("nas")}
        was = self.store.get_setting("endpoints_ips")
        if t and was and was != ips:                      # a machine has a new address: the saved table follows (the apps read it from here)
            text = json.dumps(t, ensure_ascii=False)
            for r in ("pi", "nas"):
                text = text.replace(was.get(r, "@@"), ips[r]) if was.get(r) and was[r] != ips[r] else text
            t = json.loads(text)
            t["version"] = int(t.get("version", 1)) + 1
            t["updated"] = int(time.time())
            self.store.set_setting("endpoints", t)
        if was != ips:
            self.store.set_setting("endpoints_ips", ips)
        if not t:
            t = {**DEFAULT, "version": 1, "updated": int(time.time())}
            self.store.set_setting("endpoints", t)
        else:                                             # services added in a later release show up without wiping edits
            for k, v in DEFAULT["services"].items():
                t["services"].setdefault(k, v)
        return t

    async def put(self, services: dict) -> dict:
        for name, s in services.items():
            if not isinstance(s, dict) or not all(isinstance(u, str) and u.startswith(("http://", "https://"))
                                                  for k in ("lan", "wan") for u in s.get(k, [])):
                raise ValueError(f"{name}: lan/wan must be lists of http(s) URLs")
        for name, s in services.items():
            ck = s.get("check")
            if ck and not ck.startswith("/"):                 # a path mangled by a Windows shell ("C:/Program Files/Git/health"): keep what follows the install folder
                s["check"] = "/" + ck.split("/Git/", 1)[1] if "/Git/" in ck else "/"
        t = self.get()
        t["services"] = {**t["services"], **services}
        t["version"], t["updated"] = t["version"] + 1, int(time.time())
        self.store.set_setting("endpoints", t)
        await self.copy_to_nas()
        return t

    async def directory_md(self) -> str:
        """One plain page that tells any app or conversation where things are: services, which machine, ports, the folders on the host
        (the update folder is marked) and the addresses. No secrets: only the names of a few harmless environment variables."""
        t = self.get()["services"]
        cat = self.catalog()
        by_cont = {cont: svc for svc, (cont, _) in cat.items()}
        lines = ["# 家里服务器目录（自动生成，每分钟更新）", "",
                 "- 两台机器：香橙派（pi）和 NAS（nas）。服务跑在哪台，下面写着。",
                 "- App 读地址：`GET http://<pi地址>:8099/endpoints.json`，按服务名取 lan / wan 依次试。",
                 "- 新服务进地址表：容器起来、发布好端口，让用户在手机 HomeHub →服务器页 →“地址表”点“登记”。",
                 "- App 发版：把 apk 放进下面标着“更新文件夹”的宿主机路径，**先传 apk，最后传 version.json**（versionCode 要更大）；不要放进别的 App 的更新文件夹。",
                 "- 不要向用户要令牌或密码；需要的事用 `D:\\Github\\HomeHub\\tools\\` 里的脚本。", ""]
        seen, got = set(), {}
        for key, host in self.m.hosts.items():
            try:
                got[key] = await host.inspected()
            except Exception as ex:
                got[key] = ex
        live_names = {c["Name"].lstrip("/") for v in got.values() if isinstance(v, list) for c in v if c["State"]["Status"] == "running"}
        for key, host in self.m.hosts.items():
            try:
                rows = got[key]
                if isinstance(rows, Exception):
                    raise rows
            except Exception as ex:
                lines.append(f"（{key} 这次没读到：{str(ex)[:60]}）")
                continue
            for c in sorted(rows, key=lambda c: c["Name"]):
                n = c["Name"].lstrip("/")
                if INFRA.match(n) or n.endswith("-forward"):
                    continue
                running = c["State"]["Status"] == "running"
                if not running and (n in live_names or n in seen):          # the stopped standby copy on the other machine is not worth a section
                    continue
                seen.add(n)
                svc = (c["Config"].get("Labels") or {}).get("homehub.service") or by_cont.get(n)
                ports = sorted({p["HostPort"] for bs in (c["NetworkSettings"].get("Ports") or {}).values() for p in (bs or [])})
                lines.append(f"## {svc or n}" + (f"（容器 {n}）" if svc and svc != n else ""))
                lines.append(f"- 运行在：{'香橙派' if key == 'pi' else 'NAS'}，状态 {c['State']['Status']}" + (f"，端口 {', '.join(ports)}" if ports else ""))
                for m in c.get("Mounts") or []:
                    d = m["Destination"]
                    if m.get("Type") == "volume" or d in ("/etc/localtime", "/etc/timezone") or d.startswith(("/tls", "/var/run")):
                        continue
                    lines.append(f"- 文件夹：`{m.get('Source')}` → 容器内 `{d}`" + ("   ← **更新文件夹**" if "update" in d.lower() else ""))
                for ev in c["Config"].get("Env") or []:
                    k, _, v = ev.partition("=")
                    if any(w in k.upper() for w in ("UPDATE_DIR", "DATA_DIR")):
                        lines.append(f"- 环境变量：`{k}={v}`")
                if svc and svc in t:
                    e = t[svc]
                    lines.append(f"- 地址：内网 {', '.join(e.get('lan', []))}；外网 {', '.join(e.get('wan', []))}；检测 `{e.get('check', '/')}`")
                lines.append("")
        return "\n".join(lines)

    def _write_static(self, blob: bytes, name: str = "endpoints.json"):
        """The folder the music-update web server serves (it was moved to the Pi: its folder is under the apps folder): a plain-http copy of the
        table next to the app updates, for apps that cannot read the hub's https."""
        import glob
        root = mc.get("pi", "apps_root")
        for d in glob.glob(root + "/music-update/*/*/music-update") + glob.glob(root + "/directory/*/*/site"):    # the "directory" service serves the same files
            if os.path.isdir(d):
                tmp = os.path.join(d, "." + name + ".tmp")
                with open(tmp, "wb") as f:
                    f.write(blob)
                os.replace(tmp, os.path.join(d, name))

    async def copy_to_nas(self) -> str | None:
        """Best effort: the NAS being down must not make a change fail."""
        try:
            await asyncio.to_thread(self._write_static, json.dumps(self.get(), ensure_ascii=False).encode())
        except Exception as ex:
            log.warning("endpoints: static copy: %s", ex)
        try:
            await asyncio.to_thread(self._copy, json.dumps(self.get(), ensure_ascii=False).encode())
            return None
        except Exception as ex:
            log.warning("endpoints: NAS copy: %s", ex)
            return str(ex)[:120]

    def _copy_named(self, blob: bytes, name: str):
        nas = self.m.hosts["nas"]
        nas._ensure()
        ch = nas.work().get_transport().open_session()
        apps = mc.get("nas", "apps_root")
        ch.exec_command(nas.rootsh(f"cat > /tmp/.{name}.new && for d in {apps}/music-update/*/*/music-update {apps}/directory/*/*/site; do "
                                   f"[ -d \"$d\" ] && cp /tmp/.{name}.new \"$d/{name}\" && chmod 644 \"$d/{name}\"; done; rm -f /tmp/.{name}.new; true"))
        ch.sendall(blob)
        ch.shutdown_write()
        ch.recv_exit_status()

    def _copy(self, blob: bytes):
        nas = self.m.hosts["nas"]
        nas._ensure()
        ch = nas.work().get_transport().open_session()
        apps = mc.get("nas", "apps_root")                                  # and into the folder the moved music-update serves, if it was moved here
        ch.exec_command(nas.rootsh(f"tee {NAS_FILE} >/dev/null && chmod 644 {NAS_FILE}; for d in {apps}/music-update/*/*/music-update; do "
                                   f"[ -d \"$d\" ] && cp {NAS_FILE} \"$d/endpoints.json\" && chmod 644 \"$d/endpoints.json\"; done; "
                                   f"for d in {apps}/directory/*/*/site; do [ -d \"$d\" ] && cp {NAS_FILE} \"$d/endpoints.json\" && chmod 644 \"$d/endpoints.json\"; done; true"))
        ch.sendall(blob)
        ch.shutdown_write()
        if ch.recv_exit_status():
            raise RuntimeError(nas.scrub(ch.makefile_stderr("r").read().decode(errors="replace"))[-100:])
