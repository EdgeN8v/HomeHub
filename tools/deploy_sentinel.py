"""Deploy the sentinel (sentinel/) to both machines: build on the Pi, ship the image to the NAS, write each machine's settings,
start the containers. Run from the repo root:  python tools/deploy_sentinel.py

Needs in .env: SENTINEL_TOKEN (shared by both sentinels and the hubs), HA_TOKEN, HUB_TOKEN, NAS_USER, NAS_PASSWORD.
Nothing is printed that contains a secret."""
import os
import subprocess
import sys

import paramiko

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = {}
for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    if "=" in ln and not ln.startswith("#"):
        k, v = ln.strip().split("=", 1)
        env[k] = v
os.environ.setdefault("NAS_USER", env.get("NAS_USER", ""))
if os.path.isfile(os.path.join(ROOT, "machines.json")):          # overrides for a replaced machine / changed address
    os.environ.setdefault("MACHINES_FILE", os.path.join(ROOT, "machines.json"))
sys.path.insert(0, os.path.join(ROOT, "hub", "app"))
import machines as mc  # noqa: E402

PM, NM = mc.CFG["machines"]["pi"], mc.CFG["machines"]["nas"]
PI, NAS_HOST = PM["address"], NM["address"]
IMG = f"{PI}:{PM['registry_port']}/homehub-sentinel:1"
for k in ("SENTINEL_TOKEN", "HA_TOKEN", "HUB_TOKEN", "NAS_USER", "NAS_PASSWORD"):
    if not env.get(k):
        sys.exit(f"{k} missing in .env")
TOKEN, PW = env["SENTINEL_TOKEN"], env["NAS_PASSWORD"]
SSH = ["ssh", "-o", "BatchMode=yes", "-p", str(PM["ssh_port"]), f"{PM['ssh_user']}@{PI}"]


def pi(cmd: str, **kw):
    return subprocess.run(SSH + [cmd], check=True, **kw)


nas = paramiko.SSHClient()
nas.set_missing_host_key_policy(paramiko.AutoAddPolicy())
nas.connect(NAS_HOST, port=NM["ssh_port"], username=NM["ssh_user"], password=PW, timeout=15)


def on_nas(cmd: str, stdin: bytes | None = None) -> str:
    ch = nas.get_transport().open_session()
    ch.exec_command(f"sh -c 'echo {PW} | sudo -S -p \"\" -v && {cmd}'")
    if stdin is not None:
        ch.sendall(stdin)
        ch.shutdown_write()
    out = (ch.makefile("r").read() + ch.makefile_stderr("r").read()).decode(errors="replace")
    if ch.recv_exit_status():
        raise SystemExit("NAS: " + out.replace(PW, "***")[-300:])
    return out.replace(PW, "***")


def envfile(d: dict) -> bytes:
    return ("\n".join(f"{k}={v}" for k, v in d.items()) + "\n").encode()


def common(me: str) -> dict:
    return {"ME": me, "SENTINEL_TOKEN": TOKEN, "READ_TOKEN": env["HUB_TOKEN"], "STATE_DIR": "/state", "TLS_DIR": "/tls"}


# ---- build on the Pi, keep in the Pi's registry, ship to the NAS
pi("mkdir -p ~/homehub/sentinel/src")
subprocess.run(["scp", "-o", "BatchMode=yes", "-P", str(PM["ssh_port"]), os.path.join(ROOT, "sentinel", "sentinel.py"), os.path.join(ROOT, "sentinel", "recovery.py"), os.path.join(ROOT, "sentinel", "agent.py"), os.path.join(ROOT, "hub", "app", "machines.py"), os.path.join(ROOT, "sentinel", "Dockerfile"),
                f"{PM['ssh_user']}@{PI}:homehub/sentinel/src/"], check=True)
pi(f"cd ~/homehub/sentinel/src && docker build -q -t {IMG} . && docker push -q {IMG} >/dev/null")
print("built", IMG)
save = subprocess.Popen(SSH + [f"docker save {IMG}"], stdout=subprocess.PIPE)
ch = nas.get_transport().open_session()
ch.exec_command(f"sh -c 'echo {PW} | sudo -S -p \"\" -v && sudo -n docker load'")
while chunk := save.stdout.read(1 << 20):
    ch.sendall(chunk)
ch.shutdown_write()
print("NAS load:", (ch.makefile("r").read().decode(errors="replace").strip().splitlines() or ["?"])[-1], ch.recv_exit_status())

# ---- Pi
PD = PM["data_root"]
STATE = PD + "/homehub-sentinel"
pi(f"mkdir -p {STATE}/state {PD}/homehub-standby")
MF = os.path.join(ROOT, "machines.json")
if os.path.isfile(MF):                                   # the address overrides travel with the data folders (hub and ops read /data/machines.json)
    for d in (PM["hub_data"], PM["ops_data"]):
        pi(f"docker run --rm -i -v {d}:/d alpine sh -c 'mkdir -p /d && cat > /d/machines.json'", input=open(MF, "rb").read())   # folders may belong to root (a container made them)
pi_env = {**common("pi"), "PEER_URL": f"https://{NAS_HOST}:{NM['sentinel_port']}", "PEER_HUB_URL": f"https://{NAS_HOST}:{NM['hub_port']}",
          "PEER_TCP_HOST": NAS_HOST, "PEER_TCP_PORT": str(NM["ssh_port"]), "SELF_HUB_URL": f"https://127.0.0.1:{PM['hub_port']}",
          "SB_DIR": PD + "/homehub-standby", "ACT_DATA": PM["hub_data"], "ACT_HA": PD + "/homeassistant/config",
          "HUB_ENV": "/hubcfg/hub.env", "DISKS": ";".join(f"{n}:{pth}" for n, pth in PM["disks"]),
          "MACHINES_FILE": PM["hub_data"] + "/machines.json"}
subprocess.run(SSH + [f"umask 077 && cat > {STATE}/sentinel.env"], input=envfile(pi_env), check=True)
pi("docker rm -f homehub-sentinel >/dev/null 2>&1; docker run -d --name homehub-sentinel --restart unless-stopped --network host --memory 160m "
   f"--env-file {STATE}/sentinel.env -v /var/run/docker.sock:/var/run/docker.sock -v {PD}:{PD} "
   f"-v {STATE}/state:/state -v {PM['hub_data']}/tls:/tls:ro -v /home/{PM['ssh_user']}/homehub/hub:/hubcfg:ro {IMG} >/dev/null && echo pi-sentinel-started")

# ---- NAS
A = os.path.dirname(NM["hub_data"])                      # .../homehub-active
ND = NM["data_root"]
on_nas(f"sudo -n mkdir -p {A}/state {A}/tls {A}/data {A}/ops {A}/ha-config {ND}/homehub-standby")
if os.path.isfile(MF):
    for d in (NM["hub_data"], NM["ops_data"]):
        on_nas(f"sudo -n tee {d}/machines.json >/dev/null", open(MF, "rb").read())
# the certificate: the standby copy of the hub's data holds the one the Pi's hub uses (an empty standby = nothing to copy yet)
on_nas(f"sudo -n cp {ND}/homehub-standby/data/tls/server.crt {ND}/homehub-standby/data/tls/server.key {A}/tls/ && sudo -n chmod 600 {A}/tls/server.key")
hub_env = {"HA_URL": "http://127.0.0.1:8123", "HA_TOKEN": env["HA_TOKEN"], "HUB_TOKEN": env["HUB_TOKEN"],
           "SENTINEL_URL": f"https://127.0.0.1:{NM['sentinel_port']}", "SENTINEL_TOKEN": TOKEN, "MACHINES_FILE": "/data/machines.json", "JOIN_SECRET": env.get("JOIN_SECRET", ""), "ENDPOINTS_TOKEN": env.get("ENDPOINTS_TOKEN", ""),
           "IDEA_TOKEN": env.get("IDEA_TOKEN", ""), "IDEA_DL": env.get("IDEA_DL", "")}
on_nas(f"umask 077 && sudo -n tee {A}/hub.env >/dev/null", envfile(hub_env))
nas_env = {**common("nas"), "PEER_URL": f"https://{PI}:{PM['sentinel_port']}", "PEER_HUB_URL": f"https://{PI}:{PM['hub_port']}", "PEER_TCP_HOST": PI,
           "PEER_TCP_PORT": str(PM["ssh_port"]), "SELF_HUB_URL": f"https://127.0.0.1:{NM['hub_port']}", "SB_DIR": ND + "/homehub-standby",
           "ACT_DATA": f"{A}/data", "ACT_HA": f"{A}/ha-config", "HUB_ENV": f"{A}/hub.env",
           "DISKS": ";".join(f"{n}:{pth}" for n, pth in NM["disks"]), "HUB_IMAGE": f"{NAS_HOST}:{NM['registry_port']}/homehub-hub:latest",
           "HA_IMAGE": f"{NAS_HOST}:{NM['registry_port']}/homeassistant:2026.10.2", "MACHINES_FILE": f"{A}/data/machines.json"}
on_nas(f"umask 077 && sudo -n tee {A}/sentinel.env >/dev/null", envfile(nas_env))
on_nas(f"sudo -n docker rm -f homehub-sentinel >/dev/null 2>&1; sudo -n docker run -d --name homehub-sentinel --restart unless-stopped --network host --memory 160m "
       f"--env-file {A}/sentinel.env -v /var/run/docker.sock:/var/run/docker.sock -v /data_s001:/data_s001 -v /tmp/zfsv3:/tmp/zfsv3:ro -v {A}/state:/state -v {A}/tls:/tls:ro "
       f"{IMG} >/dev/null && echo nas-sentinel-started")
print("done")
