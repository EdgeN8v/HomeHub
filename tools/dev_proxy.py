"""Local preview of the hub's pages for a desktop browser that won't accept the self-signed cert.

    python tools/dev_proxy.py            -> http://localhost:8300

Forwards every request to the hub with the token added, checking the hub's certificate against the
pin the app uses. Static files come from hub/app/static on this PC, so page edits show on reload
without deploying. Listens on localhost only.
"""
import base64
import hashlib
import http.server
import socketserver
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

ROOT = Path(__file__).resolve().parent.parent
ENV = dict(l.split("=", 1) for l in (ROOT / ".env").read_text(encoding="utf-8").splitlines() if "=" in l)
PROPS = dict(l.split("=", 1) for l in (ROOT / "android" / "secrets.properties").read_text(encoding="utf-8").splitlines() if "=" in l)
HOST, PORT = "192.168.31.61", 8200
STATIC = ROOT / "hub" / "app" / "static"
PAGES = {"/": "index.html", "/washer": "washer.html"}


def connect() -> http.client.HTTPSConnection:
    import http.client
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # identity is checked by the pin below instead
    conn = http.client.HTTPSConnection(HOST, PORT, context=ctx, timeout=60)
    conn.connect()
    der = conn.sock.getpeercert(binary_form=True)
    spki = x509.load_der_x509_certificate(der).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if "sha256/" + base64.b64encode(hashlib.sha256(spki).digest()).decode() != PROPS["pin"]:
        raise ssl.SSLError("hub certificate does not match the pin")
    return conn


class Proxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        path = self.path.split("?")[0]
        local = STATIC / PAGES[path] if path in PAGES else STATIC / path[len("/static/"):] if path.startswith("/static/") else None
        if local and local.is_file():
            body = local.read_bytes()
            ctype = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
                     ".json": "application/json"}.get(local.suffix, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or None
        conn = connect()
        headers = {"Authorization": "Bearer " + ENV["HUB_TOKEN"]}
        if self.headers.get("Content-Type"):
            headers["Content-Type"] = self.headers["Content-Type"]
        conn.request(self.command, self.path, body=body, headers=headers)
        r = conn.getresponse()
        stream = r.getheader("Content-Type", "").startswith("text/event-stream")
        self.send_response(r.status)
        self.send_header("Content-Type", r.getheader("Content-Type", "application/octet-stream"))
        if stream:
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while chunk := r.read1(4096):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
        else:
            data = r.read()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        conn.close()

    def log_message(self, fmt, *args):
        pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    print("HomeHub preview on http://localhost:8300")
    Server(("127.0.0.1", 8300), Proxy).serve_forever()
