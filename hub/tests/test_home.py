from app.home import Ent, build_device, build_home, make_label, parse_name


def test_parse_and_label():
    assert parse_name("卧室空调  空调 ECO节能模式", "卧室空调") == ("空调", "ECO节能模式", False)
    assert parse_name("洗衣机 * 洗液槽清理 卡槽", "洗衣机") == ("洗液槽清理", "卡槽", True)
    assert parse_name("客厅灯  灯", "客厅灯") == ("灯", "灯", False)
    assert make_label("空调", "ECO节能模式") == "ECO节能模式"
    assert make_label("卧室氛围灯", "开关") == "卧室氛围灯"
    assert make_label("浴霸风暖", "温度") == "浴霸风暖温度"
    assert make_label("冷藏室", "温度") == "冷藏室温度"
    assert make_label("开关", "默认上电状态，0:恢复断电前状态") == "默认上电状态"


def ac():
    return [
        Ent("climate.ac", "卧室空调  空调", {"hvac_modes": ["cool", "heat", "off"]}),
        Ent("light.ac_ind", "卧室空调  指示灯", {}, "config"),
        Ent("switch.ac_eco", "卧室空调  空调 ECO节能模式", {}),
        Ent("switch.ac_beep", "卧室空调  提示音 提示音", {}),
        Ent("sensor.ac_energy", "卧室空调  功耗参数 耗电量", {"device_class": "energy"}),
        Ent("event.ac_x", "卧室空调  空调 某事件", {}),
    ]


def test_ac_device():
    d = build_device({"id": "d1", "name": "卧室空调", "model": "xiaomi.airc.h55h00"}, "我的家 卧室", ac())
    assert d["kind"] == "ac" and d["home"] == "我的家" and d["room"] == "卧室"
    assert d["roles"]["main"] == "climate.ac" and d["roles"]["energy"] == "sensor.ac_energy"
    assert d["toggles"] == [{"entity_id": "climate.ac", "label": "空调"}]
    assert [f["entity_id"] for f in d["features"]] == ["switch.ac_eco"]
    assert {m["entity_id"] for m in d["more"]} == {"light.ac_ind", "switch.ac_beep"}
    assert "event.ac_x" not in d["entities"]


def test_wall_switch_key_named_after_load():
    ents = [Ent("switch.k", "卧室灯  卧室氛围灯 开关", {}), Ent("switch.f", "卧室灯  卧室氛围灯 防闪烁模式", {}),
            Ent("sensor.t", "卧室灯  卧室氛围灯 温度", {"device_class": "temperature"})]
    d = build_device({"id": "d2", "name": "卧室灯", "model": "xiaomi.switch.w2"}, "我的家 卧室", ents)
    assert d["kind"] == "switch" and d["toggles"] == [{"entity_id": "switch.k", "label": "卧室氛围灯"}]
    assert [m["entity_id"] for m in d["more"]] == ["switch.f"]


def test_button_events_are_kept_for_automations():
    ents = [Ent("switch.k", "卧室灯  卧室灯 开关", {}),
            Ent("event.dbl", "卧室灯  左键 双击", {"device_class": "button"}),
            Ent("event.reset", "卧室灯  左键 设备被重置", {}),
            Ent("event.ring", "卧室灯  门铃 门铃响了", {})]
    d = build_device({"id": "d", "name": "卧室灯", "model": "xiaomi.switch.w2"}, "家 卧室", ents)
    assert {e["entity_id"]: e["label"] for e in d["events"]} == {"event.dbl": "左键双击", "event.ring": "门铃响了"}
    assert d["labels"]["event.dbl"] == "左键双击" and d["labels"]["switch.k"] == "卧室灯"
    assert "event.dbl" not in d["entities"]


def test_sensors_and_fridge():
    occ = build_device({"id": "o", "name": "客厅传感器", "model": "xiaomi.sensor_occupy.03"}, "家 客厅",
                       [Ent("binary_sensor.o", "客厅传感器  存在传感器 有人无人状态", {"device_class": "occupancy"}),
                        Ent("sensor.b", "客厅传感器  电池 电池电量", {"device_class": "battery"})])
    assert occ["kind"] == "presence" and occ["roles"] == {"occupancy": "binary_sensor.o", "battery": "sensor.b"}
    assert occ["toggles"] == []
    fr = build_device({"id": "f", "name": "冰箱", "model": "midjd.fridge.bx09s"}, "家 厨房",
                      [Ent("sensor.c", "冰箱  冷藏室 温度", {"device_class": "temperature"}),
                       Ent("sensor.z", "冰箱  冷冻室 温度", {"device_class": "temperature"})])
    assert fr["kind"] == "fridge" and fr["roles"]["zone:冷藏室"] == "sensor.c" and fr["roles"]["zone:冷冻室"] == "sensor.z"


def test_device_without_room_goes_to_unassigned_but_plumbing_does_not():
    devices = [{"id": "d1", "name": "卧室空调", "model": "xiaomi.airc.h55h00", "area_id": "a1", "config_entries": ["x"]},
               {"id": "new", "name": "新插座", "model": "cuco.plug.v3", "area_id": None, "config_entries": ["x"]},
               {"id": "bt", "name": "hci0", "model": "Unknown", "area_id": None, "config_entries": ["b"]}]
    reg = [{"entity_id": e.entity_id, "device_id": "d1", "entity_category": e.category} for e in ac()]
    reg += [{"entity_id": "switch.p", "device_id": "new"}, {"entity_id": "sensor.bt", "device_id": "bt"}]
    states = {e.entity_id: {"attributes": {"friendly_name": e.friendly_name, **e.attributes}} for e in ac()}
    states["switch.p"] = {"attributes": {"friendly_name": "新插座  开关 开关"}}
    states["sensor.bt"] = {"attributes": {"friendly_name": "hci0"}}
    h = build_home(devices, reg, [{"area_id": "a1", "name": "家 卧室"}], states, {"x": "xiaomi_home", "b": "bluetooth"})
    assert sorted(d["id"] for d in h["devices"]) == ["d1", "new"]   # the Bluetooth adapter is left out
    new = next(d for d in h["devices"] if d["id"] == "new")
    assert new["home"] == "家" and new["room"] == "未分配"
    assert h["homes"] == [{"name": "家", "rooms": ["未分配", "卧室"]}]


def test_build_home_skips_unassigned_and_orders_rooms():
    devices = [{"id": "d1", "name": "卧室空调", "model": "xiaomi.airc.h55h00", "area_id": "a1"},
               {"id": "sun", "name": "Sun", "model": None, "area_id": None, "entry_type": "service"},
               {"id": "d3", "name": "客厅灯", "model": "xiaomi.light.ceil04", "area_id": "a2"}]
    reg = [{"entity_id": e.entity_id, "device_id": "d1", "entity_category": e.category} for e in ac()]
    reg += [{"entity_id": "light.l", "device_id": "d3"}, {"entity_id": "sun.sun", "device_id": "sun"}]
    states = {e.entity_id: {"attributes": {"friendly_name": e.friendly_name, **e.attributes}} for e in ac()}
    states["light.l"] = {"attributes": {"friendly_name": "客厅灯  灯"}}
    states["sun.sun"] = {"attributes": {"friendly_name": "Sun"}}
    h = build_home(devices, reg, [{"area_id": "a1", "name": "家 卧室"}, {"area_id": "a2", "name": "家 客厅"}], states)
    assert [d["id"] for d in h["devices"]] == ["d1", "d3"]
    assert h["homes"] == [{"name": "家", "rooms": ["客厅", "卧室"]}]


def test_vacuum_kind():
    from app.home import classify, Ent
    e = Ent("vacuum.s7", "S7", {"fan_speed_list": ["quiet", "max"]})
    assert classify("roborock.vacuum.a15", [e]) == "vacuum"
