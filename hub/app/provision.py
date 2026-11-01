"""Adding a Xiaomi Wi-Fi device from the phone: the page moves UDP packets between the device's setup
hotspot (which only the phone can join) and this service, which runs the protocol (miio_prov).

One setup = start() -> a few step()s -> find(). All state is in memory and short-lived; the home Wi-Fi
password only ever exists inside one request here, it is never logged or stored.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from . import miio_prov as mp

log = logging.getLogger("provision")
SESSION_TTL = 600
BROADCASTS = ("255.255.255.255", "192.168.31.255")


class Provisioner:
    def __init__(self, store):
        self.store = store
        self.sessions: dict[str, dict] = {}

    # ---- the protocol, one packet at a time ----------------------------------------------------
    def start(self) -> dict:
        self._expire()
        sid = uuid.uuid4().hex[:12]
        self.sessions[sid] = {"stage": "hello", "t": time.monotonic()}
        return {"session": sid, "send": {"hex": mp.HELLO.hex()}}

    def step(self, sid: str, reply_hex: str | None = None, wifi: dict | None = None) -> dict:
        s = self.sessions.get(sid)
        if not s:
            return {"error": "这次配网已经过期了，重新开始"}
        s["t"] = time.monotonic()
        try:
            return self._step(s, reply_hex, wifi)
        except mp.ProvError as e:
            self.sessions.pop(sid, None)
            return {"error": str(e)}
        except Exception as e:   # noqa: BLE001 - never leak a stack trace (or anything in it) to the page
            log.exception("provisioning step failed")
            self.sessions.pop(sid, None)
            return {"error": f"配网出错了（{type(e).__name__}）"}

    def _step(self, s: dict, reply_hex: str | None, wifi: dict | None) -> dict:
        stage = s["stage"]
        reply = bytes.fromhex(reply_hex) if reply_hex else None

        def send(method, params, next_stage):
            _, pkt = s["session"].request(method, params)
            s["stage"] = next_stage
            return {"send": {"hex": pkt.hex()}}

        if stage == "hello":
            did, stamp, token = mp.parse_hello(reply)
            if not any(token):
                raise mp.ProvError("这个设备已经配过网了（它不肯给出配网密钥）。先长按它的按钮复位，再来")
            s.update(did=did, token=token, session=mp.Session(did, token, stamp))
            return send("miIO.info", [], "info")
        if stage == "info":
            r = s["session"].response(reply)
            info = r.get("result") or {}
            s["model"], s["mac"] = info.get("model", ""), info.get("mac", "")
            return send("miIO.handshake", {"type": 1}, "hs1")
        if stage == "hs1":
            curve, sign = mp.pick_suites(s["session"].response(reply))
            s["ecdh"] = mp.Ecdh(curve, sign)
            return send("miIO.handshake", s["ecdh"].start(), "hs2")
        if stage == "hs2":
            r = s["session"].response(reply)
            if "result" not in r:
                raise mp.ProvError(f"设备拒绝了密钥协商：{r.get('error', r)}")
            s["key"] = s["ecdh"].finish(s["token"].hex(), r["result"])
            s["stage"] = "need_wifi"
            return self._need_wifi(s)
        if stage == "need_wifi":
            if not wifi or not wifi.get("ssid"):
                return self._need_wifi(s)
            self.store.set_setting("provision_wifi_ssid", str(wifi["ssid"]))   # the name only: next time it is pre-selected
            params = mp.wifi_config(s["key"], ssid=str(wifi["ssid"]), password=str(wifi.get("password", "")),
                                    uid=int(wifi.get("uid") or 0), bind_key=str(wifi.get("bind_key") or ""))
            out = send("miIO.config_router_safe", params, "config")
            out["send"]["final"] = True   # the device may drop its hotspot before answering
            return out
        if stage == "config":
            # The device may already have left its hotspot when it answers, so no answer is not a failure.
            r = s["session"].response(reply) if reply else {}
            if "error" in r:
                raise mp.ProvError(f"设备没有接受 Wi-Fi 设置：{r['error']}")
            s["stage"] = "done"
            return {"done": True, "model": s["model"], "did": s["did"], "answered": bool(reply)}
        raise mp.ProvError("配网的步骤乱了，重新开始")

    def _need_wifi(self, s: dict) -> dict:
        return {"need": "wifi", "model": s["model"], "did": s["did"],
                "ssid_hint": self.store.get_setting("provision_wifi_ssid", "")}

    # ---- afterwards: where did it land on the home network ----------------------------------------
    async def find(self, sid: str, wait: float = 22.0) -> dict:
        """Look for the device on the LAN (miio hello, broadcast) until it shows up with the same id."""
        s = self.sessions.get(sid)
        if not s or s["stage"] != "done":
            return {"error": "这次配网已经过期了"}
        found: dict = {}

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                try:
                    did, _stamp, _tok = mp.parse_hello(data)
                except mp.ProvError:
                    return
                if did == s["did"]:
                    found["ip"] = addr[0]

        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(Proto, local_addr=("0.0.0.0", 0), allow_broadcast=True)
        try:
            end = time.monotonic() + wait
            while time.monotonic() < end and "ip" not in found:
                for b in BROADCASTS:
                    transport.sendto(mp.HELLO, (b, mp.PORT))
                await asyncio.sleep(2)
        finally:
            transport.close()
        if "ip" not in found:
            return {"found": False}
        devices = self.store.get_setting("miio_devices", {})
        devices[str(s["did"])] = {"model": s["model"], "mac": s["mac"], "ip": found["ip"], "token": s["token"].hex()}
        self.store.set_setting("miio_devices", devices)
        self.sessions.pop(sid, None)
        return {"found": True, "ip": found["ip"], "model": s["model"], "did": s["did"]}

    def _expire(self):
        now = time.monotonic()
        for k in [k for k, v in self.sessions.items() if now - v["t"] > SESSION_TTL]:
            self.sessions.pop(k, None)
