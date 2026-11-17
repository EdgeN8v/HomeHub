"""The service list: the full docker configuration (docker inspect + user networks) of every container on both machines, saved
to the Pi's SD card and a copy on the NAS.

Why: to take a service over on the other machine we must recreate its containers exactly (environment, command, ports, mounts,
memory limit, network, restart policy). When a machine is already dead its containers can no longer be inspected, so the
configuration has to be written down while it is alive. The files hold passwords from the environment variables, so they are
mode 600 and never sent to the phone (only a summary is)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time

import aiohttp

from . import machines as mc
from .monitor import DOCKER_SOCK

log = logging.getLogger("specs")
DIR = mc.get("pi", "specs_dir")                       # on the Pi's SD card (inside this container as well)
NAS_DIR = mc.get("nas", "specs_dir")
EVERY = 6 * 3600


class Specs:
    def __init__(self, monitor):
        self.m, self.at, self.counts = monitor, 0.0, {}
        self.error = None

    async def _pi(self) -> dict:
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=DOCKER_SOCK), timeout=aiohttp.ClientTimeout(total=60)) as http:
            async def get(path):
                async with http.get("http://docker" + path) as r:
                    return await r.json()
            rows = await get("/containers/json?all=1")
            containers = [await get(f"/containers/{c['Id']}/json") for c in rows]
            nets = [n for n in await get("/networks") if n["Name"] not in ("bridge", "host", "none")]
        return {"containers": containers, "networks": nets}

    def _nas(self) -> dict:
        nas = self.m.hosts["nas"]
        nas._ensure()
        ids, _ = nas._run("docker ps -aq", sudo=True)
        containers = json.loads(nas._run("docker inspect " + " ".join(ids.split()), sudo=True)[0]) if ids.split() else []
        names, _ = nas._run("docker network ls --format '{{.Name}}'", sudo=True)
        user = [n for n in names.split() if n not in ("bridge", "host", "none")]
        nets = json.loads(nas._run("docker network inspect " + " ".join(user), sudo=True)[0]) if user else []
        return {"containers": containers, "networks": nets}

    async def refresh(self):
        try:
            got = {"pi": await self._pi()}
            try:
                got["nas"] = await asyncio.to_thread(self._nas)
            except Exception as ex:                      # NAS unreachable: keep its last saved list, do not overwrite
                log.warning("specs: NAS not read: %s", ex)
            os.makedirs(DIR, exist_ok=True)
            now = time.time()
            for key, data in got.items():
                path = os.path.join(DIR, mc.phys(key) + ".json")          # named after the physical machine, whichever one runs this
                tmp = path + ".tmp"
                with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                    json.dump({"at": now, "host": key, **data}, f, ensure_ascii=False)
                os.replace(tmp, path)
                self.counts[key] = len(data["containers"])
            self.at, self.error = now, None
            if "nas" in self.m.hosts:
                await asyncio.to_thread(self._copy_to_nas)
        except Exception as ex:
            log.warning("specs: %s", ex)
            self.error = str(ex)[:120]

    def _copy_to_nas(self):
        """The Pi's list also goes to the NAS (the NAS's own list lives on the Pi), so each machine holds the other's."""
        nas = self.m.hosts["nas"]
        mine = mc.phys("pi") + ".json"
        with open(os.path.join(DIR, mine), "rb") as f:
            blob = f.read()
        ch = nas.work().get_transport().open_session()
        ch.exec_command(nas.rootsh(f"mkdir -p {NAS_DIR} && umask 077 && cat > {NAS_DIR}/{mine}"))
        ch.sendall(blob)
        ch.shutdown_write()
        ch.recv_exit_status()

    def load(self, key: str) -> dict | None:
        try:
            with open(os.path.join(DIR, key + ".json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def summary(self) -> dict:
        return {"at": self.at, "counts": self.counts, "error": self.error}

    async def loop(self):
        await asyncio.sleep(45)
        while True:
            await self.refresh()
            await asyncio.sleep(EVERY)
