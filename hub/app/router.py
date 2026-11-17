"""Direct control of the Xiaomi / MiWiFi router (AX3000T, rd03) over its own local web API: the same calls its admin page makes.

Login: the page hashes the admin password with a fixed key and a one-time nonce (SHA-256 when init_info says newEncryptMode=1,
SHA-1 on older firmware) and gets a session token ("stok") back. The password is typed by the user in HomeHub and stored only in
the data folder (router.json, mode 600); it is never in the code or the repository. The router's admin page stays on the LAN:
phones outside reach it only through HomeHub.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
from pathlib import Path

import aiohttp

log = logging.getLogger("router")
KEY = "a2ffa5c9be07488bbb04a3a47d3c5f6a"          # the constant the router's own login page hashes with (public in its JS)


class RouterError(Exception):
    pass


class Router:
    def __init__(self, data: Path, host: str = "192.168.31.1"):
        self.file = Path(data) / "router.json"
        self.host = host
        self.token = ""
        self.lock = asyncio.Lock()
        self.cache: dict = {"at": 0.0, "data": None}

    # --- stored password -------------------------------------------------------------------------------------------------
    def _cfg(self) -> dict:
        try:
            return json.loads(self.file.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def configured(self) -> bool:
        return bool(self._cfg().get("password"))

    def set_password(self, password: str):
        cfg = {**self._cfg(), "password": password}
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        os.replace(tmp, self.file)
        self.token = ""
        self.cache = {"at": 0.0, "data": None}

    def forget(self):
        try:
            self.file.unlink()
        except OSError:
            pass
        self.token = ""
        self.cache = {"at": 0.0, "data": None}

    # --- web api ----------------------------------------------------------------------------------------------------------
    @staticmethod
    def encode(password: str, nonce: str, sha256: bool) -> str:
        h = hashlib.sha256 if sha256 else hashlib.sha1
        return h((nonce + h((password + KEY).encode()).hexdigest()).encode()).hexdigest()

    async def _http(self, method: str, url: str, **kw):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.request(method, url, **kw) as r:
                return json.loads(await r.text())

    async def login(self) -> str:
        pw = self._cfg().get("password")
        if not pw:
            raise RouterError("还没有填路由器后台密码")
        base = f"http://{self.host}/cgi-bin/luci"
        try:
            info = await self._http("GET", f"{base}/api/xqsystem/init_info")
            nonce = f"0_58:1c:f8:60:20:f7_{int(time.time())}_{random.randint(1000, 9999)}"
            res = await self._http("POST", f"{base}/api/xqsystem/login", data={
                "username": "admin", "logtype": "2", "nonce": nonce,
                "password": self.encode(pw, nonce, info.get("newEncryptMode") == 1)})
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise RouterError(f"路由器没应答：{type(e).__name__}")
        if res.get("code") != 0 or not res.get("token"):
            raise RouterError("路由器后台密码不对" if res.get("code") == 401 else f"登录失败 {res.get('code')} {res.get('msg', '')}")
        self.token = res["token"]
        return self.token

    async def api(self, path: str, data: dict | None = None):
        """GET (or POST with data) /api/<path>; logs in again once when the token is gone."""
        async with self.lock:
            for attempt in (0, 1):
                if not self.token:
                    await self.login()
                url = f"http://{self.host}/cgi-bin/luci/;stok={self.token}/api/{path.lstrip('/')}"
                try:
                    res = await self._http("POST" if data is not None else "GET", url, **({"data": data} if data is not None else {}))
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                    raise RouterError(f"路由器没应答：{type(e).__name__}")
                if isinstance(res, dict) and res.get("code") == 401 and attempt == 0:
                    self.token = ""
                    continue
                return res
        raise RouterError("登录失效")

    # --- what the page shows -----------------------------------------------------------------------------------------------
    async def overview(self) -> dict:
        """Everything the status page needs in one answer (cached 5 s)."""
        if self.cache["data"] and time.time() - self.cache["at"] < 5:
            return self.cache["data"]
        st, wan, wifi = await asyncio.gather(self.api("misystem/status"), self.api("xqnetwork/wan_info"), self.api("xqnetwork/wifi_detail_all"),
                                              return_exceptions=True)
        for x in (st, wan, wifi):
            if isinstance(x, RouterError):
                raise x
        data = {"status": st if isinstance(st, dict) else {}, "wan": wan if isinstance(wan, dict) else {}, "wifi": wifi if isinstance(wifi, dict) else {}}
        self.cache = {"at": time.time(), "data": data}
        return data
