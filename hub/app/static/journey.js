// Idea: every project, and in each project the nodes (one bug / feature / deployment each). A node shows why it was
// done and every attempt in order - what was tried, why, what happened, why it worked or failed. Plus a pitfall
// library (all failed attempts), a skill list, an idea notebook and the repos with changes not yet on GitHub.
// The data comes from the PC (tools/journey/collect.py), which has Claude summarise the Claude Code / Codex chats.
"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const APP = window.IdeaApp || null;                 // the Android shell (relays requests; POST bodies go in a header)
const S = { data: null, q: "", tech: "", repoOpen: new Set(), model: null };
const STATUS_CLS = { "解决": "ok", "部分解决": "part", "没解决": "bad", "进行中": "wip" };
const IDEA_STATUS = { new: "新点子", doing: "在做", done: "做完了", dropped: "放下了" };
const KIND_ICON = { bug: "🐞", "功能": "✨", "部署": "🚀", "研究": "🔍", "重构": "🧹", "其他": "📌" };

function store(k, v) { try { v === undefined ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { } }
function stored(k) { try { return localStorage.getItem(k); } catch { return null; } }

async function api(path, body) {
  const opt = { method: body === undefined ? "GET" : "POST", headers: { "Content-Type": "application/json" } };
  if (body !== undefined) {
    opt.body = JSON.stringify(body);
    if (APP) opt.headers["X-HH-Body"] = btoa(unescape(encodeURIComponent(opt.body)));   // the shell can't read POST bodies
  }
  const r = await fetch(path, opt);
  if (!r.ok) throw new Error((await r.text()) || r.status);
  return r.json();
}
const day = ts => ts ? new Date(ts).toLocaleDateString("zh-CN", { year: "numeric", month: "numeric", day: "numeric" }) : "";
const ago = sec => { const m = Math.round((Date.now() / 1000 - sec) / 60); return m < 1 ? "刚刚" : m < 60 ? `${m} 分钟前` : m < 1440 ? `${Math.round(m / 60)} 小时前` : `${Math.round(m / 1440)} 天前`; };

async function load(force = true) {
  try {
    const wasOffline = S.offline;
    const rev = await api("/api/journey/rev").then(r => r.rev, () => null);   // a few bytes; the overview itself is megabytes
    if (!force && S.data && rev && rev === S.rev && !S.offline) return;
    const d = await api("/api/journey");
    const text = JSON.stringify(d);
    S.rev = rev;
    store("idea.cache", text);
    const same = text === S.text; S.text = text;
    S.offline = false;
    if (same && S.data && !wasOffline) return;                       // nothing new: keep the page as it is (a re-render drops taps in flight)
    S.data = d; S.model = null;
  } catch (e) {
    if (!S.data) { const c = stored("idea.cache"); if (c) S.data = JSON.parse(c); }
    S.offline = true;
    if (!S.data) { $("#app").innerHTML = `<div class="card empty">连不上家里的服务器：${esc(e.message)}<br><button class="btn" data-act="reload" style="margin-top:12px">重试</button></div>`; return; }
  }
  render();
}

// ---------------------------------------------------------------- the model: chunks -> nodes per project
function model() {
  if (S.model) return S.model;
  const chunks = (S.data.chunks || []).filter(c => Array.isArray(c.nodes) && !c.skip)
    .sort((a, b) => a.session.localeCompare(b.session) || a.idx - b.idx);
  const folders = new Set((S.data.chunks || []).map(c => c.project));
  ((S.data.repos || {}).list || []).forEach(r => folders.add(r.name.split(/[\\/]/)[0]));
  const nodes = [], bySession = new Map();
  for (const c of chunks) {
    const seen = bySession.get(c.session) || new Map();
    bySession.set(c.session, seen);
    c.nodes.forEach((n, i) => {
      const prev = n.continues && seen.get(n.continues);
      if (prev) {                                   // the same problem carried on in the next stretch of the chat
        prev.attempts.push(...(n.attempts || []));
        Object.assign(prev, { status: n.status, root_cause: n.root_cause || prev.root_cause, lesson: n.lesson || prev.lesson, end_ts: c.end_ts });
        prev.tech = [...new Set([...prev.tech, ...(n.tech || [])])];
        seen.set(n.title, prev);
        return;
      }
      const node = { ...n, id: `${c.id}:${i}`, tech: n.tech || [], attempts: [...(n.attempts || [])], source: c.source, session: c.session,
                     start_ts: c.start_ts, end_ts: c.end_ts, project: n.project && folders.has(n.project) ? n.project : c.project };
      nodes.push(node); seen.set(n.title, node);
    });
  }
  const TP = S.data.topics || {};
  for (const n of nodes) n.topic = (TP[n.project] || {})[n.id] || null;
  nodes.sort((a, b) => (a.start_ts || "").localeCompare(b.start_ts || ""));
  const projects = new Map();
  for (const n of nodes) {
    if (!projects.has(n.project)) projects.set(n.project, { name: n.project, nodes: [] });
    projects.get(n.project).nodes.push(n);
  }
  const plist = [...projects.values()].map(p => ({ ...p, last: p.nodes.at(-1).end_ts || p.nodes.at(-1).start_ts, first: p.nodes[0].start_ts }))
    .sort((a, b) => (b.last || "").localeCompare(a.last || ""));
  return (S.model = { nodes, projects: plist, byId: new Map(nodes.map(n => [n.id, n])) });
}
const fails = n => n.attempts.filter(a => !a.ok).length;
function techCount(nodes) {
  const m = new Map();
  for (const n of nodes) for (const t of n.tech) {
    const k = t.trim().toLowerCase(); if (!k) continue;
    if (!m.has(k)) m.set(k, { name: t.trim(), n: 0, projects: new Set() });
    const e = m.get(k); e.n++; e.projects.add(n.project);
  }
  return [...m.values()].sort((a, b) => b.n - a.n);
}
function repoState(r) {
  if (!r.git) return { cls: "grey", text: "不是 git 仓库（没上 GitHub）", rank: 2 };
  if (r.mine === false) return { cls: "grey", text: "别人的仓库（克隆来用的，不用推）", rank: 4 };
  if (!r.remote) return { cls: "red", text: "没有远程仓库", rank: 0 };
  if (r.dirty) return { cls: "red", text: `${r.dirty} 个文件改了没提交` + (r.unpushed ? `，${r.unpushed} 个提交没推送` : ""), rank: 0 };
  if (r.unpushed) return { cls: "amber", text: `${r.unpushed} 个提交没推送`, rank: 1 };
  return { cls: "green", text: "已同步到 GitHub", rank: 3 };
}
function pendingRepos() {
  const ign = new Set(S.data.ignored || []);
  return ((S.data.repos || {}).list || []).filter(r => !ign.has(r.name) && r.git && r.mine !== false && (r.dirty || r.unpushed || !r.remote));
}

// ---------------------------------------------------------------- routes: #/  #/p/<project>  #/n/<node>  #/pits #/skills #/ideas #/repos
function route() {
  const h = decodeURIComponent(location.hash.slice(1) || "/");
  const m = h.match(/^\/(p|n|t)\/(.+)$/);
  if (m) return { view: { p: "project", n: "node", t: "topic" }[m[1]], arg: m[2] };
  const tab = h.slice(1);
  return { view: ["pits", "skills", "ideas", "repos"].includes(tab) ? tab : "projects" };
}
const go = h => { location.hash = h; };

function render() {
  if (!S.data) return;
  const r = route();
  const top = r.view === "project" || r.view === "node" || r.view === "topic";
  $("#app").innerHTML = (top ? "" : header()) + ({
    projects: viewProjects, project: () => viewProject(r.arg), node: () => viewNode(r.arg), topic: () => viewTopic(r.arg),
    pits: viewPits, skills: viewSkills, ideas: viewIdeas, repos: viewRepos,
  }[r.view])();
  document.title = "Idea";
}

function header() {
  const M = model(), pend = pendingRepos(), col = S.data.collector || {}, r = route();
  const solved = M.nodes.filter(n => n.status === "解决").length;
  const ideas = (S.data.ideas || []).filter(i => i.status === "new" || i.status === "doing").length;
  const sub = S.offline ? "离线：显示的是上次的内容"
    : col.seen ? `电脑 ${ago(col.seen)}在线${col.state === "summarising" ? " · 正在整理新对话" : ""}` : "电脑上的收集器还没连上";
  const tabs = [["", "项目"], ["pits", "踩坑"], ["skills", "技能"], ["ideas", "点子"], ["repos", "仓库", pend.length]];
  const cur = r.view === "projects" ? "" : r.view;
  const app = APP ? ` · v${esc(APP.versionName())} · ${esc(APP.route())} · <a href="#" data-act="update">检查更新</a>` : "";
  return `<div class="hero"><div class="brand"><span class="bulb">💡</span>Idea</div><div class="sub">${esc(sub)}${app}</div></div>
    <div class="stats">
      <div class="stat"><b>${M.projects.length}</b><span>项目</span></div>
      <div class="stat"><b>${M.nodes.length}</b><span>节点</span></div>
      <div class="stat"><b>${M.nodes.length ? Math.round(100 * solved / M.nodes.length) : 0}%</b><span>解决率</span></div>
      <div class="stat"><b>${M.nodes.reduce((s, n) => s + fails(n), 0)}</b><span>失败尝试</span></div>
      <div class="stat"><b>${ideas}</b><span>待做点子</span></div></div>
    ${pend.length && r.view !== "repos" ? `<div class="card banner" data-go="#/repos">⚠️ ${pend.length} 个项目的改动还没上 GitHub：${pend.slice(0, 3).map(x => esc(x.name)).join("、")}${pend.length > 3 ? " 等" : ""} ›</div>` : ""}
    <div class="tabs">${tabs.map(([k, n, d]) => `<button class="tab ${cur === k ? "on" : ""}" data-go="#/${k}">${n}${d ? `<span class="dot">${d}</span>` : ""}</button>`).join("")}</div>`;
}

function back(label, to) { return `<div class="topbar"><button class="back" data-back="${esc(to)}">‹ ${esc(label)}</button></div>`; }
function statusBadge(s) { return `<span class="badge ${STATUS_CLS[s] || "wip"}">${esc(s || "进行中")}</span>`; }
function tries(n) { return n.attempts.map(a => a.ok ? "✅" : "❌").join(""); }

function viewProjects() {
  const M = model();
  if (!M.projects.length) return `<div class="card empty">还没有内容。电脑上的收集器正在把你和 Claude / Codex 的历史对话整理成节点，每段大约 1 分钟，整理好一段就会出现一段。</div>`;
  const recent = [...M.nodes].sort((a, b) => (b.end_ts || b.start_ts || "").localeCompare(a.end_ts || a.start_ts || "")).slice(0, 8);
  const latest = `<div class="sub" style="margin:4px 2px 8px">最近的节点（刚做完的事，从新到旧）</div>` + recent.map(n => `<div class="card node" data-go="#/n/${encodeURIComponent(n.id)}">
      <div class="row"><b class="grow">${KIND_ICON[n.kind] || "📌"} ${esc(n.title)}</b>${statusBadge(n.status)}</div>
      <div class="muted">${esc(n.project)} · ${esc(day(n.end_ts || n.start_ts))} · 试了 ${n.attempts.length} 次 ${tries(n)}</div></div>`).join("")
    + `<div class="sub" style="margin:14px 2px 8px">所有项目</div>`;
  return latest + M.projects.map(p => {
    const solved = p.nodes.filter(n => n.status === "解决").length, open = p.nodes.filter(n => n.status !== "解决").length;
    const tech = techCount(p.nodes).slice(0, 6).map(t => `<span class="tag">${esc(t.name)}</span>`).join("");
    const lastN = p.nodes.at(-1);
    return `<div class="card proj" data-go="#/p/${encodeURIComponent(p.name)}">
      <div class="row"><b class="grow pname">${esc(p.name)}</b><span class="muted">${esc(day(p.last))}</span></div>
      <div class="muted">${p.nodes.length} 个节点 · 解决 ${solved}${open ? ` · <span class="bad-t">未完 ${open}</span>` : ""} · 失败尝试 ${p.nodes.reduce((s, n) => s + fails(n), 0)}</div>
      <div class="muted last">最近：${KIND_ICON[lastN.kind] || "📌"} ${esc(lastN.title)}</div>
      <div>${tech}</div></div>`;
  }).join("");
}

const nodeCard = (n, i) => `<div class="step"><div class="dotnum ${STATUS_CLS[n.status] || "wip"}">${i + 1}</div>
      <div class="card node" data-go="#/n/${encodeURIComponent(n.id)}">
        <div class="row"><b class="grow">${KIND_ICON[n.kind] || "📌"} ${esc(n.title)}</b>${statusBadge(n.status)}</div>
        <div class="muted">${esc(day(n.start_ts))} · 试了 ${n.attempts.length} 次 ${tries(n)}</div>
        <div class="trig">${esc(n.trigger)}</div></div></div>`;
const NEW_TOPIC = "新节点", SMALL_TOPIC = "其他零散";

function viewTopic(arg) {
  const k = arg.indexOf("/"), name = arg.slice(0, k), topic = arg.slice(k + 1);
  const p = model().projects.find(x => x.name === name);
  if (!p) return back("项目", "#/") + `<div class="card empty">没有这个项目</div>`;
  const size = new Map();
  p.nodes.forEach(n => size.set(n.topic, (size.get(n.topic) || 0) + 1));
  const tp = n => !n.topic ? NEW_TOPIC : size.get(n.topic) < 3 ? SMALL_TOPIC : n.topic;
  const ns = p.nodes.map((n, i) => ({ n, i })).filter(({ n }) => tp(n) === topic);
  const solved = ns.filter(({ n }) => n.status === "解决").length;
  return back(name, "#/p/" + encodeURIComponent(name)) + `<div class="card"><div class="ptitle">${esc(topic)}</div>
      <div class="muted">${esc(name)} · ${ns.length} 个节点 · 解决 ${solved} · 失败尝试 ${ns.reduce((s, { n }) => s + fails(n), 0)}</div></div>
    <div class="chain">${ns.reverse().map(({ n, i }) => nodeCard(n, i)).join("")}</div>`;
}

function viewTopics(p) {
  const by = new Map();
  const size = new Map();
  p.nodes.forEach(n => size.set(n.topic, (size.get(n.topic) || 0) + 1));
  p.nodes.forEach(n => { const t = !n.topic ? NEW_TOPIC : size.get(n.topic) < 3 ? SMALL_TOPIC : n.topic; if (!by.has(t)) by.set(t, []); by.get(t).push(n); });
  const rows = [...by.entries()].map(([t, ns]) => ({ t, ns, last: ns.reduce((m, n) => ((n.end_ts || n.start_ts || "") > m ? (n.end_ts || n.start_ts) : m), "") }))
    .sort((a, b) => b.last.localeCompare(a.last));
  return rows.map(({ t, ns, last }) => {
    const latest = [...ns].sort((a, b) => (b.end_ts || b.start_ts || "").localeCompare(a.end_ts || a.start_ts || ""))[0];
    const solved = ns.filter(n => n.status === "解决").length;
    return `<div class="card proj" data-go="#/t/${encodeURIComponent(p.name + "/" + t)}">
      <div class="row"><b class="grow pname">${esc(t)}</b><span class="muted">${esc(day(last))}</span></div>
      <div class="muted">${ns.length} 个节点 · 解决 ${solved} · 失败尝试 ${ns.reduce((s, n) => s + fails(n), 0)}</div>
      <div class="trig">最近：${esc(latest.title)}</div></div>`;
  }).join("");
}

function viewProject(name) {
  const p = model().projects.find(x => x.name === name);
  if (!p) return back("项目", "#/") + `<div class="card empty">没有这个项目</div>`;
  if (p.nodes.filter(n => n.topic).length >= 10 && S.flat !== name) {
    const solvedAll = p.nodes.filter(n => n.status === "解决").length;
    return back("全部项目", "#/") + `<div class="card"><div class="ptitle">${esc(p.name)}</div>
      <div class="muted">${esc(day(p.first))} 开始 · ${p.nodes.length} 个节点 · 解决 ${solvedAll} · 失败尝试 ${p.nodes.reduce((s, n) => s + fails(n), 0)}</div>
      <div class="muted" style="margin-top:4px">按话题分好了，点一个话题看里面的节点。</div></div>
      ${viewTopics(p)}
      <div class="card empty" data-act="flat" data-name="${esc(name)}">不分话题，看全部节点（按时间） ›</div>`;
  }
  const tech = techCount(p.nodes).slice(0, 16).map(t => `<span class="tag">${esc(t.name)} ${t.n}</span>`).join("");
  const solved = p.nodes.filter(n => n.status === "解决").length;
  const LIM = S.showAll === name ? Infinity : 40, hidden = Math.max(0, p.nodes.length - LIM);
  const list = p.nodes.map((n, i) => ({ n, i })).filter(({ i }) => i >= hidden).reverse().map(({ n, i }) => `<div class="step"><div class="dotnum ${STATUS_CLS[n.status] || "wip"}">${i + 1}</div>
      <div class="card node" data-go="#/n/${encodeURIComponent(n.id)}">
        <div class="row"><b class="grow">${KIND_ICON[n.kind] || "📌"} ${esc(n.title)}</b>${statusBadge(n.status)}</div>
        <div class="muted">${esc(day(n.start_ts))} · 试了 ${n.attempts.length} 次 ${tries(n)}</div>
        <div class="trig">${esc(n.trigger)}</div></div></div>`).join("")
    + (hidden ? `<div class="card empty" data-act="all" data-name="${esc(name)}">还有更早的 ${hidden} 个节点，点这里全部展开 ›</div>` : "");
  return back("全部项目", "#/") + `<div class="card"><div class="ptitle">${esc(p.name)}</div>
      <div class="muted">${esc(day(p.first))} 开始 · ${p.nodes.length} 个节点 · 解决 ${solved} · 失败尝试 ${p.nodes.reduce((s, n) => s + fails(n), 0)}</div>
      <div style="margin-top:6px">${tech}</div></div>
    <div class="chain">${list}</div>`;
}

function viewNode(id) {
  const n = model().byId.get(id);
  if (!n) return back("返回", "#/") + `<div class="card empty">这个节点不见了（可能重新整理过），回项目里再找找</div>`;
  const steps = n.attempts.map((a, i) => `<div class="attempt ${a.ok ? "ok" : "no"}">
      <div class="an">第 ${i + 1} 次 ${a.ok ? "✅ 成功" : "❌ 失败"}</div>
      <div class="what">${esc(a.what)}</div>
      <dl><dt>为什么这么做</dt><dd>${esc(a.reason)}</dd>
          <dt>结果</dt><dd>${esc(a.result)}</dd>
          <dt>${a.ok ? "为什么成功" : "为什么失败"}</dt><dd class="why">${esc(a.why)}</dd></dl></div>`).join(`<div class="arrow">↓</div>`);
  return back(n.project, `#/p/${encodeURIComponent(n.project)}`) + `<div class="card">
      <div class="row"><div class="ntitle grow">${KIND_ICON[n.kind] || "📌"} ${esc(n.title)}</div>${statusBadge(n.status)}</div>
      <div class="muted">${esc(n.project)} · ${esc(day(n.start_ts))} · ${n.source === "codex" ? "Codex" : "Claude"} · 试了 ${n.attempts.length} 次</div>
      <div class="sect">为什么要做</div><div>${esc(n.trigger)}</div></div>
    <div class="attempts">${steps || '<div class="card empty">没有记录尝试</div>'}</div>
    <div class="card final"><div class="sect">${n.kind === "bug" ? "根本原因" : "关键做法"}</div><div>${esc(n.root_cause)}</div>
      <div class="sect">经验</div><div class="lesson">💡 ${esc(n.lesson)}</div>
      ${n.tech.length ? `<div class="sect">用到的技术</div><div>${n.tech.map(t => `<span class="tag">${esc(t)}</span>`).join("")}</div>` : ""}</div>`;
}

function viewPits() {
  const q = S.q.trim().toLowerCase(), out = [];
  for (const n of model().nodes) n.attempts.forEach((a, i) => { if (!a.ok) out.push({ a, i, n }); });
  const list = out.reverse().filter(({ a, n }) => !q || [a.what, a.reason, a.result, a.why, n.title, n.project, ...n.tech].join(" ").toLowerCase().includes(q));
  return `<div class="card"><input type="text" id="q" placeholder="搜：报错、技术、项目…（比如 证书、Gradle、WebView）" value="${esc(S.q)}"></div>` +
    (list.length ? list.slice(0, 300).map(({ a, i, n }) => `<div class="card pit" data-go="#/n/${encodeURIComponent(n.id)}">
      <div class="what">❌ ${esc(a.what)}</div>
      <div class="muted" style="margin-top:4px">当时的想法：${esc(a.reason)}</div>
      <div class="why" style="margin-top:4px">失败原因：${esc(a.why)}</div>
      <div class="muted" style="margin-top:6px">${esc(n.project)} · ${esc(n.title)} · 第 ${i + 1} 次 · ${esc(day(n.start_ts))} ›</div></div>`).join("")
      : `<div class="card empty">${q ? "没搜到" : "还没有失败记录"}</div>`);
}

function viewSkills() {
  const nodes = model().nodes, all = techCount(nodes);
  if (!all.length) return `<div class="card empty">节点整理出来后，这里会汇总你用过的所有技术。</div>`;
  const max = all[0].n;
  let detail = "";
  if (S.tech) {
    const k = S.tech.toLowerCase(), ns = nodes.filter(n => n.tech.some(t => t.trim().toLowerCase() === k));
    detail = `<div class="card"><div class="row"><b class="grow">${esc(S.tech)} · ${ns.length} 个节点</b><button class="btn small ghost" data-tech="">收起</button></div>
      ${ns.map(n => `<div class="srow" data-go="#/n/${encodeURIComponent(n.id)}"><b>${esc(n.title)}</b> ${tries(n)}
        <div class="muted">${esc(n.project)} · ${esc(day(n.start_ts))}${n.lesson ? " · 💡 " + esc(n.lesson) : ""}</div></div>`).join("")}</div>`;
  }
  return detail + `<div class="card">${all.map(t => `<div class="skill" data-tech="${esc(t.name)}">
      <div><b>${esc(t.name)}</b><div class="muted">${esc([...t.projects].join("、"))}</div></div>
      <div class="bar" style="width:${Math.max(4, 100 * t.n / max)}%"></div><div class="n">${t.n}</div></div>`).join("")}</div>`;
}

function viewIdeas() {
  const ideas = S.data.ideas || [], projects = model().projects.map(p => p.name);
  const order = { doing: 0, new: 1, done: 2, dropped: 3 };
  const list = [...ideas].sort((a, b) => order[a.status] - order[b.status] || b.id - a.id);
  const saved = new Set(ideas.map(i => i.text));
  const loose = [];
  for (const c of S.data.chunks || []) if (Array.isArray(c.nodes)) for (const t of c.ideas || []) if (!saved.has(t)) loose.push({ t, c });
  loose.sort((a, b) => (b.c.start_ts || "").localeCompare(a.c.start_ts || ""));
  return `<div class="card"><textarea id="newIdea" placeholder="突然冒出的点子，先记下来…"></textarea>
      <div class="row" style="margin-top:8px"><select id="newProj" class="grow"><option value="">不属于哪个项目 / 新项目</option>
      ${projects.map(p => `<option>${esc(p)}</option>`).join("")}</select><button class="btn" data-act="add-idea">记下</button></div></div>
    <div class="card">${list.length ? list.map(i => `<div class="idea s-${i.status}">
        <div class="grow"><div class="txt">${esc(i.text)}</div>
          <div class="muted">${i.project ? esc(i.project) + " · " : ""}${esc(day(i.created * 1000))}${i.origin ? " · 来自对话" : ""}</div></div>
        <button class="chip ${i.status === "doing" ? "on" : ""}" data-cycle="${i.id}">${IDEA_STATUS[i.status]}</button>
        <button class="chip" data-del="${i.id}" title="删除">✕</button></div>`).join("") : `<div class="empty">点子本还是空的</div>`}</div>
    ${loose.length ? `<div class="card"><b>对话里冒出过的点子</b> <span class="muted">（AI 从聊天里捡出来的，点 ＋ 收进点子本）</span>
      ${loose.slice(0, 80).map(({ t, c }) => `<div class="row" style="margin-top:8px"><div class="grow">${esc(t)}<div class="muted">${esc(c.project)} · ${esc(day(c.start_ts))}</div></div>
        <button class="btn small ghost" data-keep="${esc(t)}" data-proj="${esc(c.project)}" data-from="${esc(c.id)}">＋</button></div>`).join("")}</div>` : ""}`;
}

function viewRepos() {
  const repos = (S.data.repos || {}).list || [], ign = new Set(S.data.ignored || []);
  if (!repos.length) return `<div class="card empty">电脑上的收集器还没扫描过仓库。</div>`;
  const jobs = new Map();
  for (const j of S.data.jobs || []) if (!jobs.has(j.repo)) jobs.set(j.repo, j);
  const sorted = [...repos].sort((a, b) => (ign.has(a.name) - ign.has(b.name)) || repoState(a).rank - repoState(b).rank || a.name.localeCompare(b.name));
  const scanned = (S.data.repos || {}).scanned;
  return `<div class="note">上次扫描 ${esc(scanned ? new Date(scanned).toLocaleString("zh-CN") : "—")}。推送由家里电脑执行，提交信息由 Claude 按改动和你在 Idea 里的开发记录自动写（含“为什么”）。</div>` +
    sorted.map(r => {
      const st = repoState(r), job = jobs.get(r.name), open = S.repoOpen.has(r.name), ignored = ign.has(r.name);
      const canPush = r.git && r.remote && r.mine !== false && (r.dirty || r.unpushed);
      const top = !/[\\/]/.test(r.name);                                  // only top-level project folders get a repo of their own
      const canCreate = top && r.mine !== false && !r.remote && !ign.has(r.name);
      const canVis = r.git && r.remote && r.mine !== false && r.visibility;
      const busy = job && ["queued", "running"].includes(job.status);
      const word = { push: "推送", create_private: "建私有库", create_public: "建公开库", make_private: "改成私有", make_public: "改成公开" }[job?.kind] || "任务";
      const jobLine = job ? `<div class="muted" style="margin-top:6px">${{ queued: "⏳ 等电脑接单…", running: `⏳ 正在${word}…`, done: `✅ ${word}完成`, failed: `❌ ${word}失败` }[job.status] || esc(job.status)}
          ${job.status === "done" && (job.result?.commit || job.result?.repo) ? " · " + esc(job.result.commit || job.result.repo) : ""} · ${esc(ago(job.updated))}
          ${job.status === "failed" ? `<div class="warn">${esc(job.result?.error)}${job.result?.files ? "：" + esc(job.result.files.join("、")) : ""}${job.result?.detail ? `<div class="files">${esc(job.result.detail)}</div>` : ""}</div>` : ""}</div>` : "";
      return `<div class="card repo" style="${ignored ? "opacity:.55" : ""}">
        <div class="row"><div class="grow"><b>${esc(r.name)}</b>${r.branch ? ` <span class="muted">${esc(r.branch)}</span>` : ""}${r.visibility ? ` <span class="muted">${r.visibility === "public" ? "🌐 公开" : "🔒 私有"}</span>` : ""}
          <div class="st ${st.cls}">${esc(st.text)}</div>
          <div class="muted">${r.last_commit ? "上次提交 " + esc(day(r.last_commit)) + "：" + esc(r.last_msg) : r.changed ? "最近改动 " + esc(day(r.changed * 1000)) : ""}</div></div>
          ${canPush ? `<button class="btn" data-push="${esc(r.name)}" ${busy ? "disabled" : ""}>一键推送</button>` : ""}</div>
        ${canCreate ? `<div class="row" style="margin-top:8px"><button class="btn small" data-job="create_private" data-repo="${esc(r.name)}" ${busy ? "disabled" : ""}>🔒 建私有库并上传</button>
          <button class="btn small ghost" data-job="create_public" data-repo="${esc(r.name)}" ${busy ? "disabled" : ""}>🌐 建公开库并上传</button></div>` : ""}
        ${r.secrets?.length ? `<div class="warn">⚠️ 改动里有像密码/密钥的文件，推送会被拦下：${esc(r.secrets.join("、"))}（加进 .gitignore）</div>` : ""}
        ${jobLine}
        <div class="row" style="margin-top:8px">
          ${r.files?.length ? `<button class="btn small ghost" data-files="${esc(r.name)}">${open ? "收起" : `看改了哪些文件 (${r.files.length})`}</button>` : ""}
          ${canVis ? `<button class="btn small ghost" data-job="${r.visibility === "public" ? "make_private" : "make_public"}" data-repo="${esc(r.name)}" ${busy ? "disabled" : ""}>${r.visibility === "public" ? "🔒 改成私有" : "🌐 改成公开"}</button>` : ""}
          <button class="btn small ghost" data-ignore="${esc(r.name)}" data-on="${ignored ? 0 : 1}">${ignored ? "恢复提醒" : "不提醒这个"}</button></div>
        ${open ? `<div class="files">${esc(r.files.join("\n"))}</div>` : ""}</div>`;
    }).join("");
}

// ---------------------------------------------------------------- dialogs
// In-page, not confirm()/alert(): the phone app's WebView drops those silently (confirm() just returns false).
function ask(title, text, ok = "确定", cancel = "取消") {
  return new Promise(done => {
    const el = document.createElement("div");
    el.className = "modal";
    el.innerHTML = `<div class="sheet"><div class="mt">${esc(title)}</div>${text ? `<div class="mx">${esc(text)}</div>` : ""}
      <div class="row mb">${cancel ? `<button class="btn ghost grow" data-a="0">${esc(cancel)}</button>` : ""}<button class="btn grow" data-a="1">${esc(ok)}</button></div></div>`;
    el.addEventListener("click", e => {
      const b = e.target.closest("[data-a]");
      if (!b && e.target !== el) return;
      e.stopPropagation(); el.remove(); done(b?.dataset.a === "1");
    });
    document.body.appendChild(el);
  });
}
const tell = (title, text) => ask(title, text, "知道了", "");

// ---------------------------------------------------------------- actions
const scrollKey = () => "idea.scroll." + location.hash;
window.addEventListener("hashchange", () => { render(); const y = +(sessionStorage.getItem(scrollKey()) || 0); window.scrollTo(0, y); });
window.addEventListener("scroll", () => { try { sessionStorage.setItem(scrollKey(), String(window.scrollY)); } catch { } }, { passive: true });

document.addEventListener("click", async e => {
  const t = e.target.closest("[data-go],[data-back],[data-keep],[data-cycle],[data-del],[data-act],[data-push],[data-job],[data-files],[data-ignore],[data-tech]");
  if (!t) return;
  const d = t.dataset;
  if (d.go !== undefined) { try { sessionStorage.removeItem("idea.scroll." + d.go); } catch { } go(d.go); return; }
  if (d.back !== undefined) { S.navigated ? history.back() : go(d.back); return; }
  if (d.tech !== undefined) { S.tech = d.tech; render(); window.scrollTo(0, 0); return; }
  if (d.files) { S.repoOpen.has(d.files) ? S.repoOpen.delete(d.files) : S.repoOpen.add(d.files); render(); return; }
  if (d.act === "reload") { load(); return; }
  if (d.act === "all") { S.showAll = d.name; render(); return; }
  if (d.act === "flat") { S.flat = d.name; render(); window.scrollTo(0, 0); return; }
  if (d.act === "update") { e.preventDefault(); APP?.checkUpdate(); return; }
  try {
    if (d.keep) {
      if (t.disabled) return;
      t.disabled = true; t.textContent = "✓";
      try { await api("/api/journey/ideas", { text: d.keep, project: d.proj, origin: d.from }); }
      catch (err) { t.disabled = false; t.textContent = "＋"; throw err; }
    }
    else if (d.cycle) {
      const i = S.data.ideas.find(x => x.id == d.cycle), ks = Object.keys(IDEA_STATUS);
      await api(`/api/journey/ideas/${d.cycle}`, { status: ks[(ks.indexOf(i.status) + 1) % ks.length] });
    } else if (d.del) { if (!await ask("删除这个点子？", "删除后找不回来。", "删除")) return; await api(`/api/journey/ideas/${d.del}/delete`, {}); }
    else if (d.act === "add-idea") {
      const box = $("#newIdea"), text = box.value.trim(); if (!text) return;
      const project = $("#newProj").value;
      // show it at once and empty the box: the save (and the reload after it) can take seconds on a slow link,
      // and a box that still holds the text invites more taps - each one saved another copy
      const tmp = { id: -Date.now(), text, project: project || null, status: "new", created: Date.now() / 1000 };
      S.data.ideas = [tmp, ...(S.data.ideas || [])]; render();
      try { await api("/api/journey/ideas", { text, project }); }
      catch (err) { S.data.ideas = S.data.ideas.filter(i => i !== tmp); render(); $("#newIdea").value = text; throw err; }
    } else if (d.ignore) await api("/api/journey/ignore", { repo: d.ignore, ignore: d.on === "1" });
    else if (d.job) {
      const Q = {
        create_private: [`给 ${d.repo} 建私有 GitHub 仓库？`, "在你的 GitHub 建一个私有仓库，把整个文件夹传上去（没有 git 的会先初始化；会补一份 .gitignore，像密码/密钥的文件和超过 50MB 的文件不会传，遇到会停下来告诉你）。只有你自己能看到。", "建库并上传"],
        create_public: [`给 ${d.repo} 建【公开】GitHub 仓库？`, "任何人都能看到这个仓库的全部代码。会先检查有没有像密钥的文件，有就停下不传。确定要公开吗？", "公开并上传"],
        make_public: [`把 ${d.repo} 改成【公开】？`, "改成公开后任何人都能看到全部代码和历史提交，之后再改回私有，期间被别人下载或缓存的内容收不回来。会先检查历史里有没有像密钥的文件，有就不改。", "改成公开"],
        make_private: [`把 ${d.repo} 改成私有？`, "改成私有后只有你能看到。公开期间别人已经下载或缓存的内容收不回来。", "改成私有"],
      }[d.job];
      if (!Q || !await ask(Q[0], Q[1] + "\n家里电脑收到后半分钟内开始。", Q[2])) return;
      await api("/api/journey/jobs", { kind: d.job, repo: d.repo });
    } else if (d.push) {
      const r = S.data.repos.list.find(x => x.name === d.push);
      const what = [r.dirty ? `提交 ${r.dirty} 个改动的文件` : "", r.unpushed ? `推送 ${r.unpushed} 个本地提交` : ""].filter(Boolean).join("，再");
      if (!await ask(`推送 ${r.name} 到 GitHub？`, `${what || "推送"}。\n会提交仓库里所有改动（.gitignore 里的除外）；像密码/密钥的文件会被拦下。家里电脑收到后半分钟内开始。`, "推送")) return;
      await api("/api/journey/jobs", { kind: "push", repo: d.push });
    }
    await load();
  } catch (err) { tell("没成功", err.message); }
});
// "back" inside the app: the hash history is ours, so history.back() returns to the list with its scroll position
window.addEventListener("hashchange", () => { S.navigated = true; });
document.addEventListener("input", e => {
  if (e.target.id === "q") {
    S.q = e.target.value; const pos = e.target.selectionStart; render();
    const q = $("#q"); q.focus(); q.setSelectionRange(pos, pos);
  }
});

// pull down at the top of the page to refresh now (the page also reloads itself every 20 s)
{
  const bar = document.createElement("div");
  bar.style.cssText = "position:fixed;left:0;right:0;top:0;z-index:50;display:flex;justify-content:center;pointer-events:none;transform:translateY(-60px);transition:transform .2s";
  bar.innerHTML = '<div style="margin-top:calc(env(safe-area-inset-top,0px) + 10px);background:var(--card);color:var(--text);box-shadow:0 2px 14px rgba(0,0,0,.18);border-radius:20px;padding:7px 16px;font-size:13px"></div>';
  document.body.appendChild(bar);
  const label = bar.firstChild, THRESHOLD = 90;
  let y0 = null, dist = 0, busy = false;
  const show = (text, y) => { label.textContent = text; bar.style.transition = "transform .2s"; bar.style.transform = `translateY(${y}px)`; };
  const hide = () => show("", -60);
  document.addEventListener("touchstart", e => { y0 = !busy && window.scrollY <= 0 && e.touches.length === 1 ? e.touches[0].clientY : null; dist = 0; }, { passive: true });
  document.addEventListener("touchmove", e => {
    if (y0 === null) return;
    dist = e.touches[0].clientY - y0;
    if (dist > 8 && window.scrollY <= 0) { bar.style.transition = "none"; show(dist >= THRESHOLD ? "松开刷新" : "下拉刷新", Math.min(dist / 3, 40)); }
  }, { passive: true });
  document.addEventListener("touchend", async () => {
    if (y0 === null) return;
    const go = dist >= THRESHOLD; y0 = null; dist = 0;
    if (!go) { hide(); return; }
    busy = true; show("刷新中…", 20);
    await load();
    show(S.offline ? "连不上服务器，显示的是旧数据" : "已刷新", 20);
    setTimeout(() => { hide(); busy = false; }, 900);
  }, { passive: true });
}
{ const c = stored("idea.cache"); if (c) { try { S.data = JSON.parse(c); render(); } catch { } } }   // show the last copy at once
load();
let lastTouch = 0;
document.addEventListener("touchstart", () => { lastTouch = Date.now(); }, { passive: true });
setInterval(() => { if (!document.hidden && Date.now() - lastTouch > 8000 && !document.activeElement?.matches("input,textarea,select")) load(false); }, 20000);
