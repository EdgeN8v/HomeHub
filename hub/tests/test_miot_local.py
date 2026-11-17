import asyncio
import json

from app import miot_local as ml

TOKEN = bytes(range(16))


def test_encode_decode_roundtrip():
    pk = ml.encode(TOKEN, b"\x01\x02\x03\x04", 1234, {"id": 1, "method": "get_properties", "params": []})
    assert pk[:2] == b"\x21\x31" and int.from_bytes(pk[2:4], "big") == len(pk)
    assert pk[8:12] == b"\x01\x02\x03\x04" and int.from_bytes(pk[12:16], "big") == 1234
    assert ml.decode(TOKEN, pk) == {"id": 1, "method": "get_properties", "params": []}


def test_wrong_token_is_reported():
    pk = ml.encode(TOKEN, b"\0" * 4, 1, {"id": 1})
    try:
        ml.decode(bytes(16), pk)
    except ml.LocalError:
        return
    raise AssertionError("a wrong token must not decode")


def test_load_tokens_skips_bluetooth_keeps_devices_without_a_lan_address(tmp_path):
    f = tmp_path / "t.json"
    f.write_text(json.dumps([{"homes": [{"devices": [
        {"did": "1", "localip": "192.168.31.5", "token": TOKEN.hex()},
        {"did": "blt.3.x", "localip": "192.168.31.6", "token": TOKEN.hex()},
        {"did": "2", "localip": "240e::1", "token": TOKEN.hex()},
        {"did": "3", "localip": "192.168.31.7", "token": ""}]}]}]))
    assert list(ml.load_tokens(f)) == ["1", "2"]     # the IPv6 one is not on the Wi-Fi: listed, but never reachable


def test_device_talks_to_a_fake_device():
    """A tiny UDP server that behaves like a device: hello, then one encrypted call."""
    async def run():
        loop = asyncio.get_running_loop()

        class Srv(asyncio.DatagramProtocol):
            def connection_made(self, t):
                self.t = t

            def datagram_received(self, data, addr):
                if data == ml.HELLO:
                    self.t.sendto(b"\x21\x31\x00\x20\0\0\0\0" + b"\x09\x09\x09\x09" + (1000).to_bytes(4, "big") + b"\0" * 16, addr)
                    return
                req = ml.decode(TOKEN, data)
                p = req["params"][0]
                res = {"id": req["id"], "result": [{"did": p["did"], "siid": p["siid"], "piid": p["piid"], "code": 0, "value": 72}]}
                self.t.sendto(ml.encode(TOKEN, b"\x09" * 4, 1001, res), addr)

        srv, _ = await loop.create_datagram_endpoint(Srv, local_addr=("127.0.0.1", 0))
        port = srv.get_extra_info("sockname")[1]
        ml.PORT = port
        try:
            d = ml.Device("127.0.0.1", TOKEN.hex(), "7")
            assert await d.get([(2, 2)]) == {(2, 2): 72}
        finally:
            srv.close()
            ml.PORT = 54321
    asyncio.run(run())
