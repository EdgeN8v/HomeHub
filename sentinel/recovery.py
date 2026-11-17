"""Disaster recovery on the NAS: when the Orange Pi is gone, start the services that were moved to it (Ledger, Immich, Navidrome,
工账 ...) here on the NAS again, from what the NAS holds:
  * the saved container list of the Pi   (specs/pi.json, written by the ops service every few hours),
  * the nightly copies of their data     (the folders the Pi pushes back to their old place, specs/replicas.json says which is which),
  * the images                           (the NAS's own read-only registry copy on :5000).
It never touches the Pi. Memory is the limit: the NAS has about 3.6 GB shared with the hub and HomeAssistant, so groups are started in
priority order and one that does not fit is left out with the reason, never at the cost of the ones already running.

Stdlib only (the sentinel image has nothing else). `docker` is the sentinel's engine-API helper, `pull` its streaming image pull."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.parse

import machines as mc                                    # noqa: E402  (shipped next to this file)

SPEC_DIR = mc.get("nas", "specs_dir")
NAS_ADDR = mc.registry_addr("nas")
DEFAULT_ORDER = ["ledger_default", "gongzhang_default", "navidrome", "learnsync-server", "plan-server", "immich_net"]   # the heavy one last
OPTIONAL: set = set()
SKIP = {"immich_ml"}                                      # never started here (decided by the owner: face recognition can wait, and it needs 1 GB+)
SYSTEM_MOUNTS = {"/etc/localtime", "/etc/timezone"}
NEVER = {"homehub-hub", "homeassistant", "homehub-sentinel", "homehub-ops", "registry", "ddnsgo_pi"}   # the Pi's own plumbing
RECOVERED_DIR = mc.get("nas", "data_root") + "/recovered"             # working copies of small data: the nightly copies stay clean mirrors
COPY_LIMIT = 2 * 1024 ** 3                                        # bigger folders (the Immich library) are used where they are
REHEARSAL_DIR = mc.get("nas", "data_root") + "/recovery-rehearsal"   # copies of the data for a rehearsal: the real copies stay untouched
RESERVE_MB = 600                                          # kept free for the NAS itself
DEFAULT_MEM_MB = 256


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
        ready = sorted(n for n, d in left.items() if not (d & set(left))) or sorted(left)
        for n in ready:
            order.append(n)
            left.pop(n)
    return order


def target(image: str):
    """(repository path in the registry, tag) for an image name as a container lists it."""
    if not image or image.startswith("sha256:"):
        return None
    name, tag = image, "latest"
    if ":" in image.rsplit("/", 1)[-1]:
        name, tag = image.rsplit(":", 1)
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0]):
        parts = parts[1:]
    return "/".join(parts), tag


def swap_free_mb() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for ln in f:
                if ln.startswith("SwapFree:"):
                    return int(ln.split()[1]) // 1024
    except OSError:
        pass
    return 0


def map_src(src: str, by_src: dict) -> str | None:
    """The NAS copy of a Pi path: the folder itself, or a file / sub-folder inside a replicated folder."""
    src = src.rstrip("/")
    if src in by_src:
        return by_src[src]
    for k, v in by_src.items():
        if src.startswith(k + "/"):
            return v.rstrip("/") + src[len(k):]
    return None


def mem_available_mb() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for ln in f:
                if ln.startswith("MemAvailable:"):
                    return int(ln.split()[1]) // 1024
    except OSError:
        pass
    return 0


class Recovery:
    def __init__(self, docker, pull, log, state_dir: str):
        self.docker, self.pull, self.log, self.state_dir = docker, pull, log, state_dir
        self.stopped_fwd: list[str] = []
        self.ro_big: set = set()
        self.data_log: list[dict] = []

    # ---- what is known
    def _jload(self, name: str, default):
        try:
            with open(os.path.join(SPEC_DIR, name), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def inputs(self) -> dict:
        return {"pi": self._jload("pi.json", {}), "reps": self._jload("replicas.json", []), "migs": self._jload("migrations.json", []),
                "usage": self._jload("usage.json", {})}

    @staticmethod
    def live_groups(migs: list[dict]) -> dict[str, list[str]]:
        """group -> member containers, for the services whose latest move put them on the Pi."""
        latest = {}
        for m in sorted(migs, key=lambda m: m.get("ts", 0)):
            latest[m["name"]] = m
        return {n: m["members"] for n, m in latest.items() if m.get("to") == "pi"}

    def recovered(self) -> dict:
        try:
            with open(os.path.join(self.state_dir, "recovered.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"containers": [], "groups": [], "at": 0}

    def _save_recovered(self, d: dict):
        with open(os.path.join(self.state_dir, "recovered.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)

    # ---- plan
    def plan(self, only: list[str] | None = None, offset: int = 0) -> dict:
        inp = self.inputs()
        specs = {c["Name"].lstrip("/"): c for c in inp["pi"].get("containers", [])}
        groups = self.live_groups(inp["migs"])
        order = [g for g in DEFAULT_ORDER if g in groups] + sorted(g for g in groups if g not in DEFAULT_ORDER)
        if only:
            order = [g for g in order if g in only]
        # RAM that is free (cache included) plus half of the free swap: for an outage, services that sit idle may be paged out
        avail = mem_available_mb() + swap_free_mb() // 2 - RESERVE_MB
        out = []
        for g in order:
            members = [m for m in groups[g] if m not in NEVER and m not in SKIP]
            row = {"name": g, "members": members, "problems": [], "mem_mb": 0, "ports": [], "start": [], "left_out": []}
            by_src = {r["pi"].rstrip("/"): r["nas"] for r in inp["reps"] if r["service"] == g}
            need = {}
            for m in members:
                s = specs.get(m)
                if s is None:
                    row["problems"].append(f"清单里没有 {m} 的配置")
                    continue
                for mt in s.get("Mounts", []):
                    src = mt["Source"].rstrip("/")
                    mapped = map_src(src, by_src)
                    if mt["Destination"] not in SYSTEM_MOUNTS and mapped is None:
                        row["problems"].append(f"{m} 的数据文件夹 {src} 在 NAS 上没有副本")
                    elif mapped is not None and not os.path.exists(mapped):
                        row["problems"].append(f"NAS 上找不到 {mapped}（还没传过来，或者哨兵看不到这个路径）")
                used = (inp["usage"].get(m) or 0) // 1048576                     # what it really used on the Pi, plus a margin
                limit = (s["HostConfig"].get("Memory") or 0) // 1048576
                need[m] = int(used * 1.5) + 32 if used else min(limit or DEFAULT_MEM_MB, 512)
                row["ports"] += [p["host"] + offset for p in ports_of(s["HostConfig"])]
            core = [m for m in need if m not in OPTIONAL]
            core_mb = sum(need[m] for m in core)
            if row["problems"]:
                row["start"] = []
            elif core_mb > avail:
                row["left_out"] = list(need)
                row["problems"].append(f"内存不够：要约 {core_mb} MB，现在还能用约 {max(avail, 0)} MB")
            else:
                row["start"] = core
                avail -= core_mb
                for m in need:
                    if m in OPTIONAL:
                        if need[m] <= avail:
                            row["start"].append(m)
                            avail -= need[m]
                        else:
                            row["left_out"].append(m)
            row["mem_mb"] = sum(need[m] for m in row["start"])
            row["ok"] = bool(row["start"]) and not row["problems"]
            out.append(row)
        return {"groups": out, "mem_free_mb": mem_available_mb(), "have_list": bool(specs), "at_list": inp["pi"].get("at", 0)}

    # ---- run
    def _api(self, method: str, path: str, body=None, timeout: int = 120):
        st, data = self.docker(method, path, body, timeout)
        try:
            js = json.loads(data) if data else None
        except ValueError:
            js = data.decode(errors="replace")
        return st, js

    def _ensure_image(self, image: str, note):
        if self._api("GET", f"/images/{image}/json")[0] == 200:
            return
        t = target(image)
        if t is None:
            raise RuntimeError(f"镜像 {image} 的名字认不出来")
        repo, tag = t
        orig, otag = (image.rpartition(":")[0], image.rpartition(":")[2]) if ":" in image.rsplit("/", 1)[-1] else (image, "latest")
        note(f"取镜像 {repo}:{tag}")
        self.pull(f"{NAS_ADDR}/{repo}:{tag}")
        st, js = self._api("POST", f"/images/{NAS_ADDR}/{repo}:{tag}/tag?repo={orig}&tag={otag}")
        if st >= 300:
            raise RuntimeError(f"给镜像 {repo} 打标签失败：{js}")

    def start_group(self, row: dict, specs: dict, by_src: dict, offset: int, note) -> list[str]:
        g, members = row["name"], row["start"]
        info = {m: specs[m] for m in members}
        net = None if g in members else g
        for m in members:
            self._ensure_image(info[m]["Config"]["Image"], note)
        if offset == 0:                                           # the NAS's old forwarders of this service give way to the real thing
            want = {p["host"] for m in members for p in ports_of(info[m]["HostConfig"])}
            for c in self._api("GET", "/containers/json?all=1")[1] or []:
                name = c["Names"][0].lstrip("/")
                if "forward" in name and want & {p.get("PublicPort") for p in c.get("Ports", [])}:        # whatever forwards one of our ports
                    note(f"停掉旧的转发 {name}")
                    self._api("POST", f"/containers/{name}/stop?t=5")
                    self._api("POST", f"/containers/{name}/update", {"RestartPolicy": {"Name": "no"}})
                    self.stopped_fwd.append(name)
        if net:                                                    # the old compose network may still be here; creating another one of the same name makes the name ambiguous
            q = urllib.parse.quote(json.dumps({"name": [net]}))
            have = [n for n in (self._api("GET", f"/networks?filters={q}")[1] or []) if n.get("Name") == net]
            if not have:
                self._api("POST", "/networks/create", {"Name": net, "Driver": "bridge"})
        made = []
        order = start_order({m: {"Config": s["Config"], "NetworkSettings": s["NetworkSettings"]} for m, s in info.items()})
        for m in order:
            s = info[m]
            cfg, hc = s["Config"], s["HostConfig"]
            binds = []
            for mt in s.get("Mounts", []):
                src = mt["Source"].rstrip("/")
                if mt["Destination"] in SYSTEM_MOUNTS:
                    binds.append(f"{mt['Source']}:{mt['Destination']}:ro")
                else:
                    mapped = map_src(src, by_src)
                    binds.append(f"{mapped}:{mt['Destination']}" + ("" if mt.get("RW", True) else ":ro"))
            ports = {f"{p['container']}/tcp": [{"HostPort": str(p["host"] + offset)}] for p in ports_of(hc)}
            mode = net or "bridge"
            aliases = [a for a in (s["NetworkSettings"]["Networks"].get(net, {}).get("Aliases") or []) if not re.fullmatch(r"[0-9a-f]{12}", a)] if net else []
            body = {"Image": cfg["Image"], "Env": cfg.get("Env"), "Cmd": cfg.get("Cmd"), "Entrypoint": cfg.get("Entrypoint"),
                    "WorkingDir": cfg.get("WorkingDir") or "", "User": cfg.get("User") or "", "Healthcheck": cfg.get("Healthcheck"),
                    "ExposedPorts": {k: {} for k in ports},
                    "HostConfig": {"Binds": binds, "PortBindings": ports, "Memory": hc.get("Memory") or 0, "ShmSize": hc.get("ShmSize") or 67108864,
                                   "RestartPolicy": {"Name": "no" if offset else ((hc.get("RestartPolicy") or {}).get("Name") or "unless-stopped")},
                                   "NetworkMode": mode},
                    "NetworkingConfig": {"EndpointsConfig": {mode: {"Aliases": aliases or [m]}}} if net else {}}
            note(f"创建并启动 {m}")
            st0, old = self._api("GET", f"/containers/{m}/json")
            if st0 == 200:                                        # never take away a container that is already there
                if offset or (old or {}).get("State", {}).get("Running"):
                    raise RuntimeError(f"NAS 上已经有一个叫 {m} 的容器")
                self._api("DELETE", f"/containers/{m}?force=true")
            st, js = self._api("POST", f"/containers/create?name={m}", body)
            if st >= 300:
                raise RuntimeError(f"创建 {m} 失败：{js}")
            made.append(m)
            st, js = self._api("POST", f"/containers/{m}/start")
            if st >= 300:
                raise RuntimeError(f"启动 {m} 失败：{js}")
        for m in order:                                           # up, and healthy when it has a health check
            for _ in range(300):                                  # up to 10 minutes: a database starting beside other services on a busy NAS is slow
                st, js = self._api("GET", f"/containers/{m}/json")
                state = (js or {}).get("State", {})
                if not state.get("Running"):
                    raise RuntimeError(f"{m} 启动后又退出了")
                if (state.get("Health") or {}).get("Status") in (None, "healthy"):
                    break
                time.sleep(2)
            else:
                raise RuntimeError(f"{m} 一直没有变成健康状态")
        return made

    def run(self, job: dict, step, only: list[str] | None = None, offset: int = 0) -> dict:
        """Start every group of the plan that fits. `offset` > 0 is a rehearsal: ports shifted, nothing taken over. Returns a summary."""
        plan = self.plan(only, offset)
        inp = self.inputs()
        specs = {c["Name"].lstrip("/"): c for c in inp["pi"].get("containers", [])}
        done, failed = [], []
        for i, row in enumerate(plan["groups"]):
            note = lambda t, i=i: job["steps"][i].update(note=t)
            if not row["ok"]:
                step(job, i, "fail", "；".join(row["problems"]) or "没有可启动的容器")
                failed.append(row["name"])
                continue
            step(job, i, "run", "准备")
            by_src = {r["pi"].rstrip("/"): r["nas"] for r in inp["reps"] if r["service"] == row["name"]}
            try:
                # Small data is copied first and the services run on the copy (a rehearsal always): the nightly copy stays a clean mirror of
                # the Pi, so a service that ran here for a while and then goes back does not leave strange files in it.
                self.ro_big = set()
                self.data_log = []
                for k, (src, nas_dir) in enumerate(list(by_src.items())):
                    if self._size(nas_dir) > COPY_LIMIT:               # too big to copy: used where it is (a rehearsal only reads it)
                        if offset:
                            self.ro_big.add(nas_dir)
                        self.data_log.append({"group": row["name"], "pi": src, "nas": nas_dir, "direct": True})
                        continue
                    dest = os.path.join(REHEARSAL_DIR if offset else RECOVERED_DIR, row["name"], f"m{k}")
                    note(f"复制一份数据：{os.path.basename(nas_dir)}")
                    shutil.rmtree(dest, ignore_errors=True)
                    shutil.copytree(nas_dir, dest, symlinks=True)
                    by_src[src] = dest
                    self.data_log.append({"group": row["name"], "pi": src, "nas": dest, "direct": False})
                made = self.start_group(row, specs, by_src, offset, note)
                step(job, i, "ok", "已启动：" + "、".join(made) + (f"（没启动：{'、'.join(row['left_out'])}）" if row["left_out"] else ""))
                done.append((row["name"], made))
            except Exception as ex:
                step(job, i, "fail", str(ex)[:160])
                failed.append(row["name"])
        if offset == 0 and done:
            self._save_recovered({"groups": [g for g, _ in done], "containers": [m for _, ms in done for m in ms],
                                  "forwarders": list(dict.fromkeys(self.stopped_fwd + self.recovered().get("forwarders", []))), "at": time.time(),
                                  "data": self.recovered().get("data", []) + self.data_log})
        return {"done": [g for g, _ in done], "failed": failed, "made": [m for _, ms in done for m in ms]}

    @staticmethod
    def _size(path: str) -> int:
        total = 0
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
        return total

    def stop_recovered(self) -> dict:
        """Stop (not remove) what the NAS runs in place of the Pi, so its data stands still while it is carried back."""
        rec = self.recovered()
        for n in rec.get("containers", []):
            self._api("POST", f"/containers/{n}/stop?t=30", None, 90)
        return {"stopped": rec.get("containers", [])}

    def failback(self) -> dict:
        """The Pi is back and runs its services again (or they were moved back by hand): take the recovered copies away from the NAS, give the
        NAS's old port forwarders back, forget the list."""
        rec = self.recovered()
        for n in rec.get("containers", []):
            self._api("DELETE", f"/containers/{n}?force=true")
        for name in rec.get("forwarders", []):
            self._api("POST", f"/containers/{name}/update", {"RestartPolicy": {"Name": "unless-stopped"}})
            self._api("POST", f"/containers/{name}/start")
        shutil.rmtree(RECOVERED_DIR, ignore_errors=True)
        self._save_recovered({"groups": [], "containers": [], "forwarders": [], "data": [], "at": 0})
        return {"removed": rec.get("containers", []), "forwarders": rec.get("forwarders", [])}

    def cleanup(self, names: list[str], nets: list[str]):
        """Remove what a rehearsal (offset > 0) created."""
        for n in names:
            self._api("DELETE", f"/containers/{n}?force=true")
        for net in nets:
            self._api("DELETE", f"/networks/{net}")
        shutil.rmtree(REHEARSAL_DIR, ignore_errors=True)
