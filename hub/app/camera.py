"""The camera picture: go2rtc (a container next to the hub) talks to the Xiaomi camera over the LAN; this passes its player page,
its websocket and snapshots through the hub so the phone only ever talks to the hub (own login, works from outside too).

Only the player and the stream of the known cameras are passed. go2rtc's own api can rewrite its config (even run programs), so
nothing else of it is reachable through here.
"""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp
from aiohttp import WSMsgType, web

log = logging.getLogger("camera")
BASE = os.environ.get("GO2RTC_URL", "http://127.0.0.1:1984")
STREAMS = {"c700", "c700m"}          # c700: as the camera sends it (H.265); c700m: the same as motion-JPEG, which every phone shows
FILES = {"stream.html", "video-stream.js", "video-rtc.js"}
LIVE = {"api/ws", "api/frame.jpeg", "api/stream.mp4", "api/stream.m3u8", "api/frame.mp4", "api/stream.mjpeg"}


def allowed(path: str, query) -> bool:
    if path in FILES:
        return True
    return path in LIVE and query.get("src") in STREAMS


async def cam_proxy(request: web.Request):
    path = request.match_info["tail"] or "stream.html"
    if not allowed(path, request.query):
        raise web.HTTPNotFound()
    url = f"{BASE}/{path}" + (f"?{request.query_string}" if request.query_string else "")
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=5))
    try:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            up = await session.ws_connect(url)
            ws = web.WebSocketResponse()
            await ws.prepare(request)

            async def pump(src, dst):
                async for m in src:
                    if m.type == WSMsgType.TEXT:
                        await dst.send_str(m.data)
                    elif m.type == WSMsgType.BINARY:
                        await dst.send_bytes(m.data)
                    else:
                        break
                await dst.close()
            await asyncio.wait([asyncio.create_task(pump(ws, up)), asyncio.create_task(pump(up, ws))], return_when=asyncio.FIRST_COMPLETED)
            return ws
        async with session.get(url) as r:
            resp = web.StreamResponse(status=r.status, headers={k: v for k, v in r.headers.items() if k.lower() in ("content-type", "cache-control")})
            await resp.prepare(request)
            async for chunk in r.content.iter_any():
                await resp.write(chunk)
            await resp.write_eof()
            return resp
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("%s: %s %s", path, type(e).__name__, e)
        raise web.HTTPBadGateway(text=f"go2rtc: {type(e).__name__}")
    except (ConnectionResetError, asyncio.CancelledError):
        raise
    finally:
        await session.close()


MOVES = {"left": 1, "right": 2, "up": 3, "down": 4, "stop": -1001}     # the camera's motor command: {"operation": n}


async def cam_move(request: web.Request):
    """POST /api/cam/move?dir=left|right|up|down|stop (&src=c700): turn the camera head, through go2rtc's motor call."""
    d = request.query.get("dir", "")
    src = request.query.get("src", "c700")
    if d not in MOVES or src not in STREAMS:
        raise web.HTTPBadRequest(text="dir must be left/right/up/down/stop")
    body = '{"operation":%d}' % MOVES[d]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(f"{BASE}/api/xiaomi/motor", params={"src": src, "body": body}) as r:
                text = await r.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise web.HTTPBadGateway(text=f"go2rtc: {type(e).__name__}")
    return web.json_response({"ok": r.status == 200, "dir": d, "reply": text[:80]})
