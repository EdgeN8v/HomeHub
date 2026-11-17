"""Where is a service, and where do its files live? Always read live from the machines, so it is never out of date.

  python D:\\Github\\HomeHub\\tools\\info.py              one line per service
  python D:\\Github\\HomeHub\\tools\\info.py ledger       everything about one service (container, ports, folders on the host, update folder, addresses)

Any conversation can run this; nothing needs to be relayed by hand. Needs ssh to the Pi (key login) and, for services that
currently run on the NAS, NAS_USER / NAS_PASSWORD in HomeHub\\.env."""
import json
import os
import subprocess
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hub", "app"))
import machines as mc  # noqa: E402

FMT = '{"name":"{{.Name}}","state":"{{.State.Status}}","labels":{{json .Config.Labels}},"mounts":{{json .Mounts}},"ports":{{json .NetworkSettings.Ports}},"env":{{json .Config.Env}}}'
CMD = "for n in $(docker ps -a --format '{{.Names}}'); do docker inspect --format '" + FMT + "' $n; done"


def env_file() -> dict:
    out = {}
    try:
        for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
            if "=" in ln and not ln.startswith("#"):
                k, v = ln.strip().split("=", 1)
                out[k] = v
    except OSError:
        pass
    return out


def from_pi() -> list:
    pi = mc.CFG["machines"]["pi"]
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-p", str(pi["ssh_port"]), f"{pi['ssh_user']}@{pi['address']}", CMD],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    return [dict(json.loads(ln), on="pi") for ln in r.stdout.splitlines() if ln.startswith("{")]


def from_nas() -> list:
    e = env_file()
    if not e.get("NAS_USER"):
        return []
    try:
        import paramiko
        nas = mc.CFG["machines"]["nas"]
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(nas["address"], port=nas["ssh_port"], username=e["NAS_USER"], password=e["NAS_PASSWORD"], timeout=10)
        inner = CMD.replace("'", "'\''")
        _, o, _ = c.exec_command("echo '%s' | sudo -S sh -c '%s' 2>/dev/null" % (e["NAS_PASSWORD"], inner))
        return [dict(json.loads(ln), on="nas") for ln in o.read().decode().splitlines() if ln.startswith("{")]
    except Exception as ex:
        print("(NAS not read:", str(ex)[:80], ")", file=sys.stderr)
        return []


def table() -> dict:
    pi = mc.CFG["machines"]["pi"]["address"]
    try:
        return json.load(urllib.request.urlopen(f"http://{pi}:8099/endpoints.json", timeout=6))["services"]
    except Exception:
        return {}


def main():
    want = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    t = table()
    cs = from_pi() + from_nas()
    cs = [c for c in cs if c["state"] == "running" or not any(x["name"].lstrip("/") == c["name"].lstrip("/") and x["state"] == "running" for x in cs)]
    if not want:
        for n, e in sorted(t.items()):
            print(f"{n:14} {e['lan'][0] if e.get('lan') else ''}")
        print("\n(python info.py <服务名> 看某个服务的详情)")
        return
    hit = [c for c in cs if want in c["name"].lower() or want in (c["labels"] or {}).get("homehub.service", "").lower()]
    if not hit:
        sys.exit("没找到这个服务（先不带参数运行，看看有哪些）")
    for c in hit:
        print(f"\n== {c['name'].lstrip('/')}  运行在 {c['on']}  状态 {c['state']}")
        ports = [f"{b['HostPort']}->{p}" for p, bs in (c["ports"] or {}).items() for b in (bs or [])]
        print("端口:", ", ".join(ports) or "（无）")
        for m in c["mounts"] or []:
            tag = "   <== 更新文件夹" if "update" in m["Destination"].lower() else ""
            print(f"文件夹: {m.get('Source', m.get('Name'))}  ->  容器内 {m['Destination']}{tag}")
        for ev in c["env"] or []:
            k = ev.split("=", 1)[0]
            if any(w in k.upper() for w in ("UPDATE", "DIR", "PATH", "PORT")) and not any(w in k.upper() for w in ("TOKEN", "SECRET", "PASSWORD", "KEY")):
                print("环境变量:", ev)
    for n, e in t.items():
        if want in n.lower():
            print(f"\n地址表 {n}: 内网 {e.get('lan')}  外网 {e.get('wan')}  检测 {e.get('check')}")


main()
