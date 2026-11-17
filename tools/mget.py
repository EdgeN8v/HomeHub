"""Print one field of the machine table:  python tools/mget.py pi address   ->  192.168.31.61
Reads hub/app/machines.py plus the optional overrides in <repo>/machines.json, so scripts never hard-code an address."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
f = os.path.join(ROOT, "machines.json")
if os.path.isfile(f):
    os.environ.setdefault("MACHINES_FILE", f)
sys.path.insert(0, os.path.join(ROOT, "hub", "app"))
import machines as mc  # noqa: E402

if __name__ == "__main__":
    v = mc.get(sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else None
    print(v if v is not None else "")
