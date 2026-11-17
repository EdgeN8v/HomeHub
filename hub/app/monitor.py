"""Resource view of the two machines for the phone page: the Orange Pi (this host: /proc plus the docker socket)
and the NAS (over SSH, since it is a different box). Polled in the background, so a page load never waits on SSH."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shlex
import time

import aiohttp
import paramiko

from . import machines as mc

log = logging.getLogger("monitor")
INTERVAL = 5
DOCKER_SOCK = "/var/run/docker.sock"
ROLE = os.environ.get("HUB_ROLE", "pi")                # "nas": this hub is the stand-in running on the NAS
UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}


def to_bytes(s: str) -> int:
    m = re.match(r"([\d.]+)\s*([A-Za-z]+)", s.strip())
    return int(float(m.group(1)) * UNITS.get(m.group(2).lower(), 1)) if m else 0


def parse_host(cores_s: str, stat: str, mem_s: str, load_s: str, df_s: str, prev_cpu):
    """Shared by both machines; the inputs are the plain text of nproc, the first /proc/stat line,
    /proc/meminfo, /proc/loadavg and one `df -B1` row. Returns (fields, cpu sample for the next call)."""
    vals = list(map(int, stat.split()[1:8]))
    total, idle = sum(vals), vals[3] + vals[4]
    cpu = round(100 * (1 - (idle - prev_cpu[1]) / (total - prev_cpu[0])), 1) if prev_cpu and total > prev_cpu[0] else None
    mem = {k: int(v) * 1024 for k, v in re.findall(r"(\w+):\s+(\d+)", mem_s)}
    d = df_s.split()
    return {"cores": int(cores_s), "cpu": cpu, "load": [float(x) for x in load_s.split()[:3]],
            "mem_total": mem["MemTotal"], "mem_avail": mem["MemAvailable"],
            "swap_total": mem["SwapTotal"], "swap_used": mem["SwapTotal"] - mem["SwapFree"],
            "disk_total": int(d[1]), "disk_used": int(d[2])}, (total, idle)


def parse_docker_stats(out: str) -> list[dict]:
    rows = []
    for ln in out.splitlines():
        p = ln.split("|")
        if len(p) == 4:
            used, _, limit = p[2].partition("/")
            rows.append({"name": p[0], "cpu": float(p[1].rstrip("%") or 0), "mem": to_bytes(used), "mem_limit": to_bytes(limit)})
    return sorted(rows, key=lambda r: -r["mem"])


# container name -> (service it belongs to, what it does); anything not listed becomes its own service
SERVICES = {
    "registry": ("镜像仓库", "镜像仓库（香橙派上的主仓库）"), "registry-standby": ("镜像仓库", "镜像仓库的备用副本（NAS 上，只读，香橙派坏了时用）"),
    "immich_server": ("Immich 相册", "相册主程序"), "immich_postgres": ("Immich 相册", "相册数据库"),
    "immich_redis": ("Immich 相册", "相册缓存"), "immich_ml": ("Immich 相册", "人脸识别、智能搜索（AI）"),
    "ledger-api-1": ("Ledger 记账", "记账后端"), "ledger-db-1": ("Ledger 记账", "记账数据库"), "ledger-backup": ("Ledger 记账", "定时备份"),
    "gongzhang": ("工账", "工账后端"),
    "navidrome": ("音乐", "音乐服务器"), "music-update": ("音乐", "音乐 App 更新包下载"), "directory": ("家里服务器", "地址表：别的对话和 App 读它，就知道每个服务在哪、怎么连"),
    "learnsync-server": ("LearnSync", "学习同步服务"),
    "homeassistant": ("HomeHub 智能家居", "Home Assistant，管设备"), "homehub-hub": ("HomeHub 智能家居", "HomeHub 后台，手机 App 连的就是它"),
    "homehub-ops": ("HomeHub 智能家居", "运维服务：迁移、镜像仓库同步、数据回传（跟着主站）"), "homehub-sentinel": ("HomeHub 智能家居", "哨兵：决定哪台机器当主站，宕机时接管"),
    "ddnsgo_pi": ("系统", "动态域名，让香橙派的外网域名指到它"),
    "claude-remote": ("Claude 远程", "手机经电脑出网的代理入口"),
    "plan-server": ("计划", "计划服务"),
    "ddnsgo_nas": ("系统", "动态域名，让 NAS 的外网域名指到家里"), "nas_rsyncd": ("系统", "NAS 文件同步"),
}


# what may not be stopped from the phone, because the phone's own way in runs through it (the hub itself, and the
# NAS containers that forward the public ports to it)
PROTECTED = {"homehub-hub": {"stop", "restart"}}


def label(row: dict, host: str) -> dict:
    service, desc = SERVICES.get(row["name"], (row["name"], ""))
    return {"running": True, **row, "service": service, "desc": desc, "host": host, "locked": sorted(PROTECTED.get(row["name"], ()))}


def _lrow(c: dict) -> dict:
    return {"name": c["Names"][0].lstrip("/"), "running": c.get("State") == "running", "labels": c.get("Labels") or {},
            "ports": sorted({p["PublicPort"] for p in c.get("Ports", []) if p.get("PublicPort")})}


def stopped_row(name: str) -> dict:
    return {"name": name, "cpu": 0.0, "mem": 0, "mem_limit": 0, "running": False}


DISKS = [tuple(d) for d in mc.get("pi", "disks")]       # what the Pi has; the SD card is mounted into the hub container for this


def local_disks(disks=None) -> list[dict]:
    out = []
    for label_, path in (disks or DISKS):
        try:
            if path != "/" and not os.path.ismount(path):
                continue
            st = os.statvfs(path)
            out.append({"label": label_, "path": path, "total": st.f_blocks * st.f_frsize, "used": (st.f_blocks - st.f_bfree) * st.f_frsize})
        except OSError:
            pass
    return out


def stats_row(name: str, s: dict) -> dict:
    cpu_d = s["cpu_stats"]["cpu_usage"]["total_usage"] - s["precpu_stats"]["cpu_usage"]["total_usage"]
    sys_d = s["cpu_stats"].get("system_cpu_usage", 0) - s["precpu_stats"].get("system_cpu_usage", 0)
    ncpu = s["cpu_stats"].get("online_cpus") or os.cpu_count()
    m = s["memory_stats"]
    cache = m.get("stats", {}).get("inactive_file", m.get("stats", {}).get("cache", 0))   # docker stats' "usage" leaves out page cache
    return {"name": name, "cpu": round(100 * cpu_d / sys_d * ncpu, 2) if sys_d > 0 and cpu_d >= 0 else 0.0,
            "mem": max(m.get("usage", 0) - cache, 0), "mem_limit": m.get("limit", 0)}


class LocalHost:
    """The machine this container shares /proc with (network_mode host); docker through its socket."""
    disk = "/data"

    def __init__(self, name=mc.get("pi", "name"), address=mc.addr("pi"), disks=None):
        self.name, self.address, self.disks = name, address, disks
        self.prev_cpu = None

    def _read(self, path: str) -> str:
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _host_text(self):
        st = os.statvfs(self.disk)
        df = f"x {st.f_blocks * st.f_frsize} {(st.f_blocks - st.f_bfree) * st.f_frsize}"
        mem = "".join(ln for ln in self._read("/proc/meminfo").splitlines(True) if ln.split(":")[0] in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"))
        return str(os.cpu_count()), self._read("/proc/stat").splitlines()[0], mem, self._read("/proc/loadavg"), df

    async def _container(self, http: aiohttp.ClientSession, c: dict) -> dict:
        async with http.get(f"http://docker/containers/{c['Id']}/stats", params={"stream": "false"}) as r:
            s = await r.json()
        return stats_row(c["Names"][0].lstrip("/"), s)

    async def control(self, name: str, action: str):
        url = f"http://docker/containers/{name}/{action}"
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=DOCKER_SOCK), timeout=aiohttp.ClientTimeout(total=60)) as http:
            async with http.post(url, params={"t": "10"} if action != "start" else None) as r:
                if r.status not in (204, 304):
                    raise RuntimeError((await r.text())[:200])

    async def inspected(self) -> list[dict]:
        """Every container in full (folders, environment): what the directory page for other apps and conversations is made of."""
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=DOCKER_SOCK), timeout=aiohttp.ClientTimeout(total=30)) as http:
            async with http.get("http://docker/containers/json", params={"all": "1"}) as r:
                ids = [c["Id"] for c in await r.json()]
            out = []
            for i in ids:
                async with http.get(f"http://docker/containers/{i}/json") as r:
                    out.append(await r.json())
        return out

    async def labelled(self) -> list[dict]:
        """Every container with its labels and published ports (the address table is built from the labelled ones)."""
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=DOCKER_SOCK), timeout=aiohttp.ClientTimeout(total=20)) as http:
            async with http.get("http://docker/containers/json", params={"all": "1"}) as r:
                return [_lrow(c) for c in await r.json()]

    async def poll(self) -> dict:
        fields, self.prev_cpu = parse_host(*self._host_text(), self.prev_cpu)
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=DOCKER_SOCK), timeout=aiohttp.ClientTimeout(total=20)) as http:
            async with http.get("http://docker/containers/json", params={"all": "1"}) as r:
                listing = await r.json()
            rows = list(await asyncio.gather(*(self._container(http, c) for c in listing if c["State"] == "running")))
            rows += [stopped_row(c["Names"][0].lstrip("/")) for c in listing if c["State"] != "running"]
        return {**fields, "disk_path": "/", "disks": local_disks(self.disks), "containers": [label(r, self.name) for r in sorted(rows, key=lambda r: -r["mem"])], "docker_error": None}


class RemoteHost:
    """The other machine through its sentinel (host numbers + a narrow docker proxy): used by the stand-in hub on the NAS to still
    show and manage the Orange Pi, which has no login for the NAS. The sentinel refuses to start or stop the hub / HomeAssistant."""
    disk = "/"

    def __init__(self, name: str, address: str, url: str, token: str, cafile: str):
        self.name, self.address, self.url, self.token = name, address, url, token
        self.prev_cpu = None
        import ssl
        try:
            self.ssl = ssl.create_default_context(cafile=cafile)
            self.ssl.check_hostname = False
        except Exception:                                  # no certificate to trust yet: still token-protected on the LAN
            self.ssl = ssl.create_default_context()
            self.ssl.check_hostname, self.ssl.verify_mode = False, ssl.CERT_NONE

    async def _call(self, method: str, path: str, timeout: int = 20):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
            async with http.request(method, self.url + path, headers={"Authorization": "Bearer " + self.token}, ssl=self.ssl) as r:
                if r.status >= 300:
                    raise RuntimeError((await r.text())[:160])
                return await r.json() if r.content_type == "application/json" else await r.text()

    async def control(self, name: str, action: str):
        await self._call("POST", f"/docker/containers/{name}/{action}" + ("?t=10" if action != "start" else ""), 70)

    async def _one(self, c: dict) -> dict:
        s = await self._call("GET", f"/docker/containers/{c['Id']}/stats?stream=false")
        return stats_row(c["Names"][0].lstrip("/"), s)

    async def inspected(self) -> list[dict]:
        out = []
        for c in await self._call("GET", "/docker/containers/json?all=1"):
            out.append(await self._call("GET", f"/docker/containers/{c['Id']}/json"))
        return out

    async def labelled(self) -> list[dict]:
        return [_lrow(c) for c in await self._call("GET", "/docker/containers/json?all=1")]

    async def poll(self) -> dict:
        h = await self._call("GET", "/hoststats")
        fields, self.prev_cpu = parse_host(h["cores"], h["stat"], h["mem"], h["load"], h["df"], self.prev_cpu)
        listing = await self._call("GET", "/docker/containers/json?all=1")
        gate = asyncio.Semaphore(4)                            # a few at a time: the sentinel on the other side is small

        async def one(c):
            async with gate:
                return await self._one(c)
        rows = list(await asyncio.gather(*(one(c) for c in listing if c["State"] == "running")))
        rows += [stopped_row(c["Names"][0].lstrip("/")) for c in listing if c["State"] != "running"]
        return {**fields, "disk_path": "/", "disks": h["disks"], "containers": [label(r, self.name) for r in sorted(rows, key=lambda r: -r["mem"])], "docker_error": None}


class SshHost:
    """The NAS: one SSH session per poll cycle (reconnects by itself), docker through sudo when the account is not in the docker group."""
    name = "NAS"

    def __init__(self, host: str, port: int, user: str, password: str, disk: str, key: str | None = None, name: str = "NAS",
                 disks: list | None = None):
        self.address, self.port, self.user, self.password, self.disk = host, port, user, password, disk
        self.disks = disks or [["硬盘 1", "/data_s001"], ["硬盘 2", "/data_s002"]]      # (label, path) of the disks to report
        self.key = key if key and os.path.exists(key) else None       # a key login when there is one; `password` is then only for sudo
        self.name = name
        self.client, self.prev_cpu, self.use_sudo = None, None, False
        self._work = None

    def _new_client(self):
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        if self.key:
            c.connect(self.address, port=self.port, username=self.user, key_filename=self.key, timeout=8, banner_timeout=8,
                      look_for_keys=False, allow_agent=False)
        else:
            c.connect(self.address, port=self.port, username=self.user, password=self.password or None, timeout=8, banner_timeout=8,
                      look_for_keys=not self.password, allow_agent=False)
        return c

    def scrub(self, text: str) -> str:
        """Hide the sudo password in text that is about to be shown (an empty password must not be "replaced": that would put *** between every letter)."""
        return text.replace(self.password, "***") if self.password else text

    def rootsh(self, script: str) -> str:
        """A command line that runs `script` (a sh script) as root on this machine and passes stdin / stdout through.
        With a sudo password: sudo. Without: docker (the login is in the docker group, which is root in all but name) - the host's
        root folder is mounted into a throw-away container and the script runs chrooted in it, so it works on any machine that has docker."""
        if self.password:
            inner = f"echo {shlex.quote(self.password)} | sudo -S -p '' -v && sudo -n sh -c {shlex.quote(script)}"
            return f"sh -c {shlex.quote(inner)}"
        return f"docker run --rm -i --net=host -v /:/host alpine chroot /host sh -c {shlex.quote(script)}"

    def work(self):
        """A second SSH connection for long jobs (copying data, pushing images). The 5-second poll of the page resets `self.client`
        whenever one poll fails, which used to cut a copy in the middle ("Socket is closed"); this one only the job uses."""
        tr = self._work.get_transport() if self._work else None
        if not tr or not tr.is_active():
            c = self._new_client()
            c.get_transport().set_keepalive(20)
            self._work = c
        return self._work

    def _connect(self):
        self.client = self._new_client()

    def _run(self, cmd: str, sudo: bool = False) -> tuple[str, str]:
        if sudo:
            cmd = self.rootsh(cmd)
        _, o, e = self.client.exec_command(cmd, timeout=20)
        return o.read().decode(errors="replace"), e.read().decode(errors="replace")

    def _docker(self):
        fmt = "docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}'"
        out, err = self._run(fmt, sudo=self.use_sudo)
        if not out.strip() and "ermission denied" in err and self.password:
            self.use_sudo = True
            out, err = self._run(fmt, sudo=True)
        if not out.strip() and err.strip():
            return [], err.strip().splitlines()[-1].replace(self.password, "***")
        rows = parse_docker_stats(out)
        ps, _ = self._run("docker ps -a --format '{{.Names}}|{{.State}}'", sudo=self.use_sudo)
        known = {r["name"] for r in rows}
        for ln in ps.splitlines():
            n, _, state = ln.partition("|")
            if n and n not in known and state != "running":
                rows.append(stopped_row(n))
        return rows, None

    async def inspected(self) -> list[dict]:
        def go():
            self._ensure()
            out, _ = self._run("docker ps -aq", sudo=self.use_sudo)
            ids = out.split()
            if not ids:
                return []
            txt, _ = self._run("docker inspect " + " ".join(ids), sudo=self.use_sudo)
            return json.loads(txt)
        return await asyncio.to_thread(go)

    async def labelled(self) -> list[dict]:
        def go():
            self._ensure()
            out, _ = self._run("docker ps -a --format '{{.Names}}	{{.State}}	{{.Labels}}	{{.Ports}}'", sudo=self.use_sudo)
            rows = []
            for ln in out.splitlines():
                p = ln.split("	")
                if len(p) < 4:
                    continue
                labels = dict(kv.split("=", 1) for kv in p[2].split(",") if "=" in kv)
                rows.append({"name": p[0], "running": p[1] == "running", "labels": labels,
                             "ports": sorted({int(x) for x in re.findall(r":(\d+)->", p[3])})})
            return rows
        return await asyncio.to_thread(go)

    def _control(self, name: str, action: str):
        out, err = self._run(f"docker {action} {shlex.quote(name)}", sudo=self.use_sudo)
        if not out.strip() and err.strip():
            raise RuntimeError(err.strip().splitlines()[-1].replace(self.password, "***"))

    async def control(self, name: str, action: str):
        await asyncio.to_thread(self._control, name, action)

    # ---- used by migrate.py (all blocking: call through asyncio.to_thread)
    def _ensure(self):
        tr = self.client.get_transport() if self.client else None
        if not tr or not tr.is_active():
            self._connect()

    def inspect(self, name: str) -> dict:
        self._ensure()
        out, err = self._run(f"docker inspect {shlex.quote(name)}", sudo=True)
        return json.loads(out)[0]

    def du(self, path: str) -> int:
        self._ensure()
        out, _ = self._run(f"du -sb {shlex.quote(path)}", sudo=True)
        return int(out.split()[0]) if out.strip() else 0

    def image_size(self, image: str) -> int:
        self._ensure()
        out, _ = self._run("docker image inspect --format '{{.Size}}' " + shlex.quote(image), sudo=True)
        return int(out.strip() or 0)

    def network_members(self, net: str) -> list[str]:
        self._ensure()
        out, _ = self._run("docker network inspect --format '{{range .Containers}}{{.Name}} {{end}}' " + shlex.quote(net), sudo=True)
        return out.split()

    def stream(self, cmd: str, sudo: bool = False) -> bytes:
        """Binary output of a shell pipeline (docker save, tar); as root when asked."""
        self._ensure()
        if sudo:
            cmd = self.rootsh(cmd)
        _, o, _ = self.work().exec_command(cmd, timeout=900)
        return o.read()

    def forwarder(self, name: str, port: int, target: str):
        """A small nginx on this machine that passes TCP :port on to target:port, so addresses that point here keep working."""
        conf = "\n".join(["worker_processes 1;", "events { worker_connections 256; }", "stream {", "    server {",
                           f"        listen {port};", f"        listen [::]:{port};", f"        proxy_pass {target}:{port};",
                           "        proxy_connect_timeout 5s;", "        proxy_timeout 1h;", "    }", "}", ""])
        d = f"{mc.get('nas', 'data_root')}/{name}"
        b64 = base64.b64encode(conf.encode()).decode()
        self._ensure()
        self._run(f"sh -c 'rm -rf {d}/nginx.conf; mkdir -p {d} && echo {b64} | base64 -d > {d}/nginx.conf'", sudo=True)
        self._run(f"docker rm -f {shlex.quote(name)}", sudo=True)
        out, err = self._run(f"docker run -d --name {shlex.quote(name)} --restart unless-stopped --memory 32m -p {port}:{port} "
                             f"-v {d}/nginx.conf:/etc/nginx/nginx.conf:ro nginx:alpine", sudo=True)
        if err.strip() and not out.strip():
            raise RuntimeError(err.strip().splitlines()[-1].replace(self.password, "***"))

    def images_in_use(self) -> list[str]:
        self._ensure()
        out, _ = self._run("docker ps -a --format '{{.Image}}'", sudo=True)
        return sorted(set(out.split()))

    def push_image(self, image: str, dest: str):
        """docker tag + push (as root); long-running, so it has its own generous timeout."""
        self._ensure()
        inner = f"docker tag {shlex.quote(image)} {shlex.quote(dest)} && docker push {shlex.quote(dest)}; r=$?; docker rmi {shlex.quote(dest)} >/dev/null 2>&1; exit $r"
        cmd = self.rootsh(inner)
        _, o, e = self.work().exec_command(cmd, timeout=3600)
        text = (o.read().decode(errors="replace") + e.read().decode(errors="replace")).replace(self.password, "***")
        if o.channel.recv_exit_status():
            raise RuntimeError(text.strip().splitlines()[-1] if text.strip() else "push failed")

    def remove(self, name: str):
        self._ensure()
        self._run(f"docker rm -f {shlex.quote(name)}", sudo=True)

    def _poll(self) -> dict:
        tr = self.client.get_transport() if self.client else None
        if not tr or not tr.is_active():
            self._connect()
        out, _ = self._run(f"nproc; echo ==; head -1 /proc/stat; echo ==; grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo; echo ==; "
                           f"cat /proc/loadavg; echo ==; df -B1 {self.disk} | tail -1; echo ==; df -B1 {' '.join(shlex.quote(d[1]) for d in self.disks)} 2>/dev/null | tail -n +2")
        parts = [s.strip() for s in out.split("==")]
        fields, self.prev_cpu = parse_host(*parts[:5], self.prev_cpu)
        disks = []
        for i, ln in enumerate(parts[5].splitlines() if len(parts) > 5 else []):
            d = ln.split()
            if len(d) >= 3 and d[1].isdigit():
                disks.append({"label": self.disks[i][0] if i < len(self.disks) else f"硬盘 {i + 1}", "path": d[-1], "total": int(d[1]), "used": int(d[2])})
        containers, derr = self._docker()
        return {**fields, "disks": disks, "disk_path": self.disk, "containers": [label(r, self.name) for r in containers], "docker_error": derr}

    async def poll(self) -> dict:
        try:
            return await asyncio.to_thread(self._poll)
        except Exception:
            self.client = None
            raise


class Monitor:
    def __init__(self):
        self.hosts = {"pi": LocalHost()}
        user = os.environ.get("NAS_USER", "")
        if ROLE == "nas":      # the hub is running on the NAS (the Pi is down): this machine is the local one, the Pi is not managed from here
            self.hosts = {"nas": LocalHost(mc.get("nas", "name"), mc.addr("nas"), [tuple(d) for d in mc.get("nas", "disks")])}
            peer = os.environ.get("PEER_SENTINEL_URL")
            if peer and os.environ.get("SENTINEL_TOKEN"):   # the Pi is still running its other services: keep showing and managing them
                self.hosts = {"pi": RemoteHost(mc.get("pi", "name"), mc.addr("pi"), peer, os.environ["SENTINEL_TOKEN"], "/data/tls/server.crt"), **self.hosts}
        elif mc.get("nas", "ssh_user"):
            # The other machine: reached over SSH with the shared key. A sudo password exists only for the NAS (the Pi's login is in the docker
            # group, which is enough: see SshHost.rootsh).
            other = mc.CFG["machines"]["nas"]
            self.hosts["nas"] = SshHost(os.environ.get("NAS_HOST", other["address"]), int(os.environ.get("NAS_PORT", other["ssh_port"])), other["ssh_user"],
                                        "" if mc.SWAPPED else os.environ.get("NAS_PASSWORD", ""), os.environ.get("NAS_DISK", other["disks"][0][1]),
                                        key=os.environ.get("SSH_KEY", "/keys/homehub_ed25519"), name=other["name"], disks=other["disks"])
        self.state = {k: {"id": k, "name": h.name, "address": h.address, "ok": False, "error": "等待首次采集", "ts": 0} for k, h in self.hosts.items()}

    async def _loop(self, key: str):
        host = self.hosts[key]
        while True:
            try:
                self.state[key].update(await host.poll(), ok=True, error=None, ts=time.time())
            except Exception as ex:
                log.warning("%s: %s", key, type(ex).__name__)
                self.state[key].update(ok=False, error=f"{type(ex).__name__}"[:120])
            await asyncio.sleep(INTERVAL)

    def start(self):
        self.tasks = [asyncio.create_task(self._loop(k)) for k in self.hosts]

    def stop(self):
        for t in self.tasks:
            t.cancel()

    def _check(self, key: str, name: str, action: str):
        host = self.hosts.get(key)
        row = next((c for c in self.state.get(key, {}).get("containers", []) if c["name"] == name), None)
        if host is None or row is None:
            raise ValueError("没有这个容器")
        if action in PROTECTED.get(name, ()):
            raise ValueError(f"{name} 不能从手机上{'停止' if action == 'stop' else '重启'}：手机连家里靠的就是它")
        return host, row

    async def control(self, key: str, name: str, action: str) -> list[dict]:
        if action not in ("start", "stop", "restart"):
            raise ValueError("不支持的操作")
        host, _ = self._check(key, name, action)
        log.info("%s: %s %s", key, action, name)
        await host.control(name, action)
        await self.refresh(key)
        return self.snapshot()

    async def control_all(self, key: str, action: str) -> dict:
        """One tap for a whole machine: start everything that is stopped, or stop everything that runs except
        what the phone's own connection depends on. Returns how many it touched."""
        if action not in ("start_all", "stop_all"):
            raise ValueError("不支持的操作")
        if key not in self.hosts:
            raise ValueError("没有这台机器")
        one, want_running = ("start", False) if action == "start_all" else ("stop", True)
        names = [c["name"] for c in self.state[key].get("containers", []) if c["running"] == want_running and one not in PROTECTED.get(c["name"], ())]
        failed = []
        for n in names:
            try:
                await self.hosts[key].control(n, one)
            except Exception as ex:
                failed.append(f"{n}: {ex}"[:120])
        await self.refresh(key)
        return {"count": len(names) - len(failed), "failed": failed, "hosts": self.snapshot()}

    async def refresh(self, key: str):
        try:
            self.state[key].update(await self.hosts[key].poll(), ok=True, error=None, ts=time.time())
        except Exception as ex:
            log.warning("%s: %s", key, type(ex).__name__)

    def snapshot(self) -> list[dict]:
        return list(self.state.values())
