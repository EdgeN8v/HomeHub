"""开发闯关地图 (/journey): what was done in every project, which roads worked and which did not, the
tech used, ideas to keep, and which repos have changes not yet on GitHub.

The work happens on the Windows PC (tools/journey/collect.py): it reads the Claude Code and Codex chat
logs, has `claude -p` turn each stretch of a chat into a "level" (goal, roads tried, worked or not and
why, lessons, tech), scans the git repos, and posts it all here. The hub only stores and serves it, and
queues "push this repo" jobs from the page, which the PC picks up (only the PC has the code and the keys).
"""
from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from aiohttp import web

IDEA_STATUS = ("new", "doing", "done", "dropped")
JOB_KINDS = ("push", "create_private", "create_public", "make_private", "make_public")


class Journey:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, source TEXT NOT NULL, session TEXT NOT NULL,
                idx INTEGER NOT NULL, start_ts TEXT, end_ts TEXT, data TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, project TEXT, note TEXT,
                status TEXT NOT NULL DEFAULT 'new', origin TEXT, created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, repo TEXT NOT NULL, status TEXT NOT NULL,
                result TEXT, created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    # ---- key/value (repo scan, ignored repos, collector heartbeat)
    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT INTO kv VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    # ---- levels
    def upsert_chunks(self, rows: list[dict]) -> int:
        now = time.time()
        for r in rows:
            self.db.execute(
                "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "project = excluded.project, start_ts = excluded.start_ts, end_ts = excluded.end_ts, "
                "data = excluded.data, updated = excluded.updated",
                (str(r["id"]), str(r["project"]), str(r["source"]), str(r["session"]), int(r["idx"]),
                 r.get("start_ts"), r.get("end_ts"), json.dumps(r.get("data") or {}, ensure_ascii=False), now))
        self.db.commit()
        return len(rows)

    def chunks(self) -> list[dict]:
        rows = self.db.execute("SELECT id, project, source, session, idx, start_ts, end_ts, data FROM chunks ORDER BY start_ts")
        return [{"id": i, "project": p, "source": s, "session": se, "idx": x, "start_ts": a, "end_ts": b, **json.loads(d)}
                for i, p, s, se, x, a, b, d in rows]

    def chunk_ids(self) -> dict:
        return {i: u for i, u in self.db.execute("SELECT id, updated FROM chunks")}

    # ---- ideas
    def ideas(self) -> list[dict]:
        rows = self.db.execute("SELECT id, text, project, note, status, origin, created, updated FROM ideas ORDER BY id DESC")
        return [dict(zip(("id", "text", "project", "note", "status", "origin", "created", "updated"), r)) for r in rows]

    def add_idea(self, text: str, project: str | None, note: str | None, origin: str | None) -> int:
        now = time.time()
        dup = self.db.execute("SELECT id FROM ideas WHERE text = ? AND COALESCE(project, '') = ? AND created > ? ORDER BY id DESC LIMIT 1",
                              (text, project or "", now - 60)).fetchone()
        if dup:                                   # the same text again within a minute is a double tap, not a new idea
            return dup[0]
        cur = self.db.execute("INSERT INTO ideas (text, project, note, status, origin, created, updated) VALUES (?, ?, ?, 'new', ?, ?, ?)",
                              (text, project, note, origin, now, now))
        self.db.commit()
        return cur.lastrowid

    def edit_idea(self, iid: int, b: dict):
        sets, args = [], []
        for k in ("text", "project", "note", "status"):
            if k in b:
                if k == "status" and b[k] not in IDEA_STATUS:
                    raise ValueError("bad status")
                sets.append(f"{k} = ?")
                args.append(b[k])
        if sets:
            self.db.execute(f"UPDATE ideas SET {', '.join(sets)}, updated = ? WHERE id = ?", (*args, time.time(), iid))
            self.db.commit()

    def delete_idea(self, iid: int):
        self.db.execute("DELETE FROM ideas WHERE id = ?", (iid,))
        self.db.commit()

    # ---- push jobs
    def jobs(self, only_open=False, limit=30) -> list[dict]:
        q = "SELECT id, kind, repo, status, result, created, updated FROM jobs"
        if only_open:
            q += " WHERE status IN ('queued', 'running')"
        rows = self.db.execute(q + " ORDER BY id DESC LIMIT ?", (limit,))
        return [{"id": i, "kind": k, "repo": r, "status": s, "result": json.loads(res) if res else None, "created": c, "updated": u}
                for i, k, r, s, res, c, u in rows]

    def add_job(self, kind: str, repo: str) -> int:
        open_same = self.db.execute("SELECT id FROM jobs WHERE repo = ? AND status IN ('queued', 'running')", (repo,)).fetchone()
        if open_same:
            return open_same[0]
        now = time.time()
        cur = self.db.execute("INSERT INTO jobs (kind, repo, status, created, updated) VALUES (?, ?, 'queued', ?, ?)", (kind, repo, now, now))
        self.db.commit()
        return cur.lastrowid

    def set_job(self, jid: int, status: str, result):
        self.db.execute("UPDATE jobs SET status = ?, result = ?, updated = ? WHERE id = ?",
                        (status, json.dumps(result, ensure_ascii=False) if result is not None else None, time.time(), jid))
        self.db.commit()


# ---------------------------------------------------------------- HTTP

def _j(request) -> Journey:
    return request.app["journey"]


async def overview(request: web.Request):
    j = _j(request)
    return web.json_response({"chunks": j.chunks(), "ideas": j.ideas(), "repos": j.get("repos", {}),
                              "ignored": j.get("ignored", []), "jobs": j.jobs(), "collector": j.get("collector", {})})


async def revision(request: web.Request):
    """A few bytes that change whenever the overview would: the page asks this every 20 s and only downloads the
    (megabytes of) overview when it moved."""
    j = _j(request)
    q = lambda sql: j.db.execute(sql).fetchone()
    parts = [q("SELECT COUNT(*), COALESCE(MAX(updated), 0) FROM chunks"), q("SELECT COUNT(*), COALESCE(MAX(updated), 0) FROM ideas"),
             q("SELECT COUNT(*), COALESCE(MAX(updated), 0) FROM jobs")]
    extra = [len(json.dumps(j.get("repos", {}), sort_keys=True)), len(json.dumps(j.get("ignored", []))), (j.get("collector", {}) or {}).get("state")]
    return web.json_response({"rev": "|".join(str(x) for r in parts for x in r) + "|" + "|".join(str(x) for x in extra)})


async def post_chunks(request: web.Request):
    b = await request.json()
    return web.json_response({"saved": _j(request).upsert_chunks(b.get("chunks", []))})


async def chunk_index(request: web.Request):
    return web.json_response(_j(request).chunk_ids())


async def post_repos(request: web.Request):
    b = await request.json()
    _j(request).put("repos", {"scanned": b.get("scanned"), "list": b.get("repos", [])})
    return web.json_response({"ok": True})


async def heartbeat(request: web.Request):
    b = await request.json()
    _j(request).put("collector", {**b, "seen": time.time()})
    return web.json_response({"ok": True})


async def ignore_repo(request: web.Request):
    b = await request.json()
    j, name = _j(request), str(b.get("repo", ""))
    ign = set(j.get("ignored", []))
    (ign.add if b.get("ignore", True) else ign.discard)(name)
    j.put("ignored", sorted(ign))
    return web.json_response({"ignored": sorted(ign)})


async def add_idea(request: web.Request):
    b = await request.json()
    text = str(b.get("text", "")).strip()
    if not text:
        raise web.HTTPBadRequest(text="text required")
    iid = _j(request).add_idea(text[:2000], b.get("project") or None, b.get("note") or None, b.get("origin") or None)
    return web.json_response({"id": iid})


async def edit_idea(request: web.Request):
    try:
        _j(request).edit_idea(int(request.match_info["id"]), await request.json())
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"ok": True})


async def delete_idea(request: web.Request):
    _j(request).delete_idea(int(request.match_info["id"]))
    return web.json_response({"ok": True})


async def add_job(request: web.Request):
    b = await request.json()
    kind, repo = str(b.get("kind", "push")), str(b.get("repo", ""))
    if kind not in JOB_KINDS or not repo:
        raise web.HTTPBadRequest(text="kind/repo")
    return web.json_response({"id": _j(request).add_job(kind, repo)})


async def list_jobs(request: web.Request):
    return web.json_response(_j(request).jobs(only_open=request.query.get("open") == "1"))


async def set_job(request: web.Request):
    b = await request.json()
    _j(request).set_job(int(request.match_info["id"]), str(b.get("status", "done")), b.get("result"))
    return web.json_response({"ok": True})


# ---- the Idea phone app: its updates (behind its token) and the first download (a secret link, no token yet on the phone)

APK_RE = re.compile(r"^Idea-v[\w.]+\.apk$")


def _updates(request) -> Path:
    return request.app["journey_updates"]


async def app_version(request: web.Request):
    f = _updates(request) / "version.json"
    if not f.exists():
        raise web.HTTPNotFound(text="no release yet")
    return web.FileResponse(f, headers={"Cache-Control": "no-cache"})


async def app_apk(request: web.Request):
    name = request.match_info["name"]
    f = _updates(request) / name
    if not APK_RE.match(name) or not f.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(f, headers={"Content-Type": "application/vnd.android.package-archive"})


async def app_download(request: web.Request):
    """GET /idea-dl/<IDEA_DL>/Idea.apk - the newest release, for installing the app the first time (from a browser)."""
    slug = os.environ.get("IDEA_DL", "")
    if not slug or not hmac.compare_digest(request.match_info["slug"].encode(), slug.encode()):
        raise web.HTTPNotFound()
    try:
        v = json.loads((_updates(request) / "version.json").read_text(encoding="utf-8"))
        f = _updates(request) / v["apk"]
    except (OSError, ValueError, KeyError):
        raise web.HTTPNotFound(text="no release yet")
    return web.FileResponse(f, headers={"Content-Type": "application/vnd.android.package-archive",
                                        "Content-Disposition": f'attachment; filename="{v["apk"]}"'})


async def delete_idea_post(request: web.Request):      # the phone shell relays GET and POST only
    return await delete_idea(request)


def setup(app: web.Application, data: Path):
    app["journey"] = Journey(data / "journey.db")
    app["journey_updates"] = data / "idea_updates"
    app.add_routes([
        web.get("/api/journey/app/version.json", app_version),
        web.get("/api/journey/app/{name}", app_apk),
        web.get("/idea-dl/{slug}/Idea.apk", app_download),
        web.post("/api/journey/ideas/{id}/delete", delete_idea_post),
        web.get("/api/journey", overview),
        web.get("/api/journey/rev", revision),
        web.get("/api/journey/chunk-index", chunk_index),
        web.post("/api/journey/chunks", post_chunks),
        web.post("/api/journey/repos", post_repos),
        web.post("/api/journey/heartbeat", heartbeat),
        web.post("/api/journey/ignore", ignore_repo),
        web.post("/api/journey/ideas", add_idea),
        web.post("/api/journey/ideas/{id}", edit_idea),
        web.delete("/api/journey/ideas/{id}", delete_idea),
        web.post("/api/journey/jobs", add_job),
        web.get("/api/journey/jobs", list_jobs),
        web.post("/api/journey/jobs/{id}", set_job),
    ])
