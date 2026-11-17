"""The way back: move a service from the Orange Pi to the NAS (same idea as migrate.py, other direction).

The NAS already holds a copy of the data of every service that was moved from it (its old folder, kept up to date every night by
replica.py), so the last sync only has to send what changed. Images come from the registry (the Pi's, or the NAS's own standby copy),
the containers are created on the NAS from the Pi containers' own configuration, and the Pi keeps a tiny forwarder so the Pi
addresses keep working.

Needs the Pi to be alive (this runs inside the hub on the Pi). For a dead Pi the same steps would have to run from a hub on the NAS."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shlex
import time

from .migrate import SD_ROOT, ports_of, start_order
from .monitor import PROTECTED
from .registry import ADDR, NAS_ADDR, target
from .replica import Replicator

from . import machines as mc

log = logging.getLogger("takeover")
NAS_APPS = mc.get("nas", "apps_root")
_U = mc.get("pi", "ssh_user")
READABLE = (mc.get("pi", "data_root") + "/", f"/home/{_U}/learnsync/", f"/home/{_U}/plan/")   # paths this container can read (the ops container mounts them)
PI_STAY = {
    "homehub-hub": "这是管理界面本身，在香橙派上才能指挥迁移",
    "registry": "这是镜像仓库的主库，NAS 上已经有它的备用副本",
    "registry-standby": "这是镜像仓库的只读副本，跟着主站走，不能搬",
    "homeassistant": "它要用香橙派上的设备和主机网络，搬不了",
    "homehub-ops": "它是迁移服务本身，要用香橙派的硬盘和 Docker，必须留在香橙派上",
    "homehub-sentinel": "哨兵每台机器各一个，管本机的 hub 和 HA，不能搬",
    "ddnsgo_pi": "它让香橙派的外网域名指到香橙派，要留在香橙派上",
}
STEPS = ["检查", "复制镜像", "数据预同步（服务照常运行）", "停止香橙派上的容器", "数据最后一次同步", "在 NAS 上启动", "确认能访问", "香橙派上留转发", "清理香橙派上的旧容器"]


class Takeover:
    def __init__(self, mig):
        self.mig = mig

    # ---- NAS docker through its socket (as root), so the same JSON the Pi accepts can be used
    def _nas_api(self, method: str, path: str, body: dict | None = None):
        nas = self.mig.m.hosts["nas"]
        nas._ensure()
        data = f"echo {base64.b64encode(json.dumps(body).encode()).decode()} | base64 -d | " if body is not None else ""
        script = (f"{data}curl -s -S -w '\\n%{{http_code}}' -X {method} --unix-socket /var/run/docker.sock "
                  f"-H 'Content-Type: application/json' {'-d @- ' if body is not None else ''}http://d{path}")
        _, o, e = nas.work().exec_command(nas.rootsh(script), timeout=900)
        text = nas.scrub(o.read().decode(errors="replace"))
        err = nas.scrub(e.read().decode(errors="replace"))
        out, _, code = text.rpartition("\n")
        if not code.strip().isdigit() or int(code) >= 400:
            raise RuntimeError((out or err or text).strip()[:300])
        return json.loads(out) if out.strip()[:1] in ("{", "[") else out

    def _nas_listening(self) -> set[int]:
        nas = self.mig.m.hosts["nas"]
        out, _ = nas._run("ss -ltnH")
        return {int(m.group(1)) for m in re.finditer(r":(\d+)\s", out)}

    def _nas_forward_ports(self) -> set[int]:
        nas = self.mig.m.hosts["nas"]
        out, _ = nas._run("docker ps --format '{{.Names}} {{.Ports}}'", sudo=True)
        ports = set()
        for ln in out.splitlines():
            name, _, rest = ln.partition(" ")
            if name.endswith("-forward"):
                ports |= {int(x) for x in re.findall(r":(\d+)->", rest)}
        return ports

    async def _inspect(self, name: str) -> dict:
        return await self.mig._pi("GET", f"/containers/{name}/json")

    async def plan(self, name: str) -> dict:
        m, nas = self.mig.m, self.mig.m.hosts.get("nas")
        if nas is None or not m.state["nas"].get("ok"):
            return {"ok": False, "reasons": ["连不上 NAS"]}
        if name.endswith("-forward") or name in PI_STAY or "stop" in PROTECTED.get(name, ()):
            return {"ok": False, "stay": True, "reasons": [PI_STAY.get(name, "这是转发或入口，留在原处")]}
        info = await self._inspect(name)
        if (info["Config"].get("Labels") or {}).get("homehub.native"):         # a stand-in for one of the NAS's own services: not a Pi service
            return {"ok": False, "stay": True, "reasons": ["这是 NAS 自己的服务在香橙派上的临时副本"]}
        net = info["HostConfig"].get("NetworkMode", "default")
        names = [name]
        if net not in ("default", "bridge", "host"):
            nets = await self.mig._pi("GET", f"/networks/{net}")
            names = sorted((c["Name"] for c in (nets.get("Containers") or {}).values()))
            if name not in names:
                names.append(name)
        specs = {n: (info if n == name else await self._inspect(n)) for n in names}
        group = net if net not in ("default", "bridge", "host") else name
        replicas = {r["pi"].rstrip("/"): r["nas"] for r in self.mig.store.get_setting("replicas", [])}
        busy = await asyncio.to_thread(self._nas_listening)
        fwd_ports = await asyncio.to_thread(self._nas_forward_ports)     # ports held by a forwarder: it is taken away when the service arrives
        reasons, warnings, members, mounts, ports = [], [], [], [], []
        for n in names:
            i = specs[n]
            hc = i["HostConfig"]
            if n in PI_STAY or "stop" in PROTECTED.get(n, ()):
                reasons.append(f"{n}：{PI_STAY.get(n, '不能搬')}")
            if hc.get("Privileged") or hc.get("Devices"):
                reasons.append(f"{n} 用到了设备或特权，搬不了")
            if hc.get("NetworkMode") == "host":
                reasons.append(f"{n} 用的是主机网络，搬不了")
            for p in ports_of(hc):
                ports.append(p["host"])
                if p["host"] in busy and p["host"] not in fwd_ports:
                    reasons.append(f"NAS 的 {p['host']} 端口已经被占用")
            for k, mt in enumerate(i.get("Mounts", [])):
                src = mt["Source"].rstrip("/")
                if mt["Type"] == "volume":
                    warnings.append(f"{n} 有一个缓存卷，在 NAS 上会重新生成")
                    continue
                if mt["Destination"] in ("/etc/localtime", "/etc/timezone"):
                    continue
                if not src.startswith(READABLE):
                    reasons.append(f"{n} 的数据 {src} 不在 SD 卡上，读不到")
                    continue
                size = await asyncio.to_thread(self._du, src)
                dest = replicas.get(src) or f"{NAS_APPS}/{group}/{n}/m{k}/{os.path.basename(src)}"
                mounts.append({"container": n, "src": src, "dst": mt["Destination"], "size": size, "rw": mt.get("RW", True), "nas": dest, "known": src in replicas})
            members.append({"name": n, "image": i["Config"]["Image"], "running": i["State"]["Running"], "mem": hc.get("Memory") or 0})
        need = sum(x["mem"] for x in members) + 300 * 1024 ** 2
        avail = m.state["nas"].get("mem_avail", 0)
        if avail and avail < need:
            warnings.append(f"NAS 可用内存约 {avail / 1024 ** 3:.1f} GB，这组要约 {need / 1024 ** 3:.1f} GB，会比较吃紧")
        total = sum(x["size"] for x in mounts if not x["known"])
        return {"ok": not reasons, "dir": "pi2nas", "reasons": list(dict.fromkeys(reasons)), "warnings": warnings, "name": name, "group": group,
                "members": members, "mounts": mounts, "data_mb": round(total / 1024 ** 2), "ports": sorted(set(ports)), "disk": "NAS 硬盘",
                "image": ", ".join(x["image"] for x in members), "desc": "、".join(x["name"] for x in members), "running": any(x["running"] for x in members),
                "image_mb": 0, "big": any(x["known"] for x in mounts), "dest": "NAS"}

    @staticmethod
    def _du(path: str) -> int:
        tot = 0
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    tot += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
        return tot

    # ---- the job
    async def run(self, job: str, name: str, plan: dict):
        mig, nas = self.mig, self.mig.m.hosts["nas"]
        undo: list = []
        stopped: list[str] = []
        step = 0
        group, members = plan["group"], [x["name"] for x in plan["members"]]
        rep = Replicator(mig.m, mig.store)
        try:
            mig._set(job, 0, "run")
            specs = {n: await self._inspect(n) for n in members}
            order = start_order({n: {"Config": s["Config"], "NetworkSettings": s["NetworkSettings"]} for n, s in specs.items()})
            mig._set(job, 0, "ok")

            step = 1; mig._set(job, 1, "run")
            for n in members:
                image = specs[n]["Config"]["Image"]
                t = target(image)
                if t is None:
                    raise RuntimeError(f"镜像 {image} 不在仓库里")
                repo, tag = t
                orig = image.rpartition(":")[0] if ":" in image.rsplit("/", 1)[-1] else image
                otag = image.rpartition(":")[2] if ":" in image.rsplit("/", 1)[-1] else "latest"
                for addr in (ADDR, NAS_ADDR):                          # the Pi's registry, or the NAS's own copy when the first is out of reach
                    out, err = await asyncio.to_thread(nas._run, f"docker pull {addr}/{repo}:{tag}", True)
                    if "Downloaded" in out or "up to date" in out:
                        await asyncio.to_thread(nas._run, f"docker tag {addr}/{repo}:{tag} {orig}:{otag}", True)
                        break
                else:
                    raise RuntimeError(f"NAS 拉不到镜像 {repo}:{tag}")
            mig._set(job, 1, "ok")

            step = 2; mig._set(job, 2, "run")
            for k, mt in enumerate(plan["mounts"]):
                mig._set(job, 2, "run", f"{k + 1} / {len(plan['mounts'])} · {os.path.basename(mt['src'])}", int(100 * k / max(len(plan["mounts"]), 1)))
                await asyncio.to_thread(rep._push, nas, mt["src"], mt["nas"])
            mig._set(job, 2, "ok")

            step = 3; mig._set(job, 3, "run")
            for n in reversed(order):
                if specs[n]["State"]["Running"]:
                    await mig._pi("POST", f"/containers/{n}/stop?t=20", raw=True)
                    stopped.append(n)
            mig._set(job, 3, "ok")

            step = 4; mig._set(job, 4, "run")
            for k, mt in enumerate(plan["mounts"]):
                mig._set(job, 4, "run", f"{k + 1} / {len(plan['mounts'])} · {os.path.basename(mt['src'])}", int(100 * k / max(len(plan["mounts"]), 1)))
                await asyncio.to_thread(rep._push, nas, mt["src"], mt["nas"])
            mig._set(job, 4, "ok")

            step = 5; mig._set(job, 5, "run")
            # the NAS forwarders of this service give way: the real service takes their ports
            fwd_old = [c["name"] for c in mig.m.state["nas"].get("containers", []) if re.fullmatch(rf"({re.escape(group)}|{re.escape(name)})-forward(-\d+)?", c["name"])]
            for f in fwd_old:
                await asyncio.to_thread(nas.remove, f)
            undo.append(lambda: self._restore_forwarders(nas, fwd_old, ports_of_all(specs)))
            net = None if group in members else group
            if net:
                try:
                    await asyncio.to_thread(self._nas_api, "POST", "/networks/create", {"Name": net, "Driver": "bridge", "CheckDuplicate": True})
                    undo.append(lambda: asyncio.to_thread(self._nas_api, "DELETE", f"/networks/{net}"))
                except RuntimeError as ex:
                    if "exists" not in str(ex):
                        raise
            by_src = {mt["src"]: mt for mt in plan["mounts"]}
            for n in order:
                info = specs[n]
                cfg, hc = info["Config"], info["HostConfig"]
                binds = []
                for mt in info.get("Mounts", []):
                    src = mt["Source"].rstrip("/")
                    if mt["Destination"] in ("/etc/localtime", "/etc/timezone"):
                        binds.append(f"{mt['Source']}:{mt['Destination']}:ro")
                    elif src in by_src:
                        binds.append(f"{by_src[src]['nas']}:{mt['Destination']}" + ("" if mt.get("RW", True) else ":ro"))
                ports = {f"{p['container']}/tcp": [{"HostPort": str(p["host"])}] for p in ports_of(hc)}
                mode = net or "bridge"
                aliases = [a for a in (info["NetworkSettings"]["Networks"].get(net, {}).get("Aliases") or []) if not re.fullmatch(r"[0-9a-f]{12}", a)] if net else []
                create = {
                    "Image": cfg["Image"], "Env": cfg.get("Env"), "Cmd": cfg.get("Cmd"), "Entrypoint": cfg.get("Entrypoint"),
                    "WorkingDir": cfg.get("WorkingDir") or "", "User": cfg.get("User") or "", "Healthcheck": cfg.get("Healthcheck"),
                    "ExposedPorts": {k: {} for k in ports},
                    "HostConfig": {"Binds": binds, "PortBindings": ports, "Memory": hc.get("Memory") or 0, "ShmSize": hc.get("ShmSize") or 67108864,
                                   "RestartPolicy": {"Name": (hc.get("RestartPolicy") or {}).get("Name") or "unless-stopped"}, "NetworkMode": mode},
                    "NetworkingConfig": {"EndpointsConfig": {mode: {"Aliases": aliases or [n]}}} if net else {},
                }
                await asyncio.to_thread(self._nas_api, "POST", f"/containers/create?name={n}", create)
                undo.append(lambda n=n: asyncio.to_thread(nas.remove, n))
                await asyncio.to_thread(self._nas_api, "POST", f"/containers/{n}/start")
            mig._set(job, 5, "ok")

            step = 6; mig._set(job, 6, "run")
            for n in order:
                for _ in range(150):
                    st = (await asyncio.to_thread(self._nas_api, "GET", f"/containers/{n}/json"))["State"]
                    if not st.get("Running"):
                        raise RuntimeError(f"{n} 启动后又退出了")
                    if (st.get("Health") or {}).get("Status") in (None, "healthy"):
                        await asyncio.sleep(3)
                        if (await asyncio.to_thread(self._nas_api, "GET", f"/containers/{n}/json"))["State"].get("Running"):
                            break
                    await asyncio.sleep(2)
                else:
                    raise RuntimeError(f"{n} 一直没有变成健康状态")
            for port in ports_of_all(specs):
                for _ in range(60):
                    try:
                        r, w = await asyncio.wait_for(asyncio.open_connection(mc.addr("nas"), port), 2)
                        w.close()
                        break
                    except Exception:
                        await asyncio.sleep(2)
                else:
                    raise RuntimeError(f"NAS 的 {port} 端口没有响应")
            mig._set(job, 6, "ok")

            step = 7; mig._set(job, 7, "run")
            pi_fwd = []
            for n in members:                                   # still there (stopped) and holding the ports: remove first, then forward
                await mig._pi("DELETE", f"/containers/{n}?force=true", raw=True)
            for port in (ports_of_all(specs) if mig.store.get_setting("leave_forward", False) else []):     # off by default: apps know both addresses
                fname = f"{group}-forward-{port}" if len(ports_of_all(specs)) > 1 else f"{group}-forward"
                await self._pi_forwarder(fname, port, mc.addr("nas"))
                pi_fwd.append(fname)
            mig._set(job, 7, "ok")

            step = 8; mig._set(job, 8, "run")
            if net:
                try:
                    await mig._pi("DELETE", f"/networks/{net}", raw=True)
                except RuntimeError:
                    pass
            mig._set(job, 8, "ok")
            moved = mig.store.get_setting("migrations", [])
            moved.append({"name": group, "members": members, "from": "pi", "to": "nas", "ts": time.time(), "ports": ports_of_all(specs)})
            mig.store.set_setting("migrations", moved)
            # the service lives on the NAS again: the Pi must no longer push its (now older) data over it
            gone = {mt["src"] for mt in plan["mounts"]}
            mig.store.set_setting("replicas", [r for r in mig.store.get_setting("replicas", []) if r["pi"].rstrip("/") not in gone])
            mig.jobs[job].update(done=True, ok=True, ended=time.time())
            await mig.m.refresh("nas"); await mig.m.refresh("pi")
        except Exception as ex:
            log.warning("takeover %s failed at step %d: %s", name, step, ex)
            mig._set(job, step, "fail")
            for u in reversed(undo):
                try:
                    await u()
                except Exception:
                    pass
            for n in stopped:
                try:
                    await mig._pi("POST", f"/containers/{n}/start", raw=True)
                except Exception:
                    pass
            mig.jobs[job].update(done=True, ok=False, ended=time.time(), error=f"{STEPS[step]}：{str(ex)[:150]}。已恢复原样（香橙派上的容器继续运行）")
            await mig.m.refresh("nas"); await mig.m.refresh("pi")

    async def _restore_forwarders(self, nas, names: list[str], ports: list[int]):
        for f in names:
            m = re.search(r"-(\d+)$", f)
            port = int(m.group(1)) if m else (ports[0] if ports else None)
            if port:
                await asyncio.to_thread(nas.forwarder, f, port, mc.addr("pi"))

    async def _pi_forwarder(self, name: str, port: int, target_ip: str):
        """A small nginx on the Pi that passes :port on to the NAS (the Pi-side address of a service that went back)."""
        d = f"{SD_ROOT}/../forwarders/{name}"
        d = os.path.normpath(d)
        os.makedirs(d, exist_ok=True)
        conf = "\n".join(["worker_processes 1;", "events { worker_connections 256; }", "stream {", "    server {",
                           f"        listen {port};", f"        proxy_pass {target_ip}:{port};", "        proxy_connect_timeout 5s;", "        proxy_timeout 1h;", "    }", "}", ""])
        with open(os.path.join(d, "nginx.conf"), "w", encoding="utf-8") as f:
            f.write(conf)
        await self.mig._pi("POST", f"/images/create?fromImage={ADDR}/nginx&tag=alpine", raw=True)
        await self.mig._pi("POST", f"/images/{ADDR}/nginx:alpine/tag", params={"repo": "nginx", "tag": "alpine"}, raw=True)
        try:
            await self.mig._pi("DELETE", f"/containers/{name}?force=true", raw=True)
        except RuntimeError:
            pass
        spec = {"Image": "nginx:alpine", "ExposedPorts": {f"{port}/tcp": {}},
                "HostConfig": {"Binds": [f"{d}/nginx.conf:/etc/nginx/nginx.conf:ro"], "PortBindings": {f"{port}/tcp": [{"HostPort": str(port)}]},
                               "Memory": 32 * 1024 ** 2, "RestartPolicy": {"Name": "unless-stopped"}}}
        await self.mig._pi("POST", f"/containers/create?name={name}", json=spec)
        await self.mig._pi("POST", f"/containers/{name}/start", raw=True)


def ports_of_all(specs: dict[str, dict]) -> list[int]:
    return sorted({p["host"] for s in specs.values() for p in ports_of(s["HostConfig"])})
