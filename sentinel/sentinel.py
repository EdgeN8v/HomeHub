#!/usr/bin/env python3
"""Sentinel: decides which of the two machines (Orange Pi / NAS) runs the "main pair" - the HomeHub hub and HomeAssistant - and
moves it when asked or when the machine running it dies.

One sentinel runs on each machine and controls only its own docker. It is a separate container so that it can stop and start
the hub (a hub cannot move itself). The two sentinels talk to each other over HTTPS (port 8210, bearer token, the hub's own
certificate is the only one trusted), so no machine needs a login on the other.

Rules that keep it from running the pair twice (the pair gives commands to devices, two of them would double every command):
  * the machine that is not the main one keeps its hub/HA containers stopped and with restart policy "no" (a reboot does not
    start them);
  * every change of main machine raises a "term" number; the machine that sees a higher term than its own, held by a machine
    whose hub answers, stops its own pair (fencing), so after a network split the newer main wins;
  * a take-over without being asked (auto) only happens when everything of the main machine is silent for DEAD_AFTER seconds:
    its sentinel, its hub and its ssh port.
The main machine copies its data (hub database, certificate, app updates, HomeAssistant configuration) to the other every few
minutes, so the one that takes over starts from data that is at most a few minutes old."""
from __future__ import annotations

import hmac
import http.client
import http.server
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import ssl
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

ME = os.environ["ME"]                                   # "pi" or "nas"
OTHER = "nas" if ME == "pi" else "pi"
PEER_URL = os.environ["PEER_URL"]                       # https://<other machine>:8210
PEER_HUB = os.environ["PEER_HUB_URL"]                   # where the other machine's hub answers when it is the main one
PEER_TCP = (os.environ["PEER_TCP_HOST"], int(os.environ["PEER_TCP_PORT"]))
SELF_HUB = os.environ["SELF_HUB_URL"]
TOKEN = os.environ["SENTINEL_TOKEN"]
READ_TOKEN = os.environ.get("READ_TOKEN", "")            # the hub token the phone has: may read status and job progress, nothing else
STATE_DIR = os.environ.get("STATE_DIR", "/state")
TLS_DIR = os.environ.get("TLS_DIR", "/tls")
SB = os.environ["SB_DIR"]                               # where the main machine's copy arrives (standby)
ACT_DATA, ACT_HA = os.environ["ACT_DATA"], os.environ["ACT_HA"]   # where this machine's hub / HA read their data
import machines as mc                                              # noqa: E402  (shipped next to this file)
ACT_OPS = os.environ.get("ACT_OPS", mc.get(ME, "ops_data"))          # and the ops service its own
PEER_URL, PEER_HUB = mc.sentinel_url(OTHER), mc.hub_url(OTHER)       # the machine table is the authority for where the other machine is
PEER_TCP = (mc.addr(OTHER), mc.get(OTHER, "ssh_port"))
HUB_ENV = os.environ["HUB_ENV"]
PUSH_EVERY, CHECK_EVERY = 300, 15
DEAD_AFTER = int(os.environ.get("DEAD_AFTER", "120"))   # seconds of total silence before an automatic take-over
UNHEALTHY_AFTER = 300                                   # the main machine answers but its hub does not, for this long
EXCLUDE_HA = ("home-assistant_v2.db", "home-assistant_v2.db-wal", "home-assistant_v2.db-shm", ".ha_run.lock")
EXCLUDE_HUB = ("hub.db", "hub.db-wal", "hub.db-shm", "hub.db.snap")
HUB, HA, OPS = "homehub-hub", "homeassistant", "homehub-ops"
REG_RW, REG_RO = "registry", "registry-standby"          # a machine's registry runs as the first (writable) or the second (read-only copy), never both
EXCLUDE_OPS = ("ops.db", "ops.db-wal", "ops.db-shm", "ops.db.snap")
NATIVE = mc.get(ME, "native") or {}                      # this machine's own small services, mirrored to the other machine
NATIVE_EVERY, NATIVE_BATCH = 900, 12 * 1024 ** 2
# a machine that has no containers yet (a freshly joined one) takes the images from the other machine's registry
HA_IMAGE_TAG = os.environ.get("HA_IMAGE") or f"{mc.registry_addr(OTHER)}/homeassistant:2026.10.2"
HUB_IMAGE_TAG = os.environ.get("HUB_IMAGE") or f"{mc.registry_addr(OTHER)}/homehub-hub:latest"

lock = threading.RLock()
jobs: dict[str, dict] = {}
peer: dict = {"ok": False, "at": 0}
watch = {"silent_since": None, "unhealthy_since": None}
sync = {"pushed_at": 0, "pushed_ok": None, "pushed_error": None, "received_at": 0, "manifest": {}}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- state (term + who is main)
def load_state() -> dict:
    try:
        with open(os.path.join(STATE_DIR, "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"term": 1, "primary": "pi", "auto": False, "at": 0}


def save_state(st: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = os.path.join(STATE_DIR, "state.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, os.path.join(STATE_DIR, "state.json"))


# ---------------------------------------------------------------- docker (engine API over the socket)
class _Unix(http.client.HTTPConnection):
    def __init__(self, timeout):
        super().__init__("localhost", timeout=timeout)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


def docker(method: str, path: str, body=None, timeout: int = 120) -> tuple[int, bytes]:
    c = _Unix(timeout)
    try:
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def cstate(name: str) -> str:
    st, data = docker("GET", f"/containers/{name}/json")
    if st == 404:
        return "missing"
    return "running" if json.loads(data)["State"]["Running"] else "stopped"


def cstop(name: str, t: int = 30):
    if cstate(name) == "running":
        docker("POST", f"/containers/{name}/stop?t={t}", timeout=t + 60)


def cstart(name: str):
    if cstate(name) == "missing":
        raise RuntimeError(f"本机没有 {name} 容器")
    docker("POST", f"/containers/{name}/start")


def crestart_policy(name: str, policy: str):
    if cstate(name) != "missing":
        docker("POST", f"/containers/{name}/update", {"RestartPolicy": {"Name": policy}})


def read_env(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if "=" in ln and not ln.lstrip().startswith("#")]


def pull(image: str, on_pct=None):
    """Pull an image; `on_pct(0..1)` is called about once a second with the real progress (bytes downloaded and unpacked)."""
    repo, _, tag = image.rpartition(":")
    c = _Unix(900)
    layers: dict[str, tuple[float, int]] = {}            # layer -> (fraction done, size)
    last = 0.0
    try:
        c.request("POST", f"/images/create?fromImage={repo}&tag={tag}")
        r = c.getresponse()
        while True:
            line = r.readline()
            if not line:
                break
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("error"):
                raise RuntimeError("拉取镜像失败：" + str(d["error"])[:160])
            i, st, pd = d.get("id"), d.get("status", ""), d.get("progressDetail") or {}
            if i and len(i) == 12:
                size = layers.get(i, (0.0, 1))[1]
                if st == "Downloading" and pd.get("total"):
                    layers[i] = (0.5 * pd["current"] / pd["total"], pd["total"])
                elif st == "Download complete":
                    layers[i] = (0.5, size)
                elif st == "Extracting" and pd.get("total"):
                    layers[i] = (0.5 + 0.5 * pd["current"] / pd["total"], pd["total"])
                elif st in ("Pull complete", "Already exists"):
                    layers[i] = (1.0, size)
                if on_pct and layers and time.time() - last > 0.7:
                    last = time.time()
                    total = sum(t for _, t in layers.values()) or 1
                    on_pct(sum(f * t for f, t in layers.values()) / total)
    finally:
        c.close()


def create_nas_pair():
    """On the NAS the two containers are created fresh at each take-over, so they run the newest image the NAS registry holds."""
    prog(3, 2, "取最新的 hub 镜像")
    pull(HUB_IMAGE_TAG, lambda f: prog(3, 2 + int(8 * f), "取最新的 hub 镜像"))
    prog(3, 10, "取最新的 HomeAssistant 镜像（第一次要几分钟）")
    pull(HA_IMAGE_TAG, lambda f: prog(3, 10 + int(30 * f), "取最新的 HomeAssistant 镜像（第一次要几分钟）"))
    prog(3, 40, "创建容器")
    for n in (HUB, HA):
        if cstate(n) != "missing":
            cstop(n, 60)
            docker("DELETE", f"/containers/{n}?force=true")
    bind = lambda s, t, ro=False: {"Type": "bind", "Source": s, "Target": t, "ReadOnly": ro}
    docker("POST", f"/containers/create?name={HA}", {
        "Image": HA_IMAGE_TAG, "Env": ["TZ=Asia/Shanghai"],
        "HostConfig": {"NetworkMode": "host", "RestartPolicy": {"Name": "no"}, "Memory": 900 * 1024 ** 2,
                       "Mounts": [bind(ACT_HA, "/config"), bind("/etc/localtime", "/etc/localtime", True)]}})
    env = read_env(HUB_ENV)
    st, data = docker("POST", f"/containers/create?name={HUB}", {
        "Image": HUB_IMAGE_TAG, "Env": env + ["HUB_ROLE=nas", "HUB_PORT=8095", "PEER_SENTINEL_URL=" + PEER_URL,
                                    "OPS_URL=https://127.0.0.1:8220"],
        "HostConfig": {"NetworkMode": "host", "RestartPolicy": {"Name": "no"}, "Memory": 300 * 1024 ** 2,
                       "Mounts": [bind(ACT_DATA, "/data"), bind("/var/run/docker.sock", "/var/run/docker.sock"),
                                  bind("/data_s001", "/data_s001"), bind("/data_s002", "/data_s002")]}})
    if st >= 300:
        raise RuntimeError("创建 hub 容器失败：" + data.decode(errors="replace")[-160:])


def create_pi_pair():
    """The Pi-form hub and HomeAssistant, created only when this machine has none yet (a brand-new machine in the Pi's role). On the Pi that
    has been running for a long time they exist already (deploy_hub.ps1 / deploy_ha.ps1 made them) and nothing happens."""
    if cstate(HUB) != "missing" and cstate(HA) != "missing":
        return
    user = mc.get(ME, "ssh_user")
    prog(3, 2, "第一次在这台机器上创建：取 hub 镜像")
    pull(HUB_IMAGE_TAG, lambda f: prog(3, 2 + int(8 * f), "取 hub 镜像"))
    prog(3, 10, "取 HomeAssistant 镜像（第一次要几分钟）")
    pull(HA_IMAGE_TAG, lambda f: prog(3, 10 + int(30 * f), "取 HomeAssistant 镜像（第一次要几分钟）"))
    prog(3, 40, "创建容器")
    if cstate(HA) == "missing":
        st, data = docker("POST", f"/containers/create?name={HA}", {
            "Image": HA_IMAGE_TAG, "Env": ["TZ=Asia/Shanghai"],
            "HostConfig": {"NetworkMode": "host", "RestartPolicy": {"Name": "no"},
                           "Binds": [f"{ACT_HA}:/config", "/etc/localtime:/etc/localtime:ro"]}})
        if st >= 300:
            raise RuntimeError("创建 HomeAssistant 容器失败：" + data.decode(errors="replace")[-160:])
    if cstate(HUB) == "missing":
        st, data = docker("POST", f"/containers/create?name={HUB}", {
            "Image": HUB_IMAGE_TAG, "Env": read_env(HUB_ENV),
            "HostConfig": {"NetworkMode": "host", "RestartPolicy": {"Name": "no"},
                           "Binds": [f"{ACT_DATA}:/data", "/var/run/docker.sock:/var/run/docker.sock", f"/home/{user}/apps:/apps",
                                     f"{mc.get(ME, 'registry_dir')}/data:/registry-data:ro", f"{mc.get(ME, 'data_root')}:{mc.get(ME, 'data_root')}"]}})
        if st >= 300:
            raise RuntimeError("创建 hub 容器失败：" + data.decode(errors="replace")[-160:])


def create_pi_ops():
    if cstate(OPS) != "missing":
        return
    user = mc.get(ME, "ssh_user")
    pull(HUB_IMAGE_TAG)
    binds = [f"{ACT_OPS}:/data", "/var/run/docker.sock:/var/run/docker.sock", f"/home/{user}/apps:/apps", f"/home/{user}/learnsync:/home/{user}/learnsync",
             f"/home/{user}/plan:/home/{user}/plan", f"{mc.get(ME, 'keys_dir')}:/keys:ro", f"{mc.get(ME, 'registry_dir')}/data:/registry-data:ro",
             f"{mc.get(ME, 'data_root')}:{mc.get(ME, 'data_root')}"]
    st, data = docker("POST", f"/containers/create?name={OPS}", {
        "Image": HUB_IMAGE_TAG, "Cmd": ["python", "-m", "app.ops"], "Env": read_env(HUB_ENV),
        "HostConfig": {"NetworkMode": "host", "RestartPolicy": {"Name": "no"}, "Binds": binds}})
    if st >= 300:
        raise RuntimeError("创建 ops 容器失败：" + data.decode(errors="replace")[-160:])


def create_registry(mode: str):
    """A registry container made from nothing (a brand-new machine): the image comes from the other machine's registry."""
    img = f"{mc.registry_addr(OTHER)}/registry:2"
    pull(img)
    rd = mc.get(ME, "registry_dir")
    ro = ":ro" if mode == "ro" else ""
    name = REG_RW if mode == "rw" else REG_RO
    st, data = docker("POST", f"/containers/create?name={name}", {
        "Image": img, "Env": ["REGISTRY_HTTP_ADDR=0.0.0.0:5000", "REGISTRY_HTTP_TLS_CERTIFICATE=/certs/registry.crt",
                              "REGISTRY_HTTP_TLS_KEY=/certs/registry.key", "REGISTRY_STORAGE_DELETE_ENABLED=true"],
        "ExposedPorts": {"5000/tcp": {}},
        "HostConfig": {"Binds": [f"{rd}/data:/var/lib/registry{ro}", f"{rd}/certs:/certs:ro"], "PortBindings": {"5000/tcp": [{"HostPort": str(mc.get(ME, 'registry_port'))}]},
                       "RestartPolicy": {"Name": "no"}}})
    if st >= 300:
        raise RuntimeError("创建镜像仓库容器失败：" + data.decode(errors="replace")[-160:])


# ---------------------------------------------------------------- health
_noverify = ssl.create_default_context()
_noverify.check_hostname, _noverify.verify_mode = False, ssl.CERT_NONE


def hub_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/static/icon.svg", context=_noverify, timeout=4) as r:
            return r.status == 200 and b"<svg" in r.read(2000)
    except Exception:
        return False


def ha_ok() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8123/", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def tcp_ok(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=3).close()
        return True
    except OSError:
        return False


def wait_for(fn, secs: int, on_tick=None) -> bool:
    start = time.time()
    while time.time() - start < secs:
        if fn():
            return True
        if on_tick:
            on_tick(time.time() - start)
        time.sleep(2)
    return False


cur: dict = {"job": None}


def prog(i: int, pct: int, note: str):
    """What the phone's progress window shows for the running step."""
    j = cur["job"]
    if j:
        j["steps"][i].update(pct=pct, note=note)


# ---------------------------------------------------------------- talking to the other sentinel
def _peer_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=os.path.join(TLS_DIR, "server.crt"))
    ctx.check_hostname = False                           # trust exactly the hub's own certificate
    return ctx


def call_peer(path: str, body=None, timeout: int = 10, raw: bytes | None = None, method: str | None = None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(PEER_URL + path, data=data, method=method or ("POST" if data is not None else "GET"),
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/octet-stream" if raw is not None else "application/json"})
    with urllib.request.urlopen(req, context=_peer_ctx(), timeout=timeout) as r:
        return r.read()


def call_url(url: str, body: dict, timeout: int = 10):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=_noverify, timeout=timeout) as r:
        return r.read()


def my_id() -> str:
    """This machine's identity: a random id kept in the state folder. The other machine remembers it, so it can find this one again if the
    router hands out a different IP."""
    p = os.path.join(STATE_DIR, "identity")
    try:
        return open(p, encoding="utf-8").read().strip()
    except OSError:
        v = uuid.uuid4().hex
        with open(p, "w", encoding="utf-8") as f:
            f.write(v)
        return v


MY_ID = my_id()


def scan_for_peer(peer_id: str) -> str | None:
    """The other machine does not answer where the table says it is: look for the machine with its id in the same /24 network."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((PEER_TCP[0], 1))
        me_ip = s.getsockname()[0]
        s.close()
    except OSError:
        return None
    prefix = me_ip.rsplit(".", 1)[0]
    port = mc.get(OTHER, "sentinel_port")
    found: list[str] = []

    def probe(n: int):
        ip = f"{prefix}.{n}"
        if ip in (me_ip, PEER_TCP[0]) or found:
            return
        try:
            socket.create_connection((ip, port), timeout=0.6).close()
        except OSError:
            return
        try:
            req = urllib.request.Request(f"https://{ip}:{port}/status", headers={"Authorization": "Bearer " + (READ_TOKEN or TOKEN)})
            with urllib.request.urlopen(req, context=_noverify, timeout=4) as r:
                if json.loads(r.read()).get("mid") == peer_id:
                    found.append(ip)
        except Exception:
            pass

    ts = [threading.Thread(target=probe, args=(n,), daemon=True) for n in range(1, 255)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(8)
    return found[0] if found else None


def set_machine_address(role: str, address: str):
    """Write the new address into the machine table files this machine's hub / ops / sentinel read."""
    for d in (ACT_DATA, ACT_OPS):
        path = os.path.join(d, "machines.json")
        try:
            with open(path, encoding="utf-8-sig") as f:
                cur = json.load(f)
        except (OSError, ValueError):
            cur = {}
        cur.setdefault("machines", {}).setdefault(role, {})["address"] = address
        os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)


def restart_soon():
    """Everything that reads the table starts again (the sentinel last: its restart policy brings it back)."""
    def go():
        time.sleep(4)
        for n in (HUB, OPS):
            if cstate(n) == "running":
                docker("POST", f"/containers/{n}/restart?t=10", timeout=60)
        os._exit(3)
    threading.Thread(target=go, daemon=True).start()


def refresh_peer():
    try:
        d = json.loads(call_peer("/status", timeout=6))
        d.update(ok=True, at=time.time())
        peer.clear()
        peer.update(d)
    except Exception as ex:
        peer.update(ok=False, error=str(ex)[:100])


# ---------------------------------------------------------------- data copy
def _walk_files(root: str, exclude: tuple):
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f in exclude or f.endswith(".tmp") or f.endswith(".log") or ".log." in f:
                continue
            yield os.path.join(dirpath, f)


def db_ok(path: str) -> bool:
    """Is this SQLite file sound? (A sudden power cut can leave a database 'malformed'.)"""
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            return c.execute("pragma quick_check").fetchone()[0] == "ok"
        finally:
            c.close()
    except sqlite3.Error:
        return False


def snapshot_db(src: str, dst: str) -> str | None:
    """A consistent copy of a live database. It replaces the previous copy only if it is sound, so the last good one is never lost to a bad one."""
    if not os.path.isfile(src):
        return None
    tmp = dst + ".new"
    try:
        os.unlink(tmp)
    except OSError:
        pass
    a = sqlite3.connect(src)                              # a plain open: a read-only open of a WAL database can fail
    try:
        b = sqlite3.connect(tmp)
        try:
            a.backup(b)
        finally:
            b.close()
    except sqlite3.Error as ex:
        raise RuntimeError(f"{os.path.basename(src)} 读不出来（可能损坏）：{ex}")
    finally:
        a.close()
    if not db_ok(tmp):
        os.unlink(tmp)
        raise RuntimeError(f"{os.path.basename(src)} 的副本检查不通过（数据库可能损坏），没有推送，保留上一份好的")
    os.replace(tmp, dst)
    return dst


db_guard_state: dict = {"checked_at": 0, "ok": None, "bad_streak": 0, "repaired_at": 0, "note": ""}


def db_guard():
    """The live hub database is checked every 10 minutes while this machine is main. If it is broken twice in a row, the hub is stopped, the
    broken file is kept under another name, the last good copy takes its place and the hub starts again."""
    live = os.path.join(ACT_DATA, "hub.db")
    g = db_guard_state
    g["checked_at"] = time.time()
    if not os.path.isfile(live) or db_ok(live):
        g.update(ok=True, bad_streak=0)
        return
    g["bad_streak"] += 1
    g.update(ok=False, note="hub.db 检查不通过")
    log("hub.db is not sound (", g["bad_streak"], ")")
    if g["bad_streak"] < 2:
        return
    good = os.path.join(STATE_DIR, "hub.db.snap")
    if not (os.path.isfile(good) and db_ok(good)):
        g["note"] = "hub.db 损坏，而且没有可用的好副本，需要人来处理"
        return
    cstop(HUB, 30)
    keep = f"{live}.corrupt-{int(time.time())}"
    shutil.copy2(live, keep)
    shutil.copy2(good, live + ".new")
    for ext in ("-journal", "-wal", "-shm"):
        try:
            os.unlink(live + ext)
        except OSError:
            pass
    os.replace(live + ".new", live)
    cstart(HUB)
    g.update(ok=True, bad_streak=0, repaired_at=time.time(), note=f"hub.db 损坏已用最近的好副本修复，坏的留在 {os.path.basename(keep)}")
    log(g["note"])


def snapshot_hub_db() -> str | None:
    return snapshot_db(os.path.join(ACT_DATA, "hub.db"), os.path.join(STATE_DIR, "hub.db.snap"))


def snapshot_ops_db() -> str | None:
    return snapshot_db(os.path.join(ACT_OPS, "ops.db"), os.path.join(STATE_DIR, "ops.db.snap"))


def build_tar(full: bool) -> tuple[str, int]:
    """data/ + ha-config/ as one tar file. Only files changed since the last successful push unless `full`."""
    snap = snapshot_hub_db()
    fd, path = tempfile.mkstemp(dir=STATE_DIR, suffix=".tar")
    os.close(fd)
    n = 0
    seen = {}
    with tarfile.open(path, "w") as tf:
        items = [(p, "data/" + os.path.relpath(p, ACT_DATA)) for p in _walk_files(ACT_DATA, EXCLUDE_HUB)]
        items += [(p, "ha-config/" + os.path.relpath(p, ACT_HA)) for p in _walk_files(ACT_HA, EXCLUDE_HA)]
        if snap:
            items.append((snap, "data/hub.db"))
        osnap = snapshot_ops_db()
        if os.path.isdir(ACT_OPS):
            items += [(p, "ops/" + os.path.relpath(p, ACT_OPS)) for p in _walk_files(ACT_OPS, EXCLUDE_OPS)]
            if osnap:
                items.append((osnap, "ops/ops.db"))
        for p, arc in items:
            try:
                st = os.stat(p)
            except OSError:
                continue
            key = (st.st_size, int(st.st_mtime))
            seen[arc] = key
            if not full and sync["manifest"].get(arc) == key and arc not in ("data/hub.db", "ops/ops.db"):
                continue
            try:
                tf.add(p, arcname=arc, recursive=False)
                n += 1
            except OSError:
                pass
    build_tar.seen = seen
    return path, n


native_sync: dict = {"manifest": {}, "at": 0, "ok": None, "error": None}


def push_native() -> int:
    """The configuration and the folders of this machine's own services go to the other machine (in small batches: the sentinel is small)."""
    sent, batch, size, pending = 0, [], 0, {}

    def flush():
        nonlocal sent, batch, size, pending
        if not batch:
            return
        fd, path = tempfile.mkstemp(dir=STATE_DIR, suffix=".tar")
        os.close(fd)
        try:
            with tarfile.open(path, "w") as tf:
                for p, arc in batch:
                    tf.add(p, arcname=arc, recursive=False)
            with open(path, "rb") as f:
                call_peer("/import", raw=f.read(), timeout=300)
        finally:
            os.unlink(path)
        native_sync["manifest"].update(pending)
        sent += len(batch)
        batch, size, pending = [], 0, {}

    try:
        for name, folders in NATIVE.items():
            st, data = docker("GET", f"/containers/{name}/json")
            if st != 200:
                continue
            spec = os.path.join(STATE_DIR, f"native-{name}.json")
            with open(spec, "w", encoding="utf-8") as f:
                json.dump({"inspect": json.loads(data), "dirs": {d: f"d{i}" for i, d in enumerate(folders)}}, f)
            batch.append((spec, f"native/{name}/spec.json"))
            for i, root in enumerate(folders):
                for p in _walk_files(root, ()):
                    arc = f"native/{name}/d{i}/" + os.path.relpath(p, root).replace(os.sep, "/")
                    try:
                        s = os.stat(p)
                    except OSError:
                        continue
                    key = (s.st_size, int(s.st_mtime))
                    if native_sync["manifest"].get(arc) == key:
                        continue
                    batch.append((p, arc))
                    pending[arc] = key
                    size += s.st_size
                    if size >= NATIVE_BATCH:
                        flush()
        flush()
        native_sync.update(at=time.time(), ok=True, error=None)
    except Exception as ex:
        native_sync.update(ok=False, error=str(ex)[:120])
        raise
    return sent


def native_names() -> list[str]:
    root = os.path.join(SB, "native")
    try:
        return sorted(d for d in os.listdir(root) if os.path.isfile(os.path.join(root, d, "spec.json")))
    except OSError:
        return []


def native_takeover(st: dict):
    """The other machine is gone: run its small services here from the copies it sent."""
    running = []
    for name in native_names():
        if cstate(name) != "missing":                        # a container of that name is here already: leave it alone
            continue
        try:
            with open(os.path.join(SB, "native", name, "spec.json"), encoding="utf-8") as f:
                spec = json.load(f)
            info, dirs = spec["inspect"], spec["dirs"]
            hc = info["HostConfig"]
            binds = []
            for b in hc.get("Binds") or []:
                src, _, rest = b.partition(":")
                base = next((k for k in dirs if src == k or src.startswith(k + "/")), None)
                if base:
                    binds.append(os.path.join(SB, "native", name, dirs[base]) + src[len(base):] + ":" + rest)
                elif os.path.exists(src):
                    binds.append(b)
            image = info["Config"]["Image"]
            if docker("GET", f"/images/{urllib.parse.quote(image, safe='')}/json")[0] != 200:
                from recovery import target
                repo, tag = target(image)
                image = f"{mc.registry_addr(ME)}/{repo}:{tag}"
                pull(image)
            body = {"Image": image, "Env": info["Config"].get("Env"), "Cmd": info["Config"].get("Cmd"), "Entrypoint": info["Config"].get("Entrypoint"),
                    "ExposedPorts": info["Config"].get("ExposedPorts"), "Labels": {"homehub.native": "1"},
                    "HostConfig": {"Binds": binds or None, "PortBindings": hc.get("PortBindings"), "Memory": hc.get("Memory") or 0,
                                   "NetworkMode": "host" if hc.get("NetworkMode") == "host" else "bridge", "RestartPolicy": {"Name": "unless-stopped"}}}
            s_, data = docker("POST", f"/containers/create?name={name}", body)
            if s_ >= 300:
                raise RuntimeError(data.decode(errors="replace")[-160:])
            cstart(name)
            running.append(name)
            log("native service started here:", name)
        except Exception as ex:
            log("native service", name, "not started:", ex)
    st["native_running"] = running
    save_state(st)


def native_release(st: dict):
    """The other machine is back: its own copies run again, so ours go away."""
    names = set(st.get("native_running", []))
    code, data = docker("GET", "/containers/json?all=1&filters=" + urllib.parse.quote(json.dumps({"label": ["homehub.native=1"]})))
    if code == 200:                                          # found by label: the saved list can be lost, the containers cannot
        names |= {c["Names"][0].lstrip("/") for c in json.loads(data)}
    for name in sorted(names):
        cstop(name, 10)
        docker("DELETE", f"/containers/{name}?force=true")
        log("native service handed back:", name)
    st["native_running"] = []
    save_state(st)


def push_to_peer(full: bool = False) -> int:
    path, n = build_tar(full)
    try:
        if n:
            with open(path, "rb") as f:
                call_peer("/import", raw=f.read(), timeout=300)
        sync["manifest"] = build_tar.seen
        sync.update(pushed_at=time.time(), pushed_ok=True, pushed_error=None)
        return n
    except Exception as ex:
        sync.update(pushed_ok=False, pushed_error=str(ex)[:120])
        raise
    finally:
        os.unlink(path)


def import_tar(path: str):
    incoming = os.path.join(SB, ".incoming")
    shutil.rmtree(incoming, ignore_errors=True)
    os.makedirs(incoming)
    with tarfile.open(path) as tf:
        for m in tf.getmembers():
            if m.name.startswith(("/", "..")) or ".." in m.name.split("/"):
                raise RuntimeError("bad path in archive")
        tf.extractall(incoming, filter="data")
    shutil.copytree(incoming, SB, dirs_exist_ok=True, copy_function=shutil.copy2)
    shutil.rmtree(incoming, ignore_errors=True)
    sync["received_at"] = time.time()


def promote_data():
    """standby copy -> the folders this machine's hub and HA read."""
    for src, dst in ((os.path.join(SB, "data"), ACT_DATA), (os.path.join(SB, "ha-config"), ACT_HA)):
        if not os.path.isdir(src):
            raise RuntimeError(f"本机还没有收到主站的数据（{src}）")
        os.makedirs(dst, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=shutil.copy2)
    for wal in ("hub.db-wal", "hub.db-shm"):
        try:
            os.unlink(os.path.join(ACT_DATA, wal))
        except OSError:
            pass
    a, b = os.path.join(SB, "data", "hub.db"), os.path.join(SB, "data", "hub.db.snap")
    best = max((p for p in (a, b) if os.path.isfile(p)), key=os.path.getmtime, default=None)
    if best:
        shutil.copy2(best, os.path.join(ACT_DATA, "hub.db"))
    ops_src = os.path.join(SB, "ops")                      # the ops service's own data (settings: moved services, copies back, ...)
    if os.path.isdir(ops_src):
        os.makedirs(ACT_OPS, exist_ok=True)
        shutil.copytree(ops_src, ACT_OPS, dirs_exist_ok=True, copy_function=shutil.copy2)
        for wal in ("ops.db-wal", "ops.db-shm"):
            try:
                os.unlink(os.path.join(ACT_OPS, wal))
            except OSError:
                pass


# ---------------------------------------------------------------- stop / start the pair
def stop_pair():
    cstop(HUB, 30)
    cstop(OPS, 20)
    cstop(HA, 90)                                        # HomeAssistant writes its state when it stops
    for n in (HUB, HA, OPS):
        crestart_policy(n, "no")                         # a reboot must not bring a demoted pair back
    try:
        ensure_registry("ro")                            # this machine's registry becomes the read-only copy
    except Exception as ex:
        log("registry demote:", ex)


# ---------------------------------------------------------------- the registry and the ops service travel with the main machine
def _inspect(name: str):
    st, data = docker("GET", f"/containers/{name}/json")
    return json.loads(data) if st == 200 else None


def clone_registry(src: str, dst: str, ro: bool):
    """A twin of a registry container (same image, certificates, port, memory) whose data folder is mounted writable or read-only."""
    spec = _inspect(src)
    cfg, hc = spec["Config"], spec["HostConfig"]
    mounts = []
    for m in hc.get("Mounts") or []:
        mm = {k: m[k] for k in ("Type", "Source", "Target", "ReadOnly") if k in m}
        if mm.get("Target") == "/var/lib/registry":
            mm["ReadOnly"] = ro
        mounts.append(mm)
    binds = []
    for b in hc.get("Binds") or []:
        parts = b.split(":")
        if len(parts) >= 2 and parts[1] == "/var/lib/registry":
            b = f"{parts[0]}:{parts[1]}" + (":ro" if ro else "")
        binds.append(b)
    body = {"Image": cfg["Image"], "Env": cfg.get("Env"), "Cmd": cfg.get("Cmd"), "ExposedPorts": cfg.get("ExposedPorts"),
            "HostConfig": {"Binds": binds or None, "Mounts": None if binds else (mounts or None), "PortBindings": hc.get("PortBindings"),
                           "NetworkMode": hc.get("NetworkMode"), "Memory": hc.get("Memory") or 0, "RestartPolicy": {"Name": "no"}}}
    st, data = docker("POST", f"/containers/create?name={dst}", body)
    if st >= 300:
        raise RuntimeError(f"创建 {dst} 失败：" + data.decode(errors="replace")[-160:])


def ensure_registry(mode: str):
    """mode "rw": the machine that is main runs the writable registry; "ro": the other runs the read-only copy of it. Only one of the two
    containers runs at a time (they use the same port); the missing one is created from the other."""
    want, other = (REG_RW, REG_RO) if mode == "rw" else (REG_RO, REG_RW)
    if cstate(want) == "missing":
        if cstate(other) == "missing":
            create_registry(mode)
        else:
            clone_registry(other, want, ro=(mode == "ro"))
    if cstate(other) != "missing":
        cstop(other, 20)
        crestart_policy(other, "no")
    crestart_policy(want, "unless-stopped")
    if cstate(want) != "running":
        cstart(want)
    log("registry now", mode)


def create_nas_ops():
    """The ops service on the NAS: the hub image run as `python -m app.ops` with OPS_MACHINE=nas (the roles inside it are swapped)."""
    if cstate(OPS) != "missing":
        cstop(OPS, 20)
        docker("DELETE", f"/containers/{OPS}?force=true")
    bind = lambda s, t, ro=False: {"Type": "bind", "Source": s, "Target": t, "ReadOnly": ro}
    os.makedirs(ACT_OPS, exist_ok=True)
    st, data = docker("POST", f"/containers/create?name={OPS}", {
        "Image": HUB_IMAGE_TAG, "Cmd": ["python", "-m", "app.ops"], "Env": read_env(HUB_ENV) + ["OPS_MACHINE=nas", "OPS_PORT=8220"],
        "HostConfig": {"NetworkMode": "host", "RestartPolicy": {"Name": "no"}, "Memory": 400 * 1024 ** 2,
                       "Mounts": [bind(ACT_OPS, "/data"), bind("/var/run/docker.sock", "/var/run/docker.sock"),
                                  bind("/data_s001", "/data_s001"), bind("/data_s002", "/data_s002"),
                                  bind(mc.get(ME, "keys_dir"), "/keys", True), bind(mc.get(ME, "registry_dir") + "/data", "/registry-data", True)]}})
    if st >= 300:
        raise RuntimeError("创建 ops 容器失败：" + data.decode(errors="replace")[-160:])


def start_ops():
    if ME == "nas":
        create_nas_ops()
    else:
        create_pi_ops()
    crestart_policy(OPS, "unless-stopped")
    if cstate(OPS) != "running":
        cstart(OPS)
    wait_for(lambda: tcp_ok("127.0.0.1", mc.get(ME, "ops_port")), 90)


def ops_call(method: str, path: str, body=None, timeout: int = 30):
    req = urllib.request.Request("https://127.0.0.1:8220" + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": "Bearer " + READ_TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=_noverify, timeout=timeout) as r:
        return json.loads(r.read())


def sync_registry_to_peer(max_wait: int = 900):
    """Before this machine hands the main role over, the ops service copies the registry to the other one, which becomes the writable one."""
    if not READ_TOKEN or cstate(OPS) != "running":
        return
    job = ops_call("POST", "/api/registry/mirror", {})["job"]
    end = time.time() + max_wait
    while time.time() < end:
        d = ops_call("GET", "/api/registry/job/" + job)
        if d.get("done"):
            log("registry copied to the other machine:", "ok" if d.get("ok") else d.get("error"))
            return
        time.sleep(3)


def start_pair():
    if ME == "nas":
        create_nas_pair()
    else:
        create_pi_pair()
    for n in (HA, HUB):
        crestart_policy(n, "unless-stopped")
    prog(3, 45, "启动 HomeAssistant，等它准备好")
    cstart(HA)
    if not wait_for(ha_ok, 240, lambda e: prog(3, 45 + int(40 * (1 - math.exp(-e / 50))), "等 HomeAssistant 准备好")):
        raise RuntimeError("HomeAssistant 没有在 4 分钟内起来")
    prog(3, 88, "启动 hub")
    with standin.paused():                               # the stand-in gives the port to the real hub
        cstart(HUB)
        if not wait_for(lambda: hub_ok(SELF_HUB), 90, lambda e: prog(3, 88 + int(10 * (1 - math.exp(-e / 10))), "等 hub 准备好")):
            raise RuntimeError("hub 没有在 90 秒内起来")
    for what, fn in (("镜像仓库", lambda: ensure_registry("rw")), ("运维服务", start_ops)):       # these follow the main machine, but never make the switch fail
        try:
            prog(3, 98, f"启动{what}")
            fn()
        except Exception as ex:
            log(f"{what} not started:", ex)





# ---------------------------------------------------------------- disaster recovery of the other services (NAS only)
from recovery import Recovery                                    # noqa: E402

rec = Recovery(docker, pull, log, STATE_DIR)


def run_recovery(only: list[str] | None = None, offset: int = 0, title: str = "在 NAS 上恢复其他服务") -> dict:
    """Start the services that lived on the Pi here. offset > 0 is a rehearsal on shifted ports (nothing is taken over)."""
    plan = rec.plan(only, offset)
    if not plan["groups"]:
        raise RuntimeError("清单里没有需要恢复的服务（服务清单或迁移记录还没同步到 NAS）")
    j = new_job(title + ("（演练，不接管）" if offset else ""), [g["name"] for g in plan["groups"]],
                [(max(g["mem_mb"], 50), 60) for g in plan["groups"]])
    cur["job"] = j

    def work():
        try:
            res = rec.run(j, step, only, offset)
            j.update(done=True, ok=not res["failed"], error=("没有成功：" + "、".join(res["failed"])) if res["failed"] else None, result=res)
        except Exception as ex:
            log("recovery failed:", ex)
            j.update(done=True, ok=False, error=str(ex)[:200])
    threading.Thread(target=work, daemon=True).start()
    return j


# ---------------------------------------------------------------- jobs
def new_job(title: str, steps: list[str], meta: list | None = None) -> dict:
    meta = meta or STEP_META
    j = {"id": uuid.uuid4().hex[:8], "title": title,
         "steps": [{"t": t, "s": "wait", "note": "", "w": meta[k][0], "est": meta[k][1]} for k, t in enumerate(steps)],
         "done": False, "ok": False, "error": None, "started": time.time()}
    jobs[j["id"]] = j
    return j


def step(j: dict, i: int, s: str, note: str = ""):
    j["steps"][i].update(s=s, note=note)
    if s == "run":
        j["steps"][i]["t0"] = time.time()


def active_job() -> dict | None:
    return next((j for j in jobs.values() if not j["done"]), None)


def switch_to_me(j: dict, asked: bool):
    """Become the main machine. `asked`: the other one is alive and is told to stop first; otherwise (it died) its data is what
    the last copy brought."""
    started = False
    cur["job"] = j
    try:
        if asked:
            step(j, 0, "run", "停下 hub 和 HomeAssistant")
            call_peer("/release", {}, timeout=180)
            step(j, 0, "ok")
            step(j, 1, "run", "从原主站取最新数据")
            f = call_peer("/export", timeout=300)
            fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix=".tar")
            with os.fdopen(fd, "wb") as out:
                out.write(f)
            import_tar(tmp)
            os.unlink(tmp)
            step(j, 1, "ok")
        else:
            step(j, 0, "ok", "对方没有回应，跳过")
            step(j, 1, "ok", "用最近一次复制来的数据")
        step(j, 2, "run")
        promote_data()
        step(j, 2, "ok")
        step(j, 3, "run", "HomeAssistant 第一次起来要一两分钟")
        started = True
        start_pair()
        step(j, 3, "ok")
        step(j, 4, "run")
        with lock:
            st = load_state()
            st.update(term=st["term"] + 1, primary=ME, at=time.time())
            save_state(st)
        try:
            call_peer("/state", {"term": st["term"], "primary": ME})
        except Exception:
            pass                                         # it will see the higher term when it comes back
        step(j, 4, "ok")
        j.update(done=True, ok=True)
        log("now main:", ME, "term", st["term"])
        if not asked and ME == "nas":                    # the Pi did not answer at all: its services are gone too
            try:
                run_recovery()
            except Exception as ex:
                log("recovery not started:", ex)
    except Exception as ex:
        log("switch failed:", ex)
        for s in j["steps"]:
            if s["s"] == "run":
                s["s"] = "fail"
        j.update(done=True, ok=False, error=str(ex)[:200])
        try:
            if started:
                stop_pair()
            if asked:
                call_peer("/resume", {}, timeout=240)    # give it back: it was stopped for nothing
        except Exception as ex2:
            log("rollback problem:", ex2)


STEP_META = [(20, 18), (5, 5), (4, 4), (65, 60), (2, 2)]     # (weight in the overall bar, typical seconds) per step
STEPS = ["请原来的主站停下", "取最新数据", "准备本机数据", "启动 HomeAssistant 和 hub", "记下新的主站"]


def start_switch(target: str) -> dict:
    with lock:
        if active_job():
            raise RuntimeError("已经有一个切换在进行")
        st = load_state()
        if st["primary"] == target:
            raise RuntimeError("现在就在这台")
        j = new_job(f"主站切到{'香橙派' if target == 'pi' else 'NAS'}", STEPS)
    if target == ME:
        threading.Thread(target=switch_to_me, args=(j, True), daemon=True).start()
    else:
        threading.Thread(target=lambda: _forward_switch(j, target), daemon=True).start()
    return j


def _forward_switch(j: dict, target: str):
    try:
        step(j, 0, "run", "转给对方去做")
        r = json.loads(call_peer("/switch", {"target": target, "from_peer": True}, timeout=30))
        pj = r["job"]
        while True:
            time.sleep(1.5)
            d = json.loads(call_peer("/job/" + pj, timeout=15))
            j["steps"], j["done"], j["ok"], j["error"] = d["steps"], d["done"], d["ok"], d["error"]
            if d["done"]:
                break
    except Exception as ex:
        j.update(done=True, ok=False, error=str(ex)[:200])


# ---------------------------------------------------------------- the stand-in for the hub's port
# While the hub is stopped on purpose (this machine is not main, or a switch is under way) its public port would answer nothing, and a
# phone that is following the switch from outside the house (it reaches the Pi through the NAS's port forwarder, which only passes
# bytes) would see nothing at all. So the sentinel holds the hub's port meanwhile and answers just two things: the main-machine
# status and the job progress, with the hub's certificate and the hub token. Everything else gets 503 "switching".
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect width="8" height="8" fill="#888"/></svg>'


class StandInHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        # Anything but the progress is answered by cutting the connection, not by an error page: the phone's app only looks for
        # another route when a request FAILS (a 503 would count as an answer), and its probe for "is the hub here" must not succeed
        if path != "/api/primary" and not path.startswith("/api/primary/job/"):
            self.close_connection = True
            return
        given = self.headers.get("Authorization", "")
        tok = given[7:] if given.startswith("Bearer ") else ""
        if not (tok and (hmac.compare_digest(tok, TOKEN) or (READ_TOKEN and hmac.compare_digest(tok, READ_TOKEN)))):
            return self._send(401, b'{"error":"no"}')
        try:
            if path == "/api/primary":
                return self._send(200, json.dumps(status(), ensure_ascii=False).encode())
            if path.startswith("/api/primary/job/"):
                key = path.rsplit("/", 1)[1]
                j = max(jobs.values(), key=lambda x: x["started"], default=None) if key == "current" else jobs.get(key)
                return self._send(200 if j else 404, json.dumps(dict(j, now=time.time()) if j else {"error": "no such job"}, ensure_ascii=False).encode())
        except Exception as ex:
            return self._send(500, json.dumps({"error": str(ex)[:100]}).encode())
        self.close_connection = True

    do_POST = do_GET


class _Server6(http.server.ThreadingHTTPServer):
    request_queue_size = 128                      # the hub on the other machine asks for every container at once: the default queue of 5 drops connections
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)      # one socket for IPv4 and IPv6
        super().server_bind()


class StandIn:
    def __init__(self, port: int):
        self.port, self.srv, self.hold = port, None, 0
        self.lk = threading.RLock()

    def start(self):
        with self.lk:
            if self.srv or self.hold:
                return
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain(os.path.join(TLS_DIR, "server.crt"), os.path.join(TLS_DIR, "server.key"))
            srv = _Server6(("::", self.port), StandInHandler)
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.srv = srv
            log("stand-in answering on", self.port)

    def stop(self):
        with self.lk:
            if self.srv:
                self.srv.shutdown()
                self.srv.server_close()
                self.srv = None
                log("stand-in released port", self.port)

    class _Paused:
        def __init__(self, outer):
            self.o = outer

        def __enter__(self):
            with self.o.lk:
                self.o.hold += 1
                self.o.stop()

        def __exit__(self, *a):
            with self.o.lk:
                self.o.hold -= 1

    def paused(self):
        return StandIn._Paused(self)


standin = StandIn(8200 if ME == "pi" else 8095)


def cinfo(name: str) -> tuple[str, str]:
    st, data = docker("GET", f"/containers/{name}/json")
    if st == 404:
        return "missing", ""
    d = json.loads(data)
    return d["State"]["Status"], d["HostConfig"]["RestartPolicy"]["Name"]


def standin_wanted() -> bool:
    """Only while the hub is stopped on purpose (restart policy "no", or not there at all): a hub that crashed on the main machine
    is restarted by docker at once and must find its port free."""
    status_, policy = cinfo(HUB)
    if status_ in ("running", "restarting", "paused") or (status_ != "missing" and policy != "no"):
        return False
    return True


def standin_loop():
    while True:
        try:
            want = standin_wanted()
            if want and not standin.srv and not standin.hold:
                standin.start()
            elif not want and standin.srv:
                standin.stop()
        except Exception as ex:
            log("stand-in:", str(ex)[:100])
        time.sleep(1)


# ---------------------------------------------------------------- the watching loop
def host_stats() -> dict:
    """The numbers the other machine's monitor page needs (same text formats as nproc, /proc/stat, /proc/meminfo, /proc/loadavg)."""
    with open("/proc/meminfo", encoding="utf-8") as f:
        mem = "".join(ln for ln in f if ln.split(":")[0] in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"))
    with open("/proc/stat", encoding="utf-8") as f:
        stat = f.readline()
    with open("/proc/loadavg", encoding="utf-8") as f:
        load = f.read()
    disks = []
    for item in os.environ.get("DISKS", "").split(";"):
        label_, _, path = item.partition(":")
        try:
            if label_ and path and (path == "/" or os.path.ismount(path)):
                st = os.statvfs(path)
                disks.append({"label": label_, "path": path, "total": st.f_blocks * st.f_frsize, "used": (st.f_blocks - st.f_bfree) * st.f_frsize})
        except OSError:
            pass
    first = disks[0] if disks else {"total": 1, "used": 0}
    return {"cores": os.cpu_count(), "stat": stat, "mem": mem, "load": load, "df": f"x {first['total']} {first['used']}", "disks": disks}


def status() -> dict:
    st = load_state()
    return {"me": ME, "mid": MY_ID, "term": st["term"], "primary": st["primary"], "auto": st.get("auto", False),
            "hub": {"state": cstate(HUB), "ok": hub_ok(SELF_HUB) if st["primary"] == ME else None},
            "ha": {"state": cstate(HA)}, "ops": {"state": cstate(OPS)}, "db": {k: v for k, v in db_guard_state.items()}, "native": {"has": native_names(), "running": load_state().get("native_running", []), "pushed_at": native_sync["at"], "ok": native_sync["ok"], "error": native_sync["error"]},
            "registry": "rw" if cstate(REG_RW) == "running" else "ro" if cstate(REG_RO) == "running" else "off", "sync": {k: v for k, v in sync.items() if k != "manifest"},
            "peer": {"ok": peer.get("ok", False), "at": peer.get("at", 0), "term": peer.get("term"), "primary": peer.get("primary"),
                     "hub_state": (peer.get("hub") or {}).get("state"), "hub_ok": (peer.get("hub") or {}).get("ok")},
            "watch": {"silent": round(time.time() - watch["silent_since"]) if watch["silent_since"] else 0, "dead_after": DEAD_AFTER},
            "recovered": rec.recovered()["containers"] if ME == "nas" else [],
            "recovered_at": rec.recovered().get("at", 0) if ME == "nas" else 0,
            "job": (active_job() or {}).get("id"), "time": time.time()}


def loop():
    last_push = last_native = 0.0
    while True:
        try:
            tick(last_push)
            if NATIVE and peer.get("ok") and time.time() - last_native >= NATIVE_EVERY:
                last_native = time.time()
                try:
                    n = push_native()
                    log("native copy:", n, "files")
                except Exception as ex:
                    log("native copy failed:", ex)
            if time.time() - last_push >= PUSH_EVERY and load_state()["primary"] == ME and cstate(HUB) == "running":
                last_push = time.time()
                try:
                    n = push_to_peer()
                    log("pushed", n, "files")
                except Exception as ex:
                    log("push failed:", ex)
        except Exception as ex:
            log("loop:", ex)
        time.sleep(CHECK_EVERY)


def tick(_):
    refresh_peer()
    with lock:
        st = load_state()
        if peer.get("ok") and peer.get("term", 0) > st["term"]:                  # the other one moved on while we were away
            st.update(term=peer["term"], primary=peer["primary"], at=time.time())
            save_state(st)
            log("adopted term", st["term"], "primary", st["primary"])
        if active_job():
            return
        now_ = time.time()                                   # the other machine's own small services: run them here while it is silent
        peer_dead = (not peer.get("ok")) and (not hub_ok(PEER_HUB)) and (not tcp_ok(*PEER_TCP))
        watch["peer_dead_since"] = (watch.get("peer_dead_since") or now_) if peer_dead else None
        if peer.get("ok") and peer.get("mid") and st.get("peer_id") != peer["mid"]:
            st["peer_id"] = peer["mid"]
            save_state(st)
        if (watch["peer_dead_since"] and now_ - watch["peer_dead_since"] >= 60 and st.get("peer_id")
                and now_ - watch.get("last_scan", 0) >= 180):              # not where the table says: find it by its id
            watch["last_scan"] = now_
            ip = scan_for_peer(st["peer_id"])
            if ip:
                log("the other machine moved:", PEER_TCP[0], "->", ip)
                set_machine_address(OTHER, ip)
                try:
                    call_url(f"https://{ip}:{mc.get(OTHER, 'sentinel_port')}/machines", {"role": OTHER, "address": ip})
                except Exception as ex:
                    log("could not tell it:", ex)
                restart_soon()
        if watch["peer_dead_since"] and now_ - watch["peer_dead_since"] >= DEAD_AFTER and st.get("auto") and not st.get("native_running") and native_names():
            native_takeover(st)
        elif peer.get("ok") and (st.get("native_running") or time.time() - watch.get("native_checked", 0) > 300):
            watch["native_checked"] = time.time()
            native_release(st)
        mine = st["primary"] == ME
        if mine and cstate(HUB) == "running" and time.time() - db_guard_state["checked_at"] >= 600:
            try:
                db_guard()
            except Exception as ex:
                log("db guard:", ex)
        running = cstate(HUB) == "running" or cstate(HA) == "running"
        # fencing: not main, but running the pair (rebooted, or came back after a split) -> stop it, if the main one really runs
        if not mine and running and peer.get("ok") and (peer.get("hub") or {}).get("ok"):
            log("fencing: stopping my pair, main is", st["primary"])
            stop_pair()
            return
        if ME == "pi" and peer.get("ok"):
            fenced = st.get("fenced", [])
            if peer.get("recovered"):
                for name in peer["recovered"]:              # the NAS runs these now (newer data): the Pi's copies must not run too
                    if cstate(name) == "running":
                        log("fencing recovered service:", name)
                        docker("POST", f"/containers/{name}/stop?t=20", timeout=60)
                    if cstate(name) != "missing":
                        crestart_policy(name, "no")
                        if name not in fenced:
                            fenced.append(name)
                st["fenced"] = fenced
                save_state(st)
            elif fenced:                                    # the NAS let go: the Pi's own copies run again
                for name in fenced:
                    if cstate(name) != "missing":
                        crestart_policy(name, "unless-stopped")
                        if cstate(name) == "stopped":
                            cstart(name)
                        log("started again after recovery:", name)
                st["fenced"] = []
                save_state(st)
        if mine:
            watch.update(silent_since=None, unhealthy_since=None)
            return
        # I am the stand-by: watch the main one
        dead = (not peer.get("ok")) and (not hub_ok(PEER_HUB)) and (not tcp_ok(*PEER_TCP))
        sick = peer.get("ok") and not (peer.get("hub") or {}).get("ok")
        now = time.time()
        watch["silent_since"] = (watch["silent_since"] or now) if dead else None
        watch["unhealthy_since"] = (watch["unhealthy_since"] or now) if sick else None
        why = None
        if watch["silent_since"] and now - watch["silent_since"] >= DEAD_AFTER:
            why = "主站整机没有回应"
        elif watch["unhealthy_since"] and now - watch["unhealthy_since"] >= UNHEALTHY_AFTER:
            why = "主站在线但 hub 一直不正常"
        if why and st.get("auto"):
            log("automatic take-over:", why)
            j = new_job("自动接管：" + why, STEPS)
            threading.Thread(target=switch_to_me, args=(j, bool(peer.get("ok"))), daemon=True).start()
        elif why:
            log("would take over (auto is off):", why)


# ---------------------------------------------------------------- http api
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    _seen: dict = {}

    def log_message(self, *a):
        pass

    def _note(self):
        """One line per client and path every 10 s (not for the other sentinel): shows whether a phone reaches us directly."""
        ip = self.client_address[0]
        if ip in PEER_URL:
            return
        key = (ip, self.path.split("?")[0])
        if time.time() - H._seen.get(key, 0) > 10:
            H._seen[key] = time.time()
            log("client", ip, self.command, self.path[:60])

    def _auth(self, read_only: bool = False) -> bool:
        given = self.headers.get("Authorization", "")
        ok = given.startswith("Bearer ") and (hmac.compare_digest(given[7:], TOKEN)
                                              or (read_only and READ_TOKEN != "" and hmac.compare_digest(given[7:], READ_TOKEN)))
        if not ok:
            self._send(401, {"error": "no"})
        return ok

    def _send(self, code: int, obj=None, raw: bytes | None = None):
        body = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_GET(self):
        self._note()
        if not self._auth(read_only=self.path == "/status" or self.path.startswith("/job/")):
            return
        try:
            if self.path == "/status":
                return self._send(200, status())
            if self.path == "/hoststats":
                return self._send(200, host_stats())
            if self.path.startswith("/docker/"):
                return self._docker_proxy("GET")
            if self.path.startswith("/job/"):
                key = self.path[5:]
                # "current" = the newest job: a phone that follows a switch ends up asking the other machine's sentinel
                j = max(jobs.values(), key=lambda x: x["started"], default=None) if key == "current" else jobs.get(key)
                return self._send(200 if j else 404, dict(j, now=time.time()) if j else {"error": "no such job"})
            if self.path == "/export":
                path, _ = build_tar(True)
                try:
                    with open(path, "rb") as f:
                        return self._send(200, raw=f.read())
                finally:
                    os.unlink(path)
            self._send(404, {"error": "?"})
        except Exception as ex:
            self._send(500, {"error": str(ex)[:200]})

    def _docker_proxy(self, method: str):
        """A narrow window to this machine's docker for the other machine's hub: list, stats, start/stop/restart. Never the hub or
        HomeAssistant (only a switch may start or stop those) and never anything that creates or deletes."""
        path = self.path[len("/docker"):]
        ok = method == "GET" and bool(re.fullmatch(r"/containers/json\?all=1", path) or re.fullmatch(r"/containers/[\w.-]+/stats\?stream=false", path))
        m = re.fullmatch(r"/containers/([\w.-]+)/(start|stop|restart)(\?t=\d+)?", path) if method == "POST" else None
        if m:
            if m.group(1) in (HUB, HA, "homehub-sentinel"):
                return self._send(409, {"error": "这个容器只能通过切换主站来启动或停止"})
            ok = True
        if not ok:
            return self._send(403, {"error": "not allowed"})
        st, data = docker(method, path, timeout=70)
        self.send_response(200 if st < 300 else st)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self._auth():
            return
        try:
            if self.path.startswith("/docker/"):
                self._body()
                return self._docker_proxy("POST")
            if self.path == "/import":
                fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix=".tar")
                with os.fdopen(fd, "wb") as out:
                    out.write(self._body())
                try:
                    import_tar(tmp)
                finally:
                    os.unlink(tmp)
                return self._send(200, {"ok": True})
            body = json.loads(self._body() or b"{}")
            if self.path == "/machines":                 # the other sentinel found this machine at another address: record it too
                set_machine_address(body["role"], body["address"])
                restart_soon()
                return self._send(200, {"ok": True})
            if self.path == "/switch":
                return self._send(200, {"job": start_switch(body["target"])["id"]})
            if self.path == "/release":                  # the other one takes over: stop my pair, keep it stopped
                log("release asked")
                try:
                    sync_registry_to_peer()
                except Exception as ex:
                    log("registry copy before release failed:", ex)
                stop_pair()
                return self._send(200, {"ok": True})
            if self.path == "/resume":                   # a take-over failed: run again here
                log("resume asked")
                start_pair()
                return self._send(200, {"ok": True})
            if self.path == "/state":
                with lock:
                    st = load_state()
                    if body["term"] > st["term"]:
                        st.update(term=body["term"], primary=body["primary"], at=time.time())
                        save_state(st)
                return self._send(200, {"ok": True})
            if self.path == "/auto":
                with lock:
                    st = load_state()
                    st["auto"] = bool(body["on"])
                    save_state(st)
                if not body.get("from_peer"):            # the switch belongs to both machines: whichever is the stand-by is the one that uses it
                    try:
                        call_peer("/auto", {"on": st["auto"], "from_peer": True})
                    except Exception:
                        pass
                return self._send(200, {"auto": st["auto"]})
            if self.path == "/recovery/plan":
                return self._send(200, rec.plan(body.get("only"), int(body.get("offset", 0))))
            if self.path == "/recovery/run":
                return self._send(200, {"job": run_recovery(body.get("only"), int(body.get("offset", 0)))["id"]})
            if self.path == "/recovery/cleanup":             # take away what a rehearsal created
                rec.cleanup(body.get("containers", []), body.get("networks", []))
                return self._send(200, {"ok": True})
            if self.path == "/recovery/state":               # what runs here instead of on the Pi, and where its working data is
                return self._send(200, rec.recovered())
            if self.path == "/recovery/stop":                # stop the stand-ins but keep them (their data is about to be carried back)
                return self._send(200, rec.stop_recovered())
            if self.path == "/recovery/failback":            # the Pi is back: the NAS gives the services up (the Pi's sentinel starts its own copies)
                return self._send(200, rec.failback())
            if self.path == "/push":
                return self._send(200, {"files": push_to_peer(full=True)})
            self._send(404, {"error": "?"})
        except Exception as ex:
            self._send(500 if not isinstance(ex, RuntimeError) else 409, {"error": str(ex)[:200]})


class Server(http.server.ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(SB, exist_ok=True)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(os.path.join(TLS_DIR, "server.crt"), os.path.join(TLS_DIR, "server.key"))
    srv = Server(("0.0.0.0", 8210), H)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=standin_loop, daemon=True).start()
    log("sentinel", ME, "ready; state", load_state())
    srv.serve_forever()


if __name__ == "__main__":
    main()
