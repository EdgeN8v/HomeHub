"""开发闯关地图 - the PC side. Runs on the Windows PC (where the chats and the code are):

  python collect.py run        keep running: push jobs every 20 s, repo scan every 10 min, new chats every hour
  python collect.py once       one pass of everything (scan + summarise), then exit
  python collect.py scan       only the git scan
  python collect.py dry FILE   print the condensed text of one chat log (to check what the summariser sees)

What it does:
  1. Reads Claude Code chats (~/.claude/projects/**/*.jsonl) and Codex chats (~/.codex/sessions/**/*.jsonl),
     condenses each into the human prompts, the AI's replies, the tools it ran and the errors it hit, and cuts
     it into "levels" of at most CHUNK_CHARS characters (whole turns only, so earlier levels never change).
  2. Has `claude -p` (the desktop app's bundled Claude Code, so it runs on the user's subscription) turn each
     level into JSON: goal, roads tried (worked or not, why, lesson), tech used, ideas. Cached in CACHE, so a
     level is summarised once; the last level of a chat is redone only when the chat grew and went quiet.
  3. Scans the git repos under D:\\Github: uncommitted files, commits not pushed, no remote, not a repo.
  4. Picks up "push" jobs queued from the page and does commit + push, refusing when a changed file looks
     like a secret.
Everything is posted to HomeHub (/api/journey/*) with HUB_TOKEN from HomeHub\\.env.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]           # D:\Github\HomeHub
GITHUB = Path(os.environ.get("JOURNEY_CODE_ROOT", r"D:\Github"))
HOME = Path.home()
CLAUDE_LOGS = HOME / ".claude" / "projects"
CODEX_LOGS = HOME / ".codex" / "sessions"
STATE = Path(os.environ.get("LOCALAPPDATA", HOME)) / "Journey"
CACHE = STATE / "levels.json"
WORK = STATE / "work"                                 # cwd for claude -p, so its runs land nowhere near the projects
LOG = STATE / "collector.log"
CHUNK_CHARS = 60_000
QUIET_SECONDS = int(os.environ.get("JOURNEY_QUIET", "300"))        # a chat idle this long counts as finished and gets summarised
MODEL = os.environ.get("JOURNEY_MODEL", "haiku")                 # chunk summaries: the cheap model (quota)
SIMPLE_MODEL = os.environ.get("JOURNEY_SIMPLE_MODEL", "haiku")   # commit messages and other short jobs
SECRET_RE = re.compile(r"(^|[\\/])(\.env(\.(?!example$|sample$)[^\\/]*)?|[^\\/]*\.(pem|key|p12|pfx|jks|keystore)|local\.properties|"
                       r"auth\.json|[^\\/]*(secret|password|passwd|token|credential)s?[^\\/]*\.(json|txt|ya?ml|ini|cfg|conf|env|properties))$", re.I)
CTX = ssl.create_default_context()
CTX.check_hostname, CTX.verify_mode = False, ssl.CERT_NONE   # the hub's cert is self-signed; the token is what matters


def log(*a):
    line = f"{datetime.now():%m-%d %H:%M:%S} " + " ".join(str(x) for x in a)
    print(line, flush=True)
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- hub

def _env() -> dict:
    vals = {}
    for ln in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(\w+)=(.*)$", ln)
        if m:
            vals[m.group(1)] = m.group(2).strip()
    return vals


TOKEN = _env().get("HUB_TOKEN", "")
_hub_base = None


def hub_bases() -> list[str]:
    """The homehub service's addresses from the address table (directory service), LAN first."""
    for url in ("http://192.168.31.61:8120/endpoints.json", "http://192.168.31.131:8120/endpoints.json",
                "http://192.168.31.131:8099/endpoints.json"):
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                d = json.load(r)
            s = (d.get("services") or d)["homehub"]
            return list(s.get("lan", [])) + list(s.get("wan", []))
        except Exception:
            continue
    # last resort: the LAN addresses, then the public ones from HomeHub\.env (IDEA_WAN=url,url) - not written here, the repo is public
    wan = [u.strip() for u in _env().get("IDEA_WAN", "").split(",") if u.strip()]
    return ["https://192.168.31.61:8200", "https://192.168.31.131:8095"] + wan


def hub(method: str, path: str, body=None, timeout=30):
    global _hub_base
    bases = [_hub_base] if _hub_base else hub_bases()
    err = None
    for base in bases:
        req = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                _hub_base = base
                return json.load(r)
        except urllib.error.HTTPError:
            raise
        except Exception as e:
            err = e
    _hub_base = None
    raise ConnectionError(f"hub unreachable: {err}")


# ---------------------------------------------------------------- reading chats

TAG_RE = re.compile(r"<(system-reminder|environment_context|user_instructions|local-command-stdout|command-message|"
                    r"permissions instructions|turn_aborted)>.*?</\1>", re.S)


def clean(text: str) -> str:
    return TAG_RE.sub("", text or "").strip()


def project_of(cwd: str | None) -> str:
    if not cwd:
        return "其他"
    p = cwd.replace("/", "\\")
    root = str(GITHUB).rstrip("\\") + "\\"
    if p.lower().startswith(root.lower()):
        rest = p[len(root):].split("\\")
        return rest[0] or "其他"
    if p.rstrip("\\").lower() == str(GITHUB).lower():
        return "Github(总目录)"
    return "其他"


PATH_RE = re.compile(r"[A-Za-z]:[\/]+Github[\/]+([A-Za-z0-9_.-]+)", re.I)


def infer_project(text: str, fallback: str, where: dict | None = None) -> str:
    """Chats started in D:\Github itself (most desktop sessions) say which project only through the paths
    they touch: take the project folder mentioned most in this level."""
    names = {d.name.lower(): d.name for d in GITHUB.iterdir() if d.is_dir()}
    counts = dict(where or {})
    for m in PATH_RE.finditer(text):
        n = names.get(m.group(1).lower())
        if n:
            counts[n] = counts.get(n, 0) + 1
    if not counts:
        return fallback
    best = max(counts, key=counts.get)
    return best if counts[best] >= 2 else fallback


# HomeHub was started inside a ClaudeRemote chat (2026-10-21 16:00) and every chat since inherited that folder: from that moment
# on, chats whose working folder is ClaudeRemote are HomeHub work.
FOLDER_MEANS = {"ClaudeRemote": ("2026-10-21T16:00", "HomeHub")}


def effective_project(project: str, start_ts: str | None) -> str:
    since = FOLDER_MEANS.get(project)
    return since[1] if since and (start_ts or "") >= since[0] else project


IDEA_TOKENS = re.compile(r"idea_app|journey|collect\.py|收集器|Idea")


def idea_or(text: str, project: str) -> str:
    """The Idea app lives inside the HomeHub folder (its page, hub routes, collector, Android shell), so paths say HomeHub; the
    stretches of a chat that are about building Idea itself count as the project Idea."""
    if project != "HomeHub":
        return project
    k = len(IDEA_TOKENS.findall(text))
    return "Idea" if k >= 12 and k * 3 >= text.lower().count("homehub") else project


def redirect_project(text: str, project: str) -> str:
    """A chat started in one project folder (ClaudeRemote) can spend a whole stretch editing another (HomeHub's files): when the
    paths in this stretch point mostly elsewhere, the stretch belongs there."""
    names = {d.name.lower(): d.name for d in GITHUB.iterdir() if d.is_dir()}
    counts: dict = {}
    for m in PATH_RE.finditer(text):
        n = names.get(m.group(1).lower())
        if n:
            counts[n] = counts.get(n, 0) + 1
    low = text.lower()
    for low_name, n in names.items():                      # the compressed record keeps few full paths, but the names stay
        if len(low_name) >= 5:
            k = low.count(low_name)
            if k:
                counts[n] = counts.get(n, 0) + k
    if not counts:
        return project
    top = max(counts, key=counts.get)
    return top if top != project and counts[top] >= 8 and counts[top] > 2 * counts.get(project, 0) else project


def _short(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n] + "…"


def read_claude(path: Path) -> dict:
    """-> {source, session, cwd, turns: [{ts, lines[]}], last_ts}. A turn starts at each human prompt."""
    turns, cwd, cur, last = [], None, None, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if d.get("isSidechain"):
                continue
            cwd = cwd or d.get("cwd")
            t, ts = d.get("type"), d.get("timestamp")
            if cur is not None and d.get("cwd"):          # the folder moves during desktop chats (D:\Github -> a project)
                pj = project_of(d["cwd"])
                cur.setdefault("where", {})[pj] = cur.get("where", {}).get(pj, 0) + 1
            msg = d.get("message") or {}
            if t == "user" and not d.get("isMeta"):
                c = msg.get("content")
                items = [{"type": "text", "text": c}] if isinstance(c, str) else (c or [])
                human = [clean(i.get("text", "")) for i in items if i.get("type") == "text"]
                human = [h for h in human if h and not h.startswith("<command-") and not h.startswith("[Request interrupted")]
                if human:
                    cur = {"ts": ts, "lines": ["【用户】" + _short("\n".join(human), 3000)]}
                    turns.append(cur)
                    continue
                for i in items:
                    if i.get("type") == "tool_result" and i.get("is_error") and cur:
                        rc = i.get("content")
                        txt = rc if isinstance(rc, str) else " ".join(x.get("text", "") for x in rc or [] if isinstance(x, dict))
                        cur["lines"].append("  [报错] " + _short(txt, 400))
            elif t == "assistant" and cur:
                for i in msg.get("content") or []:
                    if i.get("type") == "text" and i.get("text", "").strip():
                        cur["lines"].append("【AI】" + _short(i["text"], 1500))
                    elif i.get("type") == "tool_use":
                        tl = tool_line(i.get("name", ""), i.get("input") or {})
                        if tl:
                            cur["lines"].append("  " + tl)
            if ts:
                last = ts
    return {"source": "claude", "session": path.stem, "cwd": cwd, "turns": turns, "last_ts": last}


def tool_line(name: str, inp: dict) -> str:
    if name in ("Bash", "PowerShell"):
        return f"[{name}] " + _short(inp.get("description") or inp.get("command"), 160)
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return f"[改文件] {inp.get('file_path', '')}"
    if name == "Read":
        return ""
    if name in ("Grep", "Glob"):
        return ""
    if name == "WebSearch":
        return "[搜索] " + _short(inp.get("query"), 120)
    return f"[{name}] " + _short(json.dumps(inp, ensure_ascii=False), 120)


def read_codex(path: Path) -> dict:
    turns, cwd, cur, last, sid = [], None, None, None, path.stem
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            t, p, ts = d.get("type"), d.get("payload") or {}, d.get("timestamp")
            last = ts or last
            if t == "session_meta":
                cwd, sid = p.get("cwd"), p.get("id") or sid
                continue
            if t != "response_item":
                continue
            pt = p.get("type")
            if pt == "message":
                text = clean(" ".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict)))
                if not text:
                    continue
                if p.get("role") == "user":
                    cur = {"ts": ts, "lines": ["【用户】" + _short(text, 3000)]}
                    turns.append(cur)
                elif p.get("role") == "assistant" and cur:
                    cur["lines"].append("【AI】" + _short(text, 1500))
            elif pt in ("function_call", "custom_tool_call", "local_shell_call") and cur:
                args = p.get("arguments") or p.get("input") or p.get("action") or ""
                cur["lines"].append(f"  [{p.get('name', 'shell')}] " + _short(args, 160))
            elif pt in ("function_call_output", "custom_tool_call_output") and cur:
                out = p.get("output")
                out = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                if re.search(r"error|exit code: [1-9]|failed|traceback|not found|denied", out[:600], re.I):
                    cur["lines"].append("  [报错] " + _short(out, 400))
    return {"source": "codex", "session": sid, "cwd": cwd, "turns": turns, "last_ts": last}


def chunk(chat: dict) -> list[dict]:
    """Whole turns, greedily packed up to CHUNK_CHARS, so adding turns at the end only changes the last chunk."""
    out, cur, size = [], [], 0
    for t in chat["turns"]:
        text = "\n".join(x for x in t["lines"] if x)
        if len(text) > CHUNK_CHARS:
            text = text[:CHUNK_CHARS // 2] + "\n…(中间省略)…\n" + text[-CHUNK_CHARS // 3:]
        if cur and size + len(text) > CHUNK_CHARS:
            out.append(cur)
            cur, size = [], 0
        cur.append({"ts": t["ts"], "text": text, "where": t.get("where", {})})
        size += len(text)
    if cur:
        out.append(cur)
    def where(c):
        w = {}
        for x in c:
            for k, v in x["where"].items():
                if k not in ("Github(总目录)", "其他"):
                    w[k] = w.get(k, 0) + v
        return w
    return [{"idx": i, "start_ts": c[0]["ts"], "end_ts": c[-1]["ts"], "text": "\n\n".join(x["text"] for x in c),
             "where": where(c)} for i, c in enumerate(out)]


def all_logs() -> list[tuple[str, Path]]:
    found = [("claude", Path(p)) for p in glob.glob(str(CLAUDE_LOGS / "*" / "*.jsonl"))]
    found += [("codex", Path(p)) for p in glob.glob(str(CODEX_LOGS / "**" / "*.jsonl"), recursive=True)]
    return [(s, p) for s, p in found if "Journey" not in str(p)]


# ---------------------------------------------------------------- summarising

SCHEMA_VERSION = 2
NODE = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "kind", "project", "trigger", "attempts", "status", "root_cause", "lesson", "tech", "continues"],
    "properties": {
        "title": {"type": "string", "description": "这个节点要解决的问题或要做的事，≤20字，具体（如\"手机外网连不上 hub\"）"},
        "kind": {"type": "string", "enum": ["bug", "功能", "部署", "研究", "重构", "其他"]},
        "project": {"type": "string", "description": "这个节点属于哪个项目：从给出的项目文件夹名里选一个；判断不了就空字符串"},
        "trigger": {"type": "string", "description": "为什么要做这件事：发现了什么现象/问题，或者用户提出了什么需求"},
        "attempts": {"type": "array", "description": "为解决它做过的每一次尝试，严格按时间顺序；失败的尝试一定要列出来，不能只写最后成功的那次",
                     "items": {"type": "object", "additionalProperties": False, "required": ["what", "reason", "ok", "result", "why"],
                               "properties": {
                                   "what": {"type": "string", "description": "这次具体做了什么（改了什么、跑了什么、换了什么方案）"},
                                   "reason": {"type": "string", "description": "当时为什么这么做：依据是什么、怀疑的原因是什么"},
                                   "ok": {"type": "boolean", "description": "这次成功了吗"},
                                   "result": {"type": "string", "description": "做完看到的结果/现象（报错要点、表现）"},
                                   "why": {"type": "string", "description": "为什么成功/为什么失败：要写到根因，不要只重复结果"}}}},
        "status": {"type": "string", "enum": ["解决", "部分解决", "没解决", "进行中"]},
        "root_cause": {"type": "string", "description": "最后查明的根本原因（bug 类）或关键做法（功能类）"},
        "lesson": {"type": "string", "description": "下次遇到同类问题可以直接用的一句经验"},
        "tech": {"type": "array", "items": {"type": "string"},
                 "description": "这个节点里实际用到的具体技术/库/工具/协议，用通用规范名（如 Jetpack Compose、Docker、aiohttp、SQLite、ADB），不写泛泛的词"},
        "continues": {"type": "string", "description": "如果这是\"上一段已有节点\"里同一个问题的继续，填那个节点的 title 原文；否则空字符串"},
    },
}
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["skip", "nodes", "ideas"],
    "properties": {
        "skip": {"type": "boolean", "description": "这段只是闲聊/问答、没有实际开发或排错时为 true"},
        "nodes": {"type": "array", "items": NODE, "description": "这段对话里处理的每个问题/任务，一个问题一个节点，按时间顺序"},
        "ideas": {"type": "array", "items": {"type": "string"}, "description": "对话里提到但没做、留到以后的点子或改进"},
    },
}

PROMPT = """你是用户的技术复盘教练。下面是用户和 AI 编程助手（{source}）的一段开发对话的压缩记录（第 {idx} 段，{when}，大概属于项目「{project}」）。
【用户】是用户说的话，【AI】是助手的回复，方括号是助手执行的操作，[报错] 是出错的地方。

用户最想知道的是：每个节点（一个 bug、一个功能、一次部署……）到底干了什么、为什么这么干；
一个问题往往不是一次就成功的——中间每次尝试为什么这么试、结果怎样、为什么成功或为什么失败。

请把这段对话拆成节点：
- 一个问题/任务一个节点；同一个问题的多次尝试放在同一个节点的 attempts 里，按时间顺序，失败的也要写。
- reason 写"当时为什么这么做"（依据、猜测），why 写"为什么成功/失败"（根因），两者不要重复。
- 用中文，具体，能复用；只写记录里真实发生的事，不编造。很小的琐碎操作（看文件、改一个字）不要单独成节点。
- project 从这些项目文件夹里选：{projects}
- 上一段已有的节点：{previous}。如果这段在继续其中某个问题，continues 填它的 title 原文。
- 没有实际开发内容就 skip=true，nodes 为空。

=== 对话记录 ===
{text}
"""


def claude_exe() -> str:
    env = os.environ.get("JOURNEY_CLAUDE")
    if env:
        return env
    cands = glob.glob(str(Path(os.environ.get("LOCALAPPDATA", "")) / "Claude-Data" / "*" / "claude-code" / "*" / "*" / "claude.exe"))
    cands += glob.glob(str(HOME / ".local" / "bin" / "claude.exe"))
    if not cands:
        raise FileNotFoundError("claude.exe not found; set JOURNEY_CLAUDE")
    return max(cands, key=os.path.getmtime)


def ask_claude(prompt: str, schema: dict | None, timeout=900, model: str | None = None) -> dict | str:
    WORK.mkdir(parents=True, exist_ok=True)
    cmd = [claude_exe(), "-p", "--model", model or MODEL, "--output-format", "json", "--no-session-persistence",
           "--tools", "", "--setting-sources", ""]
    if schema:
        cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=WORK, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(f"claude -p failed ({r.returncode}): {(r.stderr or r.stdout)[:400]}")
    if out.get("is_error"):
        raise RuntimeError(f"claude -p error: {str(out.get('result'))[:400]}")
    if schema:
        so = out.get("structured_output")
        if so is None:
            so = json.loads(re.search(r"\{.*\}", out.get("result", ""), re.S).group(0))
        return so
    return out.get("result", "")


def load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(c: dict):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CACHE)


def summarise(limit: int | None = None):
    cache, done = load_cache(), 0
    for cid in [k for k, lv in cache.items() if lv.get("v") == SCHEMA_VERSION and not lv.get("posted")]:
        post_level(cache, cid)                 # re-labelled or not-yet-sent levels go out even when summarising is not possible now
    now = time.time()
    projects = "、".join(sorted(d.name for d in GITHUB.iterdir() if d.is_dir() and not d.name.startswith((".", "_"))))
    logs = sorted(all_logs(), key=lambda sp: os.path.getmtime(sp[1]), reverse=True)   # newest first: recent work shows up first
    for source, path in logs:
        try:
            chat = read_claude(path) if source == "claude" else read_codex(path)
        except Exception as e:
            log("read failed", path, e)
            continue
        if not chat["turns"]:
            continue
        quiet = now - os.path.getmtime(path) > QUIET_SECONDS
        project = project_of(chat["cwd"])
        chunks = chunk(chat)
        previous = []                          # node titles of the chunk before, so a problem carried over keeps one node
        for c in chunks:
            cid = f"{source}:{chat['session']}:{c['idx']}"
            h = hashlib.sha1(c["text"].encode()).hexdigest()[:16]
            is_last = c["idx"] == len(chunks) - 1
            have = cache.get(cid)
            if have and have.get("v") != SCHEMA_VERSION:
                have = None                    # summarised in the old format: redo
            if have and (have["hash"] == h or (is_last and not quiet)):
                if not have.get("posted"):
                    post_level(cache, cid)
                previous = [n["title"] for n in have["data"].get("nodes", [])]
                continue
            if is_last and not quiet:
                continue                       # still being written; wait until it goes quiet
            if len(c["text"]) < 400:
                continue                       # a one-line chat is not a node
            when = (c["start_ts"] or "")[:10]
            proj = infer_project(c["text"], project, c.get("where")) if project in ("Github(总目录)", "其他") else idea_or(c["text"], redirect_project(c["text"], effective_project(project, c.get("start_ts"))))
            log(f"summarise {proj} {cid} ({len(c['text'])} chars)")
            try:
                prompt = PROMPT.format(source="Claude Code" if source == "claude" else "Codex", project=proj,
                                                idx=c["idx"] + 1, when=when, projects=projects,
                                                previous="、".join(previous) or "无", text=c["text"])
                try:
                    data = ask_claude(prompt, SCHEMA)
                except RuntimeError as e:          # now and then an empty error comes back; once more usually works
                    if any(w in str(e).lower() for w in ("limit", "quota", "usage")):
                        raise
                    log("  retry after:", e)
                    data = ask_claude(prompt, SCHEMA)
            except Exception as e:
                log("  failed:", e)
                if any(w in str(e).lower() for w in ("limit", "quota", "usage")):
                    save_cache(cache)
                    return done                # out of usage; try again next hour
                continue
            cache[cid] = {"v": SCHEMA_VERSION, "hash": h, "project": proj, "source": source, "session": chat["session"],
                          "idx": c["idx"], "start_ts": c["start_ts"], "end_ts": c["end_ts"], "data": data, "posted": False}
            previous = [n["title"] for n in data.get("nodes", [])]
            save_cache(cache)
            post_level(cache, cid)
            done += 1
            if limit and done >= limit:
                return done
    save_cache(cache)
    return done


def post_level(cache: dict, cid: str):
    lv = cache[cid]
    try:
        hub("POST", "/api/journey/chunks", {"chunks": [{"id": cid, **{k: lv[k] for k in
                                            ("project", "source", "session", "idx", "start_ts", "end_ts", "data")}}]})
        lv["posted"] = True
        save_cache(cache)
    except Exception as e:
        log("  post failed:", e)


# ---------------------------------------------------------------- topics: sort a big project's nodes into areas (washer, heater...)
NL = chr(10)
TOPICS_FILE = STATE / "topics.json"
posted_topics: set = set()
TOPIC_MIN = 15                                   # a project with fewer nodes stays one flat list
TOPIC_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["assignments"],
                "properties": {"assignments": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                               "required": ["i", "topic"], "properties": {"i": {"type": "integer"}, "topic": {"type": "string"}}}}}}


def classify_topics():
    """Give every node of a project with many nodes a topic (one device or area), so the page can open
    project -> topic -> nodes. Done in batches by the cheap model; known topics are offered again so the names stay stable."""
    try:
        store = json.loads(TOPICS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        store = {}
    folders = {d.name for d in GITHUB.iterdir() if d.is_dir()}
    per: dict = {}
    for cid, lv in load_cache().items():
        if lv.get("v") != SCHEMA_VERSION or not lv.get("data"):
            continue
        for i, n in enumerate(lv["data"].get("nodes", [])):
            proj = n.get("project") if n.get("project") in folders else lv.get("project")
            per.setdefault(proj, []).append((lv.get("start_ts") or "", f"{cid}:{i}", n))
    for proj, items in per.items():
        if len(items) < TOPIC_MIN:
            continue
        items.sort(key=lambda x: x[0])
        assign = store.setdefault(proj, {})
        live = {nid for _, nid, _ in items}
        for k in [k for k in assign if k not in live]:
            del assign[k]                                  # a node that no longer exists (chunk re-summarised)
        todo = [(nid, n) for _, nid, n in items if nid not in assign]
        for k in range(0, len(todo), 90):
            batch = todo[k:k + 90]
            known = sorted(set(assign.values()))
            lines = NL.join(f"{j}: {_short(n.get('title'), 40)} — {_short(n.get('trigger'), 60)}" for j, (_, n) in enumerate(batch))
            prompt = (f"下面是项目「{proj}」的开发节点（编号: 标题 — 起因）。把每个节点归到一个「话题」：按它主要涉及的设备或功能领域分，"
                      "例如 洗衣机、踢脚线、摄像头、空调、热水器、空气炸锅、地址与连接、部署与服务器、手机 App、首页与界面……" + NL +
                      "规则：话题名不超过 6 个字，不含斜杠；已有话题尽量复用：" + ("、".join(known) or "（暂无）") + "；确实不属于再新建，"
                      "全部话题控制在 16 个以内；一个节点只归一类，选最主要的；和具体设备无关的归「通用」。每个编号都要给出话题。" + NL + NL + lines)
            try:
                data = ask_claude(prompt, TOPIC_SCHEMA, timeout=600, model=SIMPLE_MODEL)
            except Exception as e:
                log("  topics failed:", e)
                break
            for a in data.get("assignments", []):
                if 0 <= a.get("i", -1) < len(batch):
                    assign[batch[a["i"]][0]] = (a.get("topic") or "通用").strip().replace("/", "·")[:12] or "通用"
            log(f"topics {proj}: {min(k + 90, len(todo))}/{len(todo)}")
        if todo:
            TOPICS_FILE.parent.mkdir(parents=True, exist_ok=True)
            TOPICS_FILE.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
        if todo or proj not in posted_topics:                   # once per run even when nothing is new: the hub may have missed it
            try:
                hub("POST", "/api/journey/topics", {"project": proj, "assign": assign})
                posted_topics.add(proj)
            except Exception as e:
                log("  topics post failed:", e)


# ---------------------------------------------------------------- git

def _proxy_env(base: dict | None = None) -> dict:
    """github.com is only reachable through the PC's proxy (Clash). A shell has HTTPS_PROXY set; the background task does not,
    and git ignores the Windows system proxy: take it from the system setting."""
    e = dict(base if base is not None else os.environ)
    if not (e.get("HTTPS_PROXY") or e.get("https_proxy")):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
                if winreg.QueryValueEx(k, "ProxyEnable")[0]:
                    srv = str(winreg.QueryValueEx(k, "ProxyServer")[0]).split(";")[0].split("=")[-1].strip()
                    if srv:
                        url = srv if "://" in srv else "http://" + srv
                        e["HTTPS_PROXY"] = e["HTTP_PROXY"] = url
        except Exception:
            pass
    return e


def git(repo: Path, *args, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=_proxy_env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def candidates() -> list[Path]:
    out = []
    for d in sorted(GITHUB.iterdir()):
        if d.is_dir() and not d.name.startswith((".", "_")):
            out.append(d)
            for sub in d.iterdir() if not (d / ".git").exists() else []:
                if sub.is_dir() and (sub / ".git").exists():
                    out.append(sub)        # repos one level down (e.g. LearnSync\\server)
    return out


_me = None


def _find_gh() -> str:
    """The background task has a short PATH: look in the usual install places too."""
    import shutil
    for c in (shutil.which("gh"), r"C:\Program Files\GitHub CLI\gh.exe", r"C:\Program Files (x86)\GitHub CLI\gh.exe",
              str(Path.home() / "AppData/Local/Programs/GitHub CLI/gh.exe")):
        if c and Path(c).exists():
            return c
    return "gh"


GH = _find_gh()


def _gh_env() -> dict:
    """gh finds its login through USERPROFILE/APPDATA; the background task may not pass them on."""
    e = _proxy_env()
    home = e.get("USERPROFILE") or str(Path.home())
    e.setdefault("USERPROFILE", home)
    e.setdefault("APPDATA", home + r"\AppData\Roaming")
    e.setdefault("LOCALAPPDATA", home + r"\AppData\Local")
    tok = STATE / "gh_token"          # the background task cannot see gh's own login (it lives in the interactive profile)
    if tok.exists() and not e.get("GH_TOKEN"):
        e["GH_TOKEN"] = tok.read_text(encoding="utf-8").strip()
    return e


def my_login() -> str:
    """The GitHub account the user pushes as (gh CLI); repos cloned from someone else are not theirs to push."""
    global _me
    if _me is None:
        try:
            _me = subprocess.run([GH, "api", "user", "--jq", ".login"], capture_output=True, text=True, timeout=20, env=_gh_env(),
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        except Exception:
            _me = ""
        if not _me:                            # gh missing / not on PATH in the background task: the owner of most repos here
            owners = {}
            for d in GITHUB.iterdir():
                if (d / ".git").exists():
                    m = re.search(r"github\.com[:/]([^/]+)/", git(d, "remote", "get-url", "origin").stdout)
                    if m:
                        owners[m.group(1)] = owners.get(m.group(1), 0) + 1
            _me = max(owners, key=owners.get) if owners else ""
    return _me


_vis: dict = {}


def gh(*args, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run([GH, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=_gh_env(),
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def slug_of(remote: str) -> str:
    return re.sub(r"\.git$", "", remote.split("github.com")[-1].lstrip(":/"))


def visibility(slug: str) -> str | None:
    """'public' / 'private' from GitHub (cached 10 min); None when gh cannot tell."""
    hit = _vis.get(slug)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        r = gh("repo", "view", slug, "--json", "visibility", "--jq", ".visibility", timeout=20)
        v = r.stdout.strip().lower() if r.returncode == 0 else None
        if v is None:
            log("  gh repo view", slug, "failed:", (r.stderr or r.stdout)[-200:])
    except Exception as e:
        log("  gh repo view", slug, "error:", repr(e))
        v = None
    _vis[slug] = (time.time(), v)
    return v


def scan_repo(d: Path) -> dict:
    name = str(d.relative_to(GITHUB))
    info = {"name": name, "path": str(d)}
    if not (d / ".git").exists():
        newest = 0
        try:
            for p in d.rglob("*"):
                if p.is_file() and not any(x in p.parts for x in ("node_modules", "build", ".gradle", "__pycache__")):
                    newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
        info.update(git=False, changed=newest or None)
        return info
    st = git(d, "status", "--porcelain=v1", "-b", "-uall")
    lines = st.stdout.splitlines()
    head = lines[0] if lines and lines[0].startswith("##") else ""
    files = [ln[3:] for ln in lines[1:]][:300]
    remote = git(d, "remote", "get-url", "origin").stdout.strip()
    ahead = re.search(r"ahead (\d+)", head)
    upstream = "..." in head
    unpushed = int(ahead.group(1)) if ahead else 0
    if not upstream and remote:
        unpushed = int(git(d, "rev-list", "--count", "HEAD", "--not", "--remotes").stdout.strip() or 0)
    last = git(d, "log", "-1", "--format=%cI|%s").stdout.strip().split("|", 1)
    owner = re.search(r"github\.com[:/]([^/]+)/", remote)
    mine = not (owner and my_login()) or owner.group(1).lower() == my_login().lower()
    info.update(git=True, mine=mine, branch=re.sub(r"^## (No commits yet on )?", "", head).split("...")[0], remote=re.sub(r"//[^@/]+@", "//", remote),
                upstream=upstream, dirty=len(lines) - 1 if head else len(lines), files=files, unpushed=unpushed,
                last_commit=last[0] if last and last[0] else None, last_msg=last[1] if len(last) > 1 else None,
                secrets=[f for f in files if SECRET_RE.search(f.split(" -> ")[-1].strip('"'))])
    if owner and mine:
        info["visibility"] = visibility(slug_of(remote))
    return info


def scan():
    repos = []
    for d in candidates():
        try:
            repos.append(scan_repo(d))
        except Exception as e:
            log("scan failed", d, e)
    hub("POST", "/api/journey/repos", {"scanned": datetime.now(timezone.utc).isoformat(), "repos": repos})
    return repos


def recent_nodes(repo: Path, since: str | None) -> list[dict]:
    """The Idea nodes of this project worked on since the last commit: they hold the reason behind the diff."""
    name = repo.name
    try:
        chunks = hub("GET", "/api/journey").get("chunks", [])
    except Exception:
        return []
    out = []
    for c in chunks:
        if c.get("project") != name and not any(n.get("project") == name for n in (c.get("nodes") or [])):
            continue
        if since and (c.get("end_ts") or "") < since:
            continue
        for n in c.get("nodes") or []:
            if (n.get("project") or c.get("project")) == name:
                out.append({"ts": c.get("end_ts") or "", **n})
    out.sort(key=lambda n: n["ts"])
    return out[-6:]


def why_block(repo: Path) -> str:
    since = git(repo, "log", "-1", "--format=%cI").stdout.strip() or None
    lines = []
    for n in recent_nodes(repo, since):
        bits = [f"- 节点「{n.get('title', '')}」（{n.get('status', '')}）", f"  起因：{n.get('trigger', '')}"]
        if n.get("root_cause"):
            bits.append(f"  根因/关键做法：{n['root_cause']}")
        fails = [a for a in n.get("attempts") or [] if not a.get("ok")]
        if fails:
            bits.append("  走不通的路：" + "；".join(f"{a.get('what', '')}（{a.get('why', '')}）" for a in fails[:3]))
        lines.append("\n".join(bits))
    return "\n".join(lines)


NL = chr(10)


def tidy_message(msg: str) -> str:
    """The small model sometimes wraps the message in a code fence or adds a note about its own sources: keep only the message."""
    lines = [ln for ln in str(msg).strip().splitlines() if not ln.strip().startswith("```")]
    paras = [q for q in NL.join(lines).strip().split(NL + NL) if q.strip()]
    while len(paras) > 1 and paras[-1].lstrip().startswith(("注意", "说明：", "备注")):
        paras.pop()
    return (NL + NL).join(paras).strip()


def commit_message(repo: Path, first: bool = False) -> str:
    stat = git(repo, "diff", "--cached", "--stat").stdout[-3000:]
    diff = git(repo, "diff", "--cached", "--unified=1").stdout[:20000]
    why = "" if first else why_block(repo)
    ask = "根据下面的 git 改动写一条提交信息：第一行≤60字符的英文概括（祈使句），空一行后用中文列 2-5 条要点说明改了什么；"
    if why:
        ask += ("再空一行，写一段以「为什么：」开头的中文（2-4 句），依据下面给出的开发记录讲清这次改动的起因、试过哪些走不通的路、"
                "最后为什么这么做；只使用记录里真实有的内容，不要编造，和改动无关的记录跳过。")
    ask += "只输出提交信息本身。\n\n"
    if why:
        ask += "【开发记录（Idea 里这个项目最近的节点）】\n" + why + "\n\n"
    try:
        msg = tidy_message(ask_claude(ask + stat + "\n\n" + diff, None, timeout=300, model=SIMPLE_MODEL))
        if msg:
            return msg
    except Exception as e:
        log("  commit message via claude failed:", e)
    n = len(git(repo, "diff", "--cached", "--name-only").stdout.split())
    return f"{'Initial commit' if first else 'Update'} {n} files ({datetime.now():%Y-%m-%d})"


def do_push(name: str) -> tuple[str, dict]:
    repo = (GITHUB / name).resolve()
    if GITHUB.resolve() not in repo.parents or not (repo / ".git").exists():
        return "failed", {"error": "不是 D:\\Github 下的 git 仓库"}
    info = scan_repo(repo)
    if not info.get("mine", True):
        return "failed", {"error": "这是别人的仓库（克隆来的），不能推送"}
    if not info.get("remote"):
        return "failed", {"error": "这个仓库没有远程地址（origin），先在 GitHub 建库"}
    if info["secrets"]:
        return "failed", {"error": "改动里有像密码/密钥的文件，没推送；把它们加进 .gitignore 再试", "files": info["secrets"]}
    steps = {}
    if info["dirty"]:
        git(repo, "add", "-A")
        staged = git(repo, "diff", "--cached", "--name-only").stdout.split("\n")
        bad = [f for f in staged if f and SECRET_RE.search(f)]
        if bad:
            git(repo, "reset", "-q")
            return "failed", {"error": "改动里有像密码/密钥的文件，没推送", "files": bad}
        msg = commit_message(repo) + "\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
        c = subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-F", "-"], input=msg, capture_output=True, text=True,
            encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if c.returncode:
            return "failed", {"error": "提交失败", "detail": (c.stderr or c.stdout)[-800:]}
        steps["commit"] = msg.splitlines()[0]
    args = ["push"] if info["upstream"] else ["push", "-u", "origin", "HEAD"]
    p = git(repo, *args, timeout=300)
    if p.returncode:
        return "failed", {"error": "推送失败", "detail": (p.stderr or p.stdout)[-800:], **steps}
    steps["push"] = (p.stderr or p.stdout).strip()[-300:] or "ok"
    return "done", steps


DEFAULT_IGNORE = """# added by Idea: things that must never reach GitHub, and build leftovers
.env
.env.*
!.env.example
*.pem
*.key
*.p12
*.pfx
*.jks
*.keystore
local.properties
secrets.properties
keystore.properties
auth.json
node_modules/
__pycache__/
*.pyc
.gradle/
build/
.idea/
.vscode/
*.log
.DS_Store
Thumbs.db
"""
BIG_FILE = 50 * 1024 * 1024
LEAK_RE = r"-----BEGIN [A-Z ]*PRIVATE KEY|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{32,}|AIza[0-9A-Za-z_-]{35}"


def ensure_gitignore(repo: Path):
    f = repo / ".gitignore"
    have = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
    have_lines = {x.strip() for x in have.splitlines()}
    need = [x for x in DEFAULT_IGNORE.splitlines() if x and not x.startswith("#") and x not in have_lines]
    if not f.exists():
        f.write_text(DEFAULT_IGNORE, encoding="utf-8")
    elif need:
        f.write_text(have.rstrip("\n") + "\n\n# added by Idea\n" + "\n".join(need) + "\n", encoding="utf-8")


def history_leaks(repo: Path) -> list[str]:
    """What making the repo public would expose: secret-looking file names anywhere in history, and key/token patterns
    in the current files. Not a guarantee, but it stops the usual accidents."""
    bad = set()
    names = git(repo, "log", "--all", "--name-only", "--pretty=format:", timeout=120).stdout.split("\n")
    for n in {x.strip() for x in names if x.strip()}:
        if SECRET_RE.search(n):
            bad.add(n)
    g = git(repo, "grep", "-I", "-l", "-E", LEAK_RE, "HEAD", timeout=120)
    for ln in g.stdout.splitlines():
        bad.add(ln.split(":", 1)[-1] + "（内容像密钥）")
    return sorted(bad)


def do_create(name: str, public: bool) -> tuple[str, dict]:
    if "\\" in name or "/" in name:
        return "failed", {"error": "只能给 D:\\Github 下的一级项目文件夹建库"}
    repo = (GITHUB / name).resolve()
    if repo.parent != GITHUB.resolve() or not repo.is_dir():
        return "failed", {"error": "找不到这个项目文件夹"}
    me = my_login()
    if not me:
        return "failed", {"error": "没读到 GitHub 登录（gh auth status）"}
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "project"
    if gh("repo", "view", f"{me}/{slug}").returncode == 0:
        return "failed", {"error": f"GitHub 上已经有 {me}/{slug}，没有创建（避免覆盖）"}
    steps = {}
    fresh = not (repo / ".git").exists()
    if not fresh and git(repo, "remote", "get-url", "origin").stdout.strip():
        return "failed", {"error": "这个仓库已经有远程地址了"}
    ensure_gitignore(repo)
    if fresh:
        i = git(repo, "init", "-q", "-b", "main")
        if i.returncode:
            return "failed", {"error": "git init 失败", "detail": i.stderr[-400:]}
        steps["init"] = "ok"
    git(repo, "add", "-A")
    staged = [f for f in git(repo, "diff", "--cached", "--name-only").stdout.split("\n") if f]
    has_head = git(repo, "rev-parse", "--verify", "-q", "HEAD").returncode == 0
    bad = [f for f in staged if SECRET_RE.search(f)]
    big = [f for f in staged if (repo / f).is_file() and (repo / f).stat().st_size > BIG_FILE]
    if bad or big or (not staged and not has_head):
        git(repo, "reset", "-q")
        if bad:
            return "failed", {"error": "里面有像密码/密钥的文件，没有上传；加进 .gitignore 再试", "files": bad}
        if big:
            return "failed", {"error": "有超过 50MB 的大文件（GitHub 单个文件上限 100MB），没有上传；加进 .gitignore 再试", "files": big[:20]}
        return "failed", {"error": "文件夹里没有可上传的文件"}
    if staged:
        msg = commit_message(repo, first=not has_head) + "\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
        c = subprocess.run(["git", "-C", str(repo), "commit", "-q", "-F", "-"], input=msg, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if c.returncode:
            return "failed", {"error": "提交失败", "detail": (c.stderr or c.stdout)[-800:]}
        steps["commit"] = msg.splitlines()[0]
    if public:
        leaks = history_leaks(repo)
        if leaks:
            return "failed", {"error": "公开会让这些内容被所有人看到，先处理再公开（已提交在本地，没上传）", "files": leaks[:20], **steps}
    r = gh("repo", "create", f"{me}/{slug}", "--public" if public else "--private", "--source", str(repo),
           "--remote", "origin", "--push", timeout=300)
    if r.returncode:
        return "failed", {"error": "GitHub 建库/上传失败", "detail": (r.stderr or r.stdout)[-800:], **steps}
    steps["repo"] = f"{me}/{slug}（{'公开' if public else '私有'}）"
    return "done", steps


def do_visibility(name: str, public: bool) -> tuple[str, dict]:
    repo = (GITHUB / name).resolve()
    if GITHUB.resolve() not in repo.parents or not (repo / ".git").exists():
        return "failed", {"error": "不是 D:\\Github 下的 git 仓库"}
    info = scan_repo(repo)
    if not info.get("mine", True) or not info.get("remote"):
        return "failed", {"error": "不是你自己的、有远程的仓库"}
    slug = slug_of(info["remote"])
    if public:
        leaks = history_leaks(repo)
        if leaks:
            return "failed", {"error": "公开会让这些内容被所有人看到（包括历史提交），没有改", "files": leaks[:20]}
    r = gh("repo", "edit", slug, "--visibility", "public" if public else "private",
           "--accept-visibility-change-consequences", timeout=60)
    _vis.pop(slug, None)
    if r.returncode:
        return "failed", {"error": "改可见性失败", "detail": (r.stderr or r.stdout)[-800:]}
    return "done", {"repo": slug, "visibility": "public" if public else "private"}


def do_job(kind: str, repo: str) -> tuple[str, dict]:
    if kind == "push":
        return do_push(repo)
    if kind in ("create_private", "create_public"):
        return do_create(repo, kind == "create_public")
    if kind in ("make_private", "make_public"):
        return do_visibility(repo, kind == "make_public")
    return "failed", {"error": f"不认识的任务 {kind}"}


def run_jobs():
    for job in hub("GET", "/api/journey/jobs?open=1"):
        if job["status"] != "queued":
            continue
        log("job", job["id"], job["kind"], job["repo"])
        hub("POST", f"/api/journey/jobs/{job['id']}", {"status": "running"})
        try:
            status, result = do_job(job["kind"], job["repo"])
        except Exception as e:
            status, result = "failed", {"error": str(e)}
        log("  ->", status, result)
        hub("POST", f"/api/journey/jobs/{job['id']}", {"status": status, "result": result})
        try:
            scan()
        except Exception:
            pass


def heartbeat(**extra):
    try:
        hub("POST", "/api/journey/heartbeat", {"host": os.environ.get("COMPUTERNAME", "pc"), **extra}, timeout=10)
    except Exception:
        pass


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "dry":
        p = Path(sys.argv[2])
        chat = read_codex(p) if "codex" in str(p) else read_claude(p)
        for c in chunk(chat):
            print(f"===== level {c['idx']} {c['start_ts']} ({len(c['text'])} chars)\n{c['text'][:3000]}\n")
        return
    if cmd == "scan":
        print(json.dumps(scan(), ensure_ascii=False, indent=1))
        return
    if cmd == "once":
        scan()
        n = summarise(int(sys.argv[2]) if len(sys.argv) > 2 else None)
        log("summarised", n)
        classify_topics()
        return
    import threading

    def job_loop():                      # its own thread, so a push is not stuck behind an hour of summarising
        while True:
            try:
                run_jobs()
            except Exception as e:
                log("jobs error:", e)
            time.sleep(15)
    threading.Thread(target=job_loop, daemon=True).start()
    last_scan = last_sum = 0.0
    log("collector started")
    while True:
        try:
            if time.time() - last_scan > 600:
                scan()
                last_scan = time.time()
                heartbeat(state="idle")
            if time.time() - last_sum > 600:
                last_sum = time.time()
                heartbeat(state="summarising")
                n = summarise()
                log("summarised", n)
                classify_topics()
                heartbeat(state="idle", summarised=n)
        except Exception as e:
            log("loop error:", e)
        time.sleep(20)


if __name__ == "__main__":
    main()
