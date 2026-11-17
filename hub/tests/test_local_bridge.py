import asyncio
from pathlib import Path

from app import local_bridge as lb, miot_local as ml


class FakeStore:
    def __init__(self):
        self.d = {}

    def get_setting(self, k, default=None):
        return self.d.get(k, default)

    def set_setting(self, k, v):
        self.d[k] = v


SPEC = {"urn": "urn:miot-spec-v2:device:washer:0000A01F:mibx5-x13:5", "services": [{"iid": 13, "properties": [
    {"iid": 3, "format": "uint8", "value_list": [{"value": 24, "description": "Quick", "description_trans": "快速"}, {"value": 1, "description": "Smart", "description_trans": "智能"}]},
    {"iid": 6, "format": "uint8"}]}]}


def make(tmp_path, enabled=True):
    b = lb.Bridge(tmp_path, FakeStore())
    dev = ml.Device("127.0.0.1", "00" * 16, "860976874", "洗衣机", "mibx5.washer.x13")
    b.devs = {"860976874": dev}
    b.specs = [("mibx5-x13", lb.Spec(SPEC))]
    if enabled:
        b.enable(["860976874"])
    sent = []

    async def fake_set(s, i, v):
        sent.append((s, i, v)); return 0
    dev.set = fake_set
    return b, sent


def test_select_and_number_are_translated(tmp_path):
    b, sent = make(tmp_path)
    assert asyncio.run(b.try_call("select", "select_option", {"entity_id": "select.mibx5_cn_860976874_x13_mode_p_13_3", "option": "快速"}))
    assert asyncio.run(b.try_call("number", "set_value", {"entity_id": "number.mibx5_cn_860976874_x13_rinsh_times_p_13_6", "value": 1.0}))
    assert sent == [(13, 3, 24), (13, 6, 1)]


def test_not_enabled_or_unknown_goes_to_home_assistant(tmp_path):
    b, sent = make(tmp_path, enabled=False)
    assert not asyncio.run(b.try_call("select", "select_option", {"entity_id": "select.mibx5_cn_860976874_x13_mode_p_13_3", "option": "快速"}))
    b.enable(["860976874"])
    assert not asyncio.run(b.try_call("select", "select_option", {"entity_id": "select.mibx5_cn_860976874_x13_mode_p_13_3", "option": "不存在"}))
    assert not asyncio.run(b.try_call("light", "turn_on", {"entity_id": "light.x_cn_860976874_y_p_2_1"}))
    assert sent == [] and b.used["fallback"] == 0      # not translated is not a device failure


def test_light_on_off_and_brightness_go_direct_everything_else_to_ha(tmp_path):
    b, sent = make(tmp_path)
    b.devs["944295092"] = b.devs["860976874"]
    b.enable(["944295092"])
    eid = "light.xiaomi_cn_944295092_lamp31_s_2_light"
    assert asyncio.run(b.try_call("light", "turn_on", {"entity_id": eid, "brightness_pct": 40}))
    assert asyncio.run(b.try_call("light", "turn_off", {"entity_id": eid}))
    assert not asyncio.run(b.try_call("light", "turn_on", {"entity_id": eid, "color_temp_kelvin": 3000}))
    assert not asyncio.run(b.try_call("light", "turn_on", {"entity_id": "light.xiaomi_cn_944295092_lamp31_s_3_indicator_light"}))
    assert sent == [(2, 1, True), (2, 2, 40), (2, 1, False)]


def test_air_conditioner_calls_go_direct(tmp_path):
    b, sent = make(tmp_path)
    b.specs = [("mibx5-x13", lb.Spec({"urn": "urn:x:device:x:0:mibx5-x13:1", "services": [{"iid": 3, "properties": [
        {"iid": 2, "format": "uint8", "value_list": [{"value": 4, "description_trans": "four"}]}]}]}))]
    eid = "climate.mibx5_cn_860976874_x13"
    run = lambda svc, **d: asyncio.run(b.try_call("climate", svc, {"entity_id": eid, **d}))
    assert run("turn_on") and run("turn_off")
    assert run("set_hvac_mode", hvac_mode="heat") and run("set_hvac_mode", hvac_mode="off")
    assert run("set_temperature", temperature=24.5, hvac_mode="cool")
    assert run("set_fan_mode", fan_mode="four") and run("set_swing_mode", swing_mode="both")
    assert not run("set_preset_mode", preset_mode="eco") and not run("set_temperature", target_temp_high=30)
    assert sent == [(2, 1, True), (2, 1, False), (2, 1, True), (2, 2, 5), (2, 1, False), (2, 1, True), (2, 2, 2), (2, 4, 24.5),
                    (3, 2, 4), (3, 3, True), (3, 4, True)]


def test_device_refusal_is_an_error_when_strict_and_a_detour_when_not(tmp_path):
    import pytest
    b, sent = make(tmp_path)

    async def refuse(s, i, v):
        return -5000
    b.devs["860976874"].set = refuse
    eid = "number.mibx5_cn_860976874_x13_rinsh_times_p_13_6"
    with pytest.raises(lb.DirectFailed):
        asyncio.run(b.try_call("number", "set_value", {"entity_id": eid, "value": 1}))
    b.store.set_setting("local_strict", False)
    assert asyncio.run(b.try_call("number", "set_value", {"entity_id": eid, "value": 1})) is False


def test_fan_calls_go_direct(tmp_path):
    b, sent = make(tmp_path)
    b.specs = [("mibx5-x13", lb.Spec({"urn": "urn:x:device:x:0:mibx5-x13:1", "services": [{"iid": 2, "properties": [
        {"iid": 3, "format": "uint8", "value_list": [{"value": 1, "description_trans": "nature"}]}]}]}))]
    eid = "fan.mibx5_cn_860976874_x13_s_2_fan"
    run = lambda svc, **d: asyncio.run(b.try_call("fan", svc, {"entity_id": eid, **d}))
    assert run("turn_on") and run("turn_off") and run("set_percentage", percentage=40) and run("set_percentage", percentage=0)
    assert run("set_preset_mode", preset_mode="nature") and run("turn_on", percentage=70, preset_mode="nature")
    assert not run("oscillate", oscillating=True)
    assert sent == [(2, 1, True), (2, 1, False), (2, 1, True), (2, 5, 40), (2, 1, False), (2, 3, 1), (2, 1, True), (2, 3, 1), (2, 5, 70)]


def test_yeelink_light_uses_the_old_yeelight_commands(tmp_path):
    b = lb.Bridge(tmp_path, FakeStore())
    dev = ml.Device("127.0.0.1", "00" * 16, "753576378", "卧室亮度", "yeelink.light.ceiling22")
    b.devs = {"753576378": dev}
    b.enable(["753576378"])
    sent = []

    async def fake_call(method, params):
        sent.append((method, params)); return ["ok"]
    dev.call = fake_call
    eid = "light.yeelink_cn_753576378_ceiling22_s_2_light"
    assert asyncio.run(b.try_call("light", "turn_on", {"entity_id": eid, "brightness_pct": 40, "color_temp_kelvin": 3000}))
    assert asyncio.run(b.try_call("light", "turn_off", {"entity_id": eid}))
    assert not asyncio.run(b.try_call("light", "turn_on", {"entity_id": eid, "rgb_color": [1, 2, 3]}))
    assert [m for m, _ in sent] == ["set_power", "set_bright", "set_ct_abx", "set_power"]
    assert sent[1][1][0] == 40 and sent[2][1][0] == 3000 and sent[3][1][0] == "off"
