"""Give the services back to the Orange Pi WITH the data they produced on the NAS in the meantime.

While the Pi was gone the NAS ran its services on working copies of their data (small folders were copied, the Immich library was used
where it is). When the Pi is back those services have newer data than the Pi's own folders. Steps:
  1. read from the NAS sentinel what runs there and where each service's data is;
  2. stop the stand-ins on the NAS (stopped, not removed: their data stands still) and make sure the Pi's own copies are stopped;
  3. carry the data back: a small folder replaces the Pi's folder (the old one is kept next to it as `<name>.before-failback-<time>`),
     a big one (the library) is brought up to date with rsync, adding and updating files, never deleting;
  4. the NAS removes its stand-ins and gives its old port forwarders back; the Pi's sentinel starts the Pi's services, which now see the
     newer data.
Runs in the ops service, which is on the Pi: it needs the Pi's disks and its docker."""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import ssl
import tarfile
import time
import uuid

import aiohttp

from . import machines as mc
from .migrate import NAS_IP, RSYNC_IMAGE, RSYNC_PASS, RSYNC_PORT, RSYNC_USER

log = logging.getLogger("failback")
SENTINEL = os.environ.get("NAS_SENTINEL_URL", mc.sentinel_url("nas"))
RSYNC_MODULES = {mc.get("nas", "data_root") + "/learnsync/immich/library": "immich"}   # NAS folder that IS the root of a module of the NAS's rsync daemon
STEPS = ["读取 NAS 上代跑的服务", "停下 NAS 上代跑的服务", "把新数据带回香橙派", "交还，香橙派上的服务重新启动"]


class Failback:
    def __init__(self, monitor, store, migrator):
        self.m, self.store, self.mig, self.jobs = monitor, store, migrator, {}

    def start(self) -> str:
        job = uuid.uuid4().hex[:8]
        self.jobs[job] = {"id": job, "started": time.time(), "steps": [{"t": t, "s": "wait"} for t in STEPS], "done": False, "ok": None, "error": None}
        asyncio.create_task(self.run(job))
        return job

    def _set(self, job: str, i: int, s: str, note: str | None = None, pct: int | None = None):
        st = self.jobs[job]["steps"][i]
        st["s"] = s
        if note is not None:
            st["note"] = note
        if pct is not None:
            st["pct"] = pct
        if s == "ok":
            st["pct"] = 100

    async def _sent(self, method: str, path: str, body=None):
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
            async with s.request(method, SENTINEL + path, json=body, headers={"Authorization": "Bearer " + os.environ["SENTINEL_TOKEN"]}, ssl=ctx) as r:
                data = await r.json()
                if r.status >= 300:
                    raise RuntimeError(str(data.get("error", data))[:160])
                return data

    # ---- data
    def _pull_dir(self, nas, work: str, pi_dir: str, note):
        """A NAS folder (as root) streamed here into <pi_dir>.incoming, then swapped in; the old folder stays beside it."""
        incoming = pi_dir.rstrip("/") + ".incoming"
        import shutil
        shutil.rmtree(incoming, ignore_errors=True)
        os.makedirs(incoming)
        cmd = nas.rootsh(f"tar -C {shlex.quote(work)} -cf - .")
        _, o, e = nas.work().exec_command(cmd, timeout=3600)
        got = [0]

        class Counting:
            def read(self, n=-1):
                b = o.read(n)
                got[0] += len(b or b"")
                if got[0] % (50 << 20) < len(b or b""):
                    note(f"{got[0] // (1 << 20)} MB")
                return b
        with tarfile.open(fileobj=Counting(), mode="r|") as t:
            t.extractall(incoming, filter="fully_trusted")
        if o.channel.recv_exit_status():
            raise RuntimeError("读取 NAS 数据失败：" + nas.scrub(e.read().decode(errors="replace"))[-100:])
        old = f"{pi_dir.rstrip('/')}.before-failback-{int(time.time())}"
        if os.path.exists(pi_dir):
            os.rename(pi_dir, old)
        os.rename(incoming, pi_dir)

    async def _rsync_in(self, nas_path: str, pi_dir: str, progress):
        p = nas_path.rstrip("/")
        for base, module in RSYNC_MODULES.items():
            if p == base or p.startswith(base + "/"):
                rel = p[len(base):].lstrip("/")
                os.makedirs(pi_dir, exist_ok=True)
                await self.mig._helper(RSYNC_IMAGE, ["rsync", "-a", "--partial", "--info=progress2", "--password-file=/p", "--timeout=900",
                                                     f"rsync://{RSYNC_USER}@{NAS_IP}:{RSYNC_PORT}/{module}/" + (rel + "/" if rel else ""), "/dest/"],
                                       [f"{RSYNC_PASS}:/p:ro", f"{pi_dir}:/dest"], host_net=True, progress=progress)
                return
        raise RuntimeError(f"{nas_path} 太大，又没有 rsync 通道，不能带回")

    async def run(self, job: str):
        step = 0
        nas = self.m.hosts["nas"]
        try:
            self._set(job, 0, "run")
            state = await self._sent("POST", "/recovery/state", {})
            names, data = state.get("containers", []), state.get("data", [])
            if not names:
                raise RuntimeError("NAS 现在没有在代跑服务，不用交还")
            self._set(job, 0, "ok", f"{len(names)} 个容器，{len(data)} 个数据文件夹")

            step = 1; self._set(job, 1, "run")
            await self._sent("POST", "/recovery/stop", {})
            for n in names:                                           # the Pi's own copies must not run while their folders change
                try:
                    await self.mig._pi("POST", f"/containers/{n}/stop?t=20", raw=True)
                except RuntimeError:
                    pass
            self._set(job, 1, "ok")

            step = 2; self._set(job, 2, "run")
            for k, d in enumerate(data):
                label = os.path.basename(d["pi"].rstrip("/"))
                base_pct = int(100 * k / max(len(data), 1))
                self._set(job, 2, "run", f"{k + 1} / {len(data)} · {label}", base_pct)
                if d.get("direct"):
                    await self._rsync_in(d["nas"], d["pi"], lambda p, b=base_pct, n=max(len(data), 1): self._set(job, 2, "run", None, b + int(p / n)))
                else:
                    await asyncio.to_thread(self._pull_dir, nas, d["nas"], d["pi"], lambda t, k=k, label=label: self._set(job, 2, "run", f"{k + 1} / {len(data)} · {label} · {t}"))
            self._set(job, 2, "ok")

            step = 3; self._set(job, 3, "run")
            await self._sent("POST", "/recovery/failback", {})
            for _ in range(40):                                       # the Pi's sentinel starts its services within a few seconds
                up = 0
                for n in names:
                    try:
                        up += bool((await self.mig._pi("GET", f"/containers/{n}/json"))["State"].get("Running"))
                    except RuntimeError:
                        pass
                if up == len(names):
                    break
                await asyncio.sleep(3)
            self._set(job, 3, "ok", f"{up} / {len(names)} 个服务已在香橙派上运行")
            self.jobs[job].update(done=True, ok=True, ended=time.time())
        except Exception as ex:
            log.warning("failback failed at step %d: %s", step, ex)
            self._set(job, step, "fail")
            self.jobs[job].update(done=True, ok=False, ended=time.time(),
                                  error=f"{STEPS[step]}：{str(ex)[:150]}。NAS 上的代跑已经停下但还没删除，数据都在；告诉我再处理")
