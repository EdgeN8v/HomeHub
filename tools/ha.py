"""Small Home Assistant REST client for poking at HA from this PC.

Reads HA_URL / HA_TOKEN from ../.env (written by save_ha_token.ps1).

  python tools/ha.py states [filter]        entity_id, state, friendly name (filter matches id or name)
  python tools/ha.py state <entity_id>      full JSON of one entity
  python tools/ha.py call <domain.service> '<json data>'
"""
import json
import sys
import urllib.request
from pathlib import Path


def load_env():
    env = {}
    for line in (Path(__file__).resolve().parent.parent / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env["HA_URL"], env["HA_TOKEN"]


URL, TOKEN = load_env()


def api(method, path, data=None):
    req = urllib.request.Request(
        URL + "/api/" + path,
        method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "states"
    if cmd == "states":
        f = sys.argv[2].lower() if len(sys.argv) > 2 else ""
        for s in sorted(api("GET", "states"), key=lambda s: s["entity_id"]):
            name = s["attributes"].get("friendly_name", "")
            if f in s["entity_id"].lower() or f in name.lower():
                print(f'{s["entity_id"]}\t{s["state"]}\t{name}')
    elif cmd == "state":
        print(json.dumps(api("GET", "states/" + sys.argv[2]), ensure_ascii=False, indent=2))
    elif cmd == "call":
        domain, service = sys.argv[2].split(".", 1)
        data = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        print(json.dumps(api("POST", f"services/{domain}/{service}", data), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
