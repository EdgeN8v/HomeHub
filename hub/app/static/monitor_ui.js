// Server page (#/server): the Orange Pi and the NAS as two big cards you can drag into your own order. Each card has
// the machine's CPU / memory / disk and a sortable table of its docker containers; a tap on a container opens what it
// is for and lets you start / stop / restart it, and each card can start or stop everything in one go.
// Data: GET /api/monitor (polled every 3 s while the page is open), POST /api/monitor/container and /api/monitor/all.

const MON = { data: [], sort: { k: "mem", dir: -1 }, order: [], open: {}, timer: 0, drag: null, busy: false, pending: null, reg: null, at: 0, jobs: [] };
try {
  MON.sort = JSON.parse(localStorage.getItem("mon-sort")) || MON.sort;
  MON.order = JSON.parse(localStorage.getItem("mon-order")) || [];
  MON.open = {};     // every card starts folded each time the page is opened (a tap unfolds it for as long as the page stays)
  const c = JSON.parse(localStorage.getItem("mon-cache") || "null");  // last data seen: the page draws at once, then refreshes
  if (c) { MON.data = c.data || []; MON.reg = c.reg || null; MON.at = c.at || 0; MON.pri = c.pri || null; MON.rec = c.rec || null; MON.join = c.join || []; MON.addr = c.addr || null; }
} catch (e) { /* storage unavailable: defaults */ }
const monSave = () => { try { localStorage.setItem("mon-cache", JSON.stringify({ data: MON.data, reg: MON.reg, at: MON.at, pri: MON.pri, rec: MON.rec, join: MON.join, addr: MON.addr })); } catch (e) { /* ignore */ } };

const mGB = 1024 ** 3, mMB = 1024 ** 2;
const mGb = b => (b / mGB).toFixed(b < 10 * mGB ? 2 : 1) + " GB";
const mMb = b => b >= mGB ? mGb(b) : Math.round(b / mMB) + " MB";
const mLvl = p => (p >= 90 ? "bad" : p >= 75 ? "warn" : "");

// null = no limit set (docker then reports the whole machine's memory as the limit)
const monLimit = (c, h) => (!c.mem_limit || c.mem_limit >= h.mem_total * 0.95 ? null : c.mem_limit);
const monLimitPct = (c, h) => (monLimit(c, h) ? 100 * c.mem / c.mem_limit : 0);

function monBar(label, pct, text) {
  return `<div class="mon-row"><div class="mon-l"><b>${label}</b><span>${text}</span></div>
    <div class="mon-bar ${mLvl(pct)}"><i style="width:${Math.min(100, pct)}%"></i></div></div>`;
}

function monSorted(h) {
  const { k, dir } = MON.sort;
  const key = { name: c => c.name, cpu: c => c.cpu, mem: c => c.mem, lim: c => (monLimit(c, h) ? c.mem_limit : -1) }[k];
  return [...(h.containers || [])].sort((a, b) => {
    const x = key(a), y = key(b);
    return (typeof x === "string" ? x.localeCompare(y) : x - y) * dir || a.name.localeCompare(b.name);
  });
}

// Two kinds of services are not like the others: the "pair" (hub, ops, HomeAssistant, registry: one of the two machines runs them, the other keeps them
// stopped, which is right) and the services of one single machine (the domain updaters, the file service, the sentinel).
const MON_PAIR = { "homehub-hub": "hub", "homehub-ops": "ops", "homeassistant": "ha", "registry": "reg", "registry-standby": "reg" };
const monSolo = n => /^ddnsgo_|^nas_rsyncd$|^homehub-sentinel$/.test(n);
function monKind(c) { return MON_PAIR[c.name] ? "pair" : monSolo(c.name) ? "solo" : ""; }
function monPairNote(c, h) {                              // a stopped member of the pair that runs somewhere else: normal, and say where
  if (c.running || !MON_PAIR[c.name]) return "";
  for (const o of MON.data) {
    if (o.containers && o.containers.some(x => x.running && MON_PAIR[x.name] === MON_PAIR[c.name])) return o.id === h.id ? "待命" : `→${monNm(o.id)}`;
  }
  return "";
}

function monCard(h) {
  let s = `<div class="mon-card" data-host="${h.id}"><div class="mon-h"><span class="mon-dot ${h.ok ? "ok" : ""}"></span>
    <div class="mon-t"><b>${esc(h.name)}</b><small>${esc(h.address)}${h.ok ? "" : " · 连不上"}</small></div>
    ${h.cores ? `<div class="mon-all"><button data-mon="all" data-host="${h.id}" data-act2="start_all">全部启动</button><button class="stop" data-mon="all" data-host="${h.id}" data-act2="stop_all">全部停止</button></div>` : ""}</div>`;
  if (h.error) s += `<div class="mon-err">${esc(h.error)}</div>`;
  if (h.cores) {
    const used = h.mem_total - h.mem_avail, cpu = h.cpu ?? 0;
    s += monBar(`CPU（${h.cores} 核）`, cpu, h.cpu == null ? "…" : `${cpu}% · 空闲约 ${(h.cores * (100 - cpu) / 100).toFixed(1)} 核`);
    s += monBar("内存", 100 * used / h.mem_total, `已用 ${mGb(used)} / ${mGb(h.mem_total)} · <b>可用 ${mGb(h.mem_avail)}</b>`);
    if (h.swap_total) s += monBar("交换分区", 100 * h.swap_used / h.swap_total, `${mMb(h.swap_used)} / ${mMb(h.swap_total)}`);
    for (const d of h.disks && h.disks.length ? h.disks : [{ label: "磁盘", total: h.disk_total, used: h.disk_used }])
      s += monBar(d.label, 100 * d.used / d.total, `已用 ${mGb(d.used)} / ${mGb(d.total)} · <b>剩余 ${mGb(d.total - d.used)}</b>`);
    if (h.docker_error) s += `<div class="mon-err">${esc(h.docker_error)}</div>`;
    const n = (h.containers || []).length, on = !!MON.open[h.id];
    s += `<button class="mon-fold" data-mon="fold" data-host="${h.id}">${n} 个容器<span class="${on ? "up" : ""}">›</span></button>`;
    if (!on) return s + "</div>";
    const kinds = new Set((h.containers || []).map(monKind));
    const legend = kinds.has("pair") || kinds.has("solo") ? `<div class="mon-lg">${kinds.has("pair") ? `<span><i class="kd pair"></i>成对</span>` : ""}${kinds.has("solo") ? `<span><i class="kd solo"></i>单机</span>` : ""}</div>` : "";
    const arrow = k => (MON.sort.k === k ? (MON.sort.dir < 0 ? " ▼" : " ▲") : "");
    const th = (k, t, cls = "") => `<button class="${cls} ${MON.sort.k === k ? "on" : ""}" data-mon="sort" data-k="${k}">${t}${arrow(k)}</button>`;
    s += `${legend}<div class="mon-tb"><div class="mon-th">${th("name", "容器")}${th("cpu", "CPU", "r")}${th("mem", "内存", "r")}${th("lim", "上限", "r")}</div>
      ${monSorted(h).map(c => {
        const lim = monLimit(c, h), p = monLimitPct(c, h);
        const note = monPairNote(c, h), k = monKind(c);
        return `<div class="mon-tr ${c.running ? "" : "stop"}" data-mon="info" data-host="${h.id}" data-name="${esc(c.name)}">
          <div class="n">${k ? `<i class="kd ${k}"></i>` : ""}<span>${esc(c.name)}</span>${monJobOf(c.name) ? "<em class=\"mv\">迁移中</em>" : c.running ? "" : note ? `<em class="ok">${note}</em>` : "<em>已停止</em>"}</div>
          <div class="r">${c.running ? c.cpu.toFixed(1) + "%" : "—"}</div><div class="r">${c.running ? mMb(c.mem) : "—"}</div>
          <div class="r ${mLvl(p)}">${lim ? mMb(lim) : "—"}</div></div>`;
      }).join("")}</div>`;
    const giving = MON.rec && MON.rec.recovered && MON.rec.recovered.length;      // while the NAS runs stand-ins, "give back" is the one action
    if (n && !giving) s += `<div class="mon-foot"><button class="mon-mv" data-mon="tr-ask" data-from="${h.id}">全部转到${monNm(h.id === "pi" ? "nas" : "pi")} ›</button></div>`;
  }
  return s + "</div>";
}

function monCards() {
  const rank = id => { const i = MON.order.indexOf(id); return i < 0 ? 99 : i; };
  const all = [{ id: "primary", html: monPrimary }, { id: "join", html: monJoin }, { id: "rec", html: monRecovery },
    ...MON.data.map(h => ({ id: h.id, html: () => monCard(h) }))];
  if (MON.reg) all.push({ id: "registry", html: monRegCard });
  try {
    // your own order is applied to the cards that have one; a card that was never moved keeps its place
    const known = all.filter(x => MON.order.includes(x.id)).sort((a, b) => rank(a.id) - rank(b.id));
    let k = 0;
    const ordered = all.map(x => (MON.order.includes(x.id) ? known[k++] : x));
    return monBanner() + ordered.map(x => x.html()).join("") || `<div class="loading">加载中…</div>`;
  } catch (e) {                                        // never leave the page blank: say what broke
    return `<div class="mon-card"><div class="mon-err">页面出错：${esc(String(e.message || e))}</div></div>`;
  }
}
const monStepNow = j => { const s = j.steps.find(x => x.s === "run" || x.s === "fail") || j.steps.find(x => x.s === "wait"); return s ? s.t : "完成"; };
function monBanner() {
  return (MON.jobs || []).map(j => `<div class="mon-card mon-job" data-mon="job" data-job="${j.id}">
    <div class="mon-h"><span class="mon-dot ${j.done ? (j.ok ? "ok" : "") : "run"}"></span><div class="mon-t"><b>${j.done ? (j.ok ? "已迁移" : "迁移失败") : "正在迁移"} ${esc(j.group || j.name)}</b>
    <small>${j.done ? (j.ok ? "已在香橙派运行" : esc(j.error || "")) : "当前：" + esc(monStepNow(j))}</small></div><span class="mon-go">›</span></div></div>`).join("");
}
const monJobOf = name => (MON.jobs || []).find(j => !j.done && j.members.includes(name));
const monDraw = () => { if (isServer() && !MON.drag && !MON.busy && !(MON.touch && Date.now() - MON.touch < 4000) && $("#mon-cards")) $("#mon-cards").innerHTML = monCards(); };

function renderServer() {
  if (!$("#mon-cards")) {
    MON.drag = null;                                  // never carry a stuck drag into a fresh page
    $("#app").innerHTML = `<div class="page">
      <div class="top"><div class="top-row"><div class="home-name"><span>NAS</span></div>
        <a href="/idea" style="margin-left:auto;color:#fff;text-decoration:none;font-size:14px;background:var(--chip-sky);padding:6px 12px;border-radius:999px">💡 Idea ›</a></div></div>
      <div id="mon-cards"></div></div>${bottomNav("nas")}`;
  }
  if (!MON.drag) $("#mon-cards").innerHTML = monCards();   // what was seen last time, until fresh data arrives
  if (!MON.timer) { MON.timer = setInterval(monTick, 3000); monTick(); }
  monRegLoad(false);
}

async function monTick() {
  if (!isServer()) { clearInterval(MON.timer); MON.timer = 0; return; }
  try { MON.data = await api("/api/monitor"); MON.at = Date.now(); monSave(); MON.err = ""; }
  catch (e) { MON.err = String(e.message || e).slice(0, 80); if (!MON.data.length && $("#mon-cards")) $("#mon-cards").innerHTML = `<div class="loading">连不上服务器：${esc(MON.err)}</div>`; return; }
  try { MON.jobs = await api("/api/monitor/migrate/active"); } catch (e) { /* keep the last list */ }
  try { MON.pri = await api("/api/primary"); monSave(); } catch (e) { /* keep the last answer */ }
  monRecLoad(false);
  monJoinLoad(false);
  monAddrLoad(false);
  monDraw();
  monRegLoad(false);
}

// ---- tap on a container: what it is, and start / stop / restart
function monFind(host, name) {
  const h = MON.data.find(x => x.id === host);
  return [h, h?.containers.find(c => c.name === name)];
}

function monShowJob(id, title) {
  sheet(`<h3>${esc(title)}</h3><div id="mon-prog"></div>`);
  monWatch("/api/monitor/migrate/job/" + id, "迁移完成。");
}

function monInfo(host, name, plain) {
  const [h, c] = monFind(host, name);
  if (!c) return;
  if (name === "directory") MON.addrHost = host;
  const mj = monJobOf(name);
  if (mj) return monShowJob(mj.id, `迁移 ${mj.group || name}`);
  const lim = monLimit(c, h), p = monLimitPct(c, h);
  const btn = (act, label, cls = "") => c.locked.includes(act)
    ? `<button disabled>${label}</button>` : `<button class="${cls}" data-mon="ctl" data-host="${host}" data-name="${esc(name)}" data-act2="${act}">${label}</button>`;
  const ctl = `<div class="sheet-btns mon-ctl">${c.running ? btn("restart", "重启") + btn("stop", "停止", "danger") : btn("start", "启动", "pri")}</div>
    ${!c.locked.length && !/-forward(-\d+)?$/.test(name) && !["nas_rsyncd", "registry-standby", "ddnsgo_nas", "jeessy_ddns-go", "registry", "homeassistant", "homehub-hub", "homehub-ops", "homehub-sentinel", "ddnsgo_pi"].includes(name) ? `<div class="sheet-btns"><button data-mon="mig-plan" data-host="${host}" data-name="${esc(name)}">${host === "nas" ? "迁移到香橙派…" : "迁移到 NAS…"}</button></div>` : ""}`;
  if (name === "directory")       // the list scrolls, the buttons stay in view at the bottom
    return sheet(`<h3>${esc(c.name)}</h3><div class="dir-list">${monAddrBlock()}</div><div class="dir-ctl">${ctl}</div>`, "dir");
  sheet(`<h3>${esc(c.name)}</h3>
    <div class="mon-desc">${esc(c.desc || "这个容器还没有写说明。")}</div>
    <div class="sheet-row"><span>所属服务</span><span>${esc(c.service)}</span></div>
    <div class="sheet-row"><span>所在机器</span><span>${esc(h.name)}</span></div>
    <div class="sheet-row"><span>状态</span><span>${c.running ? "运行中" : "已停止"}</span></div>
    ${c.running ? `<div class="sheet-row"><span>CPU</span><span>${c.cpu.toFixed(1)}%</span></div>
    <div class="sheet-row"><span>内存</span><span>${mMb(c.mem)}</span></div>
    <div class="sheet-row"><span>内存上限<small>超过上限容器会被系统强制结束</small></span><span>${lim ? `${mMb(lim)}（已用 ${Math.round(p)}%）` : "—（没设上限）"}</span></div>` : ""}
    ${c.locked.length ? `<p class="sheet-note">手机连家里靠它，不能在这里停止${c.locked.includes("restart") ? "或重启" : ""}。</p>` : ""}
    <div class="sheet-btns mon-ctl">${c.running ? btn("restart", "重启") + btn("stop", "停止", "danger") : btn("start", "启动", "pri")}</div>
    ${!c.locked.length && !/-forward(-\d+)?$/.test(name) && !["nas_rsyncd", "registry-standby", "ddnsgo_nas", "jeessy_ddns-go", "registry", "homeassistant", "homehub-hub", "homehub-ops", "homehub-sentinel", "ddnsgo_pi"].includes(name) ? `<div class="sheet-btns"><button data-mon="mig-plan" data-host="${host}" data-name="${esc(name)}">${host === "nas" ? "迁移到香橙派…" : "迁移到 NAS…"}</button></div>` : ""}`);
}

function monConfirm(title, text, host, name, action, all) {
  MON.pending = { host, name, action, all };
  sheet(`<h3>${esc(title)}</h3><p class="sheet-note" style="font-size:14px;margin:0 0 4px">${text}</p>
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri danger" data-mon="go">确定</button></div>`);
}

async function monRun() {
  const p = MON.pending; MON.pending = null;
  closeSheet();
  if (!p) return;
  MON.busy = true;
  toast(p.all ? "正在执行…" : { start: "正在启动…", stop: "正在停止…", restart: "正在重启…" }[p.action]);
  try {
    const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (p.all) {
      const r = await post("/api/monitor/all", { host: p.host, action: p.action });
      MON.data = r.hosts;
      toast(r.failed.length ? `完成 ${r.count} 个，失败 ${r.failed.length} 个：${r.failed[0]}` : `已处理 ${r.count} 个容器`);
    } else {
      MON.data = await post("/api/monitor/container", { host: p.host, name: p.name, action: p.action });
      toast("完成");
    }
  } catch (e) {
    toast("失败：" + String(e.message || e).slice(0, 80));
  }
  MON.busy = false;
  monSave();
  if (isServer()) renderServer();
}

function monAsk(host, name, action) {
  const [h, c] = monFind(host, name);
  const verb = { start: "启动", stop: "停止", restart: "重启" }[action];
  const warn = action === "stop" ? "停止后里面的服务会暂时用不了，记得再启动。" : action === "restart" ? "重启期间服务会中断几秒。" : "";
  monConfirm(`${verb} ${name}？`, `${esc(h.name)} 上的 ${esc(c.desc || name)}。${warn}`, host, name, action, false);
}

function monAskAll(host, action) {
  const h = MON.data.find(x => x.id === host);
  const one = action === "start_all" ? "start" : "stop";
  const list = h.containers.filter(c => c.running === (one === "stop") && !c.locked.includes(one));
  if (!list.length) return toast(one === "stop" ? "没有可以停止的容器" : "容器都在运行");
  const skip = h.containers.filter(c => c.running && c.locked.includes("stop")).map(c => c.name);
  monConfirm(`${one === "stop" ? "全部停止" : "全部启动"} · ${h.name}`,
    `将${one === "stop" ? "停止" : "启动"} ${list.length} 个容器：${esc(list.map(c => c.name).join("、"))}。${one === "stop" && skip.length ? `<br>${esc(skip.join("、"))} 不会停（手机连家里靠它）。` : ""}`,
    host, null, action, true);
}

// ---- move a container from the NAS to the Orange Pi (server: app/migrate.py)
async function monMigratePlan(host, name) {
  sheet(`<h3>迁移 ${esc(name)}</h3><p class="sheet-note">正在检查能不能搬…</p>`);
  let p;
  try { p = await api(`/api/monitor/migrate/plan?host=${host}&name=${encodeURIComponent(name)}`); }
  catch (e) { return sheet(`<h3>迁移 ${esc(name)}</h3><p class="sheet-note">检查失败：${esc(String(e.message || e).slice(0, 100))}</p><div class="sheet-btns"><button data-act="sheet-close">关闭</button></div>`); }
  if (!p.ok) return sheet(`<h3>${esc(name)} ${p.stay ? "留在 NAS 上" : "现在不能搬"}</h3><div class="mon-desc" style="text-align:left">${p.reasons.map(r => "· " + esc(r)).join("<br>")}</div><div class="sheet-btns"><button data-act="sheet-close">知道了</button></div>`);
  sheet(`<h3>把 ${esc(p.members.length > 1 ? p.group : name)} 搬到${p.dest || "香橙派"}</h3>
    <div class="mon-desc">${p.members.length > 1 ? `它们是一组，要一起搬：${esc(p.members.map(m => m.name).join("、"))}` : esc(p.desc || "")}</div>
    <div class="sheet-row"><span>要搬的数据</span><span>${p.data_mb >= 1024 ? (p.data_mb / 1024).toFixed(1) + " GB" : p.data_mb + " MB"}${p.big ? "（照片库已有备份，只补差异）" : ""}</span></div>
    <div class="sheet-row"><span>端口</span><span>${p.ports.length ? p.ports.join("、") : "无"}</span></div>
    ${(p.warnings || []).map(w => `<p class="sheet-note" style="font-size:13px;color:#c98414">· ${esc(w)}</p>`).join("")}
    <p class="sheet-note" style="font-size:13px">${p.dest === "NAS" ? "NAS 从镜像仓库拉镜像，数据用 NAS 上原来的文件夹，只补差异；先停掉香橙派上的，最后同步一次，再在 NAS 上按原配置启动；香橙派上留很小的转发（地址不变），然后清掉香橙派上的旧容器，SD 卡上的数据保留，之后每晚自动拉回。" : "香橙派从镜像仓库拉镜像，数据存在 SD 卡；先停掉 NAS 上的，最后同步一次数据，再在香橙派上按原配置启动；NAS 上留很小的转发（地址不变），然后清掉 NAS 上的旧容器，数据文件夹在 NAS 上保留，之后每晚自动同步回去。"}中途失败会自动恢复原样。期间服务会中断几分钟。</p>
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri" data-mon="mig-go" data-host="${host}" data-name="${esc(name)}">开始迁移</button></div>`);
}

async function monMigrateRun(host, name) {
  sheet(`<h3>迁移 ${esc(name)}</h3><div id="mon-prog"></div>`);
  let id;
  try {
    id = (await api("/api/monitor/migrate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ host, name }) })).job;
  } catch (e) { return sheet(`<h3>没有开始</h3><p class="sheet-note">${esc(String(e.message || e).slice(0, 160))}</p><div class="sheet-btns"><button data-act="sheet-close">关闭</button></div>`); }
  await monWatch("/api/monitor/migrate/job/" + id, "迁移完成。");
}

// Progress of a background job (migration, image sync): a step list that ticks as the server works. Closing the sheet
// does not stop the job.
// how far the running step is (0..1): its own percentage when it reports one, else an estimate from how long it has run
// against how long it usually takes, so the bar keeps creeping instead of waiting for the next report
function monStepFrac(j, s) {
  if (s.pct != null) return s.pct / 100;
  if (s.t0 == null || !s.est || j.now == null) return 0;
  const el = (j.now - s.t0) + (Date.now() - (j._recv || Date.now())) / 1000;
  return Math.min(0.9, 1 - Math.exp(-Math.max(el, 0) / s.est));
}

// where the whole job stands, 0..100 (not rounded): finished steps by weight, plus the running one
function monJobTarget(j) {
  const n = j.steps.length, doneN = j.steps.filter(s => s.s === "ok").length;
  const cur = j.steps.find(s => s.s === "run" || s.s === "fail");
  const weighted = j.steps.every(s => s.w);
  const tot = weighted ? j.steps.reduce((a, s) => a + s.w, 0) : n;
  const done = weighted ? j.steps.filter(s => s.s === "ok").reduce((a, s) => a + s.w, 0) : doneN;
  const curW = cur && cur.s === "run" ? (weighted ? cur.w : 1) : 0;
  return j.done && j.ok ? 100 : Math.min(99, 100 * (done + (cur && cur.s === "run" ? curW * monStepFrac(j, cur) : 0)) / tot);
}

function monJobView(j, okText) {
  const n = j.steps.length, doneN = j.steps.filter(s => s.s === "ok").length;
  const cur = j.steps.find(s => s.s === "run" || s.s === "fail");
  // what is shown follows MON.disp (moved a little every 100 ms by monWatch), so the bar glides instead of jumping
  const overall = Math.floor(MON.disp || 0);
  const state = j.done ? (j.ok ? "ok" : "bad") : "run";
  const head = j.done ? (j.ok ? okText : esc(j.error || "失败")) : `第 ${Math.min(doneN + 1, n)} / ${n} 步 · ${esc(cur ? cur.t : "准备中")}`;
  const icon = { wait: "", run: "●", ok: "✓", fail: "✗" };
  return `<div class="mg-top ${state}"><div class="mg-pct">${overall}%</div><div class="mg-head">${head}</div>
      <div class="mon-bar ${state === "bad" ? "bad" : ""}"><i style="width:${(MON.disp || 0).toFixed(2)}%"></i></div></div>
    <div class="mg-steps">${j.steps.map(s => `<div class="mg-s ${s.s}"><b>${icon[s.s]}</b><div><span>${esc(s.t)}</span>${s.s === "run" && s.note ? `<small>${esc(s.note)}</small>` : ""}
      ${s.s === "run" && s.pct != null ? `<div class="mon-bar"><i style="width:${s.pct}%"></i></div>` : ""}</div><em>${s.s === "run" && s.pct != null ? s.pct + "%" : ""}</em></div>`).join("")}</div>
    <div class="sheet-btns">${j.done ? `<button class="pri" data-act="sheet-close">${j.ok ? "完成" : "关闭"}</button>`
      : `<button data-act="sheet-close">放到后台</button>`}</div>
    ${j.done ? "" : `<p class="mg-hint">关掉窗口迁移也会继续，服务器页顶部的卡片可以随时查看。</p>`}`;
}

async function monWatch(path, okText) {
  MON.busy = true;
  let last = null, sig = "";
  MON.disp = 0;
  const draw = () => { const el = $("#mon-prog"); if (el && last) el.innerHTML = monJobView(last, okText) + (last._stale ? `<p class="mg-hint">连接中断了一下，正在重新连接…</p>` : ""); };
  // The page is only rebuilt when a step changes state or text. Between those, every 100 ms the displayed number moves a little
  // toward where the job really is (it never goes back), and the bars are updated in place: a smooth glide, never a jump.
  const ticker = setInterval(() => {
    if (!last || last.done) return;
    const target = monJobTarget(last);
    if (target > MON.disp) MON.disp = Math.min(target, MON.disp + Math.max(0.06, (target - MON.disp) * 0.06));
    const bar = $("#mon-prog .mg-top .mon-bar i"), pct = $("#mon-prog .mg-pct");
    if (bar) bar.style.width = MON.disp.toFixed(2) + "%";
    if (pct) pct.textContent = Math.floor(MON.disp) + "%";
    const run = last.steps.find(s => s.s === "run");
    const sb = $("#mon-prog .mg-s.run .mon-bar i"), se = $("#mon-prog .mg-s.run em");
    if (run && run.pct != null) { if (sb) sb.style.width = run.pct + "%"; if (se) se.textContent = run.pct + "%"; }
  }, 100);
  for (;;) {
    let j;
    if (!$("#mon-prog")) break;
    // the progress comes straight from the sentinels (the hub itself is stopped for a while when the main machine is switched);
    // if even that fails for a moment, keep showing the last progress
    try { j = await api(path); } catch (e) { if (last) { last._stale = true; draw(); } await new Promise(r => setTimeout(r, 1500)); continue; }
    j.steps = j.steps.filter(s => !/留转发/.test(s.t));    // forwarders are not left behind any more (the address table follows the service): do not show a step that does nothing
    j._recv = Date.now();
    last = j;
    const now = JSON.stringify([j.done, j.ok, j.error, j.steps.map(s => [s.s, s.note])]) + (j._stale ? 1 : 0);
    if (now !== sig || j.done) { sig = now; draw(); }
    if (j.done) break;
    await new Promise(r => setTimeout(r, 1000));
  }
  clearInterval(ticker);
  if (last && last.done && last.ok) {      // finished: glide the last stretch to 100% instead of jumping there
    for (let k = 0; k < 20 && MON.disp < 99.5; k++) {
      MON.disp += (100 - MON.disp) * 0.3 + 0.2;
      const bar = $("#mon-prog .mg-top .mon-bar i"), pct = $("#mon-prog .mg-pct");
      if (!bar) break;
      bar.style.width = Math.min(100, MON.disp).toFixed(2) + "%"; if (pct) pct.textContent = Math.floor(Math.min(100, MON.disp)) + "%";
      await new Promise(r => setTimeout(r, 70));
    }
    MON.disp = 100; draw();
  }
  MON.busy = false;
  monTick(); monRegLoad(true);
}

// ---- the image registry card
async function monRegLoad(force) {
  if (!force && Date.now() - (MON.regAt || 0) < 30000) return;
  MON.regAt = Date.now();
  try { MON.reg = await api("/api/registry"); monSave(); } catch (e) { if (!MON.reg) MON.reg = { ok: false, images: [] }; }
  monDraw();
}

const monAgo = ts => { const m = Math.round((Date.now() / 1000 - ts) / 60); return m < 1 ? "刚刚" : m < 60 ? `${m} 分钟前` : m < 2880 ? `${Math.round(m / 60)} 小时前` : `${Math.round(m / 1440)} 天前`; };
function monSpecs(sp) {
  if (!sp) return "";
  const n = Object.values(sp.counts || {}).reduce((a, b) => a + b, 0);
  return `<div class="mon-row"><div class="mon-l"><b>服务清单</b><span>${sp.at ? `${n} 个容器的配置 · ${monAgo(sp.at)}已保存` : sp.error ? "保存失败" : "还没保存"}</span></div></div>`;
}

function monReplica(r) {
  if (!r) return "";
  return `<div class="mon-row"><div class="mon-l"><b>迁走服务的数据回 NAS</b><span>${r.busy ? "同步中…" : r.at ? (r.ok ? `${monAgo(r.at)}已同步` : "上次失败：" + esc(r.error || "")) : "还没同步"}</span></div></div>`;
}

function monMirror(m) {
  const nas = (MON.data.find(h => h.id === "nas")?.containers || []).find(c => c.name === "registry-standby");
  const line = m.busy ? "同步中…" : m.dirty ? "有新镜像，点右上角“同步”" : m.at && m.ok !== false ? `${monAgo(m.at)}已同步${nas && nas.running ? "" : "（备用仓库未运行）"}` : m.at ? "上次同步失败" : "还没有同步";
  return `<div class="mon-row"><div class="mon-l"><b>NAS 备份</b><span>${line}</span></div><div class="mon-path">${esc(m.path || "")}</div></div>`;
}

// one row per service image: the version that is running (or the newest), and a small capsule that switches to the other kept version
function monRegGroups(r) {
  const run = new Set(r.running || []), by = {};
  for (const i of r.images) (by[i.repo] = by[i.repo] || []).push(i);
  return Object.entries(by).map(([repo, list]) => {
    list.sort((a, b) => (b.pushed || 0) - (a.pushed || 0));
    const live = list.find(x => run.has(`${repo}:${x.tag}`)), shown = live || list[0];
    let other = null;
    if (live && list.length > 1) other = shown === list[0] ? { tag: list[1].tag, up: false } : { tag: list[0].tag, up: true };
    return { repo, shown, other, size: shown.size || 0 };
  }).sort((a, b) => b.size - a.size);
}

function monRegCard() {
  const r = MON.reg;
  if (!r) return "";
  const on = !!MON.open.registry, groups = monRegGroups(r);
  return `<div class="mon-card" data-host="registry"><div class="mon-h"><span class="mon-dot ${r.ok ? "ok" : ""}"></span>
    <div class="mon-t"><b>镜像仓库</b><small>${esc(r.address || "")}${r.ok ? ` · ${groups.length} 个镜像 · ${mGb(r.total || 0)}` : " · 连不上"}</small></div>
    <div class="mon-all"><button data-mon="reg-sync">同步</button></div></div>
    ${r.ok ? monMirror(r.mirror || {}) + monSpecs(r.specs) + monReplica(r.replica) : ""}
    <button class="mon-fold" data-mon="fold" data-host="registry">${groups.length} 个镜像<span class="${on ? "up" : ""}">›</span></button>
    ${on ? `<div class="mon-tb">${groups.map(g => `<div class="mon-tr" style="grid-template-columns:minmax(0,1fr) auto 62px"><div class="n"><span>${esc(g.repo)} <small style="color:var(--sub)">${esc(g.shown.tag)}</small></span></div>
      <div>${g.other ? `<button class="mon-pill" data-mon="reg-rb" data-repo="${esc(g.repo)}" data-tag="${esc(g.other.tag)}" data-cur="${esc(g.shown.tag)}">${g.other.up ? "↪" : "↩"} ${esc(g.other.tag)}</button>` : ""}</div>
      <div class="r">${g.size ? mMb(g.size) : "—"}</div></div>`).join("") || `<div class="mon-tr"><div class="n"><span>还没有镜像，点右上角“同步”</span></div></div>`}</div>` : ""}</div>`;
}

function monRegRbAsk(repo, tag, cur) {
  sheet(`<h3>${esc(repo)} 切到 ${esc(tag)}？</h3><p class="sheet-note" style="font-size:14px;margin:0 0 4px">现在运行 ${esc(cur)}，换完容器会重启一下；换不成会自动恢复。</p>
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri" data-mon="reg-rb-go" data-repo="${esc(repo)}" data-tag="${esc(tag)}">切换</button></div>`);
}

async function monRegRbGo(repo, tag) {
  sheet(`<h3>切换 ${esc(repo)}</h3><div id="mon-prog"><p class="sheet-note">正在开始…</p></div>`);
  let r;
  try { r = await api("/api/registry/rollback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo, tag }) }); }
  catch (e) { return sheet(`<h3>没有开始</h3><p class="sheet-note">${esc(String(e.message || e).slice(0, 160))}</p><div class="sheet-btns"><button data-act="sheet-close">关闭</button></div>`); }
  await monWatch("/api/registry/job/" + r.job, `已切到 ${tag}。`);
  monRegLoad(true);
}

// ---- a new machine appears on the network: take it in (it replaces the Pi or the NAS)
async function monJoinLoad(force) {
  if (!force && Date.now() - (MON.joinAt || 0) < 15000) return;
  MON.joinAt = Date.now();
  try { MON.join = (await api("/api/join/candidates")).machines || []; monSave(); } catch (e) { /* keep the last answer */ }
  monDraw();
}

function monJoin() {
  const list = MON.join || [];
  if (!list.length) return "";
  const rows = list.map(c => `<div class="mon-row"><div class="mon-l"><b>${esc(c.host)}</b>
      <span>${esc(c.addr)} · ${Math.round(c.mem_mb / 102.4) / 10} GB 内存 · ${esc(c.arch)}</span></div>
      <div class="mon-all"><button data-mon="join-ask" data-id="${esc(c.id)}">纳管…</button></div></div>`).join("");
  return `<div class="mon-card" data-host="join"><div class="mon-h"><span class="mon-dot ok"></span>
    <div class="mon-t"><b>发现新机器</b><small>网络里出现了还没有纳管的机器</small></div></div>${rows}</div>`;
}

function monJoinAsk(id) {
  const c = (MON.join || []).find(x => x.id === id);
  if (!c) return;
  const pri = MON.pri || {}, deadPeer = pri.peer && pri.peer.ok === false;
  const peerNm = pri.me ? monNm(pri.me === "pi" ? "nas" : "pi") : "";
  sheet(`<h3>纳管 ${esc(c.host)}？</h3>
    <p class="sheet-note" style="font-size:14px;margin:0 0 4px">${esc(c.addr)}。纳管后它会装上哨兵，之后自动同步数据，随时可以接管；原来同角色的那台机器可以退役。${deadPeer ? `<br>现在${esc(peerNm)}没有回应，它可能就是来接替${esc(peerNm)}的。` : ""}</p>
    <div class="sheet-btns"><button data-act="sheet-close">取消</button></div>
    <div class="sheet-btns"><button class="pri" data-mon="join-go" data-id="${esc(id)}" data-role="pi">它是新的香橙派</button>
    <button class="pri" data-mon="join-go" data-id="${esc(id)}" data-role="nas">它是新的 NAS</button></div>`);
}

async function monJoinGo(id, role) {
  sheet(`<h3>纳管新机器</h3><div id="mon-prog"><p class="sheet-note">正在开始…</p></div>`);
  let r;
  try { r = await api("/api/join/claim", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id, role }) }); }
  catch (e) { return sheet(`<h3>没有开始</h3><p class="sheet-note">${esc(String(e.message || e).slice(0, 160))}</p><div class="sheet-btns"><button data-act="sheet-close">关闭</button></div>`); }
  await monWatch("/api/join/job/" + r.job, "新机器已纳管。");
  monJoinLoad(true);
}

// ---- the address table: which services are in it, where they run, and the ones that are not in it yet (one tap)
async function monAddrLoad(force) {
  if (!force && Date.now() - (MON.addrAt || 0) < 20000) return;
  MON.addrAt = Date.now();
  try { MON.addr = await api("/api/endpoints/audit"); monSave(); } catch (e) { /* keep the last answer */ }
  monDraw();
}

// the address table, inside the "directory" container's own sheet (same look as every other container)
function monAddrBlock() {
  const a = MON.addr;
  if (!a) return `<p class="sheet-note">地址表还没读到，稍等一下再点开。</p>`;
  const rows = a.rows.slice().sort((x, y) => x.name.localeCompare(y.name)), miss = a.missing;
  const addrOf = n => { const e = a.table[n]; return e && e.lan && e.lan[0] ? e.lan[0].replace(/^https?:\/\//, "") : "—"; };
  return `${miss.map(m => `<div class="sheet-row"><span>${esc(m.container)}<small>${esc(monNm(m.on))} · ${m.ports.join(" ")}</small></span><button data-mon="addr-reg" data-c="${esc(m.container)}" data-n="${esc(m.guess)}" data-p="${m.ports[0] || ""}">登记为 ${esc(m.guess)}</button></div>`).join("")}
    ${rows.map(r => `<div class="sheet-row" data-mon="addr-info" data-n="${esc(r.name)}"><span>${esc(r.name)}</span><span>${esc(addrOf(r.name))} ›</span></div>`).join("")}
    <div class="sheet-btns"><button data-mon="addr-refresh">刷新地址表</button>${miss.length > 1 ? `<button data-mon="addr-all">全部登记</button>` : ""}</div>`;
}
const monAddrSheet = () => monInfo(MON.addrHost || "pi", "directory");

async function monAddrEdit(what, body) {
  try { MON.addr = await api("/api/endpoints/" + what, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) }); monSave(); }
  catch (e) { return sheet(`<h3>没有成功</h3><p class="sheet-note">${esc(String(e.message || e).slice(0, 160))}</p><div class="sheet-btns"><button data-act="sheet-close">关闭</button></div>`); }
  monAddrSheet();
}

async function monAddrInfo(name) {
  const a = MON.addr, e = a && a.table[name];
  if (!e) return;
  const where = a.rows.filter(r => r.name === name).map(r => `${monNm(r.on)}${r.running ? "" : "（已停）"}`).join("、") || "—";
  const urls = l => (l || []).map(u => `<div class="mon-row"><div class="mon-l"><span style="text-align:left;word-break:break-all">${esc(u)}</span></div></div>`).join("");
  sheet(`<h3>${esc(name)}</h3>
    <p class="sheet-note" style="margin:0 0 4px">运行在：${esc(where)} · <span id="addr-probe">检测中…</span></p>
    <p class="sheet-note" style="margin:8px 0 0">局域网</p>${urls(e.lan)}
    <p class="sheet-note" style="margin:8px 0 0">外网</p>${urls(e.wan)}
    <div class="sheet-btns"><button data-mon="addr-rename" data-n="${esc(name)}">改名</button><button class="stop" data-mon="addr-forget" data-n="${esc(name)}">取消登记</button></div>
    <div class="sheet-btns"><button data-mon="addr-back">返回</button></div>`);
  try {
    const r = await api("/api/endpoints/probe?name=" + encodeURIComponent(name));
    const el = $("#addr-probe");
    if (el) el.textContent = r.ok ? "能连上" : "连不上" + (r.why ? "（" + r.why + "）" : "");
  } catch (x) { const el = $("#addr-probe"); if (el) el.textContent = "没检测成功"; }
}

function monAddrRename(name) {
  sheet(`<h3>改名</h3><input id="addr-new" value="${esc(name)}" autocapitalize="none" style="width:100%;box-sizing:border-box;padding:10px;font-size:16px;border:1px solid var(--line);border-radius:10px;background:transparent;color:inherit">
    <p class="sheet-note" style="margin:6px 0 0">小写字母、数字、- 或 _。用到旧名字的 App 要同步改。</p>
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri" data-mon="addr-rename-go" data-n="${esc(name)}">确定</button></div>`);
}

function monAddrForget(name) {
  sheet(`<h3>取消登记 ${esc(name)}？</h3><p class="sheet-note">只是从地址表里去掉，服务本身不受影响。App 之后读不到它的地址。</p>
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="stop" data-mon="addr-forget-go" data-n="${esc(name)}">取消登记</button></div>`);
}

async function monAddrReg(items) {
  const ok = items.filter(i => i.port);
  if (!ok.length) return;
  try { MON.addr = await api("/api/endpoints/register", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items: ok }) }); }
  catch (e) { return sheet(`<h3>没有登记</h3><p class="sheet-note">${esc(String(e.message || e).slice(0, 160))}</p><div class="sheet-btns"><button data-act="sheet-close">关闭</button></div>`); }
  monDraw();
}

// ---- disaster recovery: what the NAS takes over when the Pi is gone, and giving it back
const MON_REC_NAMES = { ledger_default: "记账", gongzhang_default: "工账", navidrome: "Navidrome 音乐", "learnsync-server": "LearnSync",
  "plan-server": "计划", immich_net: "Immich 相册（不含人脸识别）" };
async function monRecLoad(force) {
  if (!force && Date.now() - (MON.recAt || 0) < 30000) return;
  MON.recAt = Date.now();
  try { MON.rec = await api("/api/recovery"); monSave(); } catch (e) { /* keep the last answer */ }
  monDraw();
}

function monRecovery() {
  const r = MON.rec;
  if (!r || !r.recovered || !r.recovered.length) return "";        // nothing to say while nothing is running in place of the Pi
  return `<div class="mon-card" data-host="rec"><div class="mon-h">
    <div class="mon-t"><b>NAS 正在代跑 ${r.recovered.length} 个容器</b><small>香橙派恢复后，一键全部交还</small></div>
    <div class="mon-all"><button data-mon="rec-ask">全部交还给香橙派</button></div></div></div>`;
}

// ---- one tap: move all the services of one machine to the other (some can be unticked)
const MON_TR_NAMES = [["immich_ml", "Immich 人脸识别"], ["immich", "Immich 相册"], ["ledger", "记账"], ["gongzhang", "工账"], ["navidrome", "Navidrome 音乐"],
  ["learnsync", "LearnSync"], ["plan-server", "计划"], ["music-update", "Music 更新"]];
const monTrName = g => { const m = MON_TR_NAMES.find(([k]) => g.members.some(x => x.startsWith(k))); return m ? m[1] : g.members.join("、"); };
const monTrSize = mb => (mb >= 1024 ? (mb / 1024).toFixed(1) + " GB" : mb ? mb + " MB" : "");

async function monTrAsk(from) {
  sheet(`<h3>正在看有哪些服务…</h3>`);
  let r;
  try { r = await api("/api/transfer/plan?from=" + from); }
  catch (e) { return sheet(`<h3>看不到服务列表</h3><p class="sheet-note">${esc(String(e.message || e).slice(0, 160))}</p><div class="sheet-btns"><button data-act="sheet-close">关闭</button></div>`); }
  MON.tr = { from, groups: r.groups || [] };
  const to = from === "pi" ? "nas" : "pi";
  const rows = MON.tr.groups.map((g, i) => g.ok ? `<label class="tr-row"><input type="checkbox" data-tr="${i}" ${g.skip ? "" : "checked"}>
      <span class="tr-n">${esc(monTrName(g))}</span><em>${monTrSize(g.data_mb)}</em></label>` : "").join("");
  const no = MON.tr.groups.filter(g => !g.ok).map(g => `${esc(monTrName(g))}：${esc((g.reasons || [])[0] || "现在不能搬")}`).join("<br>");
  sheet(`<h3>全部转到${monNm(to)}</h3>
    <div class="tr-list">${rows || `<p class="sheet-note">没有可以搬的服务</p>`}</div>
    ${no ? `<div class="tr-no">不能搬<br>${no}</div>` : ""}
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri" data-mon="tr-go"${rows ? "" : " disabled"}>开始转移</button></div>`);
}

async function monTrGo() {
  const t = MON.tr;
  if (!t) return;
  const skip = [];
  document.querySelectorAll("input[data-tr]").forEach(el => { if (!el.checked) skip.push(...t.groups[+el.dataset.tr].members); });
  sheet(`<h3>转移服务</h3><div id="mon-prog"><p class="sheet-note">正在开始…</p></div>`);
  let r;
  try { r = await api("/api/transfer/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ from: t.from, skip }) }); }
  catch (e) { return sheet(`<h3>没有开始</h3><p class="sheet-note">${esc(String(e.message || e).slice(0, 160))}</p><div class="sheet-btns"><button data-act="sheet-close">关闭</button></div>`); }
  await monWatch("/api/transfer/job/" + r.job, "所有勾选的服务都搬完了。");
}

function monRecAsk() {
  const r = MON.rec || {};
  const mins = r.recovered_at ? Math.round((Date.now() / 1000 - r.recovered_at) / 60) : 0;
  sheet(`<h3>把服务交还给香橙派？</h3>
    <p class="sheet-note" style="font-size:14px;margin:0 0 4px">会先停下 NAS 上代跑的服务，把它们这 ${mins} 分钟里产生的新数据拷回香橙派（香橙派原来的文件夹会留一份备份，名字带 before-failback），再让香橙派上的服务重新启动，地址不变。数据多的话要几分钟。</p>
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri" data-mon="rec-go-data">带上新数据交还</button></div>
    <div class="sheet-btns"><button class="danger" data-mon="rec-go">不带数据，直接交还（丢掉 NAS 上的新数据）</button></div>`);
}

async function monRecGoData() {
  sheet(`<h3>交还给香橙派</h3><div id="mon-prog"><p class="sheet-note">正在开始…</p></div>`);
  let r;
  try { r = await api("/api/recovery/failback-data", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); }
  catch (e) { return sheet(`<h3>没有开始</h3><p class="sheet-note">${esc(String(e.message || e).slice(0, 160))}</p><div class="sheet-btns"><button data-act="sheet-close">关闭</button></div>`); }
  await monWatch("/api/recovery/job/" + r.job, "已交还给香橙派，新数据也带回来了。");
  monRecLoad(true);
}

async function monRecGo() {
  closeSheet();
  toast("正在交还…");
  try {
    const d = await api("/api/recovery/failback", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(`已交还 ${(d.removed || []).length} 个容器`);
  } catch (e) { toast("失败：" + String(e.message || e).slice(0, 60)); }
  monRecLoad(true);
}

// ---- which machine runs the hub + HomeAssistant ("main"), and the switch
const monNm = k => (k === "pi" ? "香橙派" : "NAS");
function monPrimary() {
  const p = MON.pri;
  if (!p || !p.me) return "";
  const other = p.primary === "pi" ? "nas" : "pi", peer = p.peer || {}, sy = p.sync || {};
  const line = p.primary === p.me
    ? `${peer.ok ? "在线" : "没有回应"}${sy.pushed_at ? ` · ${monAgo(sy.pushed_at)}已复制${sy.pushed_ok === false ? "（失败）" : ""}` : ""}`
    : `本机 · 主站${peer.ok ? "在线" : "没有回应"}`;
  return `<div class="mon-card" data-host="primary"><div class="mon-h"><span class="mon-dot ${p.hub && p.hub.ok !== false ? "ok" : ""}"></span>
    <div class="mon-t"><b>主站 · ${monNm(p.primary)}</b></div>
    <div class="mon-all"><button data-mon="pri-ask" data-to="${other}">${p.job ? "查看" : "切换到 " + monNm(other)}</button></div></div>
    <div class="mon-row"><div class="mon-l"><b>备用 ${monNm(other)}</b><span>${esc(line)}</span></div></div>
    <div class="mon-row mon-sw"><div class="mon-l"><b>自动接管</b></div><div class="sw ${p.auto ? "on" : ""}" data-mon="pri-auto" data-on="${p.auto ? 0 : 1}"></div></div></div>`;
}

function monPrimaryAsk(to) {
  if (MON.pri && MON.pri.job) { sheet(`<h3>切换主站</h3><div id="mon-prog"></div>`); return monWatch("/api/primary/job/current", "主站已切换。"); }
  sheet(`<h3>把主站切到${monNm(to)}？</h3>
    <p class="sheet-note" style="font-size:14px;margin:0 0 4px">先停掉${monNm(to === "pi" ? "nas" : "pi")}上的 HomeHub 和 HomeAssistant，把最新数据交给${monNm(to)}再启动，约 1–3 分钟，期间手机连不上 HomeHub。</p>
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri danger" data-mon="pri-go" data-to="${to}">开始切换</button></div>`);
}

async function monPrimaryGo(to) {
  sheet(`<h3>切换主站</h3><div id="mon-prog"><p class="sheet-note">正在开始…</p></div>`);
  try { await api("/api/primary/switch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target: to }) }); }
  catch (e) { return sheet(`<h3>没有开始</h3><p class="sheet-note">${esc(String(e.message || e).slice(0, 160))}</p><div class="sheet-btns"><button data-act="sheet-close">关闭</button></div>`); }
  await monWatch("/api/primary/job/current", "主站已切换。");
}

async function monPrimaryAuto(on) {
  try { await api("/api/primary/auto", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on }) }); MON.pri = await api("/api/primary"); }
  catch (e) { toast("失败：" + String(e.message || e).slice(0, 60)); }
  monDraw();
}

async function monRegMirror() {
  sheet(`<h3>同步到 NAS</h3><div id="mon-prog"><p class="sheet-note">正在开始…</p></div>`);
  let r;
  try { r = await api("/api/registry/mirror", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); }
  catch (e) { return sheet(`<h3>没有开始</h3><p class="sheet-note">${esc(String(e.message || e).slice(0, 160))}</p><div class="sheet-btns"><button data-act="sheet-close">关闭</button></div>`); }
  await monWatch("/api/registry/job/" + r.job, "NAS 上的副本已是最新。");
}

async function monRegSync() {
  sheet(`<h3>同步</h3><div id="mon-prog"><p class="sheet-note">正在整理要推送的镜像…</p></div>`);
  let r;
  try { r = await api("/api/registry/sync", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); }
  catch (e) { return sheet(`<h3>没有开始</h3><p class="sheet-note">${esc(String(e.message || e).slice(0, 160))}</p><div class="sheet-btns"><button data-act="sheet-close">关闭</button></div>`); }
  await monWatch("/api/registry/job/" + r.job, "镜像已存入仓库，NAS 副本会接着自动更新。");
}

document.addEventListener("click", e => {
  const t = e.target.closest("[data-mon]");
  if (MON.drag && !MON.drag.isConnected) MON.drag = null;   // a lift whose card was replaced never gets its touchend: do not stay stuck
  if (!t || MON.drag) return;
  const k = t.dataset.mon;
  if (k === "sort") {
    MON.sort = { k: t.dataset.k, dir: MON.sort.k === t.dataset.k ? -MON.sort.dir : (t.dataset.k === "name" ? 1 : -1) };
    try { localStorage.setItem("mon-sort", JSON.stringify(MON.sort)); } catch (x) { /* ignore */ }
    $("#mon-cards").innerHTML = monCards();
  } else if (k === "fold") {
    MON.open[t.dataset.host] = !MON.open[t.dataset.host];
    $("#mon-cards").innerHTML = monCards();
  } else if (k === "info") monInfo(t.dataset.host, t.dataset.name, t.dataset.plain === "1");
  else if (k === "ctl") monAsk(t.dataset.host, t.dataset.name, t.dataset.act2);
  else if (k === "all") monAskAll(t.dataset.host, t.dataset.act2);
  else if (k === "go") monRun();
  else if (k === "tr-ask") monTrAsk(t.dataset.from);
  else if (k === "tr-go") monTrGo();
  else if (k === "join-ask") monJoinAsk(t.dataset.id);
  else if (k === "join-go") monJoinGo(t.dataset.id, t.dataset.role);
  else if (k === "addr-back") monAddrSheet();
  else if (k === "addr-info") monAddrInfo(t.dataset.n);
  else if (k === "addr-refresh") monAddrEdit("refresh");
  else if (k === "addr-rename") monAddrRename(t.dataset.n);
  else if (k === "addr-rename-go") monAddrEdit("rename", { name: t.dataset.n, to: ($("#addr-new").value || "").trim() });
  else if (k === "addr-forget") monAddrForget(t.dataset.n);
  else if (k === "addr-forget-go") monAddrEdit("forget", { name: t.dataset.n });
  else if (k === "addr-reg") monAddrReg([{ container: t.dataset.c, name: t.dataset.n, port: +t.dataset.p }]);
  else if (k === "addr-all") monAddrReg((MON.addr ? MON.addr.missing : []).map(m => ({ container: m.container, name: m.guess, port: m.ports[0] || 0 })));
  else if (k === "rec-ask") monRecAsk();
  else if (k === "rec-go") monRecGo();
  else if (k === "rec-go-data") monRecGoData();
  else if (k === "pri-ask") monPrimaryAsk(t.dataset.to);
  else if (k === "pri-go") monPrimaryGo(t.dataset.to);
  else if (k === "pri-auto") monPrimaryAuto(t.dataset.on === "1");
  else if (k === "reg-sync") monRegSync();
  else if (k === "reg-rb") monRegRbAsk(t.dataset.repo, t.dataset.tag, t.dataset.cur);
  else if (k === "reg-rb-go") monRegRbGo(t.dataset.repo, t.dataset.tag);
  else if (k === "reg-mirror") monRegMirror();
  else if (k === "mig-plan") monMigratePlan(t.dataset.host, t.dataset.name);
  else if (k === "mig-go") monMigrateRun(t.dataset.host, t.dataset.name);
});

// ---- drag the big cards into your own order: press and hold a card, then move it. The other cards slide out of the way
// while you drag; the new order is stored when you let go.
(() => {
  let box, cards, tops, hs, from, to, y0, timer, lifted, sx, sy;
  const GAP = 12;
  const cancel = () => { clearTimeout(timer); timer = 0; };
  const shift = () => {
    const dh = hs[from] + GAP;
    cards.forEach((c, k) => {
      if (k === from) return;
      const dy = from < to && k > from && k <= to ? -dh : from > to && k >= to && k < from ? dh : 0;
      c.style.transform = dy ? `translateY(${dy}px)` : "";
    });
  };
  document.addEventListener("touchstart", e => {
    const card = e.target.closest(".mon-card[data-host]");
    if (!card || e.touches.length !== 1 || e.target.closest("button") || !isServer()) return;
    box = $("#mon-cards"); cards = [...box.querySelectorAll(".mon-card[data-host]")];
    if (cards.length < 2) return;
    sx = e.touches[0].clientX; sy = y0 = e.touches[0].clientY; lifted = false; MON.touch = Date.now();   // the list is not redrawn under a finger
    from = to = cards.indexOf(card);
    timer = setTimeout(() => {
      if (!card.isConnected) return;
      lifted = true; MON.drag = card;
      tops = cards.map(c => c.getBoundingClientRect().top); hs = cards.map(c => c.offsetHeight);
      cards.forEach(c => { c.style.transition = "transform .2s ease"; });
      card.style.transition = "none"; card.classList.add("lift");
      navigator.vibrate?.(15);
    }, 380);
  }, { passive: true });
  document.addEventListener("touchmove", e => {
    if (!timer && !lifted) return;
    const t = e.touches[0];
    if (!lifted) { if (Math.abs(t.clientY - sy) > 10 || Math.abs(t.clientX - sx) > 10) cancel(); return; }
    e.preventDefault();                              // lifted: the finger moves the card, the page must not scroll
    const dy = t.clientY - y0;
    cards[from].style.transform = `translateY(${dy}px)`;
    // a neighbour is passed once the card's leading edge is a little way into it (at most 100px, so a tall card
    // does not need to be dragged half its height before the order changes)
    const top = tops[from] + dy, bottom = top + hs[from];
    to = from;
    for (let k = from + 1; k < cards.length && bottom > tops[k] + Math.min(hs[k] / 2, 100); k++) to = k;
    for (let k = from - 1; k >= 0 && top < tops[k] + hs[k] - Math.min(hs[k] / 2, 100); k--) to = k;
    shift();
  }, { passive: false });
  const end = () => {
    cancel(); MON.touch = 0;
    if (!lifted) return;
    lifted = false;
    const card = cards[from];
    let off = 0;                                      // where the card settles, relative to where it started
    if (to > from) for (let k = from + 1; k <= to; k++) off += hs[k] + GAP;
    if (to < from) for (let k = to; k < from; k++) off -= hs[k] + GAP;
    card.style.transition = "transform .2s ease"; card.style.transform = `translateY(${off}px)`; card.classList.remove("lift");
    setTimeout(() => {
      const ids = cards.map(c => c.dataset.host); ids.splice(to, 0, ids.splice(from, 1)[0]);
      MON.order = ids;
      try { localStorage.setItem("mon-order", JSON.stringify(ids)); } catch (x) { /* ignore */ }
      cards.forEach(c => { c.style.transition = ""; c.style.transform = ""; });
      $("#mon-cards").innerHTML = monCards();
      setTimeout(() => { MON.drag = null; }, 300);   // the lift's own click must not open a container
    }, 210);
  };
  document.addEventListener("touchend", end);
  document.addEventListener("touchcancel", end);
  document.addEventListener("contextmenu", e => { if (e.target.closest(".mon-card")) e.preventDefault(); });
})();
