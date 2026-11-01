"""One-time secrets for the phone app <-> hub link. Re-running keeps what exists.

- hub/certs/server.{crt,key}: self-signed TLS cert for the hub. The app trusts exactly this public
  key (pinning), so no CA, no domain validation and no port 80 are needed.
- HUB_TOKEN in .env: the bearer token every request must carry.
- android/secrets.properties: token, pin and URLs baked into the app at build time.

All three files are gitignored. A new cert or token means the app must be rebuilt and reinstalled.
"""
import base64
import datetime
import hashlib
import ipaddress
import secrets
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parent.parent
CERT_DIR = ROOT / "hub" / "certs"


def _env(key: str, default: str | None = None) -> str | None:
    """A value from the environment or from .env (gitignored), so the public address isn't in the repo."""
    import os
    if os.environ.get(key):
        return os.environ[key]
    f = ROOT / ".env"
    for line in f.read_text(encoding="utf-8").splitlines() if f.exists() else []:
        k, _, v = line.partition("=")
        if k.strip() == key and v.strip():
            return v.strip()
    return default


PUBLIC_HOST = _env("PUBLIC_HOST")      # your DDNS name, e.g. home.example.com; put it in .env
if not PUBLIC_HOST:
    raise SystemExit("set PUBLIC_HOST (your public DDNS name) in .env or the environment first")
NAMES = ["192.168.31.61", PUBLIC_HOST, "localhost", "127.0.0.1"]
LAN_URL = "https://192.168.31.61:8200"
PUBLIC_URL = f"https://{PUBLIC_HOST}:{_env('PUBLIC_PORT', '8095')}"


def cert() -> x509.Certificate:
    crt, key_path = CERT_DIR / "server.crt", CERT_DIR / "server.key"
    if crt.exists():
        return x509.load_pem_x509_certificate(crt.read_bytes())
    key = ec.generate_private_key(ec.SECP256R1())
    sans = []
    for n in NAMES:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            sans.append(x509.DNSName(n))
    now = datetime.datetime.now(datetime.timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "HomeHub")])
    c = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=3650))
         .add_extension(x509.SubjectAlternativeName(sans), critical=False)
         .sign(key, hashes.SHA256()))
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    crt.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    print(f"wrote {crt}")
    return c


def pin(c: x509.Certificate) -> str:
    spki = c.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return "sha256/" + base64.b64encode(hashlib.sha256(spki).digest()).decode()


def token() -> str:
    env = ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    for line in lines:
        if line.startswith("HUB_TOKEN="):
            return line.split("=", 1)[1]
    t = secrets.token_urlsafe(32)
    env.write_text("\n".join(lines + [f"HUB_TOKEN={t}"]) + "\n", encoding="utf-8")
    print(f"added HUB_TOKEN to {env}")
    return t


def main():
    c, t = cert(), token()
    p = pin(c)
    props = ROOT / "android" / "secrets.properties"
    props.parent.mkdir(parents=True, exist_ok=True)
    props.write_text(f"lanUrl={LAN_URL}\npublicUrl={PUBLIC_URL}\ntoken={t}\npin={p}\n", encoding="utf-8")
    print(f"wrote {props}\npin {p}")


if __name__ == "__main__":
    main()
