"""Keep the Pi's copy of the music-update folder in step with the one the Music app is published into on the NAS.

The Music project publishes new app versions by copying them into the NAS folder (learnsync/music-update). The web server that hands them
to the phones now runs on the Pi (it was moved there) and serves its OWN copy of that folder. This copies, every two minutes, the files that are
new or changed on the NAS into the Pi's folder. It never deletes anything and leaves alone what the hub writes there itself (endpoints.json)."""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import shlex
import time

from . import machines as mc

log = logging.getLogger("mirror")
SKIP = {"endpoints.json"}                      # written by the hub into the Pi's folder; the NAS copy of it is older
EVERY = 120


class _Local:
    def __init__(self, folder: str | None = None, pattern: str | None = None):
        self.folder = folder or (sorted(glob.glob(pattern))[0] if pattern and glob.glob(pattern) else None)

    def list(self) -> dict:
        out = {}
        if self.folder and os.path.isdir(self.folder):
            for e in os.scandir(self.folder):
                if e.is_file() and not e.name.startswith(".") and e.name not in SKIP:
                    st = e.stat()
                    out[e.name] = (st.st_size, int(st.st_mtime))
        return out

    def read(self, name: str) -> bytes:
        with open(os.path.join(self.folder, name), "rb") as f:
            return f.read()

    def write(self, name: str, data: bytes, mtime: int):
        tmp = os.path.join(self.folder, "." + name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.utime(tmp, (mtime, mtime))
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(self.folder, name))


class _Remote:
    def __init__(self, host, folder: str | None = None, pattern: str | None = None):
        self.h = host
        if folder is None and pattern:
            host._ensure()
            out, _ = host._run(f"ls -d {pattern} 2>/dev/null | head -1", sudo=True)
            folder = out.strip() or None
        self.folder = folder

    def list(self) -> dict:
        if not self.folder:
            return {}
        self.h._ensure()
        out, _ = self.h._run(f"find {shlex.quote(self.folder)} -maxdepth 1 -type f -printf '%f\t%s\t%T@\n'", sudo=True)
        res = {}
        for ln in out.splitlines():
            name, size, mt = ln.split("\t")
            if not name.startswith(".") and name not in SKIP:
                res[name] = (int(size), int(float(mt)))
        return res

    def read(self, name: str) -> bytes:
        self.h._ensure()
        ch = self.h.work().get_transport().open_session()
        ch.exec_command(self.h.rootsh(f"cat {shlex.quote(self.folder + '/' + name)}"))
        data = ch.makefile("rb").read()
        if ch.recv_exit_status():
            raise RuntimeError(f"读取 {name} 失败")
        return data

    def write(self, name: str, data: bytes, mtime: int):
        self.h._ensure()
        dst, tmp = self.folder + "/" + name, self.folder + "/." + name + ".tmp"
        ch = self.h.work().get_transport().open_session()
        ch.exec_command(self.h.rootsh(f"cat > {shlex.quote(tmp)} && chmod 644 {shlex.quote(tmp)} && touch -d @{mtime} {shlex.quote(tmp)} && mv {shlex.quote(tmp)} {shlex.quote(dst)}"))
        ch.sendall(data)
        ch.shutdown_write()
        if ch.recv_exit_status():
            raise RuntimeError(f"写入 {name} 失败")


class FolderMirror:
    def __init__(self, monitor):
        self.m = monitor
        self.status = {"at": 0, "copied": 0, "error": None}

    def _ends(self):
        """(source = the NAS folder, destination = the Pi's folder), each as local files or over SSH, whichever machine this runs on."""
        here = mc.phys("pi")                                   # the physical machine this process runs on
        remote = self.m.hosts.get("nas")                       # ... and the other one is reached over SSH
        nas_dir = mc.CFG["machines"][mc.phys("nas")]["data_root"] + "/learnsync/music-update"     # the physical NAS's entry
        pat = mc.CFG["machines"][mc.phys("pi")]["apps_root"] + "/music-update/*/*/music-update"    # the physical Pi's entry
        nas_pat = mc.CFG["machines"][mc.phys("nas")]["apps_root"] + "/music-update/*/*/music-update"   # where it runs when it was moved to the NAS
        if here == "nas":
            return _Local(folder=nas_dir), [d for d in ((_Remote(remote, pattern=pat) if remote else None), _Local(pattern=nas_pat)) if d]
        src = _Remote(remote, folder=nas_dir) if remote else None
        return src, [d for d in (_Local(pattern=pat), (_Remote(remote, pattern=nas_pat) if remote else None)) if d]

    def run(self) -> int:
        src, dsts = self._ends()
        dsts = [d for d in dsts if d.folder]                   # wherever a moved copy of the service exists (on the Pi, on the NAS, or both)
        if src is None or not dsts:
            return 0                                           # nothing to do: the service was never moved, or the other machine is not reachable
        want = src.list()
        n = 0
        for dst in dsts:
            have = dst.list()
            for name, (size, mtime) in sorted(want.items()):
                cur = have.get(name)
                if cur and cur[0] == size and cur[1] >= mtime - 2:
                    continue
                dst.write(name, src.read(name), mtime)
                n += 1
                log.info("music-update: copied %s (%d bytes) to %s", name, size, dst.folder)
        self.status.update(at=time.time(), copied=self.status["copied"] + n, error=None)
        return n

    async def loop(self):
        await asyncio.sleep(40)
        while True:
            try:
                await asyncio.to_thread(self.run)
            except Exception as ex:
                self.status["error"] = str(ex)[:120]
                log.warning("music-update mirror: %s", ex)
            await asyncio.sleep(EVERY)
