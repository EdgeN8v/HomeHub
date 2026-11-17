"""Make a machine ready to be one of the two HomeHub machines (a new Orange Pi, or the NAS taking the Pi's place).

    python tools/join_machine.py --role pi  [--address A] [--ssh-port P] [--user U] [--apply]
    python tools/join_machine.py --role nas [...]

Without --apply it only CHECKS and lists what is missing (nothing on the machine is changed). With --apply it fixes what it can:
the SSH key login, the folders the HomeHub programs expect, the registry certificate (so docker may pull from both registries),
and the shared key folder. Defaults for address / folders / user come from hub/app/machines.py (or $MACHINES_FILE).
A password is only needed the first time, to install the key: set JOIN_PASSWORD in the environment (never put it on the command line).
Afterwards it tells which steps are still done by the deploy scripts (sentinel, hub, data copy). Nothing secret is printed."""
import argparse
import os
import sys
import time

import paramiko

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hub", "app"))
import machines as mc  # noqa: E402

KEY = os.path.join(ROOT, ".keys", "homehub_ed25519")
ap = argparse.ArgumentParser()
ap.add_argument("--role", choices=("pi", "nas"), required=True)
ap.add_argument("--address")
ap.add_argument("--ssh-port", type=int)
ap.add_argument("--user")
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()

M = mc.CFG["machines"][a.role]
addr, port, user = a.address or M["address"], a.ssh_port or M["ssh_port"], a.user or M["ssh_user"]
if not user:
    sys.exit("no ssh user (set NAS_USER or --user)")
bad, todo = [], []


def say(ok, what, fix=None):
    print(("  ok   " if ok else "  MISSING ") + what)
    if not ok:
        todo.append(what)
        if fix:
            bad.append(fix)


c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
pub = open(KEY + ".pub", encoding="utf-8").read().strip()
print(f"{a.role}: {user}@{addr}:{port}  ({'apply' if a.apply else 'check only'})")
try:
    c.connect(addr, port=port, username=user, key_filename=KEY, timeout=15, look_for_keys=False, allow_agent=False)
    say(True, "key login")
except Exception:
    pw = os.environ.get("JOIN_PASSWORD")
    say(False, "key login")
    if not (a.apply and pw):
        sys.exit("  -> run with --apply and JOIN_PASSWORD set, to install the key")
    c.connect(addr, port=port, username=user, password=pw, timeout=15, look_for_keys=False, allow_agent=False)
    run0 = lambda s: c.exec_command(s)[1].channel.recv_exit_status()
    run0("mkdir -p ~/.ssh && chmod 700 ~/.ssh")
    run0(f"grep -qF '{pub}' ~/.ssh/authorized_keys 2>/dev/null || echo '{pub}' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys")
    c.close()
    c.connect(addr, port=port, username=user, key_filename=KEY, timeout=15, look_for_keys=False, allow_agent=False)
    print("  fixed: key installed")


def run(cmd: str, root: bool = False, stdin: bytes | None = None) -> tuple[int, str]:
    if root:
        q = cmd.replace("'", "'\\''")
        cmd = (f"if [ \"$(id -u)\" = 0 ]; then sh -c '{q}'; elif sudo -n true 2>/dev/null; then sudo -n sh -c '{q}'; "
               f"else docker run --rm -i -v /:/host alpine chroot /host sh -c '{q}'; fi")
    ch = c.get_transport().open_session()
    ch.exec_command(cmd)
    if stdin is not None:
        ch.sendall(stdin)
        ch.shutdown_write()
    out = (ch.makefile("r").read() + ch.makefile_stderr("r").read()).decode(errors="replace")
    return ch.recv_exit_status(), out.strip()


rc, out = run("docker version --format '{{.Server.Version}}'")
if rc:                                               # not in the docker group: sudo must work instead
    rc, out = run("sudo -n docker version --format '{{.Server.Version}}'")
say(rc == 0, f"docker running ({out})" if rc == 0 else "docker (install it; the user needs the docker group or passwordless sudo)")
if rc:
    sys.exit(1)
rc, out = run("free -m | awk '/^Mem|^内存/{print $2}'")
say(out.isdigit() and int(out) >= 1500, f"memory {out} MB (needs about 1.5 GB for hub + HomeAssistant + ops)")
rc, out = run(f"df -m --output=avail {mc.get(a.role, 'data_root')} | tail -1")
say(out.strip().isdigit() and int(out) >= 20000, f"free space under {mc.get(a.role, 'data_root')}: {out.strip()} MB")

# folders the programs expect
dirs = [M["data_root"], M["apps_root"], M["specs_dir"], M["registry_dir"] + "/data", M["registry_dir"] + "/certs", M["hub_data"], M["keys_dir"],
        M["ops_data"], M["data_root"] + "/homehub-standby", (M["data_root"] + "/homehub-sentinel/state" if a.role == "pi" else os.path.dirname(M["hub_data"]) + "/state")]
for d in dirs:
    rc, _ = run(f"test -d '{d}'", root=True)
    say(rc == 0, f"folder {d}")
    if rc and a.apply:
        run(f"mkdir -p '{d}'", root=True)
        print("  fixed: created")

# registry certificate: docker may only pull from our registries when it trusts their certificate
src_cert = None
for p in (os.path.join(ROOT, "hub", "certs", "registry.crt"),):
    if os.path.isfile(p):
        src_cert = open(p, "rb").read()
for peer in ("pi", "nas"):
    ra = f"{addr if peer == a.role else mc.addr(peer)}:{mc.get(peer, 'registry_port')}"
    path = f"/etc/docker/certs.d/{ra}/ca.crt"
    rc, _ = run(f"test -s '{path}'", root=True)
    say(rc == 0, f"docker trusts registry {ra}")
    if rc and a.apply:
        if src_cert is None:                         # take the certificate from the other machine's registry folder if the repo has none
            sys.exit("  no registry.crt in hub/certs: copy it there first (from the working machine's registry/certs folder)")
        run(f"mkdir -p '/etc/docker/certs.d/{ra}' && cat > '{path}'", root=True, stdin=src_cert)
        print("  fixed: certificate installed")

# the shared key (the two machines log into each other with it)
kd = M["keys_dir"] + "/homehub_ed25519"
rc, _ = run(f"test -s '{kd}'", root=True)
say(rc == 0, f"shared key in {M['keys_dir']}")
if rc and a.apply:
    run(f"mkdir -p '{M['keys_dir']}' && umask 077 && cat > '{kd}' && chmod 600 '{kd}'", root=True, stdin=open(KEY, "rb").read())
    run(f"cat > '{kd}.pub'", root=True, stdin=(pub + "\n").encode())
    print("  fixed: key copied")

print()
if todo and not a.apply:
    print(f"{len(todo)} thing(s) to do; run again with --apply.")
elif todo:
    print("some items were not fixed above; re-run to check.")
else:
    print("ready.")
print("Then (deploy scripts): python tools/deploy_sentinel.py  ->  tools/deploy_hub.ps1  -> data and images arrive by the normal 5-minute copy / the registry mirror.")
