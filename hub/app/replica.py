"""Copies of the data of services that run on the Pi, back to the NAS.

The rule: the data lives where the service runs, and the other machine gets a nightly copy.
  * service on the NAS: the Pi pulls it every night at 03:31 (script /usr/local/bin/nas-nightly-backup.sh, rsync from the NAS);
  * service on the Pi (moved there): this module pushes it to its old folder on the NAS every night at 03:00, only files that are new
    or changed, nothing is ever deleted on the NAS.
When a service moves, the direction follows by itself: the entries here are used only while the service really runs on the Pi, and the
Pi's nightly script is told (nightly-skip.txt) to leave out what is already live on the Pi."""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import shlex
import tarfile
import time

from . import machines as mc

log = logging.getLogger("replica")
BATCH = 300 * 1024 ** 2
SKIP_FILE = mc.get("pi", "data_root") + "/nightly-skip.txt"
# what the Pi's nightly pull script calls a dataset (its label) -> a container that means "this service lives on the Pi now"
NIGHTLY_LABELS = {
    "immich 照片库+数据库备份": "immich_server",
    "记账数据库": "ledger-db-1",
}


class Replicator:
    def __init__(self, monitor, store):
        self.m, self.store, self.busy = monitor, store, False

    def state(self) -> dict | None:
        reps = self.store.get_setting("replicas", [])
        if not reps:
            return None
        return {**self.store.get_setting("replica_status", {}), "busy": self.busy, "count": len(reps)}

    # ---- where does each service run
    def _pi_containers(self) -> set[str]:
        return {c["name"] for c in self.m.state.get("pi", {}).get("containers", []) if c.get("running")}

    def live_on_pi(self, service: str, running: set[str]) -> bool:
        """The service's data is live on the Pi when one of its containers runs there. Unknown service: assume yes (entries only exist
        for services that were moved to the Pi)."""
        for mig in self.store.get_setting("migrations", []):
            if mig.get("name") == service:
                return any(n in running for n in mig.get("members", []))
        return True

    _pushed: dict = {}

    def push_inputs(self):
        """What the NAS needs to bring the moved services back by itself when the Pi is gone: which folder on the NAS is the copy of which
        folder on the Pi (replicas), and which services live on the Pi (migrations). The container list itself is written by Specs."""
        nas = self.m.hosts.get("nas")
        if nas is None or mc.SWAPPED:                      # the NAS's recovery reads these; only the Pi's ops writes them
            return
        spec_dir = mc.get("nas", "specs_dir")
        usage = {c["name"]: c["mem"] for c in self.m.state.get("pi", {}).get("containers", []) if c.get("running") and c.get("mem")}
        for name, blob in (("replicas.json", json.dumps(self.store.get_setting("replicas", []), ensure_ascii=False).encode()),
                           ("migrations.json", json.dumps(self.store.get_setting("migrations", []), ensure_ascii=False).encode()),
                           ("usage.json", json.dumps(usage).encode())):      # how much memory each service really uses (the limit says little)
            if self._pushed.get(name) == blob:
                continue
            ch = nas.work().get_transport().open_session()
            ch.exec_command(nas.rootsh(f"mkdir -p {shlex.quote(spec_dir)} && umask 077 && cat > {shlex.quote(spec_dir + '/' + name)}"))
            ch.sendall(blob)
            ch.shutdown_write()
            if ch.recv_exit_status() == 0:
                self._pushed[name] = blob

    def write_skip(self):
        """Tell the Pi's nightly pull which datasets are live on the Pi (so it must not pull the NAS's old copy over its own view)."""
        running = self._pi_containers()
        if not self.m.state.get("pi", {}).get("ok"):
            return
        labels = sorted(label for label, c in NIGHTLY_LABELS.items() if c in running)
        try:
            tmp = SKIP_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("".join(l + "\n" for l in labels))
            os.replace(tmp, SKIP_FILE)
        except OSError as ex:
            log.warning("skip list: %s", ex)

    async def run(self):
        if self.busy:
            return
        self.busy = True
        t0, sent, err, detail = time.time(), 0, None, []
        try:
            nas = self.m.hosts["nas"]
            await asyncio.to_thread(nas._ensure)
            running = self._pi_containers()
            for rep in self.store.get_setting("replicas", []):
                if not self.live_on_pi(rep["service"], running):
                    detail.append({"path": rep["pi"], "skipped": "服务现在不在香橙派上"})
                    continue
                t1 = time.time()
                n, info = await asyncio.to_thread(self._push, nas, rep["pi"], rep["nas"])
                sent += n
                info.update(path=rep["pi"], secs=round(time.time() - t1))
                detail.append(info)
                log.info("replica %s: %s", rep["pi"], {k: v for k, v in info.items() if k != "path"})
        except Exception as ex:
            log.warning("replica: %s", ex)
            err = str(ex)[:160]
        finally:
            self.busy = False
            self.store.set_setting("replica_status", {"at": time.time(), "ok": err is None, "error": err, "bytes": sent,
                                                      "secs": round(time.time() - t0), "detail": detail})

    @staticmethod
    def _remote_files(nas, nas_dir: str, only: str = "") -> dict:
        """What is on the NAS under nas_dir: relative path -> (size, mtime in whole seconds). The `cd` has to be INSIDE the sh -c: with
        `sudo cd ... && find ...` sudo cannot run `cd`, find never ran, the list came back empty and every file was sent again."""
        inner = "cd " + shlex.quote(nas_dir) + " 2>/dev/null && find . " + (f"-maxdepth 1 -name {shlex.quote(only)} " if only else "") + "-type f -printf '%P\t%s\t%T@\n'"
        cmd = nas.rootsh(inner)
        _, o, e = nas.work().exec_command(cmd, timeout=900)
        out = o.read().decode(errors="replace")
        remote = {}
        for ln in out.splitlines():
            parts = ln.rsplit("\t", 2)
            if len(parts) == 3:
                try:
                    remote[parts[0]] = (int(parts[1]), int(float(parts[2])))
                except ValueError:
                    pass
        return remote

    def _push(self, nas, pi_dir: str, nas_dir: str, exclude: tuple = ()) -> tuple[int, dict]:
        only = ""
        if os.path.isfile(pi_dir):                                 # a single file mounted into a container (not a folder): send just that file
            pi_dir, only = os.path.split(pi_dir)
            nas_dir = os.path.dirname(nas_dir.rstrip("/"))
        elif not os.path.isdir(pi_dir):
            raise RuntimeError(f"香橙派上找不到 {pi_dir}")
        local = {}
        for root, _, files in os.walk(pi_dir):
            if only and root != pi_dir:
                break
            for f in files:
                if only and f != only:
                    continue
                if any(fnmatch.fnmatch(f, pat) for pat in exclude):
                    continue
                path = os.path.join(root, f)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                local[os.path.relpath(path, pi_dir).replace(os.sep, "/")] = (st.st_size, int(st.st_mtime))
        remote = self._remote_files(nas, nas_dir, only)
        todo, why = [], {"新文件": 0, "大小变了": 0, "时间变了": 0}
        for r, v in local.items():
            rv = remote.get(r)
            if rv == v:
                continue
            todo.append(r)
            why["新文件" if rv is None else "大小变了" if rv[0] != v[0] else "时间变了"] += 1
        info = {"local_files": len(local), "nas_files": len(remote), "to_send": len(todo), "to_send_mb": round(sum(local[r][0] for r in todo) / 1048576), "why": why}
        if len(local) > 100 and not remote:                       # an empty answer for a big folder is not believable: stop, do not resend it all
            raise RuntimeError(f"读不到 NAS 上 {nas_dir} 的文件清单，不发送")
        sent, batch, size = 0, [], 0
        for r in todo + [None]:
            if r is not None:
                batch.append(r)
                size += local[r][0]
            if batch and (r is None or size >= BATCH):
                ch = nas.work().get_transport().open_session()
                ch.exec_command(nas.rootsh(f"mkdir -p {shlex.quote(nas_dir)} && tar -xpf - -C {shlex.quote(nas_dir)}"))
                out = ch.makefile_stdin("wb", 1 << 20)           # buffered: much faster than one tiny write per tar block
                with tarfile.open(fileobj=out, mode="w|") as tf:
                    for f in batch:
                        try:
                            tf.add(os.path.join(pi_dir, f), arcname=f, recursive=False)
                        except OSError:
                            pass                                   # vanished while running
                out.flush()                                       # tarfile does not flush a file object it was given
                ch.shutdown_write()
                if ch.recv_exit_status():
                    raise RuntimeError("复制到 NAS 失败：" + nas.scrub(ch.makefile_stderr("r").read().decode(errors="replace"))[-100:])
                sent += size
                batch, size = [], 0
        return sent, info

    async def nightly(self):
        done_day = None
        await asyncio.sleep(60)
        self.write_skip()                                          # right after a start, not only 5 minutes later
        await asyncio.to_thread(self.push_inputs)
        while True:
            await asyncio.sleep(300)
            try:
                self.write_skip()                                  # keeps the Pi's 03:31 pull in step with where services run
            except Exception as ex:
                log.warning("skip list: %s", ex)
            try:
                await asyncio.to_thread(self.push_inputs)
            except Exception as ex:
                log.warning("recovery inputs: %s", ex)
            t = time.localtime()
            if t.tm_hour == 3 and t.tm_min < 25 and done_day != t.tm_yday and self.store.get_setting("replicas", []):
                done_day = t.tm_yday
                await self.run()
