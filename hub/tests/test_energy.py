import asyncio

from app.energy import Energy, band, PRICES
from app.store import Store


class FakeHome:
    def __init__(self):
        self.model = {"devices": [{"id": "plug", "entities": ["sensor.w"]}, {"id": "ac", "entities": ["sensor.k"]}]}
        self.states = {"sensor.w": {"state": "60", "attributes": {"device_class": "power", "unit_of_measurement": "W"}},
                       "sensor.k": {"state": "10.0", "attributes": {"device_class": "energy", "unit_of_measurement": "kWh"}}}


def make():
    return Energy(FakeHome(), Store(":memory:"))


def test_washer_off_all_fridge_and_baseline_learnt():
    e = make()
    e.fridge_w = 45
    assert e._share(60, False) == {"fridge": 60}
    assert 45 < e.fridge_w < 46


def test_washer_on_fridge_keeps_its_average():
    e = make()
    e.fridge_w = 45
    s = e._share(500, True)
    assert s["fridge"] == 45 and s["washer"] == 455
    assert e._share(30, True) == {"fridge": 30, "washer": 0}


def test_tick_adds_plug_watts_and_counter_delta():
    e = make()

    async def go():
        await e.tick()
        e.last_t -= 30
        e.home.states["sensor.k"]["state"] = "10.5"
        await e.tick()
    asyncio.run(go())
    t = e.summary()["today"]
    assert abs(t["who"]["ac"]["kwh"] - 0.5) < 1e-9
    assert 0 < t["who"]["fridge"]["kwh"] < 0.001


def test_price_bands():
    from datetime import datetime
    assert band(datetime(2026, 1, 1, 6), PRICES) == "peak" and band(datetime(2026, 1, 1, 22), PRICES) == "valley"
    assert band(datetime(2026, 1, 1, 5, 59), PRICES) == "valley"
