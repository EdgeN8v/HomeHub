import base64
import hashlib
import hmac
import json
import struct

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app import miio_prov as mp

TOKEN = bytes(range(16))


def test_hello_parse():
    reply = bytes.fromhex("21310020" "00000000" "075bcd15" "000000eb") + TOKEN
    did, stamp, token = mp.parse_hello(reply)
    assert (did, stamp, token) == (0x075bcd15, 0xEB, TOKEN)


def test_packet_roundtrip_matches_miio_framing():
    s = mp.Session(did=0x075bcd15, token=TOKEN, stamp0=235)
    _, pkt = s.request("miIO.info", [])
    assert pkt[:2] == b"\x21\x31" and struct.unpack(">H", pkt[2:4])[0] == len(pkt)
    # checksum = md5(header + token + payload), as every miio device checks
    assert pkt[16:32] == hashlib.md5(pkt[:16] + TOKEN + pkt[32:]).digest()
    assert s.response(pkt)["method"] == "miIO.info"   # same key/iv both ways


def _fake_device(token_hex: str, handshake_params: dict, sign_suite=1):
    """What a device does on miIO.handshake type 2, so the key derivation can be checked end to end."""
    e = handshake_params["ecdh"]
    curve = mp.CURVES[e["curve_suite"]]()
    peer = ec.EllipticCurvePublicKey.from_encoded_point(curve, base64.b64decode(e["public_key"]))
    dev = ec.generate_private_key(curve)
    dev_raw = dev.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = dev.exchange(ec.ECDH(), peer)
    prk = hmac.new(token_hex.encode(), shared, hashlib.sha256).digest()
    key = hmac.new(prk, b"\x01", hashlib.sha256).digest()[:16]
    sign = hmac.new(key, dev_raw, hashlib.sha256).digest()
    return key, {"ecdh": {"public_key": base64.b64encode(dev_raw).decode(), "sign": base64.b64encode(sign).decode()}}


def test_ecdh_agrees_with_a_device_and_rejects_a_bad_signature():
    token_hex = TOKEN.hex()
    ecdh = mp.Ecdh(curve=3, sign=1)
    dev_key, answer = _fake_device(token_hex, ecdh.start())
    assert ecdh.finish(token_hex, answer) == dev_key
    answer["ecdh"]["sign"] = base64.b64encode(bytes(32)).decode()
    ecdh2 = mp.Ecdh(curve=3, sign=1)
    _fake_device(token_hex, ecdh2.start())
    try:
        ecdh2.finish(token_hex, answer)
        raise AssertionError("bad signature accepted")
    except mp.ProvError:
        pass


def test_wifi_config_is_encrypted_and_signed():
    key = bytes(range(16, 32))
    p = mp.wifi_config(key, ssid="home", password="secret-pw", uid=0)
    enc = base64.b64decode(p["data"])
    assert b"secret-pw" not in enc
    assert base64.b64decode(p["sign"]) == hmac.new(key, enc, hashlib.sha256).digest()
    cfg = json.loads(mp._aes_cbc(key, bytes(16), enc, False))
    assert cfg["ssid"] == "home" and cfg["passwd"] == "secret-pw" and cfg["country_domain"] == "cn"


def test_pick_suites():
    assert mp.pick_suites({"result": {"ecdh": {"curve_suites": [99, 3], "sign_suites": [1, 2]}}}) == (3, 1)
