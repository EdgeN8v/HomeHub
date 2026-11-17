"""Start an ops service on the NAS (the same image as the Pi's, run with OPS_MACHINE=nas, which swaps the roles inside it: "the machine I
run on" is the NAS, "the other one" is the Pi, reached over SSH with the shared key). Used to try it out next to the real one:

    python tools/deploy_ops_nas.py            -> container homehub-ops-test on port 8221, no nightly jobs (OPS_PASSIVE)
    python tools/deploy_ops_nas.py --remove   -> take it away again

Needs in .env: NAS_USER, NAS_PASSWORD. The NAS must have the hub image (192.168.31.131:5000/homehub-hub) and the key / hub.env that the
sentinel's deployment put in homehub-active/."""
import os
import sys

import paramiko

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = {}
for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    if "=" in ln and not ln.startswith("#"):
        k, v = ln.strip().split("=", 1)
        env[k] = v
PW, A = env["NAS_PASSWORD"], "/data_s001/docker_appstore/homehub-active"
NAME, PORT = "homehub-ops-test", "8221"

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.31.131", port=int(env.get("NAS_PORT", "10000")), username=env["NAS_USER"], password=PW, timeout=15)


def sudo(cmd: str) -> str:
    ch = c.get_transport().open_session()
    ch.exec_command(f"echo {PW} | sudo -S -p '' sh -c '{cmd}'")
    return (ch.makefile("r").read() + ch.makefile_stderr("r").read()).decode(errors="replace").replace(PW, "***")


if "--remove" in sys.argv:
    print(sudo(f"docker rm -f {NAME}; rm -rf {A}/ops-test; echo removed"))
    sys.exit()

print(sudo(f"mkdir -p {A}/ops-test && docker pull -q 192.168.31.131:5000/homehub-hub:latest | tail -1"))
print(sudo(
    f"docker rm -f {NAME} >/dev/null 2>&1; docker run -d --name {NAME} --restart no --network host --memory 400m "
    f"--env-file {A}/hub.env -e OPS_MACHINE=nas -e OPS_PORT={PORT} -e OPS_PASSIVE=1 "
    f"-v {A}/ops-test:/data -v /var/run/docker.sock:/var/run/docker.sock -v /data_s001:/data_s001 -v /data_s002:/data_s002 "
    f"-v {A}/keys:/keys:ro -v /data_s001/docker_appstore/registry-mirror/data:/registry-data:ro "
    f"192.168.31.131:5000/homehub-hub:latest python -m app.ops && sleep 8 && docker logs --tail 5 {NAME} 2>&1 | cut -c1-160"))
