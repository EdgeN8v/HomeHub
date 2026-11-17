import asyncio

from app import local_bridge as lb, local_reader as lr, miot_local as ml

SPEC = {"urn": "urn:miot-spec-v2:device:washer:0000A01F:mibx5-x13:5", "services": [{"iid": 13, "properties": [
    {"iid": 14, "format": "uint8", "value_list": [{"value": 0, "description": "x", "description_trans": "off-text"}]},
    {"iid": 10, "format": "uint16"}]}]}


class FakeStore:
    def __init__(self):
        self.d = {"local_read_dids": ["1"]}

    def get_setting(self, k, default=None):
        return self.d.get(k, default)

    def set_setting(self, k, v):
        self.d[k] = v


class FakeHome:
    def __init__(self):
        self.states = {
            "select.mibx5_cn_1_x13_drying_level_p_13_14": {"state": "other", "attributes": {"options": ["off-text", "other"]}},
            "sensor.mibx5_cn_1_x13_left_time_p_13_10": {"state": "246", "attributes": {}},
            "switch.mibx5_cn_1_x13_dry_only_p_13_32": {"state": "on", "attributes": {}},
        }
        self.pushed = []

    async def on_change(self, eid, new):
        self.states[eid] = new
        self.pushed.append(eid)


def test_shown_wording():
    assert lb.shown("switch", {}, True, None) == "on"
    assert lb.shown("select", {"value_list": [{"value": 0, "description_trans": "a"}]}, 0, ["a"]) == "a"
    assert lb.shown("select", {"value_list": [{"value": 0, "description_trans": "a"}]}, 0, ["b"]) is None   # not an option -> leave HA's value
    assert lb.shown("sensor", {}, 46, None) == "46"


def test_stale_home_assistant_values_are_corrected_from_the_device(tmp_path):
    b = lb.Bridge(tmp_path, FakeStore())
    dev = ml.Device("127.0.0.1", "00" * 16, "1", "w", "mibx5.washer.x13")
    b.devs = {"1": dev}
    b.specs = [("mibx5-x13", lb.Spec(SPEC))]

    async def fake_get(props):
        return {(13, 14): 0, (13, 10): 46, (13, 32): False}
    dev.get = fake_get
    home = FakeHome()
    n = asyncio.run(lr.Reader(b, home).read_one("1"))
    assert n == 3
    assert home.states["select.mibx5_cn_1_x13_drying_level_p_13_14"]["state"] == "off-text"
    assert home.states["sensor.mibx5_cn_1_x13_left_time_p_13_10"]["state"] == "46"
    assert home.states["switch.mibx5_cn_1_x13_dry_only_p_13_32"]["state"] == "off"
    assert asyncio.run(lr.Reader(b, home).read_one("1")) == 0       # nothing left to correct


def test_ha_state_reads_are_overlaid_with_the_devices_answer(tmp_path):
    b = lb.Bridge(tmp_path, FakeStore())
    dev = ml.Device("127.0.0.1", "00" * 16, "1", "w", "mibx5.washer.x13")
    b.devs = {"1": dev}
    b.specs = [("mibx5-x13", lb.Spec(SPEC))]

    async def fake_get(props):
        return {(13, 10): 46}
    dev.get = fake_get
    st = {"sensor.mibx5_cn_1_x13_left_time_p_13_10": {"state": "246", "attributes": {}},
          "sensor.other_cn_2_x_p_1_1": {"state": "5", "attributes": {}}}
    out = asyncio.run(b.overlay(st))
    assert out["sensor.mibx5_cn_1_x13_left_time_p_13_10"]["state"] == "46"
    assert out["sensor.other_cn_2_x_p_1_1"]["state"] == "5"
