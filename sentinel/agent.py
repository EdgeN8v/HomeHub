"""The join agent: the one small program a machine runs BEFORE it belongs to HomeHub.

It does two things, nothing else:
  * shouts "I am here" on the home network every few seconds (UDP broadcast, port 8230) with who it is (machine id, host name, user,
    memory, architecture), signed with the family secret, so the hub can list it as a new machine;
  * accepts ONE command, "install this public key for logging in" (TCP, port 8231), only when it is signed with the same secret.
After that the hub logs in over SSH with that key and does everything else itself.

The secret (JOIN_SECRET) is baked into the machine image / the start command, so only machines made by the owner are listed and a
stranger on the network cannot make the agent accept a key. Stdlib only.

Start (the NAS or any machine with docker):
  docker run -d --name homehub-agent --restart unless-stopped --network host -e JOIN_SECRET=... -e AGENT_USER=<login user> \
    -e SSH_PORT=22 -v /etc/machine-id:/host-machine-id:ro -v /home/<login user>/.ssh:/ssh <sentinel image> python /srv/agent.py"""
from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import platform
import socket
import threading
import time

SECRET = os.environ["JOIN_SECRET"].encode()
USER = os.environ.get("AGENT_USER", "root")
SSH_PORT = int(os.environ.get("SSH_PORT", "22"))
SSH_DIR = os.environ.get("SSH_DIR", "/ssh")
BEACON_PORT, AGENT_PORT = 8230, 8231


def sign(*parts: str) -> str:
    return hmac.new(SECRET, "|".join(parts).encode(), hashlib.sha256).hexdigest()


def machine_id() -> str:
    for p in ("/host-machine-id", "/etc/machine-id"):
        try:
            return open(p, encoding="utf-8").read().strip()
        except OSError:
            pass
    return hashlib.sha1(socket.gethostname().encode()).hexdigest()[:32]


def mem_mb() -> int:
    try:
        for ln in open("/proc/meminfo", encoding="utf-8"):
            if ln.startswith("MemTotal:"):
                return int(ln.split()[1]) // 1024
    except OSError:
        pass
    return 0


def info() -> dict:
    return {"v": 1, "id": machine_id(), "host": os.environ.get("HOST_NAME") or socket.gethostname(), "user": USER, "ssh_port": SSH_PORT,
            "mem_mb": mem_mb(), "arch": platform.machine(), "agent_port": AGENT_PORT, "ts": int(time.time())}


def beacon_loop():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    while True:
        try:
            d = info()
            d["mac"] = sign(d["id"], str(d["ts"]), d["host"], d["user"])
            s.sendto(json.dumps(d).encode(), ("255.255.255.255", BEACON_PORT))
        except OSError:
            pass
        time.sleep(10)


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            key, ts = str(b["pubkey"]).strip(), str(b["ts"])
            if not hmac.compare_digest(str(b["mac"]), sign("claim", key, ts)) or abs(time.time() - float(ts)) > 300:
                return self._send(403, {"error": "bad signature"})
            if "\n" in key or not key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-")):
                return self._send(400, {"error": "not a public key"})
            os.makedirs(SSH_DIR, exist_ok=True)
            path = os.path.join(SSH_DIR, "authorized_keys")
            have = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
            if key not in have.splitlines():
                with open(path, "a", encoding="utf-8") as f:
                    f.write(("" if have.endswith("\n") or not have else "\n") + key + "\n")
                os.chmod(path, 0o600)
                try:                                    # as root: the file must belong to the user whose folder it is in
                    own = os.stat(SSH_DIR)
                    os.chown(path, own.st_uid, own.st_gid)
                except OSError:
                    pass
            self._send(200, {"ok": True})
        except Exception as ex:
            self._send(400, {"error": str(ex)[:100]})


if __name__ == "__main__":
    threading.Thread(target=beacon_loop, daemon=True).start()
    http.server.ThreadingHTTPServer(("0.0.0.0", AGENT_PORT), H).serve_forever()
