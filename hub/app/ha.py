"""Minimal async Home Assistant client: REST for states / services / history, WebSocket for live state changes."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger("ha")


class HA:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.token = token
        self.session: aiohttp.ClientSession | None = None

    async def start(self):
        self.session = aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=30))

    async def close(self):
        if self.session:
            await self.session.close()

    async def state(self, entity_id: str) -> dict:
        async with self.session.get(f"{self.url}/api/states/{entity_id}") as r:
            r.raise_for_status()
            return await r.json()

    async def states(self, entity_ids: list[str]) -> dict[str, dict]:
        async with self.session.get(f"{self.url}/api/states") as r:
            r.raise_for_status()
            want = set(entity_ids)
            return {s["entity_id"]: s for s in await r.json() if s["entity_id"] in want}

    async def call(self, domain: str, service: str, data: dict):
        async with self.session.post(f"{self.url}/api/services/{domain}/{service}", json=data) as r:
            r.raise_for_status()
            return await r.json()

    async def automation_config(self, automation_id: str) -> dict | None:
        """The stored config (alias, description, triggers...) of a UI-made automation, or None."""
        async with self.session.get(f"{self.url}/api/config/automation/config/{automation_id}") as r:
            return await r.json() if r.status == 200 else None

    async def save_automation_config(self, automation_id: str, config: dict):
        async with self.session.post(f"{self.url}/api/config/automation/config/{automation_id}", json=config) as r:
            if r.status != 200:
                raise RuntimeError(f"HA refused the automation: {await r.text()}")

    async def delete_automation_config(self, automation_id: str):
        async with self.session.delete(f"{self.url}/api/config/automation/config/{automation_id}") as r:
            r.raise_for_status()

    async def flow_step(self, flow_id: str, data: dict) -> dict:
        """Continue a config flow (e.g. accept a discovered device)."""
        async with self.session.post(f"{self.url}/api/config/config_entries/flow/{flow_id}", json=data) as r:
            r.raise_for_status()
            return await r.json()

    async def flow_get(self, flow_id: str) -> dict:
        """The step a config flow is waiting at (its form), without answering it."""
        async with self.session.get(f"{self.url}/api/config/config_entries/flow/{flow_id}") as r:
            r.raise_for_status()
            return await r.json()

    async def flow_post(self, flow_id: str, data: dict) -> tuple[int, dict]:
        """Answer a config flow step; HA's 400 carries the form errors, so the status is returned too."""
        async with self.session.post(f"{self.url}/api/config/config_entries/flow/{flow_id}", json=data) as r:
            return r.status, await r.json()

    async def config_translations(self, domain: str, language: str = "zh-Hans") -> dict[str, str]:
        """HA's own wording for an integration's setup screens (labels, descriptions, errors)."""
        r = await self.ws_commands([{"type": "frontend/get_translations", "language": language,
                                     "category": "config", "integration": [domain]}])
        return r["frontend/get_translations"].get("resources", {})

    async def history(self, start: datetime, entity_ids: list[str]) -> list[tuple[str, str, str]]:
        """(entity_id, state, last_changed ISO) for every recorded change since start."""
        params = {
            "filter_entity_id": ",".join(entity_ids),
            "no_attributes": "",
            "significant_changes_only": "0",
            "end_time": datetime.now(start.tzinfo).isoformat(),
        }
        async with self.session.get(f"{self.url}/api/history/period/{start.isoformat()}", params=params) as r:
            r.raise_for_status()
            out = []
            for series in await r.json():
                for s in series:
                    out.append((s["entity_id"], s["state"], s["last_changed"]))
            return out

    async def history_series(self, start: datetime, entity_id: str, attr: str | None = None) -> list[tuple[datetime, float]]:
        """(time, number) for every recorded change of one entity since start; the number is the state,
        or an attribute of it when attr is given. Non-numeric states are skipped."""
        params = {"filter_entity_id": entity_id, "significant_changes_only": "0",
                  "end_time": datetime.now(start.tzinfo).isoformat()}
        if not attr:
            params["no_attributes"] = ""
        async with self.session.get(f"{self.url}/api/history/period/{start.isoformat()}", params=params) as r:
            r.raise_for_status()
            data = await r.json()
        out = []
        for s in data[0] if data else []:
            raw = s["attributes"].get(attr) if attr else s["state"]
            try:
                out.append((datetime.fromisoformat(s["last_updated"]), float(raw)))
            except (TypeError, ValueError):
                pass
        return out

    async def ws_commands(self, types: list[str | dict]) -> dict[str, object]:
        """Run a few one-shot WebSocket commands (registries etc.) on a short-lived connection.
        A command is a type string, or a dict with "type" plus its fields; results are keyed by type."""
        # get_states for ~1000 entities is a few MB: lift aiohttp's 4 MB frame limit
        async with self.session.ws_connect(self.url.replace("http", "ws", 1) + "/api/websocket", max_msg_size=0) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": self.token})
            if (await ws.receive_json()).get("type") != "auth_ok":
                raise RuntimeError("HA auth failed")
            out = {}
            for i, t in enumerate(types, 1):
                cmd = t if isinstance(t, dict) else {"type": t}
                await ws.send_json({"id": i, **cmd})
                msg = await ws.receive_json(timeout=60)
                if not msg.get("success"):
                    raise RuntimeError(f"{cmd['type']}: {msg.get('error')}")
                out[cmd["type"]] = msg["result"]
            return out

    async def wait_running(self, timeout: float = 600):
        """Block until HA reports it has finished starting. Right after an HA restart the API is
        already up while integrations are still loading, and the registries come back empty."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                async with self.session.get(f"{self.url}/api/config") as r:
                    if r.status == 200 and (await r.json()).get("state") == "RUNNING":
                        return
            except aiohttp.ClientError:
                pass
            if asyncio.get_running_loop().time() > deadline:
                log.warning("HA still not RUNNING after %ss, going on anyway", timeout)
                return
            await asyncio.sleep(5)

    async def listen(self, on_change: Callable[[str, dict], Awaitable[None]],
                     on_connect: Callable[[], Awaitable[None]]):
        """Stream state changes (entity_id, new_state) forever, reconnecting with backoff. on_connect
        runs after every (re)connect so callers can resync anything missed while disconnected."""
        delay = 5
        ws_url = self.url.replace("http", "ws", 1) + "/api/websocket"
        while True:
            try:
                async with self.session.ws_connect(ws_url, heartbeat=30, max_msg_size=0) as ws:
                    await ws.receive_json()  # auth_required
                    await ws.send_json({"type": "auth", "access_token": self.token})
                    msg = await ws.receive_json()
                    if msg.get("type") != "auth_ok":
                        raise RuntimeError(f"HA auth failed: {msg}")
                    await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": "state_changed"})
                    log.info("connected to HA websocket")
                    delay = 5
                    await on_connect()
                    async for m in ws:
                        if m.type != aiohttp.WSMsgType.TEXT:
                            break
                        data = m.json()
                        if data.get("type") != "event":
                            continue
                        new = data["event"]["data"].get("new_state")
                        if new:
                            await on_change(new["entity_id"], new)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep listening whatever went wrong
                log.warning("HA websocket dropped: %s; retry in %ss", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
