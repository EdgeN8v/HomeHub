import asyncio

from app.notify import Notifier
from app.store import Store

E = "sensor.xiaomi_cn_2175963426_ym13_anti_icing_status_p_2_9"


def make():
    return Notifier(Store(":memory:"), lambda e: "热水器")


def test_normal_to_antifreeze_pushes_once():
    n = make()
    n.on_change(E, {"state": "Normal"})
    assert n.latest() == 0
    n.on_change(E, {"state": "Antifreeze"})
    n.on_change(E, {"state": "Antifreeze"})
    got = n.since(0)
    assert len(got) == 1 and got[0]["kind"] == "antifreeze" and "防冻" in got[0]["title"]


def test_first_sight_and_dropouts_do_not_push():
    n = make()
    n.on_change(E, {"state": "Antifreeze"})          # first sight: unknown what came before
    n.on_change(E, {"state": "unavailable"})
    n.on_change(E, {"state": "Antifreeze"})          # back from a dropout, still the same episode
    assert n.latest() == 0
    n.on_change(E, {"state": "Normal"})
    n.on_change(E, {"state": "unavailable"})
    n.on_change(E, {"state": "Antifreeze"})
    assert n.latest() == 1


def test_switch_off_suppresses_and_test_notice_forces():
    n = make()
    assert n.settings() == {"antifreeze": True}
    n.set("antifreeze", False)
    n.on_change(E, {"state": "Normal"})
    n.on_change(E, {"state": "Antifreeze"})
    assert n.latest() == 0
    assert n.push("test", "t", "b", force=True) == 1


def test_resync_catches_missed_change_and_long_poll_wakes():
    async def go():
        n = make()
        n.on_change(E, {"state": "Normal"})
        waiter = asyncio.create_task(n.wait(0, 5))
        await asyncio.sleep(0.05)
        n.resync({E: {"state": "Antifreeze"}, "light.x": {"state": "on"}})
        got = await asyncio.wait_for(waiter, 1)
        assert [g["id"] for g in got] == [1]
        assert await n.wait(1, 0.05) == []
    asyncio.run(go())
