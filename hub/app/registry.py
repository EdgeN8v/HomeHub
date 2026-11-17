"""The home image registry (container `registry` on the Pi, https://192.168.31.61:5000, self-signed cert trusted by both docker daemons).

Why: the images of our own services were only ever built on the NAS, and the public ones come from registries that are slow or
blocked from here. With every image used by either machine copied into this registry, either machine can pull what it needs to
take a service over. `sync` pushes every image that a container on the Pi or the NAS uses and the registry does not have yet
(`:latest` tags are pushed again, since they move)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import tarfile
import time
import uuid

import aiohttp

from . import machines as mc
from .monitor import DOCKER_SOCK

log = logging.getLogger("registry")
ADDR = mc.registry_addr("pi")
API = "https://127.0.0.1:5000"                # this container shares the Pi's network; the registry is on the same machine
ACCEPT = ("application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json, "
          "application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.index.v1+json")
MIN_FREE = 5 * 1024 ** 3
LOCAL = "/registry-data"                       # the Pi's registry data, mounted read-only into this container
MIRROR = mc.get("nas", "registry_dir")   # its copy on the NAS: data/ (the registry's files), certs/ (the standby's TLS cert)
NAS_ADDR = mc.registry_addr("nas")              # the standby registry on the NAS, serving the copy read-only
BATCH = 300 * 1024 ** 2
KEEP = 2                                       # versions kept per repository: the newest and one older (and any a container uses)
TAB, NL = chr(9), chr(10)


def target(image: str) -> tuple[str, str] | None:
    """(repository path in the registry, tag) for an image name as a container lists it; None if it is not a plain name."""
    if not image or image.startswith("sha256:") or image.startswith(ADDR):
        return None
    name, tag = image, "latest"
    if ":" in image.rsplit("/", 1)[-1]:
        name, tag = image.rsplit(":", 1)
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0]):
        parts = parts[1:]                    # drop the source registry's host name
    return "/".join(parts), tag


class Registry:
    def __init__(self, monitor, store=None):
        self.m, self.store, self.jobs, self._cache = monitor, store, {}, (0.0, None)
        self.mirror_busy = False

    # ---- the copy of the registry on the NAS: files are content-addressed, so syncing = send what is missing, drop what is gone
    def _newest(self) -> float:
        """mtime of the newest image (manifest link) in the registry; anything newer than the last NAS sync is not copied yet."""
        best = 0.0
        for root, _, files in os.walk(os.path.join(LOCAL, "docker/registry/v2/repositories")):
            for f in files:
                if f == "link":
                    best = max(best, os.path.getmtime(os.path.join(root, f)))
        return best

    def mirror_state(self) -> dict:
        st = self.store.get_setting("registry_mirror", {}) if self.store else {}
        try:
            dirty = self._newest() > st.get("at", 0) + 5
        except OSError:
            dirty = False
        return {**st, "dirty": dirty, "busy": self.mirror_busy, "address": NAS_ADDR, "path": MIRROR + "/data"}

    def start_mirror(self) -> str | None:
        if self.mirror_busy:
            return next((k for k, j in self.jobs.items() if j.get("kind") == "mirror" and not j["done"]), None)
        job = uuid.uuid4().hex[:8]
        self.jobs[job] = {"id": job, "kind": "mirror", "done": False, "ok": None, "error": None,
                          "steps": [{"t": t, "s": "wait"} for t in ("对比两边的文件", "把缺的复制到 NAS", "删掉 NAS 上多余的", "确认 NAS 上的备用仓库在运行")]}
        self.mirror_busy = True
        asyncio.create_task(self._mirror(job))
        return job

    async def _mirror(self, job: str):
        j = self.jobs[job]

        def step(i, st, note=None):
            j["steps"][i]["s"] = st
            if note:
                j["steps"][i]["note"] = note

        try:
            nas = self.m.hosts["nas"]
            await asyncio.to_thread(nas._ensure)
            step(0, "run")
            res = await asyncio.to_thread(self._mirror_blocking, nas, step)
            step(3, "run")
            await asyncio.to_thread(self._standby, nas)
            step(3, "ok")
            if self.store:
                self.store.set_setting("registry_mirror", {"at": time.time(), "ok": True, **res})
            j.update(done=True, ok=True)
        except Exception as ex:
            log.warning("registry mirror: %s", ex)
            for st in j["steps"]:
                if st["s"] == "run":
                    st["s"] = "fail"
            if self.store:
                old = self.store.get_setting("registry_mirror", {})
                self.store.set_setting("registry_mirror", {**old, "ok": False, "error": str(ex)[:160], "failed_at": time.time()})
            j.update(done=True, ok=False, error=str(ex)[:160])
        finally:
            self.mirror_busy = False

    def _mirror_blocking(self, nas, step) -> dict:
        skip = lambda rel: "_uploads" in rel.split("/")
        local = {}
        for root, _, files in os.walk(LOCAL):
            for f in files:
                path = os.path.join(root, f)
                rel = os.path.relpath(path, LOCAL).replace(os.sep, "/")
                if not skip(rel):
                    st = os.stat(path)
                    local[rel] = (st.st_size, int(st.st_mtime))
        # size AND modification time: a tag that is moved to a new image keeps its size (the link file holds a digest of fixed
        # length), so comparing sizes alone would never copy a re-tagged image such as ":latest" to the NAS
        out, _ = nas._run(f"cd {MIRROR}/data && find . -type f -printf '%P\t%s\t%T@\n'", sudo=True)
        remote = {}
        for ln in out.splitlines():
            parts = ln.rsplit(TAB, 2)
            if len(parts) == 3:
                remote[parts[0]] = (int(parts[1]), int(float(parts[2])))
        todo = sorted((r for r, sz in local.items() if remote.get(r) != sz), key=lambda r: ("/repositories/" in r, r))   # blobs first
        gone = [r for r in remote if r not in local and not skip(r)]
        step(0, "ok", f"本地 {len(local)} 个文件，要复制 {len(todo)} 个，要删 {len(gone)} 个")
        step(1, "run")
        sent, batch, size = 0, [], 0
        for r in todo + [None]:
            if r is not None:
                batch.append(r)
                size += local[r][0]
            if batch and (r is None or size >= BATCH):
                ch = nas.work().get_transport().open_session()
                ch.exec_command(nas.rootsh(f"tar -xf - -C {MIRROR}/data"))
                out = ch.makefile_stdin("wb", 1 << 20)           # buffered: much faster than one tiny write per tar block
                with tarfile.open(fileobj=out, mode="w|") as tf:
                    for f in batch:
                        tf.add(os.path.join(LOCAL, f), arcname=f, recursive=False)
                out.flush()                                       # tarfile does not flush a file object it was given
                ch.shutdown_write()
                if ch.recv_exit_status():
                    raise RuntimeError("复制到 NAS 失败：" + nas.scrub(ch.makefile_stderr("r").read().decode(errors="replace"))[:100])
                sent += size
                step(1, "run", f"已复制 {sent // 1024 ** 2} MB")
                batch, size = [], 0
        step(1, "ok", f"{sent // 1024 ** 2} MB")
        step(2, "run")
        if gone:
            ch = nas.work().get_transport().open_session()
            ch.exec_command(nas.rootsh(f"cd {MIRROR}/data && xargs -d '{chr(92)}n' rm -f --"))
            ch.sendall((NL.join(gone) + NL).encode())
            ch.shutdown_write()
            ch.recv_exit_status()
        nas._run(f"cd {MIRROR}/data && find . -mindepth 1 -type d -empty -delete", sudo=True)
        step(2, "ok", f"{len(gone)} 个")
        return {"files": len(local), "bytes": sum(v[0] for v in local.values())}

    def _standby(self, nas):
        """The registry-standby container on the NAS serves the copy (read-only) on :5000; created the first time."""
        if mc.SWAPPED:                        # this ops runs on the NAS: the other machine's registry is its own business (the sentinel)
            return
        out, _ = nas._run("docker ps -a --filter name=^registry-standby$ --format '{{.Status}}'", sudo=True)
        if out.strip():                       # exists: the sentinel decides whether it runs (it is stopped while this machine's twin is promoted)
            return
        cmd = (f"docker run -d --name registry-standby --restart unless-stopped --memory 96m -p 5000:5000 "
               f"-v {MIRROR}/data:/var/lib/registry:ro -v {MIRROR}/certs:/certs:ro "
               "-e REGISTRY_HTTP_TLS_CERTIFICATE=/certs/registry.crt -e REGISTRY_HTTP_TLS_KEY=/certs/registry.key "
               f"{ADDR}/registry:2")
        out, err = nas._run(cmd, sudo=True)
        if not out.strip():
            raise RuntimeError("备用仓库没起来：" + err.strip()[-100:])

    async def nightly(self):
        """Every night around 03:30 copy the registry to the NAS."""
        done_day = None
        while True:
            await asyncio.sleep(300)
            t = time.localtime()
            if t.tm_hour == 3 and t.tm_min >= 30 and done_day != t.tm_yday:
                done_day = t.tm_yday
                self.start_mirror()


    # ---- keeping the registry tidy by itself: new images go to the NAS copy, only one old version is kept, a version can be put back
    def _ours(self, image: str):
        """(repo, tag) of an image reference as a container lists it, for images of this registry (either machine's address) or public ones."""
        for pre in (ADDR + "/", NAS_ADDR + "/"):
            if image.startswith(pre):
                rest = image[len(pre):]
                name, _, tag = rest.partition(":")
                return name, tag or "latest"
        return target(image)

    async def in_use_pi(self) -> set:
        used = set()
        for c in json.loads(await self._pi("GET", "/containers/json")):               # running ones only: what is live now
            t = self._ours(c["Image"])
            if t:
                used.add(t)
        return used

    async def in_use(self) -> set:
        """Every (repo, tag) that a container on either machine (running or not) is made from: these are never deleted."""
        used = set()
        for c in json.loads(await self._pi("GET", "/containers/json?all=1")):
            t = self._ours(c["Image"])
            if t:
                used.add(t)
        nas = self.m.hosts.get("nas")
        if nas is not None:
            try:
                for image in await asyncio.to_thread(nas.images_in_use):
                    t = self._ours(image)
                    if t:
                        used.add(t)
            except Exception as ex:
                raise RuntimeError(f"NAS 上用了哪些镜像没读到，先不清理：{ex}")
        return used

    def _quiet(self, secs: int = 90) -> bool:
        """No push in the last `secs` seconds and none under way."""
        if time.time() - self._newest() < secs:
            return False
        up = os.path.join(LOCAL, "docker/registry/v2/repositories")
        for root, dirs, files in os.walk(up):
            if os.path.basename(root) == "_uploads" and (files or dirs):
                return False
        return True

    async def _prune(self) -> list[str]:
        """Per repository keep the KEEP newest tags (and whatever a container uses); delete the rest. Returns what was deleted."""
        used = await self.in_use()
        removed = []
        async with self._http() as http:
            async with http.get(f"{API}/v2/_catalog", params={"n": "200"}) as r:
                repos = (await r.json()).get("repositories") or []
            for repo in repos:
                async with http.get(f"{API}/v2/{repo}/tags/list") as r:
                    tags = (await r.json()).get("tags") or []
                if len(tags) <= KEEP:
                    continue
                info = []
                for tag in tags:
                    got = await self._manifest(http, repo, tag)
                    if not got or not got[1]:
                        continue
                    link = os.path.join(LOCAL, "docker/registry/v2/repositories", repo, "_manifests/tags", tag, "current/link")
                    try:
                        mt = os.path.getmtime(link)
                    except OSError:
                        mt = 0
                    info.append((mt, tag, got[1]))
                info.sort(reverse=True)
                keep = {t for _, t, _ in info[:KEEP]} | {t for (rp, t) in used if rp == repo}
                keep_digests = {d for _, t, d in info if t in keep}              # two tags can be the same image: never delete that one
                for mt, tag, d in info:
                    if tag in keep or d in keep_digests:
                        continue
                    async with http.delete(f"{API}/v2/{repo}/manifests/{d}") as r:
                        if r.status in (200, 202):
                            removed.append(f"{repo}:{tag}")
                        elif r.status != 404:
                            log.warning("registry prune %s:%s: HTTP %s", repo, tag, r.status)
        return removed

    async def _gc(self):
        """Free the disk space of what was deleted (the registry's own garbage collection, inside its container)."""
        ex = json.loads(await self._pi("POST", "/containers/registry/exec", json={"Cmd": ["registry", "garbage-collect", "/etc/docker/registry/config.yml"],
                                                                                 "AttachStdout": True, "AttachStderr": True}))
        await self._pi("POST", f"/exec/{ex['Id']}/start", json={"Detach": False})
        code = json.loads(await self._pi("GET", f"/exec/{ex['Id']}/json")).get("ExitCode")
        if code:
            raise RuntimeError(f"仓库清理退出码 {code}")

    async def housekeeping(self):
        """Every minute, once nothing is being pushed: images the machines use are in the registry, old versions are pruned, and the NAS copy
        follows. This is what makes 'push a new image' the only thing a conversation has to do."""
        await asyncio.sleep(150)
        while True:
            try:
                if not mc.SWAPPED and not self.mirror_busy and self._quiet():
                    await self._tick()
            except Exception as ex:
                log.warning("registry housekeeping: %s", ex)
            await asyncio.sleep(60)

    async def _tick(self):
        st = self.store.get_setting("registry_tidy", {}) if self.store else {}
        # 1. an image a container uses that the registry does not have yet (a plain "docker run" by someone): push it
        async with self._http() as http:
            todo = []
            for w in await self.wanted():
                if w["tag"] != "latest" and not await self._manifest(http, w["repo"], w["tag"]):
                    todo.append(w)
        if todo:
            self.start(todo)
            return                                                                    # _sync ends by starting the mirror
        # 2. old versions
        removed = []
        try:
            removed = await self._prune()
            if removed:
                await self._gc()
                self._cache = (0.0, None)
                log.info("registry: deleted old versions %s", ", ".join(removed))
        except Exception as ex:
            st["error"] = str(ex)[:160]
        if removed:
            st.update(at=time.time(), removed=(removed + st.get("removed", []))[:20], error=None)
        if self.store:
            self.store.set_setting("registry_tidy", st)
        # 3. the NAS copy
        if removed or self.mirror_state()["dirty"]:
            self.start_mirror()

    # ---- put an older version back (or the newer one again): the containers of that repository are re-made with the other tag
    def start_rollback(self, repo: str, tag: str) -> str:
        job = uuid.uuid4().hex[:8]
        self.jobs[job] = {"id": job, "kind": "rollback", "done": False, "ok": None, "error": None,
                          "steps": [{"t": t, "s": "wait"} for t in ("检查", f"准备 {repo}:{tag}", "换上这个版本", "确认在运行")]}
        asyncio.create_task(self._rollback(job, repo, tag))
        return job

    async def _rollback(self, job: str, repo: str, tag: str):
        j = self.jobs[job]
        made = []                                                                      # (old name, new name) for undo
        try:
            j["steps"][0]["s"] = "run"
            who = [c for c in json.loads(await self._pi("GET", "/containers/json?all=1")) if c["Image"].startswith(f"{ADDR}/{repo}:")]
            if not who:
                raise RuntimeError("这个镜像现在没有在香橙派上运行")
            async with self._http() as http:
                if not await self._manifest(http, repo, tag):
                    raise RuntimeError(f"仓库里没有 {repo}:{tag}")
            j["steps"][0]["s"] = "ok"
            j["steps"][1]["s"] = "run"
            ref = f"{ADDR}/{repo}:{tag}"
            out = await self._pi("POST", "/images/create", params={"fromImage": f"{ADDR}/{repo}", "tag": tag}, headers={"X-Registry-Auth": "e30="})
            errs = [json.loads(ln)["error"] for ln in out.splitlines() if ln.startswith("{") and '"error"' in ln]
            if errs:
                raise RuntimeError(errs[0])
            j["steps"][1]["s"] = "ok"
            j["steps"][2]["s"] = "run"
            for c in who:
                name = c["Names"][0].lstrip("/")
                info = json.loads(await self._pi("GET", f"/containers/{c['Id']}/json"))
                cfg, hc = info["Config"], info["HostConfig"]
                mode = hc.get("NetworkMode") or "default"
                nets = info["NetworkSettings"].get("Networks") or {}
                body = {k: cfg.get(k) for k in ("User", "Env", "Cmd", "Entrypoint", "WorkingDir", "Labels", "Healthcheck", "ExposedPorts", "Tty", "OpenStdin", "StopSignal")}
                body = {k: v for k, v in body.items() if v not in (None, "", {})}
                body.update(Image=ref, HostConfig=hc)
                if mode not in ("host", "bridge", "default", "none") and mode in nets:
                    aliases = [a for a in (nets[mode].get("Aliases") or []) if not re.fullmatch(r"[0-9a-f]{12}", a)]
                    body["NetworkingConfig"] = {"EndpointsConfig": {mode: {"Aliases": aliases}}}
                old = f"{name}-old-{uuid.uuid4().hex[:4]}"
                await self._pi("POST", f"/containers/{c['Id']}/stop", params={"t": "20"})
                await self._pi("POST", f"/containers/{c['Id']}/rename", params={"name": old})
                made.append((old, name))
                await self._pi("POST", f"/containers/create?name={name}", json=body)
                for net in nets:                                                       # the other networks the container was on
                    if net != mode:
                        await self._pi("POST", f"/networks/{net}/connect", json={"Container": name})
                await self._pi("POST", f"/containers/{name}/start")
            j["steps"][2]["s"] = "ok"
            j["steps"][3]["s"] = "run"
            for _, name in made:
                for _ in range(40):
                    stt = json.loads(await self._pi("GET", f"/containers/{name}/json"))["State"]
                    if not stt.get("Running"):
                        raise RuntimeError(f"{name} 启动后又退出了")
                    if (stt.get("Health") or {}).get("Status") in (None, "healthy"):
                        await asyncio.sleep(3)
                        if json.loads(await self._pi("GET", f"/containers/{name}/json"))["State"].get("Running"):
                            break
                    await asyncio.sleep(2)
                else:
                    raise RuntimeError(f"{name} 一直没有变成健康状态")
            for old, _ in made:
                await self._pi("DELETE", f"/containers/{old}", params={"force": "true"})
            j["steps"][3]["s"] = "ok"
            self._cache = (0.0, None)
            j.update(done=True, ok=True)
        except Exception as ex:
            log.warning("registry rollback %s:%s: %s", repo, tag, ex)
            for old, name in reversed(made):                                           # put the old containers back
                try:
                    await self._pi("DELETE", f"/containers/{name}", params={"force": "true"})
                except Exception:
                    pass
                try:
                    await self._pi("POST", f"/containers/{old}/rename", params={"name": name})
                    await self._pi("POST", f"/containers/{name}/start")
                except Exception as ex2:
                    log.warning("rollback undo %s: %s", name, ex2)
            for st in j["steps"]:
                if st["s"] == "run":
                    st["s"] = "fail"
            j.update(done=True, ok=False, error=f"{str(ex)[:140]}。已恢复原样")

    def _http(self):
        return aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False), timeout=aiohttp.ClientTimeout(total=30))

    async def _manifest(self, http, repo: str, tag: str):
        async with http.get(f"{API}/v2/{repo}/manifests/{tag}", headers={"Accept": ACCEPT}) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None), r.headers.get("Docker-Content-Digest")

    async def _size(self, http, repo: str, m: dict) -> int | None:
        if "manifests" in m:                                  # multi-platform index: the first entry is what we pushed
            first = m["manifests"][0]["digest"]
            got = await self._manifest(http, repo, first)
            if got is None:
                return None
            m = got[0]
        try:
            return m["config"]["size"] + sum(layer["size"] for layer in m["layers"])
        except KeyError:
            return None

    async def listing(self) -> dict:
        ts, cached = self._cache
        if cached and time.time() - ts < 20:
            return cached
        images = []
        try:
            async with self._http() as http:
                async with http.get(f"{API}/v2/_catalog", params={"n": "200"}) as r:
                    repos = (await r.json()).get("repositories") or []
                for repo in repos:
                    async with http.get(f"{API}/v2/{repo}/tags/list") as r:
                        tags = (await r.json()).get("tags") or []
                    for tag in tags:
                        got = await self._manifest(http, repo, tag)
                        try:
                            pushed = os.path.getmtime(os.path.join(LOCAL, "docker/registry/v2/repositories", repo, "_manifests/tags", tag, "current/link"))
                        except OSError:
                            pushed = 0
                        images.append({"repo": repo, "tag": tag, "pushed": pushed, "size": await self._size(http, repo, got[0]) if got else None})
            ok = True
        except Exception as ex:
            log.warning("registry listing: %s", ex)
            ok = False
        st = os.statvfs(LOCAL)
        try:
            running = sorted({f"{r}:{t}" for r, t in await self.in_use_pi()})
        except Exception:
            running = []
        out = {"ok": ok, "address": ADDR, "images": sorted(images, key=lambda i: -(i["size"] or 0)), "running": running,
               "tidy": self.store.get_setting("registry_tidy", {}) if self.store else {},
               "total": sum(i["size"] or 0 for i in images), "disk_free": st.f_bavail * st.f_frsize,
               "mirror": self.mirror_state()}
        self._cache = (time.time(), out)
        return out

    # ---- push everything the two machines run
    async def _pi(self, method: str, path: str, **kw):
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=DOCKER_SOCK), timeout=aiohttp.ClientTimeout(total=1800)) as http:
            async with http.request(method, "http://docker" + path, **kw) as r:
                body = await r.read()
                if r.status >= 400:
                    raise RuntimeError(body.decode(errors="replace")[:200])
                return body.decode(errors="replace")

    async def wanted(self) -> list[dict]:
        """Every distinct image a container on either machine uses: [{image, repo, tag, host}] (the Pi wins when both have it)."""
        found: dict[tuple[str, str], dict] = {}
        pi = json.loads(await self._pi("GET", "/containers/json?all=1"))
        for c in pi:
            t = target(c["Image"])
            if t:
                found.setdefault(t, {"image": c["Image"], "repo": t[0], "tag": t[1], "host": "pi"})
        nas = self.m.hosts.get("nas")
        if nas is not None:
            names = await asyncio.to_thread(nas.images_in_use)
            for image in names:
                t = target(image)
                if t:
                    found.setdefault(t, {"image": image, "repo": t[0], "tag": t[1], "host": "nas"})
        return sorted(found.values(), key=lambda w: (w["host"], w["repo"]))

    def start(self, items: list[dict]) -> str:
        job = uuid.uuid4().hex[:8]
        self.jobs[job] = {"id": job, "steps": [{"t": f"{w['repo']}:{w['tag']}", "s": "wait", "host": w["host"]} for w in items],
                          "done": False, "ok": None, "error": None}
        asyncio.create_task(self._sync(job, items))
        return job

    async def _sync(self, job: str, items: list[dict]):
        j = self.jobs[job]
        failed = 0
        async with self._http() as http:
            for i, w in enumerate(items):
                step = j["steps"][i]
                step["s"] = "run"
                try:
                    have = await self._manifest(http, w["repo"], w["tag"])
                    if have and w["tag"] != "latest":
                        step["s"] = "ok"; step["note"] = "已有"
                        continue
                    st = os.statvfs(LOCAL)
                    if st.f_bavail * st.f_frsize < MIN_FREE:
                        raise RuntimeError("香橙派磁盘剩余不到 5GB，先停下")
                    dest = f"{ADDR}/{w['repo']}"
                    if w["host"] == "pi":
                        await self._pi("POST", f"/images/{w['image']}/tag", params={"repo": dest, "tag": w["tag"]})
                        out = await self._pi("POST", f"/images/{dest}/push", params={"tag": w["tag"]}, headers={"X-Registry-Auth": "e30="})
                        errs = [json.loads(ln)["error"] for ln in out.splitlines() if ln.startswith("{") and '"error"' in ln]
                        if errs:
                            raise RuntimeError(errs[0])
                        await self._pi("DELETE", f"/images/{dest}:{w['tag']}")
                    else:
                        await asyncio.to_thread(self.m.hosts["nas"].push_image, w["image"], f"{dest}:{w['tag']}")
                    step["s"] = "ok"
                except Exception as ex:
                    log.warning("registry sync %s: %s", step["t"], ex)
                    step["s"] = "fail"; step["note"] = str(ex)[:120]
                    failed += 1
        self._cache = (0.0, None)
        j.update(done=True, ok=failed == 0, error=None if not failed else f"{failed} 个镜像没推成功")
        self.start_mirror()                                  # the new images go to the NAS copy too
