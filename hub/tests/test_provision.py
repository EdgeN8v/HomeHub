import base64
import hashlib
import hmac
import json
import struct

from app import miio_prov as mp
from app.provision import Provisioner

TOKEN = bytes(range(1, 17))
DID = 123456789


class FakeStore:
    def __init__(self): self.d = {}
    def get_setting(self, k, default=None): return self.d.get(k, default)
    def set_setting(self, k, v): self.d[k] = v


class FakeDevice:
    """A device in setup mode, as far as the protocol goes."""
    def __init__(self):
        self.s = mp.Session(DID, TOKEN, 100)
        self.key = None
        self.got_wifi = None

    def handle(self, pkt: bytes) -> bytes:
        if pkt == mp.HELLO:
            return bytes.fromhex("21310020" "00000000") + struct.pack(">II", DID, 100) + TOKEN
        req = json.loads(mp._aes_cbc(self.s.key, self.s.iv, pkt[32:], False))
        m, p = req["method"], req["params"]
        if m == "miIO.info":
            res = {"model": "xiaomi.light.lamp31", "mac": "AA:BB"}
        elif m == "miIO.handshake" and p["type"] == 1:
            res = {"type": 1, "ecdh": {"curve_suites": [3], "sign_suites": [1]}}
        elif m == "miIO.handshake":
            from tests.test_miio_prov import _fake_device
            self.key, res = _fake_device(TOKEN.hex(), p)
        elif m == "miIO.config_router_safe":
            enc = base64.b64decode(p["data"])
            assert base64.b64decode(p["sign"]) == hmac.new(self.key, enc, hashlib.sha256).digest()
            self.got_wifi = json.loads(mp._aes_cbc(self.key, bytes(16), enc, False))
            res = ["ok"]
        else:
            raise AssertionError(m)
        payload = json.dumps({"id": req["id"], "result": res}).encode()
        enc = mp._aes_cbc(self.s.key, self.s.iv, payload, True)
        head = struct.pack(">HHIII", 0x2131, 32 + len(enc), 0, DID, 101)
        return head + hashlib.md5(head + TOKEN + enc).digest() + enc


def test_full_setup_against_a_fake_device():
    prov, dev = Provisioner(FakeStore()), FakeDevice()
    a = prov.start()
    sid = a["session"]
    action = a
    for _ in range(10):
        if "send" in action:
            action = prov.step(sid, dev.handle(bytes.fromhex(action["send"]["hex"])).hex())
        else:
            break
    assert action["need"] == "wifi" and action["model"] == "xiaomi.light.lamp31"
    action = prov.step(sid, wifi={"ssid": "home-2.4G", "password": "pw123456"})
    assert action["send"]["final"] is True
    action = prov.step(sid, dev.handle(bytes.fromhex(action["send"]["hex"])).hex())
    assert action["done"] and dev.got_wifi["ssid"] == "home-2.4G" and dev.got_wifi["passwd"] == "pw123456"


def test_a_device_that_hides_its_token_is_reported_as_already_set_up():
    prov = Provisioner(FakeStore())
    sid = prov.start()["session"]
    hidden = bytes.fromhex("21310020" "00000000") + struct.pack(">II", DID, 100) + bytes(16)
    assert "配过网" in prov.step(sid, hidden.hex())["error"]
    assert "过期" in prov.step(sid, hidden.hex())["error"]   # and the session is gone
