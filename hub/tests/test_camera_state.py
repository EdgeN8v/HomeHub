import asyncio
import json
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
from app.camrec import cam_power, stream_fresh, CAM_DID


def fixture(method="GET", on="1"):
    dev = SimpleNamespace(get=AsyncMock(return_value={(2, 1): True}), set=AsyncMock(return_value=0))
    rec = SimpleNamespace(connected=False, ring=deque(), vtrack=1, wake=asyncio.Event())
    req = SimpleNamespace(method=method, query={"on": on}, app={"camrec": rec, "local": SimpleNamespace(devs={CAM_DID: dev})})
    return req, rec, dev


def test_fresh_stream_is_positive_evidence_without_control_queue():
    req, rec, dev = fixture()
    rec.connected = True
    rec.ring.append(SimpleNamespace(track=1, t=time.time()))
    r = json.loads(asyncio.run(cam_power(req)).text)
    assert r == {"on": True, "online": True, "streaming": True}
    dev.get.assert_not_awaited()


def test_old_video_and_fresh_audio_are_not_live_video():
    req, rec, _ = fixture()
    rec.connected = True
    rec.ring.extend([SimpleNamespace(track=1,t=90), SimpleNamespace(track=2,t=100)])
    assert not stream_fresh(rec, 100)


def test_sleeping_is_not_offline():
    req, _, dev = fixture()
    dev.get.return_value = {(2,1): False}
    assert json.loads(asyncio.run(cam_power(req)).text) == {"on":False,"online":True,"streaming":False}


def test_unreachable_does_not_lie_about_power_and_is_cached():
    req, _, dev = fixture()
    dev.get.side_effect = TimeoutError()
    async def go():
        one = json.loads((await cam_power(req)).text)
        two = json.loads((await cam_power(req)).text)
        return one, two
    one, two = asyncio.run(go())
    assert one == two and one["online"] is False and one["on"] is None
    assert dev.get.await_count == 1


def test_missing_property_is_unknown_not_on():
    req, _, dev = fixture()
    dev.get.return_value = {}
    assert json.loads(asyncio.run(cam_power(req)).text)["on"] is None


def test_post_requires_actual_readback():
    req, _, dev = fixture("POST", "1")
    dev.get.return_value = {(2,1): False}
    try:
        asyncio.run(cam_power(req))
    except web.HTTPBadGateway:
        pass
    else:
        assert False, "must not claim a successful wake"


def test_confirmed_wake_signals_recorder():
    req, rec, _ = fixture("POST", "1")
    assert json.loads(asyncio.run(cam_power(req)).text)["on"] is True
    assert rec.wake.is_set()


def test_waiting_for_device_is_bounded_and_cancelled(monkeypatch):
    import app.camrec as mod
    req, rec, dev = fixture()
    cancelled = []
    async def slow(*args):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)
    dev.get = slow
    original = asyncio.wait_for
    monkeypatch.setattr(mod.asyncio, "wait_for", lambda coro, timeout: original(coro, .01))
    r = json.loads(asyncio.run(cam_power(req)).text)
    assert r["online"] is False and cancelled == [True]
