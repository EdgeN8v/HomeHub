"""Talking to a Xiaomi Wi-Fi device directly on the LAN (miIO/MIoT UDP protocol, port 54321), without Home Assistant,
without the Xiaomi cloud and without the Mijia app. All it needs is the device's address and its token
(tools/get_tokens.ps1 dumps them from the owner's own account into mi_tokens.json).

Protocol (the well-known miIO one):
  * hello: 32 bytes 21 31 00 20 + ff*28; the device answers with its id and a clock ("stamp")
  * a request is a 32-byte header (magic, length, id, stamp, md5 checksum) + AES-128-CBC encrypted JSON,
    key = md5(token), iv = md5(key + token). The stamp must be the device's own clock, +1 per request.
  * get_properties / set_properties / action are the MIoT calls: (siid, piid) and (siid, aiid) of the device spec.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

log = logging.getLogger("miot_local")
PORT = 54321
HELLO = bytes.fromhex("21310020") + b"\xff" * 28


class LocalError(Exception):
    pass


def _keys(token: bytes) -> tuple[bytes, bytes]:
    key = hashlib.md5(token).digest()
    return key, hashlib.md5(key + token).digest()


def encode(token: bytes, device_id: bytes, stamp: int, payload: dict) -> bytes:
    key, iv = _keys(token)
    body = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(json.dumps(payload, separators=(",", ":")).encode(), 16))
    head = b"\x21\x31" + (32 + len(body)).to_bytes(2, "big") + b"\0\0\0\0" + device_id + stamp.to_bytes(4, "big")
    return head + hashlib.md5(head + token + body).digest() + body


def decode(token: bytes, packet: bytes) -> dict:
    if len(packet) <= 32:
        raise LocalError("empty answer")
    key, iv = _keys(token)
    try:
        text = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(packet[32:]), 16).rstrip(b"\0")
        return json.loads(text)
    except (ValueError, KeyError) as e:
        raise LocalError("answer does not decrypt (wrong token?)") from e


class _Proto(asyncio.DatagramProtocol):
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()

    def datagram_received(self, data, addr):
        self.q.put_nowait(data)

    def error_received(self, exc):
        pass


class Device:
    """One device: ip + token + miot did. One request at a time (the devices do not like more)."""

    def __init__(self, ip: str, token: str, did: str, name: str = "", model: str = ""):
        self.ip, self.token, self.did = ip, bytes.fromhex(token), str(did)
        self.name, self.model = name, model
        self.home = 0                        # which home of the account it belongs to (0 = the first)
        self.lock = asyncio.Lock()
        self._id = 0
        self._dev: bytes | None = None
        self._offset = 0                     # device clock - our clock
        self._seen = 0.0

    async def _exchange(self, proto: _Proto, transport, packet: bytes, timeout: float = 3.0) -> bytes:
        while not proto.q.empty():
            proto.q.get_nowait()
        transport.sendto(packet, (self.ip, PORT))
        try:
            return await asyncio.wait_for(proto.q.get(), timeout)
        except asyncio.TimeoutError as e:
            raise LocalError(f"{self.ip} does not answer") from e

    async def call(self, method: str, params, tries: int = 3) -> list | dict:
        """UDP drops a packet now and then (the washer loses about one in eight): a lost handshake is simply asked again, a lost
        answer too, except for an action, which may already have run and is never sent twice."""
        async with self.lock:
            for n in range(tries):
                loop = asyncio.get_running_loop()
                transport, proto = await loop.create_datagram_endpoint(_Proto, remote_addr=None, local_addr=("0.0.0.0", 0))
                sent = False
                try:
                    hello = await self._exchange(proto, transport, HELLO)
                    self._dev = hello[8:12]
                    stamp = int.from_bytes(hello[12:16], "big")
                    self._id = self._id % 9000 + 1
                    packet = encode(self.token, self._dev, stamp + 1, {"id": self._id, "method": method, "params": params})
                    sent = True
                    answer = decode(self.token, await self._exchange(proto, transport, packet))
                except LocalError:
                    if n == tries - 1 or (sent and method == "action"):
                        raise
                    continue
                finally:
                    transport.close()
                if "error" in answer:
                    raise LocalError(f"{method}: {answer['error']}")
                return answer.get("result")

    # ---- MIoT calls
    async def get(self, props: list[tuple[int, int]]) -> dict[tuple[int, int], object]:
        r = await self.call("get_properties", [{"did": self.did, "siid": s, "piid": p} for s, p in props])
        return {(x["siid"], x["piid"]): x.get("value") for x in r if x.get("code") == 0}

    async def set(self, siid: int, piid: int, value) -> int:
        """Returns the device's code; 0 = accepted."""
        r = await self.call("set_properties", [{"did": self.did, "siid": siid, "piid": piid, "value": value}])
        return r[0].get("code", -1)

    async def action(self, siid: int, aiid: int, params: list | None = None) -> int:
        r = await self.call("action", {"did": self.did, "siid": siid, "aiid": aiid, "in": params or []})
        return r.get("code", -1)

    async def info(self) -> dict:
        return await self.call("miIO.info", [])


def load_tokens(path: str | Path) -> dict[str, Device]:
    """did -> Device for every device of the dump that has a local address and a token."""
    out: dict[str, Device] = {}
    for srv in json.loads(Path(path).read_text(encoding="utf-8")):
        for n, home in enumerate(srv.get("homes", [])):
            for d in home.get("devices", []):
                ip, token, did = d.get("localip"), d.get("token"), d.get("did")
                if ip and token and did and not did.startswith("blt."):
                    out[str(did)] = dev = Device(ip, token, str(did), d.get("name", ""), d.get("model", ""))
                    dev.home = n
    return out


async def probe(devs: dict[str, Device]) -> dict[str, bool]:
    """Which devices answer a local call right now (miIO.info)."""
    async def one(d: Device) -> bool:
        try:
            await d.info()
            return True
        except Exception:
            return False
    res = await asyncio.gather(*(one(d) for d in devs.values()))
    return dict(zip(devs, res))


class _Hello(asyncio.DatagramProtocol):
    def __init__(self, found: dict):
        self.found = found

    def datagram_received(self, data, addr):
        if len(data) >= 32:
            self.found[int.from_bytes(data[8:12], "big")] = addr[0]


async def discover(prefix: str, wait: float = 2.5) -> dict[str, str]:
    """did -> current ip of every miIO device answering on <prefix>.1-254 (the handshake carries the device id).
    Devices get a new address from the router's DHCP now and then; the address in the account dump is the one it had when it
    last told the Xiaomi cloud."""
    found: dict[int, str] = {}
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: _Hello(found), local_addr=("0.0.0.0", 0))
    try:
        for _ in range(2):
            for i in range(1, 255):
                transport.sendto(HELLO, (f"{prefix}.{i}", PORT))
            await asyncio.sleep(wait)
    finally:
        transport.close()
    return {str(k): v for k, v in found.items()}


def lan_prefix(devs: dict[str, Device]) -> str | None:
    """The /24 most devices sit in (the hub's own network)."""
    from collections import Counter
    c = Counter(".".join(d.ip.split(".")[:3]) for d in devs.values() if d.ip.startswith(("192.168.", "10.", "172.")))
    return c.most_common(1)[0][0] if c else None
