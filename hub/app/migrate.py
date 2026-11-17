"""Move a service from the NAS to the Orange Pi from the phone. A service is a container plus everything on its own docker
network (Immich = server + redis + postgres), moved together.

Steps (each shown on the phone as it happens):
  1. check            what belongs together, whether it fits, whether ports / names are free
  2. images           the Pi pulls every image from its own registry (the NAS pushes the ones the registry does not have yet)
  3. data, running    big folders that are already backed up on the SD card (the Immich library) are seeded from that copy and
                      brought up to date with rsync while the service still runs, so the stop below is short
  4. stop             the containers on the NAS stop (they stay, only stopped, until the very end)
  5. data, final      the last changes of the big folders + a copy of the small folders (databases) as tar streams
  6. start            network and containers are created on the Pi from the NAS containers' own configuration, dependencies first
  7. check            every container is up / healthy and answers on its ports
  8. forward          a tiny nginx stays on the NAS for each published port, so addresses do not change
  9. clean up         the old containers are removed from the NAS (their data folders stay: that is the NAS's copy)
Anything that fails rolls back: what was created on the Pi is removed and the NAS containers are started again.

Data lives on the Pi's SD card (/mnt/nas-sd/apps/<service>/...); the SSD is for the system only. Not offered: containers the
phone's connection depends on, non-TCP ports, privileged / device containers, folders over 15 GB that are not already backed up."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import tarfile
import time
import uuid

import aiohttp

from . import machines as mc
from .monitor import DOCKER_SOCK, PROTECTED
from .registry import ADDR, target

log = logging.getLogger("migrate")
SD_ROOT = mc.get(mc.me(), "apps_root")                # data of moved services (same path inside this container and on the Pi)
MAX_TAR = 15 * 1024 ** 3                    # a folder copied as a tar stream
SYSTEM_MOUNTS = {"/etc/localtime", "/etc/timezone"}   # exist on the Pi too: bound as they are
RSYNC_IMAGE = f"{ADDR}/nas-rsyncd:1"
NAS_IP, RSYNC_PORT, RSYNC_USER, RSYNC_PASS = mc.addr("nas"), 8730, "pibackup", "/etc/nas-backup.pass"
# NAS folders that the Pi already holds a copy of (nightly rsync into the SD card): NAS path -> (rsync module, that copy on the SD card)
BACKED_UP = {
    mc.get("nas", "data_root") + "/learnsync/immich/library": ("immich", mc.get("pi", "data_root") + "/nightly/immich"),
}
# Containers that belong on the NAS, with the reason shown instead of a plan
STAY = {
    "nas_rsyncd": "它把 NAS 的文件提供给香橙派做每晚备份，必须留在 NAS 上",
    "registry-standby": "它是镜像仓库在 NAS 上的备用副本，留在 NAS 才有意义",
    "ddnsgo_nas": "它负责公网域名解析，和 NAS 的公网入口绑在一起，要留在 NAS 上",
    "jeessy_ddns-go": "这是一个已停止的旧公网域名解析容器，不需要迁移（可以直接删掉）",
    "homehub-sentinel": "哨兵每台机器各一个，管本机的 hub 和 HA，不能搬",
}
STEPS = ["检查", "复制镜像", "数据预同步（服务照常运行）", "停止 NAS 上的容器", "数据最后一次同步", "在香橙派上启动", "确认能访问", "NAS 上留转发", "清理 NAS 上的旧容器"]


STEPS_BACK = ["检查", "复制镜像", "数据预同步（服务照常运行）", "停止香橙派上的容器", "数据最后一次同步", "在 NAS 上启动", "确认能访问", "香橙派上留转发", "清理香橙派上的旧容器"]


def where(total: int) -> bool:
    """Whether the SD card has room for `total` bytes (twice, for safety)."""
    try:
        os.makedirs(SD_ROOT, exist_ok=True)
        st = os.statvfs(SD_ROOT)
    except OSError:
        return False
    return st.f_bavail * st.f_frsize >= total * 2 + 1024 ** 3


def listening_ports() -> set[int]:
    """TCP ports something on this machine already listens on (the hub shares the host's network)."""
    ports = set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            for ln in open(f).read().splitlines()[1:]:
                p = ln.split()
                if p[3] == "0A":
                    ports.add(int(p[1].rsplit(":", 1)[1], 16))
        except OSError:
            pass
    return ports


def ports_of(hc: dict) -> list[dict]:
    out = {}
    for cport, binds in (hc.get("PortBindings") or {}).items():
        for b in binds or []:
            out[int(b["HostPort"])] = {"container": int(cport.split("/")[0]), "host": int(b["HostPort"]), "proto": cport.partition("/")[2] or "tcp"}
    return list(out.values())


def start_order(specs: dict[str, dict]) -> list[str]:
    """Containers other members' environment points at (a database named in DB_HOSTNAME or a URL) first."""
    names = {m: {m} | {a for net in info["NetworkSettings"]["Networks"].values() for a in (net.get("Aliases") or []) if not re.fullmatch(r"[0-9a-f]{12}", a)}
             for m, info in specs.items()}
    deps = {}
    for n, info in specs.items():
        vals = " ".join(info["Config"].get("Env") or [])
        deps[n] = {m for m in specs if m != n and any(re.search(rf"(?<![\w-]){re.escape(a)}(?![\w-])", vals) for a in names[m])}
    order, left = [], dict(deps)
    while left:
        ready = sorted(n for n, d in left.items() if not (d & set(left)))
        if not ready:
            ready = sorted(left)
        for n in ready:
            order.append(n)
            left.pop(n)
    return order


class Migrator:
    def __init__(self, monitor, store):
        self.m, self.store, self.jobs = monitor, store, {}
        from .takeover import Takeover
        self.back = Takeover(self)

    # ---- what would be moved, and whether it may be
    async def _group(self, nas, name: str) -> tuple[str, list[str], dict[str, dict]]:
        info = await asyncio.to_thread(nas.inspect, name)
        net = info["HostConfig"].get("NetworkMode", "default")
        names = [name]
        if net not in ("default", "bridge", "host"):
            names = sorted(await asyncio.to_thread(nas.network_members, net))
            if name not in names:
                names.append(name)
        specs = {name: info}
        for n in names:
            if n not in specs:
                specs[n] = await asyncio.to_thread(nas.inspect, n)
        return (net if net not in ("default", "bridge", "host") else name), names, specs

    async def plan(self, key: str, name: str) -> dict:
        nas, pi = self.m.hosts.get("nas"), self.m.hosts["pi"]
        if key == "pi":
            return await self.back.plan(name)
        if key != "nas" or nas is None:
            return {"ok": False, "reasons": ["不认识这台机器"]}
        rows = {c["name"]: c for c in self.m.state["nas"].get("containers", [])}
        if name not in rows:
            return {"ok": False, "reasons": ["NAS 上没有这个容器"]}
        if name.endswith("-forward") or "-forward-" in name:
            return {"ok": False, "stay": True, "reasons": ["这是迁移后留在 NAS 上的转发，服务本身已经在香橙派上运行，地址才不用改"]}
        if name in STAY:
            return {"ok": False, "stay": True, "reasons": [STAY[name]]}
        group, names, specs = await self._group(nas, name)
        reasons = []
        pi_names = {c["name"] for c in self.m.state["pi"].get("containers", [])}
        busy = listening_ports()
        members, mounts, total, ports = [], [], 0, []
        for n in names:
            info = specs[n]
            cfg, hc = info["Config"], info["HostConfig"]
            if "stop" in PROTECTED.get(n, ()) or n.endswith("-forward"):
                reasons.append(f"{n} 是手机连家里要用的入口，不能搬")
            if hc.get("Privileged") or hc.get("Devices"):
                reasons.append(f"{n} 用到了设备或特权，搬不了")
            if n in pi_names:
                reasons.append(f"香橙派上已经有同名容器 {n}")
            if hc.get("NetworkMode") == "host":
                reasons.append(f"{n} 用的是主机网络，和香橙派自己的服务容易冲突")
            for p in ports_of(hc):
                if p["proto"] != "tcp":
                    reasons.append(f"{n} 的端口 {p['host']} 不是 TCP，转发不了")
                if p["host"] in busy:
                    reasons.append(f"香橙派的 {p['host']} 端口已经被占用")
                ports.append(p["host"])
            for mt in info.get("Mounts", []):
                src = mt["Source"].rstrip("/")
                if src in SYSTEM_MOUNTS:
                    continue
                seeded = BACKED_UP.get(src)
                size = 0 if seeded else await asyncio.to_thread(nas.du, src)   # the big backed-up folder is not measured (slow) and not copied
                total += size
                if not seeded and size > MAX_TAR:
                    reasons.append(f"{n} 的数据文件夹有 {size / 1024 ** 3:.1f} GB，又没有备份副本，不适合现在搬")
                mounts.append({"container": n, "src": src, "dst": mt["Destination"], "size": size, "rw": mt.get("RW", True), "seeded": bool(seeded)})
            row = rows.get(n, {})
            members.append({"name": n, "image": cfg["Image"], "desc": row.get("desc", ""), "running": row.get("running", False),
                            "mem": hc.get("Memory") or 0})
        if not where(sum(m["size"] for m in mounts) + (5 * 1024 ** 3 if any(m["seeded"] for m in mounts) else 0)):
            reasons.append("香橙派的 SD 卡剩余空间不够")
        need = sum(max(m["mem"], rows.get(m["name"], {}).get("mem", 0)) for m in members) + 400 * 1024 ** 2
        pi_avail = self.m.state["pi"].get("mem_avail", 0)
        if pi_avail and pi_avail < need:
            reasons.append(f"香橙派可用内存只有 {pi_avail / 1024 ** 3:.1f} GB，不够（要约 {need / 1024 ** 3:.1f} GB）")
        return {"ok": not reasons, "reasons": list(dict.fromkeys(reasons)), "name": name, "group": group, "members": members,
                "mounts": mounts, "data_mb": round(total / 1024 ** 2), "ports": sorted(set(ports)), "disk": "SD 卡",
                "image": ", ".join(m["image"] for m in members), "desc": members[0]["desc"] if len(members) == 1 else "、".join(m["name"] for m in members),
                "running": any(m["running"] for m in members), "image_mb": 0, "big": any(m["seeded"] for m in mounts)}

    # ---- the job
    def start(self, name: str, plan: dict) -> str:
        job = uuid.uuid4().hex[:8]
        self.jobs[job] = {"id": job, "name": name, "group": plan["group"], "members": [m["name"] for m in plan["members"]], "started": time.time(), "to": plan.get("dest", "香橙派"), "steps": [{"t": t, "s": "wait"} for t in (STEPS_BACK if plan.get("dir") == "pi2nas" else STEPS)], "done": False, "ok": None, "error": None}
        asyncio.create_task(self.back.run(job, name, plan) if plan.get("dir") == "pi2nas" else self._run(job, name, plan))
        return job

    def active(self) -> list[dict]:
        """Jobs still running, and ones that ended in the last 10 minutes (so the phone can show how it went)."""
        now = time.time()
        return [j for j in self.jobs.values() if not j["done"] or now - j.get("ended", now) < 600]

    def _set(self, job: str, i: int, s: str, note: str | None = None, pct: int | None = None):
        st = self.jobs[job]["steps"][i]
        st["s"] = s
        if note is not None:
            st["note"] = note
        if pct is not None:
            st["pct"] = pct
        if s == "ok":
            st["pct"] = 100

    async def _pi(self, method: str, path: str, raw: bool = False, **kw):
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=DOCKER_SOCK), timeout=aiohttp.ClientTimeout(total=3600)) as http:
            async with http.request(method, "http://docker" + path, **kw) as r:
                body = await r.read()
                if r.status >= 400:
                    raise RuntimeError(body.decode(errors="replace")[:300])
                return body if raw else (json.loads(body) if body[:1] in (b"{", b"[") else body.decode(errors="replace"))

    async def _helper(self, image: str, cmd: list[str], binds: list[str], host_net: bool = False, progress=None) -> str:
        """Run a throw-away container on the Pi, wait for it, return its output; raise when it fails."""
        spec = {"Image": image, "Entrypoint": [cmd[0]], "Cmd": cmd[1:], "HostConfig": {"Binds": binds, "NetworkMode": "host" if host_net else "bridge"}}
        cid = (await self._pi("POST", f"/containers/create?name=hh-helper-{uuid.uuid4().hex[:6]}", json=spec))["Id"]
        try:
            await self._pi("POST", f"/containers/{cid}/start")
            wait = asyncio.create_task(self._pi("POST", f"/containers/{cid}/wait"))
            while progress and not wait.done():
                await asyncio.sleep(2)
                try:
                    tail = (await self._pi("GET", f"/containers/{cid}/logs?stdout=1&tail=3", raw=True)).decode(errors="replace")
                    m = re.findall(r"(\d+)%", tail)
                    if m:
                        progress(int(m[-1]))
                except Exception:
                    pass
            res = await wait
            logs = (await self._pi("GET", f"/containers/{cid}/logs?stdout=1&stderr=1", raw=True)).decode(errors="replace")
            logs = re.sub(r"[\x00-\x08]", "", logs)
            if res.get("StatusCode") not in (0, 24):
                raise RuntimeError(f"{cmd[0]} 失败（{res.get('StatusCode')}）：{logs[-200:]}")
            return logs
        finally:
            await self._pi("DELETE", f"/containers/{cid}?force=true")

    async def _rsync(self, module: str, dest: str, progress=None):
        await self._helper(RSYNC_IMAGE, ["rsync", "-a", "--delete", "--info=progress2", f"--password-file=/p", "--timeout=900",
                                         f"rsync://{RSYNC_USER}@{NAS_IP}:{RSYNC_PORT}/{module}/", "/dest/"],
                           [f"{RSYNC_PASS}:/p:ro", f"{dest}:/dest"], host_net=True, progress=progress)

    async def _pull(self, nas, image: str):
        t = target(image)
        if t is None:
            raise RuntimeError(f"镜像 {image} 不在仓库里")
        repo, tag = t
        have = await self._registry_has(repo, tag)
        if not have:
            await asyncio.to_thread(nas.push_image, image, f"{ADDR}/{repo}:{tag}")
        await self._pi("POST", f"/images/create?fromImage={ADDR}/{repo}&tag={tag}", raw=True)
        orig_repo, _, orig_tag = image.rpartition(":") if ":" in image.rsplit("/", 1)[-1] else (image, "", "latest")
        await self._pi("POST", f"/images/{ADDR}/{repo}:{tag}/tag", params={"repo": orig_repo, "tag": orig_tag}, raw=True)

    async def _registry_has(self, repo: str, tag: str) -> bool:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False), timeout=aiohttp.ClientTimeout(total=20)) as http:
            async with http.get(f"https://127.0.0.1:5000/v2/{repo}/manifests/{tag}", headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json"}) as r:
                return r.status == 200

    @staticmethod
    def _copy_tree(nas, src: str, dest: str, progress=None):
        """tar stream of a NAS folder (as root) extracted here; the folder's own name becomes `dest`."""
        os.makedirs(dest, exist_ok=True)
        parent, leaf = os.path.dirname(src), os.path.basename(src)
        cmd = nas.rootsh(f"tar -C {shlex.quote(parent)} -cf - {shlex.quote(leaf)}")
        _, o, e = nas.work().exec_command(cmd, timeout=3600)
        class Counting:                                           # counts what has come through, for the progress bar
            def read(self, n=-1):
                b = o.read(n)
                if progress and b:
                    progress(len(b))
                return b
        with tarfile.open(fileobj=Counting(), mode="r|") as t:
            t.extractall(dest, filter="fully_trusted")           # our own NAS's archive; keeps the original owners
        if o.channel.recv_exit_status():
            raise RuntimeError("读取 NAS 数据失败：" + nas.scrub(e.read().decode(errors="replace"))[-100:])

    async def _run(self, job: str, name: str, plan: dict):
        nas = self.m.hosts["nas"]
        undo: list = []                    # things to take back if a later step fails
        stopped: list[str] = []
        step = 0
        group, members = plan["group"], [m["name"] for m in plan["members"]]
        root = f"{SD_ROOT}/{group}"
        try:
            self._set(job, 0, "run")
            specs = {n: await asyncio.to_thread(nas.inspect, n) for n in members}
            order = start_order(specs)
            self._set(job, 0, "ok")

            step = 1; self._set(job, 1, "run")
            for n in members:
                await self._pull(nas, specs[n]["Config"]["Image"])
            if plan.get("big"):
                await self._pi("POST", f"/images/create?fromImage={ADDR}/nas-rsyncd&tag=1", raw=True)   # the rsync client for the big folders
            self._set(job, 1, "ok")

            # where each mount lives on the Pi
            binds: dict[str, list[str]] = {n: [] for n in members}
            small: list[tuple[str, str]] = []                    # (NAS folder, folder on the SD card) copied while stopped
            seeded: list[tuple[str, str]] = []                   # (rsync module, folder on the SD card)
            for i, mt in enumerate(plan["mounts"]):
                ro = "" if mt["rw"] else ":ro"
                if mt["src"] in BACKED_UP:
                    module, copy = BACKED_UP[mt["src"]]
                    live = f"{root}/{os.path.basename(mt['src'])}"
                    seeded.append((module, live, copy))
                    binds[mt["container"]].append(f"{live}:{mt['dst']}{ro}")
                else:
                    dest = f"{root}/{mt['container']}/m{i}"
                    small.append((mt["src"], dest))
                    binds[mt["container"]].append(f"{dest}/{os.path.basename(mt['src'])}:{mt['dst']}{ro}")
            for n in members:
                for mt in specs[n].get("Mounts", []):
                    if mt["Source"].rstrip("/") in SYSTEM_MOUNTS:
                        binds[n].append(f"{mt['Source']}:{mt['Destination']}:ro")

            step = 2; self._set(job, 2, "run")
            for module, live, copy in seeded:
                if not os.path.isdir(live):
                    os.makedirs(os.path.dirname(live), exist_ok=True)
                    # hard-link the nightly copy: the same files, no second 140 GB
                    await self._helper("alpine", ["cp", "-al", copy, live], [f"{mc.get(mc.me(), 'data_root')}:{mc.get(mc.me(), 'data_root')}"])
                await self._rsync(module, live, lambda p: self._set(job, 2, "run", pct=p))
            self._set(job, 2, "ok")

            step = 3; self._set(job, 3, "run")
            for n in reversed(order):
                if plan["members"][members.index(n)]["running"]:
                    await nas.control(n, "stop")
                    stopped.append(n)
            self._set(job, 3, "ok")

            step = 4; self._set(job, 4, "run")
            for module, live, copy in seeded:
                await self._rsync(module, live, lambda p: self._set(job, 4, "run", pct=min(p, 99)))
            total = max(sum(m["size"] for m in plan["mounts"] if m["src"] not in BACKED_UP), 1)
            done = [0, time.time()]

            def prog(n):                                          # runs in the copy thread
                done[0] += n
                el = max(time.time() - done[1], 1)
                self._set(job, 4, "run", f"{done[0] / 1024 ** 3:.1f} / {total / 1024 ** 3:.1f} GB · {done[0] / el / 1024 ** 2:.0f} MB/s",
                          min(int(100 * done[0] / total), 99))
            for src, dest in small:
                await asyncio.to_thread(self._copy_tree, nas, src, dest, prog)
            self._set(job, 4, "ok")

            step = 5; self._set(job, 5, "run")
            net = None if group in members else group
            if net:
                try:
                    await self._pi("POST", "/networks/create", json={"Name": net, "Driver": "bridge", "CheckDuplicate": True})
                    undo.append(lambda: self._pi("DELETE", f"/networks/{net}", raw=True))
                except RuntimeError as ex:
                    if "exists" not in str(ex):
                        raise
            for n in order:
                info = specs[n]
                cfg, hc = info["Config"], info["HostConfig"]
                ports = {f"{p['container']}/tcp": [{"HostPort": str(p["host"])}] for p in ports_of(hc)}
                mode = net or "bridge"
                aliases = [a for a in (info["NetworkSettings"]["Networks"].get(net, {}).get("Aliases") or []) if not re.fullmatch(r"[0-9a-f]{12}", a)] if net else []
                create = {
                    "Image": cfg["Image"], "Env": cfg.get("Env"), "Cmd": cfg.get("Cmd"), "Entrypoint": cfg.get("Entrypoint"),
                    "WorkingDir": cfg.get("WorkingDir") or "", "User": cfg.get("User") or "", "Healthcheck": cfg.get("Healthcheck"),
                    "ExposedPorts": {k: {} for k in ports},
                    "HostConfig": {"Binds": binds[n], "PortBindings": ports, "Memory": hc.get("Memory") or 0, "ShmSize": hc.get("ShmSize") or 67108864,
                                   "RestartPolicy": {"Name": (hc.get("RestartPolicy") or {}).get("Name") or "unless-stopped"}, "NetworkMode": mode},
                    "NetworkingConfig": {"EndpointsConfig": {mode: {"Aliases": aliases or [n]}}} if net else {},
                }
                await self._pi("POST", f"/containers/create?name={n}", json=create)
                undo.append(lambda n=n: self._pi("DELETE", f"/containers/{n}?force=true"))
                await self._pi("POST", f"/containers/{n}/start")
            self._set(job, 5, "ok")

            step = 6; self._set(job, 6, "run")
            for n in order:
                for _ in range(150):
                    st = (await self._pi("GET", f"/containers/{n}/json"))["State"]
                    health = (st.get("Health") or {}).get("Status")
                    if not st.get("Running"):
                        raise RuntimeError(f"{n} 启动后又退出了")
                    if health in (None, "healthy"):
                        await asyncio.sleep(3)
                        if (await self._pi("GET", f"/containers/{n}/json"))["State"].get("Running"):
                            break
                    await asyncio.sleep(2)
                else:
                    raise RuntimeError(f"{n} 一直没有变成健康状态")
            for n in order:
                for p in ports_of(specs[n]["HostConfig"]):
                    for _ in range(60):
                        try:
                            r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", p["host"]), 2)
                            w.close()
                            break
                        except Exception:
                            await asyncio.sleep(2)
                    else:
                        raise RuntimeError(f"端口 {p['host']} 没有响应")
            self._set(job, 6, "ok")

            step = 7; self._set(job, 7, "run")
            all_ports = sorted({p["host"] for n in members for p in ports_of(specs[n]["HostConfig"])})
            if not self.store.get_setting("leave_forward", False):         # the apps hold both machines' addresses now: no forwarder is left behind
                self._set(job, 7, "ok", "按设置不留转发")
                all_ports = []
            for port in all_ports:
                fwd = f"{group}-forward-{port}" if len(all_ports) > 1 else f"{group}-forward"
                await asyncio.to_thread(nas.forwarder, fwd, port, mc.addr(mc.me()))
                undo.append(lambda f=fwd: asyncio.to_thread(nas.remove, f))
                for _ in range(10):
                    try:
                        r, w = await asyncio.wait_for(asyncio.open_connection(NAS_IP, port), 2)
                        w.close()
                        break
                    except Exception:
                        await asyncio.sleep(1)
                else:
                    raise RuntimeError(f"NAS 上的 {port} 转发没有通")
            self._set(job, 7, "ok")

            step = 8; self._set(job, 8, "run")
            for n in members:
                await asyncio.to_thread(nas.remove, n)
            self._set(job, 8, "ok")
            moved = self.store.get_setting("migrations", [])
            moved.append({"name": group, "members": members, "from": "nas", "to": "pi", "ts": time.time(), "ports": all_ports})
            self.store.set_setting("migrations", moved)
            replicas = self.store.get_setting("replicas", [])
            for mt in plan["mounts"]:
                pi_dir = f"{root}/{os.path.basename(mt['src'])}" if mt["src"] in BACKED_UP else f"{root}/{mt['container']}/m{plan['mounts'].index(mt)}/{os.path.basename(mt['src'])}"
                replicas.append({"service": group, "pi": pi_dir, "nas": mt["src"]})
            self.store.set_setting("replicas", replicas)
            self.jobs[job].update(done=True, ok=True, ended=time.time())
            await self.m.refresh("nas"); await self.m.refresh("pi")
        except Exception as ex:
            log.warning("migrate %s failed at step %d: %s", name, step, ex)
            self._set(job, step, "fail")
            for u in reversed(undo):
                try:
                    await u()
                except Exception:
                    pass
            for n in stopped:
                try:
                    await nas.control(n, "start")
                except Exception:
                    pass
            self.jobs[job].update(done=True, ok=False, ended=time.time(), error=f"{STEPS[step]}：{str(ex)[:150]}。已恢复原样（NAS 上的容器继续运行）")
            await self.m.refresh("nas"); await self.m.refresh("pi")
