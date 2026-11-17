"""What the machines are, in ONE place.

Until now the Orange Pi's address, the NAS's address, the folders on each and the ports were written into a dozen files. A machine can
be replaced (a new Pi) or the services may want to live on the other one, so the code asks this module instead of knowing "the Pi is
192.168.31.61". The defaults below are today's machines. To change them, put a JSON file at /machines.json (or $MACHINES_FILE) that
overrides any of these fields, e.g. {"machines": {"pi": {"address": "192.168.31.70"}}}; nothing else needs to be touched.

Two keys exist, "pi" and "nas": the names of the two roles (the first machine, the second machine), not a promise about the hardware.
`me()` is the machine the running process is on (HUB_MACHINE, or "nas" when the hub runs in its NAS role); `other()` is the other one."""
from __future__ import annotations

import copy
import json
import os

_DEFAULT = {
    "machines": {
        "pi": {"name": "香橙派", "address": "192.168.31.61", "domain": "", "ssh_port": 22, "ssh_user": "CUI", "sudo": False,
               "hub_port": 8200, "sentinel_port": 8210, "ops_port": 8220, "registry_port": 5000,
               "data_root": "/mnt/nas-sd", "apps_root": "/mnt/nas-sd/apps", "specs_dir": "/mnt/nas-sd/homehub/specs",
               "registry_dir": "/mnt/nas-sd/registry", "hub_data": "/mnt/nas-sd/homehub/data", "keys_dir": "/mnt/nas-sd/homehub/keys",
               "ops_data": "/mnt/nas-sd/homehub/ops", "native": {},
               "disks": [["固态硬盘", "/"], ["SD 卡", "/mnt/nas-sd"]]},
        "nas": {"name": "NAS", "address": "192.168.31.131", "domain": "", "ssh_port": 10000, "ssh_user": "", "sudo": True,
                "hub_port": 8095, "sentinel_port": 8210, "ops_port": 8220, "registry_port": 5000,
                "data_root": "/data_s001/docker_appstore", "apps_root": "/data_s001/docker_appstore/apps",
                "specs_dir": "/data_s001/docker_appstore/registry-mirror/specs", "registry_dir": "/data_s001/docker_appstore/registry-mirror",
                "hub_data": "/data_s001/docker_appstore/homehub-active/data", "keys_dir": "/data_s001/docker_appstore/homehub-active/keys",
                "ops_data": "/data_s001/docker_appstore/homehub-active/ops",
                # this machine's own small services (they belong to it, not to the pair): name -> folders they read. The other machine keeps a copy
                # of the container's configuration and these folders and starts them if this one stays silent.
                "native": {"claude-remote": ["/data_s001/docker_appstore/claude-remote"]},       # music-update was moved to the Pi
                "disks": [["硬盘 1", "/data_s001"], ["硬盘 2", "/data_s002"]]},
    },
}


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _load() -> dict:
    cfg = copy.deepcopy(_DEFAULT)
    try:
        with open(os.environ.get("MACHINES_FILE", "/machines.json"), encoding="utf-8-sig") as f:   # PowerShell writes a BOM
            _merge(cfg, json.load(f))
    except (OSError, ValueError):
        pass
    cfg["machines"]["nas"]["ssh_user"] = cfg["machines"]["nas"]["ssh_user"] or os.environ.get("NAS_USER", "")
    return cfg


CFG = _load()

# The ops engines were written as "this machine is the Pi (key "pi"), the other one is the NAS (key "nas")". Rather than rewrite them for
# every direction, an ops process that runs on the NAS gets the two entries SWAPPED: for it "pi" simply means "the machine I run on" and
# "nas" means "the other machine". PHYS maps such a role key back to the physical machine (for files and settings other programs read).
SWAPPED = os.environ.get("OPS_MACHINE") == "nas"
if SWAPPED:
    CFG["machines"]["pi"], CFG["machines"]["nas"] = CFG["machines"]["nas"], CFG["machines"]["pi"]


def phys(role: str) -> str:
    """The physical machine ("pi" or "nas") behind an engine role key."""
    return ("nas" if role == "pi" else "pi") if SWAPPED else role


def localize(obj):
    """Texts in the engines say "香橙派" for the machine the ops runs on and "NAS" for the other one. For an ops running on the NAS (roles
    swapped) those words are swapped in everything shown to the person; otherwise nothing changes."""
    if not SWAPPED:
        return obj
    if isinstance(obj, str):
        return (obj.replace("香橙派", "@@A").replace("NAS", "@@B").replace("SD 卡", "@@C")
                .replace("@@A", CFG["machines"]["pi"]["name"]).replace("@@B", CFG["machines"]["nas"]["name"])
                .replace("@@C", CFG["machines"]["pi"]["disks"][-1][0]))
    if isinstance(obj, list):
        return [localize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: localize(v) for k, v in obj.items()}
    return obj


def me() -> str:
    explicit = os.environ.get("HUB_MACHINE")
    if explicit in CFG["machines"]:
        return explicit
    return "nas" if os.environ.get("HUB_ROLE") == "nas" else "pi"


def other(key: str | None = None) -> str:
    return "nas" if (key or me()) == "pi" else "pi"


def get(key: str, field: str):
    return CFG["machines"][key][field]


def addr(key: str) -> str:
    return get(key, "address")


def registry_addr(key: str) -> str:
    return f"{addr(key)}:{get(key, 'registry_port')}"


def hub_url(key: str) -> str:
    return f"https://{addr(key)}:{get(key, 'hub_port')}"


def sentinel_url(key: str) -> str:
    return f"https://{addr(key)}:{get(key, 'sentinel_port')}"


def ops_url(key: str) -> str:
    return f"https://{addr(key)}:{get(key, 'ops_port')}"
