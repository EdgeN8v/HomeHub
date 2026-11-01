"""Xiaomi Wi-Fi device setup over the device's own hotspot, as the Mijia app does it (11.8.707, read from
its code): hello -> token, then miIO.handshake (ECDH) and an encrypted miIO.config_router_safe that
carries the home Wi-Fi name and password. Pure protocol, no I/O: the caller moves the UDP packets (the
phone does, because only the phone can join the hotspot).

Packet = 0x2131 | length | 0 | device id | stamp | md5(header + token + payload) | AES-128-CBC(payload),
key = md5(token), iv = md5(key + token). Payload = JSON-RPC.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import hashes, padding, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HELLO = bytes.fromhex("21310020" + "ff" * 28)
PORT = 54321

# Mijia's ECCurve table (suite number -> curve) and the sign suites (1 = HMAC-SHA256, 2 = ECDSA-SHA256)
_CURVE_NAMES = {1: "SECP192R1", 2: "SECP224R1", 3: "SECP256R1", 4: "SECP384R1", 5: "SECP521R1",
                10: "SECP192K1", 11: "SECP224K1", 12: "SECP256K1"}
CURVES = {i: getattr(ec, n) for i, n in _CURVE_NAMES.items() if hasattr(ec, n)}   # what this cryptography build has


class ProvError(Exception):
    pass


def _aes_cbc(key: bytes, iv: bytes, data: bytes, encrypt: bool) -> bytes:
    c = Cipher(algorithms.AES(key), modes.CBC(iv))
    if encrypt:
        p = padding.PKCS7(128).padder()
        e = c.encryptor()
        return e.update(p.update(data) + p.finalize()) + e.finalize()
    d = c.decryptor()
    u = padding.PKCS7(128).unpadder()
    return u.update(d.update(data) + d.finalize()) + u.finalize()


def parse_hello(reply: bytes) -> tuple[int, int, bytes]:
    """(device id, stamp, token). An unconfigured device in setup mode shows its token here."""
    if len(reply) < 32 or reply[:2] != b"\x21\x31":
        raise ProvError("设备没有按小米协议回应")
    did, stamp = struct.unpack(">II", reply[8:16])
    return did, stamp, reply[16:32]


@dataclass
class Session:
    did: int
    token: bytes
    stamp0: int
    t0: float = field(default_factory=time.monotonic)
    msg_id: int = 0

    @property
    def key(self) -> bytes:
        return hashlib.md5(self.token).digest()

    @property
    def iv(self) -> bytes:
        return hashlib.md5(self.key + self.token).digest()

    def request(self, method: str, params) -> tuple[int, bytes]:
        """(message id, packet to send) for one JSON-RPC call."""
        self.msg_id += 1
        payload = json.dumps({"id": self.msg_id, "method": method, "params": params}, separators=(",", ":")).encode()
        enc = _aes_cbc(self.key, self.iv, payload, True)
        stamp = self.stamp0 + int(time.monotonic() - self.t0) + 1
        head = struct.pack(">HHIII", 0x2131, 32 + len(enc), 0, self.did, stamp)
        return self.msg_id, head + hashlib.md5(head + self.token + enc).digest() + enc

    def response(self, packet: bytes) -> dict:
        if len(packet) < 48 or packet[:2] != b"\x21\x31":
            raise ProvError("设备的回复不对")
        body = packet[32:]
        try:
            raw = _aes_cbc(self.key, self.iv, body, False).rstrip(b"\x00")
            return json.loads(raw)
        except Exception as e:   # noqa: BLE001
            raise ProvError(f"解不开设备的回复：{e}") from e


# ---- the key exchange ------------------------------------------------------------------------

@dataclass
class Ecdh:
    curve: int
    sign: int
    private: ec.EllipticCurvePrivateKey = None
    key: bytes = b""   # the 16-byte session key once the device's answer verified

    def start(self) -> dict:
        """params of the miIO.handshake type-2 call: our public key as 04 || X || Y."""
        self.private = ec.generate_private_key(CURVES[self.curve]())
        pub = self.private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return {"type": 2, "ecdh": {"curve_suite": self.curve, "sign_suite": self.sign,
                                    "public_key": base64.b64encode(pub).decode()}}

    def finish(self, token_hex: str, result: dict) -> bytes:
        """Check the device's answer and return the session key (HKDF-SHA256 of the shared secret,
        salt = the token as hex text, no info, 16 bytes). Raises if the device's signature is wrong."""
        e = result.get("ecdh") or {}
        peer_raw = base64.b64decode(e.get("public_key", ""))
        sign = base64.b64decode(e.get("sign", ""))
        peer = ec.EllipticCurvePublicKey.from_encoded_point(CURVES[self.curve](), peer_raw)
        shared = self.private.exchange(ec.ECDH(), peer)
        prk = hmac.new(token_hex.encode(), shared, hashlib.sha256).digest()
        key = hmac.new(prk, b"\x01", hashlib.sha256).digest()[:16]
        if self.sign == 1:
            if not hmac.compare_digest(hmac.new(key, peer_raw, hashlib.sha256).digest(), sign):
                raise ProvError("设备的签名对不上，密钥协商失败")
        else:
            peer.verify(sign, peer_raw, ec.ECDSA(hashes.SHA256()))
        self.key = key
        return key


def pick_suites(handshake1: dict) -> tuple[int, int]:
    """The first curve and sign suite from the device's list that we support."""
    e = (handshake1.get("result") or handshake1).get("ecdh") or {}
    curve = next((c for c in e.get("curve_suites", []) if c in CURVES), None)
    sign = next((s for s in e.get("sign_suites", []) if s in (1, 2)), None)
    if curve is None or sign is None:
        raise ProvError("设备支持的加密方式 HomeHub 还不支持")
    return curve, sign


def wifi_config(session_key: bytes, *, ssid: str, password: str, uid: int = 0, bind_key: str = "",
                country: str = "CN", tz: str = "Asia/Shanghai", gmt_offset: int = 28800, config_type: str = "app") -> dict:
    """params of miIO.config_router_safe: the Wi-Fi settings, encrypted with the session key and signed."""
    cfg = {"bind_key": bind_key, "config_type": config_type, "country_domain": country.lower(), "gmt_offset": gmt_offset,
           "passwd": password, "ssid": ssid, "tz": tz, "uid": uid, "wifi_config": {"cc": country.upper()}}
    enc = _aes_cbc(session_key, bytes(16), json.dumps(cfg, separators=(",", ":")).encode(), True)
    return {"data": base64.b64encode(enc).decode(), "sign": base64.b64encode(hmac.new(session_key, enc, hashlib.sha256).digest()).decode()}
