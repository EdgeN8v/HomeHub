"""Put (or change) a service in HomeHub's address table, from any project, with no conversation needed:

    python D:\Github\HomeHub\tools\register_service.py NAME --check /health --expect ok \
        --lan http://192.168.31.61:8000 --lan http://192.168.31.131:8000 \
        --wan http://pi.example.com:8000 --wan http://nas.example.com:8000
    python D:\Github\HomeHub\tools\register_service.py --list        (shows the table)

NAME is the key the app looks up. --lan / --wan are given in the order the app should try them (the machine that runs the service first).
It uses the small ENDPOINTS_TOKEN from D:\Github\HomeHub\.env, which can only add or change service entries (not "homehub", not delete,
nothing else of the hub) - the hub token itself is not needed. The apps READ the table with no token at all: GET /api/endpoints."""
import argparse
import json
import os
import ssl
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = {}
for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    if "=" in ln and not ln.startswith("#"):
        k, v = ln.strip().split("=", 1)
        env[k] = v
ap = argparse.ArgumentParser()
ap.add_argument("name", nargs="?")
ap.add_argument("--check", default="/")
ap.add_argument("--expect", default="")
ap.add_argument("--lan", action="append", default=[])
ap.add_argument("--wan", action="append", default=[])
ap.add_argument("--list", action="store_true")
a = ap.parse_args()
sys.path.insert(0, os.path.join(ROOT, "hub", "app"))
if os.path.isfile(os.path.join(ROOT, "machines.json")):
    os.environ.setdefault("MACHINES_FILE", os.path.join(ROOT, "machines.json"))
import machines as mc  # noqa: E402

ctx = ssl.create_default_context()
ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE        # the hub's own certificate; the token travels on the home network only
hubs = [mc.hub_url("pi"), mc.hub_url("nas")]                      # whichever machine is main answers


def call(method, body=None):
    last = None
    for base in hubs:
        try:
            req = urllib.request.Request(base + "/api/endpoints", method=method, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": "Bearer " + env.get("ENDPOINTS_TOKEN", ""), "Content-Type": "application/json"})
            with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as ex:
            sys.exit(f"{base}: {ex.code} {ex.read().decode(errors='replace')[:200]}")
        except Exception as ex:
            last = ex
    sys.exit(f"hub not reachable: {last}")


if a.list or not a.name:
    t = call("GET")
    print("version", t["version"])
    for k, v in t["services"].items():
        print(f"  {k}: lan={v.get('lan')} wan={v.get('wan')} check={v.get('check')}")
    sys.exit()
if not a.lan and not a.wan:
    sys.exit("give at least one --lan or --wan address")
t = call("POST", {"services": {a.name: {"check": a.check, "expect": a.expect, "lan": a.lan, "wan": a.wan}}})
print("registered; table version", t["version"], "services:", ", ".join(t["services"]))
