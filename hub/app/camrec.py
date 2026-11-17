"""Camera events: the hub keeps one stream of the camera open (through go2rtc), notices movement from the size of the video frames
and keeps a short clip + a thumbnail of every movement - the "有人移动 17:19" list of the Mijia app, without any cloud.

How movement is noticed (no decoding, almost no CPU): the camera encodes with H.265, so a still picture costs a few hundred bytes a
frame and a moving one thousands. go2rtc hands the stream over as fragmented MP4, one small fragment per frame; the size of the
non-key frames, summed per second, is the "activity". It is compared with a slowly learnt quiet level.

A clip is the keyframe just before the movement (+ what came before it, a couple of seconds) until the movement stops or CLIP_MAX
seconds are reached, whichever is first - a person walking about for an hour is one event with a short clip, not an hour of video.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import time
from collections import deque
from pathlib import Path

import aiohttp
from aiohttp import web

log = logging.getLogger("camrec")
BASE = os.environ.get("GO2RTC_URL", "http://127.0.0.1:1984")
SRC = "c700"
CLIP_MAX = float(os.environ.get("CAM_CLIP_MAX", "15"))        # seconds kept of each event (the settings page changes it)
SENS = 2.5                                                    # movement threshold = quiet level * SENS + 2500 (lower = more sensitive)
RECORD = True                                                 # keep clips of movement at all
QUALITY = {"sd": "1", "hd": "2", "max": "3", "auto": "0"}      # what the camera is asked for (go2rtc patched: /api/xiaomi/quality)
PRE = 2.0                                                     # seconds before the movement
TAIL = 2.0                                                    # seconds after it
MERGE = 15.0                                                  # movement again within this many seconds = same event
KEEP_DAYS = 7
KEEP_EVENTS = 400
KEY_NALS = {19, 20, 21, 32, 33, 34}                           # IDR / CRA / VPS / SPS / PPS: a frame starting with one is a keyframe


def boxes(buf: bytearray):
    """Yield (type, bytes) for each complete top-level MP4 box at the start of buf, removing them from it."""
    while len(buf) >= 8:
        size = struct.unpack(">I", buf[:4])[0]
        if size < 8 or size > 64_000_000:
            raise ValueError("bad box size %d" % size)
        if len(buf) < size:
            return
        typ = bytes(buf[4:8])
        data = bytes(buf[:size])
        del buf[:size]
        yield typ, data


def video_track(moov: bytes) -> int:
    """Track id of the video track in a moov box (the one whose sample entry is hvc1/hev1/avc1)."""
    i = 8
    while i + 8 <= len(moov):
        size, typ = struct.unpack(">I4s", moov[i:i + 8])
        if size < 8:
            break
        if typ == b"trak":
            trak = moov[i:i + size]
            if b"hvc1" in trak or b"hev1" in trak or b"avc1" in trak:
                j = trak.find(b"tkhd")
                if j >= 0:
                    ver = trak[j + 4]
                    off = j + 4 + 4 + (8 if ver == 1 else 4) * 2
                    return struct.unpack(">I", trak[off:off + 4])[0]
        i += size
    return 1


def track_of(moof: bytes) -> int:
    j = moof.find(b"tfhd")
    return struct.unpack(">I", moof[j + 8:j + 12])[0] if j >= 0 else 0


def rebase(frags: list, init: bytes) -> tuple[bytes, list[bytes]]:
    """Clip timestamps start at zero (the live stream's clock is hours in) and the file claims no length, so a player does not
    wait for or seek to a time that is not there."""
    first: dict[int, int] = {}
    out = []
    for f in frags:
        moof = bytearray(f.data)
        j = moof.find(b"tfdt")
        if 0 < j < 4000:
            ver = moof[j + 4]
            fmt, width = (">Q", 8) if ver == 1 else (">I", 4)
            t = struct.unpack(fmt, moof[j + 8:j + 8 + width])[0]
            base = first.setdefault(f.track, t)
            moof[j + 8:j + 8 + width] = struct.pack(fmt, max(0, t - base))
        out.append(bytes(moof))
    head = bytearray(init)
    for tag, vpos, v0, v1 in ((b"mvhd", 4, 20, 28), (b"mehd", 4, 8, 8), (b"tkhd", 4, 24, 32), (b"mdhd", 4, 20, 28)):
        i = 0
        while True:
            i = head.find(tag, i)
            if i < 0:
                break
            ver = head[i + vpos]
            if tag == b"mehd":
                off, width = i + 8, 8 if ver == 1 else 4
            else:
                off, width = i + (v1 if ver == 1 else v0), 8 if ver == 1 else 4
            head[off:off + width] = bytes(width)
            i += 4
    return bytes(head), out


class Fragment:
    __slots__ = ("track", "data", "n", "key", "t")

    def __init__(self, track: int, data: bytes, n: int, key: bool, t: float):
        self.track, self.data, self.n, self.key, self.t = track, data, n, key, t


class Recorder:
    def __init__(self, data: Path):
        self.dir = Path(data) / "cam"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index = self.dir / "events.json"
        try:
            self.events: list[dict] = json.loads(self.index.read_text("utf-8"))
        except (OSError, ValueError):
            self.events = []
        self.init = b""
        self.vtrack = 1
        self.ring: deque[Fragment] = deque()           # the last ~12 s of fragments
        self.bucket_t = 0
        self.bucket = 0                                # non-key video bytes in the current second
        self.base = 3000.0                             # learnt quiet level, bytes per second
        self.hist: deque[tuple[float, int]] = deque(maxlen=300)
        self.over = 0
        self.under = 0
        self.subs: set[asyncio.Queue] = set()           # live viewers of the fast-start stream
        self.restart = False                            # reconnect now (picture quality changed)
        self.quality = "hd"
        self.cur: dict | None = None                   # the event in progress
        self.clip: list[Fragment] | None = None        # its fragments, while the clip is still being filled
        self.clip_t0 = 0.0
        self.suppress_until = 0.0                      # the camera turning its head looks like movement; ignore it
        self.connected = False
        self.gen = 0                                   # counts the connections to the camera stream
        self.wake = asyncio.Event()                    # someone wants the picture now: stop waiting between retries
        self.last_snap = 0.0
        self.session: aiohttp.ClientSession | None = None

    # ---- the stream ---------------------------------------------------------------------------------------------------------
    async def run(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30))
        backoff = 5
        while True:
            try:
                await self.consume()
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("camera stream: %s %s", type(e).__name__, e)
            self.connected = False
            for q in list(self.subs):
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            self.end_event(time.time())
            # wait before trying again (the camera may be switched off) - but a viewer or "switch on" cuts the wait short
            try:
                await asyncio.wait_for(self.wake.wait(), backoff)
                backoff = 5
            except asyncio.TimeoutError:
                backoff = min(backoff * 2, 60)
            self.wake.clear()

    async def consume(self):
        buf = bytearray()
        self.init = b""
        self.ring.clear()  # Never bootstrap a new connection with the previous session's GOP.
        pending: tuple[int, bytes] | None = None
        try:
            async with self.session.get(f"{BASE}/api/xiaomi/quality", params={"q": QUALITY.get(self.quality, "2")}) as qr:
                await qr.read()
        except Exception:
            pass
        self.restart = False
        async with self.session.get(f"{BASE}/api/stream.mp4", params={"src": SRC, "video": "all", "audio": "opus"}) as r:
            if r.status != 200:
                raise RuntimeError(f"go2rtc answered {r.status}")
            ftyp = b""
            async for chunk in r.content.iter_any():
                if self.restart:
                    break
                buf += chunk
                for typ, data in boxes(buf):
                    if typ == b"ftyp":
                        ftyp = data
                    elif typ == b"moov":
                        self.init = ftyp + data
                        self.vtrack = video_track(data)
                        self.connected = True
                        self.gen += 1
                        log.info("camera stream open, video track %d", self.vtrack)
                    elif typ == b"moof":
                        pending = (track_of(data), data)
                    elif typ == b"mdat" and pending:
                        track, moof = pending
                        pending = None
                        key = False
                        if track == self.vtrack and len(data) > 13:
                            key = (data[12] >> 1) & 0x3F in KEY_NALS
                        self.on_fragment(Fragment(track, moof + data, len(data) - 8, key, time.time()))

    # ---- movement ---------------------------------------------------------------------------------------------------------
    def on_fragment(self, f: Fragment):
        self.ring.append(f)
        for q in list(self.subs):
            try:
                q.put_nowait(f)
            except asyncio.QueueFull:
                self.subs.discard(q)
        while self.ring and f.t - self.ring[0].t > 12:
            self.ring.popleft()
        if self.clip is not None:
            self.clip.append(f)
            if f.t - self.clip_t0 >= CLIP_MAX or (self.cur and self.cur.get("end_at") and f.t >= self.cur["end_at"] + TAIL):
                self.finish_clip()
        if f.track != self.vtrack or f.key:
            return
        sec = int(f.t)
        if sec != self.bucket_t:
            if self.bucket_t:
                self.on_second(self.bucket_t, self.bucket)
            self.bucket_t, self.bucket = sec, 0
        self.bucket += f.n

    def on_second(self, sec: int, nbytes: int):
        self.hist.append((sec, nbytes))
        thr = self.base * SENS + 2500
        moving = RECORD and nbytes > thr and time.time() > self.suppress_until
        if moving:
            self.over += 1
            self.under = 0
            if self.cur is not None and sec - self.cur["ts"] > 40:
                # "movement" for 40 s+ is far more likely a new steady level (night vision, light change, picture quality) than a person:
                # let the quiet level climb towards it, slowly, so the event ends instead of running for half an hour
                self.base = min(40000.0, 0.97 * self.base + 0.03 * nbytes)
        else:
            self.under += 1
            self.over = 0
            if nbytes <= thr:
                self.base = min(max(8000.0, self.base), 0.98 * self.base + 0.02 * nbytes)     # learn the quiet level only from quiet seconds; a busy evening must not make it deaf
        if self.cur is None:
            if self.over >= 2:
                self.start_event(sec - 1)
        else:
            if moving:
                self.cur["last"] = sec
                self.cur.pop("end_at", None)
            elif self.under >= 4 and "end_at" not in self.cur:
                self.cur["end_at"] = float(self.cur["last"] + 1)
                self.cur["dur"] = round(self.cur["end_at"] - self.cur["ts"])
                if self.clip is None:
                    self.end_event(self.cur["end_at"])

    def start_event(self, sec: int):
        prev = self.events[-1] if self.events else None
        if prev and sec - (prev["ts"] + prev["dur"]) < MERGE and self.cur is None and prev.get("clip"):
            # movement again soon after: the same event goes on, no new clip
            self.cur = {**prev, "merged": True, "last": sec}
            self.cur.pop("end_at", None)
            return
        self.cur = {"id": time.strftime("%Y%m%d-%H%M%S", time.localtime(sec)), "ts": sec, "dur": 1, "last": sec, "clip": None, "thumb": None}
        # clip: from the keyframe just before (sec - PRE)
        frags = list(self.ring)
        start = 0
        for i, f in enumerate(frags):
            if f.track == self.vtrack and f.key and f.t <= sec - PRE + 0.5:
                start = i
        for i in range(start, -1, -1):
            if frags[i].track == self.vtrack and frags[i].key:
                start = i
                break
        self.clip = frags[start:]
        self.clip_t0 = self.clip[0].t if self.clip else time.time()
        asyncio.get_running_loop().create_task(self.take_thumb(self.cur["id"]))
        log.info("movement at %s (activity %d B/s, quiet level %d)", self.cur["id"], self.bucket, self.base)

    def finish_clip(self):
        frags, self.clip = self.clip or [], None
        ev = self.cur
        if not ev or not frags or not self.init:
            return
        path = self.dir / f"{ev['id']}.mp4"
        try:
            head, datas = rebase(frags, self.init)
            with open(path, "wb") as fh:
                fh.write(head)
                for d in datas:
                    fh.write(d)
            ev["clip"] = path.name
            ev["clip_s"] = round(frags[-1].t - frags[0].t, 1)
        except OSError as e:
            log.warning("writing clip: %s", e)
            return
        self.save_event()
        if ev.get("end_at"):
            self.end_event(ev["end_at"])

    def end_event(self, at: float):
        if self.clip is not None:
            self.finish_clip()
        ev, self.cur = self.cur, None
        if ev:
            ev.pop("end_at", None)
            ev.pop("last", None)
            ev.pop("merged", None)
            self.upsert(ev)
        self.over = self.under = 0

    def upsert(self, ev: dict):
        for i, e in enumerate(self.events):
            if e["id"] == ev["id"]:
                self.events[i] = ev
                break
        else:
            self.events.append(ev)
        self.save_event()

    def save_event(self):
        ev = self.cur
        if ev:
            e2 = {k: v for k, v in ev.items() if k not in ("end_at", "last", "merged")}
            for i, e in enumerate(self.events):
                if e["id"] == e2["id"]:
                    self.events[i] = e2
                    break
            else:
                self.events.append(e2)
        self.prune()
        tmp = self.index.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.events, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.index)

    def prune(self):
        cut = time.time() - KEEP_DAYS * 86400
        keep = [e for e in self.events if e["ts"] >= cut][-KEEP_EVENTS:]
        for e in self.events:
            if e not in keep:
                for k in ("clip", "thumb"):
                    if e.get(k):
                        try:
                            (self.dir / e[k]).unlink()
                        except OSError:
                            pass
        self.events = keep

    async def take_thumb(self, eid: str, delay: float = 1.5):
        await asyncio.sleep(delay)
        jpg = await self.frame()
        if not jpg:
            return
        name = f"{eid}.jpg"
        (self.dir / name).write_bytes(jpg)
        for e in [self.cur] + self.events:
            if e and e["id"] == eid:
                e["thumb"] = name
        self.save_event()

    async def frame(self) -> bytes | None:
        try:
            async with self.session.get(f"{BASE}/api/frame.jpeg", params={"src": SRC}, timeout=aiohttp.ClientTimeout(total=15)) as r:
                return await r.read() if r.status == 200 else None
        except Exception:
            return None

    async def snapshots(self):
        """The picture the camera card shows while nobody is watching: a fresh frame every minute."""
        while True:
            await asyncio.sleep(20)
            if self.connected and time.time() - self.last_snap > 30:
                jpg = await self.frame()
                if jpg:
                    (self.dir / "latest.jpg").write_bytes(jpg)
                    self.last_snap = time.time()


# ---- head movement: hold = keep sending the step every 0.3 s (one command turns the head about a second, resent every 0.3 s it is smooth)
MOVES = {"left": 1, "right": 2, "up": 3, "down": 4}


class Head:
    def __init__(self, rec: Recorder):
        self.rec = rec
        self.task: asyncio.Task | None = None
        self.until = 0.0

    async def send(self, op: int):
        try:
            async with self.rec.session.get(f"{BASE}/api/xiaomi/motor", params={"src": SRC, "body": '{"operation":%d}' % op},
                                            timeout=aiohttp.ClientTimeout(total=8)) as r:
                await r.read()
        except Exception as e:
            log.warning("motor %s: %s", op, e)

    async def go(self, direction: str, secs: float = 2.5):
        self.rec.suppress_until = time.time() + secs + 6
        self.until = time.time() + secs
        if self.task and not self.task.done():
            if getattr(self.task, "direction", None) == direction:
                return                                  # already turning that way: just extended
            self.task.cancel()
        self.task = asyncio.create_task(self.loop(MOVES[direction]))
        self.task.direction = direction

    async def loop(self, op: int):
        while time.time() < self.until:
            await self.send(op)
            await asyncio.sleep(0.3)

    async def stop(self):
        self.until = 0
        if self.task and not self.task.done():
            self.task.cancel()
        self.rec.suppress_until = time.time() + 6
        await self.send(-1001)


# ---- http ----------------------------------------------------------------------------------------------------------------------
async def cam_events(request: web.Request):
    rec: Recorder = request.app["camrec"]
    evs = [e for e in rec.events if e.get("clip")]
    return web.json_response({"connected": rec.connected, "clip_max": CLIP_MAX,
                              "events": [{k: e.get(k) for k in ("id", "ts", "dur", "clip_s", "thumb")} for e in reversed(evs)][:100]})


async def cam_file(request: web.Request):
    rec: Recorder = request.app["camrec"]
    name = request.match_info["name"]
    if "/" in name or ".." in name or not name.endswith((".mp4", ".jpg")):
        raise web.HTTPNotFound()
    path = rec.dir / name
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-store" if name == "latest.jpg" else "private, max-age=3600"})


async def cam_stat(request: web.Request):
    rec: Recorder = request.app["camrec"]
    return web.json_response({"connected": rec.connected, "quiet": round(rec.base), "threshold": round(rec.base * SENS + 2500),
                              "recent": [[int(t), n] for t, n in list(rec.hist)[-60:]], "events": len(rec.events)})


async def cam_move2(request: web.Request):
    rec: Recorder = request.app["camrec"]
    head: Head = request.app["camhead"]
    d = request.query.get("dir", "")
    if d == "stop":
        await head.stop()
    elif d in MOVES:
        await head.go(d)
    else:
        raise web.HTTPBadRequest(text="dir must be left/right/up/down/stop")
    return web.json_response({"ok": True, "dir": d})


async def cam_prefs(request: web.Request):
    """The camera card's own settings (name, room, hidden): a camera is not an HA device, so they are kept here."""
    store = request.app["store"]
    cur = store.get_setting("cam_prefs", {}) or {}
    if request.method == "POST":
        body = await request.json()
        if "name" in body:
            name = str(body["name"]).strip()
            if not 1 <= len(name) <= 40:
                raise web.HTTPBadRequest(text="name must be 1-40 characters")
            cur["name"] = name
        if "room" in body:
            room = str(body["room"]).strip()
            if not 1 <= len(room) <= 20:
                raise web.HTTPBadRequest(text="bad room")
            cur["room"] = room
        if "hidden" in body:
            cur["hidden"] = bool(body["hidden"])
        store.set_setting("cam_prefs", cur)
    return web.json_response(cur)


async def cam_live(request: web.Request):
    """The live picture, started at once: the recorder already holds the camera stream open, so the viewer is given its init segment and
    everything since the last keyframe right away, then the live fragments. (Asking go2rtc afresh costs a few seconds of start-up.)"""
    rec: Recorder = request.app["camrec"]
    # not connected (the camera was off, or just came back): ask the recorder to try now and wait for it, rather than failing at once
    t_end = time.time() + 15
    while not (rec.connected and rec.init) and time.time() < t_end:
        rec.wake.set()
        await asyncio.sleep(0.3)
    if not rec.connected or not rec.init:
        raise web.HTTPServiceUnavailable(text="camera not connected")
    resp = web.StreamResponse(headers={"Content-Type": "video/mp4", "Cache-Control": "no-store"})
    await resp.prepare(request)
    await resp.write(rec.init)
    q: asyncio.Queue = asyncio.Queue(maxsize=60)
    frags = list(rec.ring)
    start = 0
    for i, f in enumerate(frags):
        if f.track == rec.vtrack and f.key:
            start = i
    rec.subs.add(q)
    try:
        for f in frags[start:]:
            await asyncio.wait_for(resp.write(f.data), 1.0)
        while True:
            try:
                f = await asyncio.wait_for(q.get(), 20)
            except asyncio.TimeoutError:
                if q not in rec.subs or not rec.connected:
                    break
                continue
            if f is None or q not in rec.subs:
                break
            if time.time() - f.t > 1.0:
                raise asyncio.TimeoutError("live viewer fell behind")
            await asyncio.wait_for(resp.write(f.data), 1.0)
    except asyncio.TimeoutError:
        log.info("closing delayed camera viewer; reconnect at live edge")
        if request.transport:
            request.transport.abort()
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        rec.subs.discard(q)
    return resp


# camera settings the page may read / change (MIoT siid, piid) - through the direct local bridge, like the power switch
SETTINGS = {
    "night": (2, 3), "flip": (2, 2), "watermark": (2, 4), "fullcolor": (2, 6), "light": (3, 1), "track": (2, 11),
    "pettrack": (8, 21), "human": (8, 8), "pet": (8, 5), "sound": (8, 20), "cry": (8, 10), "cruise": (11, 1),
    "volume": (6, 2), "alarm": (5, 1),
}


async def cam_settings(request: web.Request):
    dev = request.app["local"].devs.get(CAM_DID)
    if not dev:
        raise web.HTTPNotFound(text="camera not in the account dump")
    store = request.app["store"]
    try:
        if request.method == "POST":
            body = await request.json()
            key = body.get("key")
            if key in SETTINGS:
                code = await dev.set(*SETTINGS[key], body.get("value"))
                if code != 0:
                    raise web.HTTPBadGateway(text=f"camera refused ({code})")
                # remembered: the camera forgets some of these when it is switched off and on (the watermark comes back)
                cur = store.get_setting("cam_prefs", {}) or {}
                cur.setdefault("camset", {})[key] = body.get("value")
                store.set_setting("cam_prefs", cur)
            elif key == "quality":
                v = str(body.get("value"))
                if v not in QUALITY:
                    raise web.HTTPBadRequest(text="quality: sd/hd/max/auto")
                rec = request.app["camrec"]
                cur = store.get_setting("cam_prefs", {}) or {}
                cur["quality"] = rec.quality = v
                store.set_setting("cam_prefs", cur)
                rec.restart = True                  # the recorder lets go of the camera and asks again at the new quality
            elif key in ("sens", "clip_max", "record"):
                global SENS, CLIP_MAX, RECORD
                cur = store.get_setting("cam_prefs", {}) or {}
                v = body.get("value")
                if key == "sens":
                    cur["sens"] = SENS = {"low": 3.6, "mid": 2.5, "high": 1.7}.get(v, 2.5)
                elif key == "clip_max":
                    cur["clip_max"] = CLIP_MAX = float(min(60, max(5, float(v))))
                else:
                    cur["record"] = RECORD = bool(v)
                store.set_setting("cam_prefs", cur)
            else:
                raise web.HTTPBadRequest(text="unknown setting")
        out: dict = {}
        items = list(SETTINGS.items())
        for i in range(0, len(items), 5):          # a few at a time: one UDP packet carries only so many
            got = await dev.get([v for _, v in items[i:i + 5]])
            for k, v in items[i:i + 5]:
                out[k] = got.get(v)
    except web.HTTPException:
        raise
    except Exception as e:
        raise web.HTTPBadGateway(text=f"camera: {e}")
    cur = store.get_setting("cam_prefs", {}) or {}
    out.update(sens={3.6: "low", 2.5: "mid", 1.7: "high"}.get(SENS, "mid"), clip_max=CLIP_MAX, record=RECORD, quality=request.app["camrec"].quality)
    return web.json_response(out)


# ---- talking to the camera: the phone posts its microphone (raw 16 kHz mono PCM) to /api/cam/talk. A loopback-only port serves an
# endless WAV that go2rtc's ffmpeg turns into Opus for the camera's speaker (go2rtc "dst" play). Starting that ffmpeg takes seconds,
# so it is kept running the whole time the camera is connected, fed with silence; go2rtc only lets the sound through to the camera
# while somebody really talks (/api/xiaomi/talkmode?on=1, HomeHub patch). So talking starts the moment the button is tapped.
TALK_PORT = int(os.environ.get("CAM_TALK_PORT", "8201"))
TALK_CHUNK = 640            # 20 ms


class Talk:
    def __init__(self, rec: "Recorder"):
        self.rec = rec
        self.q: asyncio.Queue | None = None     # the talker's microphone; None while nobody talks
        self.attached = False                   # go2rtc's ffmpeg is reading /mic
        self.gen = -1                           # the camera connection that ffmpeg plays into
        self.hangup = asyncio.Event()
        self.next_try = 0.0

    async def mic(self, request: web.Request):
        resp = web.StreamResponse(headers={"Content-Type": "audio/wav"})
        await resp.prepare(request)
        hdr = b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) + b"data" + struct.pack("<I", 0xFFFFFFFF)
        self.hangup.clear()
        self.attached, self.gen = True, self.rec.gen
        silence = bytes(TALK_CHUNK)
        loop = asyncio.get_running_loop()
        nxt = loop.time()
        try:
            await resp.write(hdr)
            while not self.hangup.is_set():
                q = self.q
                if q is None:
                    # nobody talks: silence, in real time
                    await resp.write(silence)
                    nxt = max(nxt + 0.02, loop.time() - 0.1)
                    if nxt > loop.time():
                        await asyncio.sleep(nxt - loop.time())
                    continue
                while q.qsize() > 12:           # sound that piled up (a network hiccup) is late: skip it and stay live
                    q.get_nowait()
                try:
                    chunk = await asyncio.wait_for(q.get(), 0.2)
                except asyncio.TimeoutError:
                    continue
                await resp.write(chunk)
                nxt = loop.time()
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.attached = False
        return resp

    async def play(self):
        src = f"ffmpeg:http://127.0.0.1:{TALK_PORT}/mic#audio=opus"
        try:
            async with self.rec.session.post(f"{BASE}/api/streams", params={"dst": SRC, "src": src}, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    log.warning("talk: go2rtc answered %s %s", r.status, (await r.text())[:120])
        except Exception as e:
            log.warning("talk: %s %s", type(e).__name__, e)

    async def gate(self, on: bool) -> str:
        try:
            async with self.rec.session.get(f"{BASE}/api/xiaomi/talkmode", params={"on": "1" if on else "0"}, timeout=aiohttp.ClientTimeout(total=3)) as r:
                return await r.text()
        except Exception as e:
            log.warning("talk gate: %s %s", type(e).__name__, e)
            return ""

    async def stat(self) -> dict:
        try:
            async with self.rec.session.get(f"{BASE}/api/xiaomi/talkmode", timeout=aiohttp.ClientTimeout(total=3)) as r:
                return dict(kv.split("=", 1) for kv in (await r.text()).split() if "=" in kv)
        except Exception:
            return {}

    async def keeper(self):
        """Keeps ffmpeg attached while the camera is connected; a new camera connection needs a new ffmpeg."""
        while self.rec.session is None:             # the recorder opens its HTTP session a moment after start
            await asyncio.sleep(0.5)
        await self.gate(False)
        while True:
            await asyncio.sleep(1)
            try:
                want = self.rec.connected
                if self.attached and (not want or self.gen != self.rec.gen):
                    self.hangup.set()
                elif want and not self.attached and time.time() > self.next_try:
                    self.next_try = time.time() + 10
                    asyncio.create_task(self.play())
            except Exception as e:
                log.warning("talk keeper: %s %s", type(e).__name__, e)

    async def start_server(self):
        app = web.Application()
        app.router.add_get("/mic", self.mic)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", TALK_PORT).start()
        return runner


async def cam_talk(request: web.Request):
    """POST /api/cam/talk: the body is the microphone, streamed until the phone lets go of the button."""
    talk: Talk = request.app["camtalk"]
    if talk.q is not None:
        raise web.HTTPConflict(text="someone is already talking")
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    talk.q = q

    async def check():
        # the sound must really be leaving for the camera; if not, ffmpeg is attached to a dead connection: start it again
        await asyncio.sleep(1.5)
        st = await talk.stat()
        if talk.attached and st.get("sent") == "0":
            log.warning("talk: nothing reaches the camera (%s), restarting the talk source", st)
            talk.hangup.set()

    task = asyncio.create_task(check())
    try:
        await talk.gate(True)
        carry = b""
        async for chunk in request.content.iter_chunked(TALK_CHUNK):
            data = carry + chunk
            if len(data) & 1:                   # whole 16-bit samples only, or one skipped chunk would turn the rest into noise
                data, carry = data[:-1], data[-1:]
            else:
                carry = b""
            if data:
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    pass
    finally:
        talk.q = None
        task.cancel()
        st = await asyncio.shield(talk.gate(False))
        log.info("talk ended: %s", st)
    return web.json_response({"ok": True})


def setup(app: web.Application, data: Path):
    rec = app["camrec"] = Recorder(data)
    app["camhead"] = Head(rec)
    talk = app["camtalk"] = Talk(rec)
    global SENS, CLIP_MAX, RECORD
    cur = app["store"].get_setting("cam_prefs", {}) or {}
    SENS, CLIP_MAX, RECORD = float(cur.get("sens", SENS)), float(cur.get("clip_max", CLIP_MAX)), bool(cur.get("record", True))
    rec.quality = cur.get("quality", "hd") if cur.get("quality", "hd") in QUALITY else "hd"
    app["camrec_tasks"] = [asyncio.create_task(talk.start_server()), asyncio.create_task(talk.keeper())]
    app["camrec_tasks"] += [asyncio.create_task(rec.run()), asyncio.create_task(rec.snapshots()), asyncio.create_task(keep_settings(app))]


async def keep_settings(app: web.Application):
    """After every new connection to the camera (it was switched on, or came back) put back the settings the user chose here."""
    rec: Recorder = app["camrec"]
    done_gen = -1
    while True:
        await asyncio.sleep(5)
        try:
            if not rec.connected or rec.gen == done_gen:
                continue
            want = (app["store"].get_setting("cam_prefs", {}) or {}).get("camset", {})
            dev = app["local"].devs.get(CAM_DID)
            if not dev:
                continue
            keys = [k for k in want if k in SETTINGS]
            got = await dev.get([SETTINGS[k] for k in keys]) if keys else {}
            for k in keys:
                if got.get(SETTINGS[k]) != want[k]:
                    code = await dev.set(*SETTINGS[k], want[k])
                    log.info("camera setting %s put back to %s (%s)", k, want[k], code)
            done_gen = rec.gen
        except Exception as e:
            log.warning("keep settings: %s %s", type(e).__name__, e)


# ---- the camera's own switch (MIoT 2.1 "Switch Status": on = filming, off = sleeping) through the direct local bridge
CAM_DID = "1144228498"


def stream_fresh(rec, now=None):
    now = time.time() if now is None else now
    return rec.connected and any(f.track == rec.vtrack and 0 <= now - f.t < 3 for f in reversed(rec.ring))


async def cam_power(request: web.Request):
    rec = request.app["camrec"]
    dev = request.app["local"].devs.get(CAM_DID)
    if not dev:
        raise web.HTTPNotFound(text="camera not configured")
    if request.method == "GET":
        # Fresh camera frames are positive evidence, not a stored thumbnail or an old on/off value.
        cached = getattr(rec, "power_cache", None)
        if cached and cached.get("online") and time.monotonic() - getattr(rec, "power_at", 0) < 2:
            return web.json_response(cached)
        if stream_fresh(rec):
            return web.json_response({"on": True, "online": True, "streaming": True})
        if cached and time.monotonic() - getattr(rec, "power_at", 0) < 2:
            return web.json_response(cached)
        try:
            got = await asyncio.wait_for(dev.get([(2, 1)]), 3.5)
            value = got.get((2, 1))
            if not isinstance(value, bool):
                raise ValueError("missing power state")
            result = {"on": value, "online": True, "streaming": False}
        except Exception:
            result = {"on": None, "online": False, "streaming": False, "error": "camera_unreachable"}
        rec.power_cache, rec.power_at = result, time.monotonic()
        return web.json_response(result)
    on = request.query.get("on", "1") == "1"
    rec.power_cache = None
    try:
        async def change():
            code = await dev.set(2, 1, on)
            if code != 0:
                raise web.HTTPBadGateway(text=f"camera refused ({code})")
            return await dev.get([(2, 1)])
        got = await asyncio.wait_for(change(), 6)
        actual = got.get((2, 1))
        if not isinstance(actual, bool) or actual != on:
            raise web.HTTPBadGateway(text="camera did not confirm requested power state")
    except web.HTTPException:
        raise
    except Exception as e:
        raise web.HTTPBadGateway(text=f"camera control unavailable ({type(e).__name__})")
    if on:
        rec.wake.set()
    rec.power_cache = {"on": actual, "online": True, "streaming": on and stream_fresh(rec)}
    rec.power_at = time.monotonic()
    return web.json_response(rec.power_cache)
