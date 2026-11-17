"""Fetch a device's MIoT spec (public, from miot-spec.org) and write it in the shape local_bridge reads.

  python tools/fetch_spec.py <urn> <out-file>

The urn is in the hub's device list ("urn" field) or in the integration's cache. The value lists get the Chinese wording
(zh_cn) as description_trans, which is what Home Assistant shows for select options.
"""
import json
import sys
import urllib.parse
import urllib.request


def get(url: str):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def main(urn: str, out: str):
    q = urllib.parse.quote(urn, safe="")
    inst = get(f"https://miot-spec.org/miot-spec-v2/instance?type={q}")
    try:
        tr = get(f"https://miot-spec.org/instance/v2/multiLanguage?urn={q}")["data"].get("zh_cn", {})
    except Exception:
        tr = {}
    services = []
    for s in inst["services"]:
        props = []
        for p in s.get("properties", []):
            key = f"service:{s['iid']:03d}:property:{p['iid']:03d}"
            vl = []
            for n, v in enumerate(p.get("value-list") or []):
                vl.append({"value": v["value"], "description": v.get("description", ""),
                           "description_trans": tr.get(f"{key}:valuelist:{n:03d}") or v.get("description", "")})
            item = {"iid": p["iid"], "type": p["type"], "name": p["type"].split(":")[3], "description": p.get("description", ""),
                    "format": p["format"], "access": p.get("access", []), "unit": p.get("unit"), "value_list": vl}
            if "value-range" in p:
                lo, hi, st = p["value-range"]
                item["value_range"] = {"min": lo, "max": hi, "step": st}
            props.append(item)
        services.append({"iid": s["iid"], "type": s["type"], "description": s.get("description", ""), "properties": props})
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"urn": inst["type"], "description": inst.get("description", ""), "services": services}, f, ensure_ascii=False)
    print(out, sum(len(s["properties"]) for s in services), "properties")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
