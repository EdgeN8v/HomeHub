"""One tap: move all the services of one machine to the other.

The engines (migrate.py: NAS -> Pi, takeover.py: Pi -> NAS) move ONE service at a time. This lists everything that may move from a
machine (with the reason when a service may not), lets the phone untick some (the big, optional ones, e.g. Immich's face recognition,
start unticked), and then runs the ticked ones one after the other as a single job with a single progress bar."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from . import machines as mc

log = logging.getLogger("transfer")
SKIP_BY_DEFAULT = ("immich_ml",)               # big and optional: the owner decided it can stay where it is


class Transfer:
    def __init__(self, migrator, monitor):
        self.mig, self.m, self.jobs = migrator, monitor, {}

    async def plan(self, host: str) -> list[dict]:
        """host: the physical machine the services are on now ("pi" / "nas")."""
        key = mc.phys(host)                                   # the engines' own name for it
        rows = self.m.state.get(key, {}).get("containers", [])
        seen, out = set(), []
        for c in rows:
            name = c["name"]
            if name in seen:
                continue
            p = await self.mig.plan(key, name)
            if p.get("stay"):
                continue                                      # forwards, the hub itself ...: they never move
            members = [m["name"] for m in p.get("members", [])] or [name]
            seen.update(members)
            skip = (not p.get("ok")) or any(any(s in m for s in SKIP_BY_DEFAULT) for m in members)
            out.append({"name": name, "members": members, "ok": bool(p.get("ok")), "reasons": p.get("reasons", []),
                        "data_mb": p.get("data_mb", 0), "running": p.get("running", c.get("running", False)), "skip": skip})
        return mc.localize(out)

    async def start(self, host: str, skip: list[str]) -> str:
        key = mc.phys(host)
        todo = [g for g in await self.plan(host) if g["ok"] and g["name"] not in skip and not any(m in skip for m in g["members"])]
        if not todo:
            raise ValueError("没有可以转移的服务（都被取消勾选，或不能搬）")
        job = uuid.uuid4().hex[:8]
        self.jobs[job] = {"id": job, "started": time.time(), "steps": [{"t": g["name"], "s": "wait"} for g in todo], "done": False, "ok": None, "error": None}
        asyncio.create_task(self._run(job, key, todo))
        return job

    async def _run(self, job: str, key: str, todo: list[dict]):
        j = self.jobs[job]
        failed = []
        for i, g in enumerate(todo):
            st = j["steps"][i]
            st["s"] = "run"
            try:
                plan = await self.mig.plan(key, g["name"])             # re-plan: what is free / fits changes as services move
                if not plan.get("ok"):
                    raise RuntimeError("；".join(plan.get("reasons", [])) or "现在不能搬")
                sub = self.mig.start(plan["name"], plan)
                while not self.mig.jobs[sub]["done"]:
                    sj = self.mig.jobs[sub]
                    done = sum(1 for s in sj["steps"] if s["s"] == "ok")
                    cur = next((s for s in sj["steps"] if s["s"] == "run"), None)
                    st["pct"] = int(100 * (done + (cur.get("pct", 0) / 100 if cur else 0)) / max(1, len(sj["steps"])))
                    st["note"] = mc.localize(cur["t"]) if cur else ""
                    await asyncio.sleep(2)
                sj = self.mig.jobs[sub]
                if not sj["ok"]:
                    raise RuntimeError(sj.get("error") or "迁移失败")
                st.update(s="ok", pct=100, note="")
            except Exception as ex:
                st.update(s="fail", note=str(ex)[:120])
                failed.append(g["name"])
        j.update(done=True, ok=not failed, ended=time.time(), error=("没有搬成功：" + "、".join(failed)) if failed else None)
