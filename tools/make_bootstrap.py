"""Make the one file a brand-new machine needs, once, so that HomeHub can find it:

    python tools/make_bootstrap.py            -> bootstrap_new_machine.sh   (contains the family secret: keep it private, it is git-ignored)

Put the machine on the network with a normal Ubuntu/Debian installed and copy the file over (scp, or a USB stick), then run it there:

    sudo sh bootstrap_new_machine.sh

It installs docker if missing, and the small join agent as a system service. A minute later the phone's server page shows
"发现新机器". Until there is a ready-made machine image this is the only manual step."""
import base64
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = dict(l.strip().split("=", 1) for l in open(os.path.join(ROOT, ".env"), encoding="utf-8") if "=" in l and not l.startswith("#"))
agent = base64.b64encode(open(os.path.join(ROOT, "sentinel", "agent.py"), "rb").read()).decode()
script = f'''#!/bin/sh
# HomeHub: make this machine discoverable. Run as root (sudo sh bootstrap_new_machine.sh).
set -e
[ "$(id -u)" = 0 ] || {{ echo "run with sudo"; exit 1; }}
U="${{SUDO_USER:-$(logname 2>/dev/null || echo root)}}"
H="$(getent passwd "$U" | cut -d: -f6)"
command -v python3 >/dev/null || apt-get install -y python3
if ! command -v docker >/dev/null; then apt-get update && apt-get install -y docker.io; fi
systemctl enable --now docker
usermod -aG docker "$U" 2>/dev/null || true
mkdir -p /opt/homehub-agent "$H/.ssh" && chown "$U" "$H/.ssh" && chmod 700 "$H/.ssh"
echo '{agent}' | base64 -d > /opt/homehub-agent/agent.py
cat > /etc/systemd/system/homehub-agent.service <<EOF
[Unit]
Description=HomeHub join agent
After=network-online.target
[Service]
Environment=JOIN_SECRET={env["JOIN_SECRET"]}
Environment=AGENT_USER=$U
Environment=SSH_PORT=$(awk '/^Port /{{print $2}}' /etc/ssh/sshd_config | head -1)
Environment=SSH_DIR=$H/.ssh
ExecStart=/usr/bin/python3 /opt/homehub-agent/agent.py
Restart=always
[Install]
WantedBy=multi-user.target
EOF
sed -i 's/^Environment=SSH_PORT=$/Environment=SSH_PORT=22/' /etc/systemd/system/homehub-agent.service
systemctl daemon-reload
systemctl enable --now homehub-agent
echo "done: this machine now announces itself; open the HomeHub server page on the phone."
'''
out = os.path.join(ROOT, "bootstrap_new_machine.sh")
open(out, "w", encoding="utf-8", newline="\n").write(script)
print("wrote", out)
