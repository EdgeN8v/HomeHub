"""Talks to the Xiaomi central hub (xiaomi.gateway.hub1) over its own local channel, without the Xiaomi cloud.

The hub serves the "米家自动化极客版" web page; that page speaks JSON-RPC 2.0 over a WebSocket at ws://<hub>/centrallinkws/.
This is a port of the page's own client (gateway.*.js on Xiaomi's CDN), which is public:

  * frames are binary: first byte = type (1 protocol list, 2 selected protocol, 3 session key exchange, 4 error, 5 data,
    32 / 33 EC-JPAKE round one / two)
  * login with the 6-digit login code from the Mijia app = an EC-JPAKE (secp256k1) password exchange; both sides get a
    32-byte secret: sha256(x of the shared point); key = first 16 bytes, salt = next 8
  * the client then sends its own random 24 bytes (key + salt) encrypted with that secret; the hub answers with its own
  * data frames: AES-128-GCM, iv = salt(8) + counter(4, little endian); payload = counter(4 LE) + ciphertext + tag(16);
    the plaintext is uint32-LE length + raw-deflate(JSON-RPC)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import struct
import zlib
from pathlib import Path

from Crypto.Cipher import AES


def gateway_ip(tokens: str | Path, model: str = "xiaomi.gateway.hub1") -> str | None:
    """The central hub's own LAN address, from the account dump (so the code need not carry it)."""
    try:
        for srv in json.loads(Path(tokens).read_text(encoding="utf-8")):
            for home in srv.get("homes", []):
                for d in home.get("devices", []):
                    if d.get("model") == model and d.get("localip"):
                        return d["localip"]
    except (OSError, ValueError):
        pass
    return None

# ---- secp256k1
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % P == 0:
        return None
    if a == b:
        lam = 3 * a[0] * a[0] * pow(2 * a[1], -1, P) % P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (lam * lam - a[0] - b[0]) % P
    return (x, (lam * (a[0] - x) - a[1]) % P)


def _mul(k, pt):
    k %= N
    out, add = None, pt
    while k:
        if k & 1:
            out = _add(out, add)
        add = _add(add, add)
        k >>= 1
    return out


def _neg(pt):
    return (pt[0], (-pt[1]) % P)


def _p(pt) -> bytes:                  # 66 bytes: 65, 04, x, y
    return bytes([65, 4]) + pt[0].to_bytes(32, "big") + pt[1].to_bytes(32, "big")


def _k(pt) -> bytes:                  # 69 bytes used in the hash: uint32 65, 04, x, y
    return struct.pack(">IB", 65, 4) + pt[0].to_bytes(32, "big") + pt[1].to_bytes(32, "big")


def _pt(b: bytes):
    if len(b) != 65 or b[0] != 4:
        raise ValueError("bad point")
    pt = (int.from_bytes(b[1:33], "big"), int.from_bytes(b[33:], "big"))
    if (pt[1] * pt[1] - pt[0] ** 3 - 7) % P:
        raise ValueError("point not on curve")
    return pt


def _rand() -> int:
    while True:
        k = int.from_bytes(os.urandom(32), "big")
        if 0 < k < N:
            return k


def _hash(g, v, x, ident: str) -> int:
    d = ident.encode()
    return int.from_bytes(hashlib.sha256(_k(g) + _k(v) + _k(x) + struct.pack(">I", len(d)) + d).digest(), "big") % N


def _zkp(g, x_pub, x_priv, ident):
    v = _rand()
    V = _mul(v, g)
    h = _hash(g, V, x_pub, ident)
    return V, (v - h * x_priv) % N


def _zkp_bytes(z) -> bytes:           # 99 bytes
    V, r = z
    return _p(V) + bytes([32]) + r.to_bytes(32, "big")


def _read_zkp(b: bytes):
    n = b[0]
    V = _pt(b[1:1 + n])
    m = b[1 + n]
    return V, int.from_bytes(b[2 + n:2 + n + m], "big")


def _zkp_len(b: bytes) -> int:
    return 1 + b[0] + 1 + b[1 + b[0]]


def _verify(g, x, V, r, ident) -> bool:
    return _add(_mul(_hash(g, V, x, ident), x), _mul(r, g)) == V


class Jpake:
    """EC-JPAKE exactly as the hub page does it (client role); role="server" exists so the code can test itself."""

    def __init__(self, secret: str, role: str = "client"):
        self.role, self.peer = role, "server" if role == "client" else "client"
        self.s = int.from_bytes(secret.encode(), "big")

    def round_one(self) -> bytes:
        self.x1, self.x2 = _rand(), _rand()
        self.X1, self.X2 = _mul(self.x1, G), _mul(self.x2, G)
        z1, z2 = _zkp(G, self.X1, self.x1, self.role), _zkp(G, self.X2, self.x2, self.role)
        return _p(self.X1) + _zkp_bytes(z1) + _p(self.X2) + _zkp_bytes(z2)

    def read_round_one(self, b: bytes):
        i = 0
        pts = []
        for _ in range(2):
            n = b[i]
            X = _pt(b[i + 1:i + 1 + n])
            i += 1 + n
            ln = _zkp_len(b[i:])
            V, r = _read_zkp(b[i:i + ln])
            i += ln
            if not _verify(G, X, V, r, self.peer):
                raise ValueError("EC-JPAKE round one failed")
            pts.append(X)
        self.X3, self.X4 = pts

    def round_two(self) -> bytes:
        g = _add(_add(self.X1, self.X3), self.X4)
        s = self.x2 * self.s % N
        A = _mul(s, g)
        out = _p(A) + _zkp_bytes(_zkp(g, A, s, self.role))
        return bytes([3, 0, 22]) + out if self.role == "server" else out

    def read_round_two(self, b: bytes) -> bytes:
        g = _add(_add(self.X1, self.X2), self.X3)
        i = 3 if self.role == "client" else 0
        n = b[i]
        B = _pt(b[i + 1:i + 1 + n])
        i += 1 + n
        V, r = _read_zkp(b[i:i + _zkp_len(b[i:])])
        if not _verify(g, B, V, r, self.peer):
            raise ValueError("EC-JPAKE round two failed (wrong login code?)")
        m = self.x2 * self.s % N
        shared = _mul(self.x2, _add(B, _neg(_mul(m, self.X4))))
        return hashlib.sha256(shared[0].to_bytes(32, "big")).digest()


class Cipher:
    def __init__(self, key: bytes, salt: bytes):
        assert len(key) == 16 and len(salt) == 8
        self.key, self.salt, self.n, self.last = key, salt, 1, 0

    def encrypt(self, data: bytes) -> bytes:
        c = self.n
        self.n += 1
        ct, tag = AES.new(self.key, AES.MODE_GCM, nonce=self.salt + struct.pack("<I", c)).encrypt_and_digest(data)
        return struct.pack("<I", c) + ct + tag

    def decrypt(self, data: bytes) -> bytes:
        c = struct.unpack("<I", data[:4])[0]
        if c <= self.last:
            raise ValueError("replayed frame")
        self.last = c
        return AES.new(self.key, AES.MODE_GCM, nonce=self.salt + struct.pack("<I", c)).decrypt_and_verify(data[4:-16], data[-16:])


def pack(obj) -> bytes:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    z = zlib.compressobj(wbits=-15)
    return struct.pack("<I", len(raw)) + z.compress(raw) + z.flush()


def unpack(b: bytes):
    return json.loads(zlib.decompress(b[4:], -15))


class HubLink:
    """async with HubLink(ip, code) as hub:  await hub.call("getDevList")"""

    def __init__(self, ip: str, code: str):
        self.ip, self.code = ip, str(code)
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.pushes: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self):
        import aiohttp
        self._session = aiohttp.ClientSession()
        self.ws = await self._session.ws_connect(f"ws://{self.ip}/centrallinkws/", heartbeat=None, max_msg_size=0)
        try:
            await self._login()
        except BaseException:
            await self.ws.close()
            await self._session.close()
            raise
        self._task = asyncio.create_task(self._reader())
        return self

    async def __aexit__(self, *a):
        self._task.cancel()
        await self.ws.close()
        await self._session.close()

    async def _recv(self, want: int, timeout: float = 10) -> bytes:
        msg = await asyncio.wait_for(self.ws.receive(), timeout)
        if msg.type != 2:           # aiohttp.WSMsgType.BINARY
            raise ConnectionError(f"hub closed the channel ({msg.type})")
        if msg.data[0] == 4:
            raise PermissionError("hub refused the login (wrong or expired login code)")
        if msg.data[0] != want:
            raise ConnectionError(f"unexpected frame {msg.data[0]}, wanted {want}")
        return msg.data[1:]

    async def _login(self):
        await self.ws.send_bytes(b"\x01" + json.dumps(["passcode"]).encode())
        sel = json.loads(await self._recv(2))
        if sel.get("protocol") != "passcode":
            raise ConnectionError(f"hub chose {sel}")
        jp = Jpake(self.code)
        await self.ws.send_bytes(b"\x20" + jp.round_one())
        jp.read_round_one(await self._recv(32))
        await self.ws.send_bytes(b"\x21" + jp.round_two())
        secret = jp.read_round_two(await self._recv(33))
        tmp = Cipher(secret[:16], secret[16:24])
        mine = os.urandom(24)
        self._out = Cipher(mine[:16], mine[16:])
        await self.ws.send_bytes(b"\x03" + tmp.encrypt(mine))
        theirs = tmp.decrypt(await self._recv(3))
        self._in = Cipher(theirs[:16], theirs[16:24])

    async def _reader(self):
        async for msg in self.ws:
            if msg.type != 2 or msg.data[0] != 5:
                continue
            m = unpack(self._in.decrypt(msg.data[1:]))
            if "result" in m or "error" in m:
                f = self._pending.pop(m.get("id"), None)
                if f and not f.done():
                    f.set_exception(RuntimeError(m["error"])) if "error" in m else f.set_result(m["result"])
            else:
                await self.pushes.put(m)

    async def call(self, method: str, params=None, timeout: float = 10):
        i, self._id = self._id, self._id + 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[i] = fut
        await self.ws.send_bytes(b"\x05" + self._out.encrypt(pack({"jsonrpc": "2.0", "id": i, "method": f"/api/{method}", "params": params if params is not None else {}})))
        return await asyncio.wait_for(fut, timeout)
