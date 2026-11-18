// 定时 of the baseboard heater, laid out like Mijia's: a list page (time, what it does, a green switch each, + to add), and an
// editor page (重复: 执行一次 / 每天 / 自定义, the time as an hour+minute wheel, 目标温度, 保存). Every timer is a Home Assistant
// automation tagged in its description "[ht:<climate>|on|22:00|mon,tue|24|<group>|<once 0/1>]", so it keeps running without the cloud.
// Saving shows at once (the new line appears in the list marked 保存中…) and is written in the background, one request at a time.

let HTD = null;                  // the timer being edited: { type: on|off|range, start, end, rep: daily|once|custom, days, temp, eids, saving }
let HT_PEND = [];                // timers being saved, shown in the list already
const HT_GONE = new Set();       // automations being deleted, hidden already
let HT_QUEUE = Promise.resolve();
const HT_GREEN = "#14a38b";
const trPost = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function htTimers(d) {
  const id = d.roles.main, descs = S.model.automation_desc || {}, out = [];
  for (const [eid, desc] of Object.entries(descs)) {
    const m = HT_TAG.exec(desc || "");
    if (!m || HT_GONE.has(eid)) continue;
    const [cl, kind, at, days, temp, grp, once] = m[1].split("|");
    if (cl !== id) continue;
    out.push({ eid, kind, at, days: !days || days === "*" ? [] : days.split(","), temp: !temp || temp === "-" ? null : +temp, grp: grp || "", once: once === "1", on: st(eid).s === "on" });
  }
  const rows = [], seen = new Set();
  for (const t of out.sort((a, b) => a.at.localeCompare(b.at))) {
    if (t.grp) {                       // a 时间段定时 is two automations (开 + 关) sharing a group: one line
      if (seen.has(t.grp)) continue;
      seen.add(t.grp);
      const pair = out.filter(u => u.grp === t.grp), on = pair.find(u => u.kind === "on"), off = pair.find(u => u.kind === "off");
      rows.push({ ...t, range: true, at: on?.at || t.at, end: off?.at || "", temp: on?.temp ?? null, eids: pair.map(u => ({ eid: u.eid, kind: u.kind })), on: pair.every(u => u.on) });
    } else rows.push({ ...t, eids: [{ eid: t.eid, kind: t.kind }] });
  }
  return [...rows, ...HT_PEND.filter(p => p.cl === id)];
}
const htDaysText = days => !days.length || days.length === 7 ? "每天" : "周" + HT_WEEK.filter(w => days.includes(w)).map(w => HT_WEEK_CN[HT_WEEK.indexOf(w)]).join("、");
const trRepText = t => t.once ? "执行一次" : htDaysText(t.days);
const trTitle = t => t.range ? `${t.at}–${t.end}` : t.at;
const trSub = t => `${t.range ? "时间段定时" : t.kind === "on" ? "定时开启" : "定时关闭"} · ${trRepText(t)}${t.temp != null && t.kind !== "off" ? ` · ${t.temp}°C` : ""}`;

const TR_ART = `<svg viewBox="0 0 120 120" class="trArt"><circle cx="60" cy="68" r="34" fill="#ececf0"/><circle cx="60" cy="68" r="27" fill="#fff"/><rect x="52" y="26" width="16" height="8" rx="3" fill="#dcdce2"/>
  <path d="M60 68V50M60 68l13 7" stroke="#c4c4cc" stroke-width="4" stroke-linecap="round"/><circle cx="60" cy="68" r="4" fill="#b4b4be"/>
  <path d="M26 36l-8-6M94 36l8-6" stroke="#dcdce2" stroke-width="5" stroke-linecap="round"/><path d="M86 20h9l-9 9h9M98 8h7l-7 7h7" stroke="#c4c4cc" stroke-width="3" fill="none" stroke-linecap="round"/></svg>`;

function htTimerPage(d) {
  const list = htTimers(d);
  htMount(`<div data-hpage="timer" class="page hs tr" style="--c:${COLORS.heater}">
      <div class="bar"><button class="back" data-tr="back">${icon("back")}</button><div class="actitle"><div class="t">定时</div></div></div>
      ${list.length ? list.map((t, i) => `<div class="accard trCard ${t.pending ? "pend" : ""}" data-tr="edit|${i}">
          <div class="trL"><b class="trTime" data-tick>${trTitle(t)}</b><small>${t.pending ? "保存中…" : trSub(t)}</small></div>
          <button class="sw ${t.on ? "on" : ""}" style="--c:${HT_GREEN}" data-tr="toggle|${i}" aria-label="定时开关" ${t.pending ? "disabled" : ""}></button></div>`).join("")
        : `<div class="trEmpty">${TR_ART}<p>定时列表为空，点击添加</p></div>`}
    </div>`, "timer");
}

function htTimerEdit(d) {
  if (!HTD) { S.htPage.view = "timer"; return htTimerPage(d); }
  const t = HTD, a = st(d.roles.main).a, bad = t.type === "range" && t.start === t.end;
  const title = { range: "时间段定时", on: "定时开启", off: "定时关闭" }[t.type];
  const row = (label, val, act) => `<div class="trRow" data-tr="${act}"><span>${label}</span><em data-tick>${val}</em><span class="chev">›</span></div>`;
  htMount(`<div data-hpage="tedit" class="page hs tr" style="--c:${COLORS.heater}">
      <div class="bar"><button class="back" data-tr="back">${icon("back")}</button><div class="actitle"><div class="t">${title}</div></div></div>
      <div class="accard trRows">${row("重复", trRepText({ once: t.rep === "once", days: t.rep === "custom" ? t.days : [] }), "rep")}</div>
      <div class="accard trRows">
        ${row(t.type === "range" ? "开始时间" : title, htHM(t.start), "time|start")}
        ${t.type === "range" ? row("结束时间", htHM(t.end) + (t.end < t.start ? "（次日）" : ""), "time|end") : ""}
        ${t.type !== "off" ? row("目标温度", `${t.temp}°C`, "temp") : ""}</div>
      ${bad ? `<p class="hsNote">开始和结束不能是同一时间</p>` : ""}
      <button class="trSave" data-tr="save" ${t.saving || bad ? "disabled" : ""}>${t.saving ? "保存中…" : "保存"}</button>
      ${t.eids ? `<button class="trDel" data-tr="del">删除这条定时</button>` : ""}
    </div>`, "tedit");
}

// ---- 重复 sheets (same look as the sleep page's)
let TRR = null;
function trRepeatSheet() {
  const row = (k, text) => `<div class="row hsOpt ${TRR.mode === k ? "on" : ""}" data-tr="rep-mode|${k}"><div class="l">${text}</div>${TRR.mode === k ? `<span class="hsOk">✓</span>` : ""}</div>`;
  sheet(`<h3>重复</h3>${row("once", "执行一次")}${row("daily", "每天")}${row("custom", "自定义")}
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri" data-tr="rep-ok">确定</button></div>`, "whs");
}
function trDaysSheet() {
  sheet(`<h3>自定义</h3>${HT_WEEK_CN.map((w, i) => `<div class="row hsOpt" style="--c:${COLORS.heater}" data-tr="rep-day|${HT_WEEK[i]}"><div class="l">周${w}</div><span class="hsCk ${TRR.days.includes(HT_WEEK[i]) ? "on" : ""}"></span></div>`).join("")}
    <div class="sheet-btns"><button data-tr="rep-back">取消</button><button class="pri" data-tr="rep-days-ok">确定</button></div>`, "whs");
}

// ---- saving: a new timer appears in the list at once; the writes follow one by one
function trConfigs(d, t) {
  const id = d.roles.main, grp = t.type === "range" ? (t.grp || Math.random().toString(36).slice(2, 8)) : "";
  const days = t.rep === "custom" ? t.days : [...HT_WEEK], once = t.rep === "once";
  const make = (kind, minutes) => {
    const at = htHM(minutes), target = { entity_id: id }, temp = kind === "on" ? t.temp : null;
    const actions = kind === "on"
      ? [{ action: "climate.turn_on", target }, ...(temp != null ? [{ action: "climate.set_temperature", target, data: { temperature: temp } }] : [])]
      : [{ action: "climate.turn_off", target }];
    if (once) actions.push({ action: "automation.turn_off", target: { entity_id: "{{ this.entity_id }}" } });   // 执行一次: switches itself off afterwards
    const tag = `[ht:${id}|${kind}|${at}|${days.length === 7 ? "*" : days.join(",")}|${temp ?? "-"}|${grp}|${once ? 1 : 0}]`;
    return { kind, cfg: { alias: `${d.name} 定时${kind === "on" ? "开启" : "关闭"} ${at}`,
      description: `${once ? "执行一次" : htDaysText(days)} ${at} ${kind === "on" ? "打开" : "关闭"}${d.name}${temp != null ? " " + temp + "°" : ""} ${tag}`,
      triggers: [{ trigger: "time", at: at + ":00" }], conditions: !once && days.length < 7 ? [{ condition: "time", weekday: days }] : [], actions } };
  };
  return t.type === "range" ? [make("on", t.start), make("off", t.end)] : [make(t.type, t.start)];
}

function trSave(d) {
  const t = HTD;
  if (t.saving) return;
  if (t.rep === "custom" && !t.days.length) return toast("至少选一天");
  t.saving = true;
  const cfgs = trConfigs(d, t), editing = t.eids;
  const pend = editing ? null : { pending: true, cl: d.roles.main, kind: t.type === "range" ? "on" : t.type, range: t.type === "range", at: htHM(t.start), end: htHM(t.end),
    days: t.rep === "custom" ? t.days : [], once: t.rep === "once", temp: t.type === "off" ? null : t.temp, on: true, eids: [] };
  if (pend) HT_PEND.push(pend);
  S.htPage.view = "timer"; HTD = null; htDir = -1; render();             // back to the list right away
  HT_QUEUE = HT_QUEUE.then(async () => {
    try {
      for (const c of cfgs) {
        const target = editing ? editing.find(e => e.kind === c.kind)?.eid : "new";
        await trPost("/api/automations/" + encodeURIComponent(target || "new"), c.cfg);
      }
      await load();
    } catch (e) { toast("没保存上：" + e.message); }
    if (pend) HT_PEND = HT_PEND.filter(p => p !== pend);
    render();
  });
}

function trDelete(d, t) {
  t.eids.forEach(e => HT_GONE.add(e.eid));
  S.htPage.view = "timer"; HTD = null; htDir = -1; render();
  HT_QUEUE = HT_QUEUE.then(async () => {
    try {
      for (const e of t.eids) await trPost("/api/automations/" + encodeURIComponent(e.eid) + "/delete", {});
      await load();
    } catch (err) { t.eids.forEach(e => HT_GONE.delete(e.eid)); toast("没删掉：" + err.message); }
    t.eids.forEach(e => HT_GONE.delete(e.eid));
    render();
  });
}

document.addEventListener("click", e => {
  const el = e.target.closest("[data-tr]");
  if (!el) return;
  const d = currentDevice();
  if (!d || d.kind !== "heater" || !d.ac) return;
  e.stopPropagation();
  const [act, arg] = el.dataset.tr.split("|"), a = st(d.roles.main).a;
  const open = t => {       // editor for an existing timer
    HTD = { type: t.range ? "range" : t.kind, start: toMin(t.at), end: t.end ? toMin(t.end) : 7 * 60, rep: t.once ? "once" : t.days.length && t.days.length < 7 ? "custom" : "daily",
      days: t.days.length ? [...t.days] : [...HT_WEEK], temp: t.temp ?? a.temperature ?? 24, eids: t.eids, grp: t.grp };
    S.htPage.view = "tedit"; htDir = 1; render();
  };
  const toMin = s => { const [h, m] = s.split(":").map(Number); return h * 60 + m; };
  switch (act) {
    case "back": if (HTD?.saving) return; return htBack();
    case "add":
      return sheet(`<h3>添加</h3>
        <div class="row hsOpt" data-tr="type|range"><div class="l">时间段定时<div class="rs">到点开机，到点关机</div></div><span class="chev">›</span></div>
        <div class="row hsOpt" data-tr="type|on"><div class="l">定时开启</div><span class="chev">›</span></div>
        <div class="row hsOpt" data-tr="type|off"><div class="l">定时关闭</div><span class="chev">›</span></div>
        <div class="sheet-btns"><button data-act="sheet-close">取消</button></div>`, "whs");
    case "type":
      closeSheet();
      HTD = { type: arg, start: arg === "off" ? 23 * 60 : 22 * 60, end: 7 * 60, rep: "daily", days: [...HT_WEEK], temp: a.temperature ?? 24, eids: null };
      S.htPage.view = "tedit"; htDir = 1; return render();
    case "edit": { const t = htTimers(d)[+arg]; return t && !t.pending ? open(t) : undefined; }
    case "toggle": {
      const t = htTimers(d)[+arg];
      if (!t || t.pending) return;
      return t.eids.forEach(x => call(x.eid, t.on ? "turn_off" : "turn_on", {}, { s: t.on ? "off" : "on" }));
    }
    case "rep": TRR = { mode: HTD.rep, days: [...HTD.days] }; return trRepeatSheet();
    case "rep-mode": TRR.mode = arg; return arg === "custom" ? trDaysSheet() : trRepeatSheet();
    case "rep-back": return trRepeatSheet();
    case "rep-day": { const i = TRR.days.indexOf(arg); if (i >= 0) TRR.days.splice(i, 1); else TRR.days.push(arg); return trDaysSheet(); }
    case "rep-days-ok": if (!TRR.days.length) return toast("至少选一天"); HTD.rep = "custom"; HTD.days = TRR.days.slice(); closeSheet(); return render();
    case "rep-ok": if (TRR.mode === "custom" && !TRR.days.length) return toast("至少选一天"); HTD.rep = TRR.mode; if (TRR.mode === "custom") HTD.days = TRR.days.slice(); closeSheet(); return render();
    case "time":
      return whHMPick(arg === "start" ? (HTD.type === "range" ? "开始时间" : HTD.type === "on" ? "定时开启" : "定时关闭") : "结束时间", HTD[arg], m => { closeSheet(); if (m != null) { HTD[arg] = m; render(); } });
    case "temp": {
      const r = kRange([a.min_temp ?? 16, a.max_temp ?? 28, 1]);
      return kPick("目标温度", r, HTD.temp, "°C", v => { HTD.temp = v; render(); }, Object.fromEntries(r.map(v => [v, v + "°C"])));
    }
    case "save": return trSave(d);
    case "del":
      return sheet(`<h3>删除这条定时？</h3><p class="sub">删掉后不能恢复。</p>
        <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri danger" data-tr="del-ok">删除</button></div>`, "whs");
    case "del-ok": closeSheet(); return HTD?.eids && trDelete(d, { eids: HTD.eids });
  }
}, true);
