"""Taking a new machine into HomeHub (in the ops service).

  * LISTEN: machines that run the join agent (sentinel/agent.py) shout on the network; this keeps the list of who was heard lately.
  * CLAIM: the owner taps "take this machine in" and says which machine it replaces (the Pi or the NAS). The job then
      1. has the agent install the shared SSH key (only the owner's secret can ask for that),
      2. logs in with that key, checks docker / memory / disk and creates the folders, the registry certificate and the key folder,
      3. writes the new address into the machine table (machines.json) on both machines,
      4. puts the sentinel on the new machine (its settings, certificate, tokens) and starts it,
      5. tells the other machine about the new address and restarts what has to read it.
    From then on the normal 5-minute copy and the registry mirror fill the new machine; the sentinels do the rest.
Physical names are used throughout ("pi", "nas" = the roles of the two machines); an ops running on the NAS has them swapped inside the
engines, `PH()` always gives the physical entry."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
import urllib.request

import paramiko

from . import machines as mc

log = logging.getLogger("joiner")
BEACON_PORT = 8230
KEY = "/keys/homehub_ed25519"
IMG_TAG = "homehub-sentinel:1"
STEPS = ["联系新机器，安装登录密钥", "检查新机器，补齐文件夹和证书", "写入新的地址表", "在新机器上装好哨兵", "通知另一台机器，让新地址生效"]


def PH(role: str) -> dict:
    return mc.CFG["machines"][mc.phys(role)]


def _sign(secret: bytes, *parts: str) -> str:
    return hmac.new(secret, "|".join(parts).encode(), hashlib.sha256).hexdigest()


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, owner):
        self.o = owner

    def datagram_received(self, data, addr):
        self.o.heard(data, addr[0])


class Joiner:
    def __init__(self, monitor, store):
        self.m, self.store = monitor, store
        self.secret = os.environ.get("JOIN_SECRET", "").encode()
        self.seen: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}

    # ---- listening
    async def start(self):
        if not self.secret:
            log.warning("JOIN_SECRET not set: new machines cannot be discovered")
            return
        try:
            await asyncio.get_running_loop().create_datagram_endpoint(lambda: _Proto(self), local_addr=("0.0.0.0", BEACON_PORT), allow_broadcast=True)
        except OSError as ex:
            log.warning("cannot listen for new machines: %s", ex)

    def heard(self, data: bytes, ip: str):
        try:
            d = json.loads(data)
            if not hmac.compare_digest(str(d.get("mac")), _sign(self.secret, d["id"], str(d["ts"]), d["host"], d["user"])) or abs(time.time() - d["ts"]) > 120:
                return
        except Exception:
            return
        d.pop("mac", None)
        self.seen[d["id"]] = {**d, "addr": ip, "last": time.time()}

    def known_role(self, ip: str) -> str | None:
        return next((r for r in ("pi", "nas") if mc.CFG["machines"][r]["address"] == ip), None)

    def candidates(self, include_known: bool = False) -> list[dict]:
        out = []
        for v in self.seen.values():
            age = time.time() - v["last"]
            if age > 60:
                continue
            phys = None
            for r in ("pi", "nas"):
                if PH(r)["address"] == v["addr"]:
                    phys = r
            if phys and not include_known:
                continue
            out.append({"id": v["id"], "host": v["host"], "addr": v["addr"], "user": v["user"], "ssh_port": v["ssh_port"], "mem_mb": v["mem_mb"],
                        "arch": v["arch"], "age": int(age), "known": phys})
        return sorted(out, key=lambda c: c["age"])

    # ---- claiming
    def start_claim(self, mid: str, role: str) -> str:
        if role not in ("pi", "nas"):
            raise ValueError("role must be pi or nas")
        c = self.seen.get(mid)
        if not c or time.time() - c["last"] > 120:
            raise ValueError("这台机器刚才没有出现，等它出现在列表里再点")
        local = mc.phys("pi")                                       # the machine this ops runs on is not replaceable from here
        if role == local:
            raise ValueError("运维服务正跑在这台机器上，不能在这里取代它：先把运维切到另一台（切换主站），或从另一台操作")
        job = uuid.uuid4().hex[:8]
        self.jobs[job] = {"id": job, "started": time.time(), "steps": [{"t": t, "s": "wait"} for t in STEPS], "done": False, "ok": None, "error": None}
        asyncio.create_task(self._run(job, c, role))
        return job

    def _set(self, job: str, i: int, s: str, note: str | None = None, pct: int | None = None):
        st = self.jobs[job]["steps"][i]
        st["s"] = s
        if note is not None:
            st["note"] = note
        if pct is not None:
            st["pct"] = pct
        if s == "ok":
            st["pct"] = 100

    async def _run(self, job: str, c: dict, role: str):
        j = self.jobs[job]
        try:
            await asyncio.to_thread(self._work, job, c, role)
            j.update(done=True, ok=True, ended=time.time())
        except Exception as ex:
            log.exception("join failed")
            for k, s in enumerate(j["steps"]):
                if s["s"] == "run":
                    s["s"] = "fail"
            j.update(done=True, ok=False, error=str(ex)[:200], ended=time.time())

    # ---- the work (runs in a thread)
    def _ssh(self, c: dict) -> paramiko.SSHClient:
        cl = paramiko.SSHClient()
        cl.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cl.connect(c["addr"], port=c["ssh_port"], username=c["user"], key_filename=KEY, timeout=20, look_for_keys=False, allow_agent=False)
        return cl

    @staticmethod
    def _run_on(cl: paramiko.SSHClient, cmd: str, root: bool = False, stdin: bytes | None = None, timeout: int = 600) -> tuple[int, str]:
        if root:
            q = cmd.replace("'", "'\\''")
            cmd = (f"if [ \"$(id -u)\" = 0 ]; then sh -c '{q}'; elif sudo -n true 2>/dev/null; then sudo -n sh -c '{q}'; "
                   f"else docker run --rm -i -v /:/host alpine chroot /host sh -c '{q}'; fi")
        ch = cl.get_transport().open_session()
        ch.settimeout(timeout)
        ch.exec_command(cmd)
        if stdin is not None:
            ch.sendall(stdin)
            ch.shutdown_write()
        out = (ch.makefile("r").read() + ch.makefile_stderr("r").read()).decode(errors="replace")
        return ch.recv_exit_status(), out.strip()

    def _work(self, job: str, c: dict, role: str):
        k_ = paramiko.Ed25519Key.from_private_key_file(KEY)
        pub = f"ssh-ed25519 {k_.get_base64()} homehub"
        new = PH(role)                                              # physical entry of the machine being replaced / joined
        S = lambda i, s, note=None, pct=None: self._set(job, i, s, note, pct)
        # 1. key
        S(0, "run")
        body = {"pubkey": pub, "ts": str(time.time())}
        body["mac"] = _sign(self.secret, "claim", pub, body["ts"])
        req = urllib.request.Request(f"http://{c['addr']}:{c.get('agent_port', 8231)}/claim", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status != 200:
                raise RuntimeError("新机器没有接受密钥")
        cl = self._ssh(c)
        S(0, "ok", f"{c['user']}@{c['addr']}")
        # 2. check and prepare
        S(1, "run")
        rc, out = self._run_on(cl, "docker version --format '{{.Server.Version}}'")
        if rc:
            rc, out = self._run_on(cl, "sudo -n docker version --format '{{.Server.Version}}'")
        if rc:
            raise RuntimeError("新机器上 docker 不能用（要装好 docker，用户在 docker 组或能免密 sudo）")
        if c["mem_mb"] < 1500:
            raise RuntimeError(f"新机器内存只有 {c['mem_mb']} MB，跑 hub 和 HomeAssistant 至少要 1.5 GB")
        dirs = [new["data_root"], new["apps_root"], new["specs_dir"], new["registry_dir"] + "/data", new["registry_dir"] + "/certs", new["hub_data"],
                new["keys_dir"], new["ops_data"], new["data_root"] + "/homehub-standby",
                (new["data_root"] + "/homehub-sentinel/state") if mc.phys("pi") != role and role == "pi" else os.path.dirname(new["hub_data"]) + "/state",
                new["hub_data"] + "/tls"]
        for d in dirs:
            self._run_on(cl, f"mkdir -p '{d}'", root=True)
        S(1, "run", "目录就绪，装证书和密钥", 40)
        local_reg_dir = mc.CFG["machines"]["pi"]["registry_dir"]      # the machine this runs on holds the registry certificate
        crt = open(local_reg_dir + "/certs/registry.crt", "rb").read()
        key = open(local_reg_dir + "/certs/registry.key", "rb").read()     # the new machine's own registry uses the same certificate
        self._run_on(cl, f"umask 077 && cat > '{new['registry_dir']}/certs/registry.crt'", root=True, stdin=crt)
        self._run_on(cl, f"umask 077 && cat > '{new['registry_dir']}/certs/registry.key'", root=True, stdin=key)
        for r in ("pi", "nas"):
            ra = f"{(c['addr'] if mc.phys(r) == role else PH(r)['address'])}:{PH(r)['registry_port']}"
            self._run_on(cl, f"mkdir -p '/etc/docker/certs.d/{ra}' && cat > '/etc/docker/certs.d/{ra}/ca.crt'", root=True, stdin=crt)
        self._run_on(cl, f"umask 077 && cat > '{new['keys_dir']}/homehub_ed25519' && chmod 600 '{new['keys_dir']}/homehub_ed25519'", root=True,
                     stdin=open(KEY, "rb").read())
        self._run_on(cl, f"cat > '{new['keys_dir']}/homehub_ed25519.pub'", root=True, stdin=(pub + "\n").encode())
        S(1, "ok", f"docker {out}，内存 {c['mem_mb']} MB")
        # 3. address table
        S(2, "run")
        changed = (new["address"], new["ssh_user"], new["ssh_port"]) != (c["addr"], c["user"], c["ssh_port"])
        ov_path = "/data/machines.json"                              # this ops's own override (also read by the hub next to it)
        try:
            ov = json.load(open(ov_path, encoding="utf-8-sig"))
        except (OSError, ValueError):
            ov = {}
        ov.setdefault("machines", {}).setdefault(role, {}).update(address=c["addr"], ssh_user=c["user"], ssh_port=c["ssh_port"])
        blob = json.dumps(ov, ensure_ascii=False, indent=1).encode()
        local = mc.phys("pi")
        for target in (mc.CFG["machines"]["pi"]["hub_data"], mc.CFG["machines"]["pi"]["ops_data"]):       # this machine: files directly
            os.makedirs(target, exist_ok=True)
            with open(target + "/machines.json", "wb") as f:
                f.write(blob)
        for d in (new["hub_data"], new["ops_data"]):                  # the new machine: over SSH
            self._run_on(cl, f"mkdir -p '{d}' && cat > '{d}/machines.json'", root=True, stdin=blob)
        S(2, "ok", "地址有变化" if changed else "地址没变，只是确认")
        # 4. sentinel on the new machine
        S(3, "run")
        self._install_sentinel(cl, role, c)
        S(3, "ok")
        # 5. the other machine learns the new address
        S(4, "run")
        if changed:
            self._tell_local(role, c)
            S(4, "ok", "本机的哨兵、hub、运维会在几秒后重启来读新地址")
            self._restart_later()
        else:
            S(4, "ok", "地址没变，不需要重启")
        cl.close()

    def _tokens(self) -> dict:
        return {k: os.environ[k] for k in ("HA_TOKEN", "HUB_TOKEN", "SENTINEL_TOKEN") if os.environ.get(k)}

    @staticmethod
    def _sent_dir(phys: str, m: dict) -> str:
        """Where a machine keeps its sentinel's settings and state."""
        return m["data_root"] + "/homehub-sentinel" if phys == "pi" else os.path.dirname(m["hub_data"])

    def _install_sentinel(self, cl, role: str, c: dict):
        """Write the sentinel's settings (the same ones tools/deploy_sentinel.py writes), the certificate and tokens, and start it."""
        M = {r: dict(PH(r)) for r in ("pi", "nas")}
        M[role].update(address=c["addr"], ssh_port=c["ssh_port"], ssh_user=c["user"])
        me, other = role, ("nas" if role == "pi" else "pi")
        mine, theirs = M[me], M[other]
        tok = self._tokens()
        img = f"{mc.registry_addr('pi')}/{IMG_TAG}"                      # the registry on the machine this ops runs on
        hub_env = {"HA_URL": "http://127.0.0.1:8123", "HA_TOKEN": tok["HA_TOKEN"], "HUB_TOKEN": tok["HUB_TOKEN"], "SENTINEL_TOKEN": tok["SENTINEL_TOKEN"],
                   "JOIN_SECRET": self.secret.decode(), "ENDPOINTS_TOKEN": os.environ.get("ENDPOINTS_TOKEN", ""), "MACHINES_FILE": "/data/machines.json",
                   "IDEA_TOKEN": os.environ.get("IDEA_TOKEN", ""), "IDEA_DL": os.environ.get("IDEA_DL", "")}
        env = {"ME": me, "SENTINEL_TOKEN": tok["SENTINEL_TOKEN"], "READ_TOKEN": tok["HUB_TOKEN"], "STATE_DIR": "/state", "TLS_DIR": "/tls",
               "PEER_URL": f"https://{theirs['address']}:{theirs['sentinel_port']}", "PEER_HUB_URL": f"https://{theirs['address']}:{theirs['hub_port']}",
               "PEER_TCP_HOST": theirs["address"], "PEER_TCP_PORT": str(theirs["ssh_port"]), "SELF_HUB_URL": f"https://127.0.0.1:{mine['hub_port']}",
               "SB_DIR": mine["data_root"] + "/homehub-standby", "DISKS": ";".join(f"{n}:{p}" for n, p in mine["disks"])}
        state = self._sent_dir(me, mine)
        if me == "pi":
            hubcfg = f"/home/{c['user']}/homehub/hub"
            env.update(ACT_DATA=mine["hub_data"], ACT_HA=mine["data_root"] + "/homeassistant/config", HUB_ENV="/hubcfg/hub.env",
                       MACHINES_FILE=mine["hub_data"] + "/machines.json")
            tls = mine["hub_data"] + "/tls"
            self._run_on(cl, f"mkdir -p '{hubcfg}' && umask 077 && cat > '{hubcfg}/hub.env'", root=True,
                         stdin=("\n".join(f"{k}={v}" for k, v in hub_env.items()) + "\n").encode())
            vols = f"-v {mine['data_root']}:{mine['data_root']} -v '{tls}':/tls:ro -v '{hubcfg}':/hubcfg:ro"
        else:
            A = state
            env.update(ACT_DATA=A + "/data", ACT_HA=A + "/ha-config", HUB_ENV=A + "/hub.env", MACHINES_FILE=A + "/data/machines.json",
                       HUB_IMAGE=f"{mine['address']}:{mine['registry_port']}/homehub-hub:latest",
                       HA_IMAGE=f"{mine['address']}:{mine['registry_port']}/homeassistant:2026.10.2")
            tls = A + "/tls"
            self._run_on(cl, f"mkdir -p '{A}/data' '{A}/ha-config' '{A}/ops' && umask 077 && cat > '{A}/hub.env'", root=True,
                         stdin=("\n".join(f"{k}={v}" for k, v in hub_env.items()) + "\n").encode())
            top = "/" + mine["data_root"].split("/")[1]
            vols = f"-v {top}:{top} -v /tmp/zfsv3:/tmp/zfsv3:ro -v '{tls}':/tls:ro"
        for f in ("server.crt", "server.key"):                          # the hub's certificate: the one this machine's hub uses
            hd = mc.CFG["machines"]["pi"]["hub_data"]            # the machine this ops runs on: its hub's certificate folder
            src = hd + "/tls/" + f if os.path.isfile(hd + "/tls/" + f) else os.path.dirname(hd) + "/tls/" + f
            data = open(src, "rb").read()
            self._run_on(cl, f"mkdir -p '{tls}' && umask 077 && cat > '{tls}/{f}'", root=True, stdin=data)
        self._run_on(cl, f"mkdir -p '{state}/state' && umask 077 && cat > '{state}/sentinel.env'", root=True,
                     stdin=("\n".join(f"{k}={v}" for k, v in env.items()) + "\n").encode())
        rc, out = self._run_on(cl, f"docker pull -q {img} >/dev/null && docker rm -f homehub-sentinel >/dev/null 2>&1; "
                                   f"docker run -d --name homehub-sentinel --restart unless-stopped --network host --memory 160m "
                                   f"--env-file '{state}/sentinel.env' -v /var/run/docker.sock:/var/run/docker.sock -v '{state}/state':/state {vols} {img}", root=True)
        if rc:
            raise RuntimeError("哨兵没有起来：" + out[-160:])

    def _tell_local(self, role: str, c: dict):
        """This machine's own sentinel reads the other machine's address from its settings file."""
        m = mc.CFG["machines"]["pi"]                                      # the machine this ops runs on
        path = self._sent_dir(mc.phys("pi"), m) + "/sentinel.env"
        upd = {"PEER_URL": f"https://{c['addr']}:{PH(role)['sentinel_port']}", "PEER_HUB_URL": f"https://{c['addr']}:{PH(role)['hub_port']}",
               "PEER_TCP_HOST": c["addr"], "PEER_TCP_PORT": str(c["ssh_port"])}
        lines = open(path, encoding="utf-8").read().splitlines()
        out = [f"{l.split('=', 1)[0]}={upd[l.split('=', 1)[0]]}" if l.split("=", 1)[0] in upd else l for l in lines]
        open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")

    def _restart_later(self):
        """After the job is reported done: restart the sentinel, hub and (last, it is this process) ops, so they read the new table."""
        import http.client
        import socket
        import threading

        class _U(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect("/var/run/docker.sock")

        def go():
            time.sleep(6)
            for name in ("homehub-sentinel", "homehub-hub", "homehub-ops"):
                try:
                    u = _U("localhost", timeout=60)
                    u.request("POST", f"/containers/{name}/restart?t=10")
                    u.getresponse().read()
                except Exception:
                    pass
        threading.Thread(target=go, daemon=True).start()
