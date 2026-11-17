// Automation editor for the 智能 page, laid out like Mijia: 如果 (any of these happens) /
// 生效时间 (time window and days) / 就执行 (steps in order). It edits HA automation configs; the
// conversion both ways lives here (parseAuto / buildAuto). A rule using anything the editor can't
// show (written by hand in HA) opens read-only rather than being rewritten.
"use strict";

const WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], WEEK_CN = ["一", "二", "三", "四", "五", "六", "日"];
const isAuto = () => location.hash.startsWith("#/auto/");
const autoTarget = () => decodeURIComponent(location.hash.slice("#/auto/".length));

// ---------------------------------------------------------------- names for entities

function ownerOf(eid) {
  for (const d of S.model.devices) if (d.entities.includes(eid) || d.events?.some(e => e.entity_id === eid)) return d;
  return null;
}
function entName(eid) {
  const d = ownerOf(eid);
  if (!d) return eid;
  const l = d.labels?.[eid];
  return l && l !== d.name && l !== d.kind_name ? `${d.name} · ${l}` : d.name;
}
const domainOf = eid => eid.split(".")[0];

// ---------------------------------------------------------------- HA config <-> editor model

function parseAuto(cfg) {
  const ed = { name: cfg.alias || "", triggers: [], conditions: [], actions: [], from: "", to: "", days: [], readonly: false, desc: cfg.description || "" };
  const ro = () => { ed.readonly = true; };
  for (const t of cfg.triggers || cfg.trigger || []) {
    const kind = t.trigger || t.platform, ids = [].concat(t.entity_id || []);
    if ((kind === "state" || kind === "numeric_state") && ids.length !== 1) { ro(); continue; }
    if (kind === "state") ed.triggers.push({ k: "state", entity: ids[0], to: t.to ?? null, from: t.from ?? null,
      ...(t.not_from ? { not_from: t.not_from, not_to: t.not_to } : {}) });
    else if (kind === "numeric_state") ed.triggers.push({ k: "num", entity: ids[0], above: t.above ?? null, below: t.below ?? null });
    else if (kind === "time" && typeof t.at === "string") ed.triggers.push({ k: "time", at: t.at.slice(0, 5) });
    else ro();
  }
  for (const c of cfg.conditions || cfg.condition || []) {
    const kind = c.condition || "", ids = [].concat(c.entity_id || []);
    if (kind === "time") { ed.from = (c.after || "").slice(0, 5); ed.to = (c.before || "").slice(0, 5); ed.days = [].concat(c.weekday || []); }
    else if (kind === "state" && ids.length === 1 && c.state != null) ed.conditions.push({ k: "state", entity: ids[0], to: c.state });
    else if (kind === "numeric_state" && ids.length === 1) ed.conditions.push({ k: "num", entity: ids[0], above: c.above ?? null, below: c.below ?? null });
    else ro();
  }
  for (const a of cfg.actions || cfg.action || []) {
    if (a.delay !== undefined) {
      const d = a.delay;
      const min = typeof d === "object" ? (d.hours || 0) * 60 + (d.minutes || 0) + (d.seconds || 0) / 60
        : d.split(":").reduce((m, v, i) => m + Number(v) * [60, 1, 1 / 60][i], 0);
      ed.actions.push({ k: "delay", minutes: Math.round(min * 10) / 10 });
      continue;
    }
    const svc = a.action || a.service, ids = [].concat(a.target?.entity_id || a.entity_id || []);
    if (!svc || !ids.length) { ro(); continue; }
    for (const id of ids) ed.actions.push({ k: "call", entity: id, service: svc.split(".")[1], data: a.data || {} });
  }
  return ed;
}

function buildAuto(ed) {
  const triggers = ed.triggers.map(t => {
    if (t.k === "time") return { trigger: "time", at: t.at + ":00" };
    if (t.k === "num") return { trigger: "numeric_state", entity_id: t.entity, ...(t.above != null ? { above: t.above } : {}), ...(t.below != null ? { below: t.below } : {}) };
    if (domainOf(t.entity) === "event")   // a button press / doorbell: any new event, but not the entity coming back online
      return { trigger: "state", entity_id: t.entity, not_from: ["unavailable", "unknown"], not_to: ["unavailable", "unknown"] };
    return { trigger: "state", entity_id: t.entity, ...(t.from != null ? { from: t.from } : {}), ...(t.to != null ? { to: t.to } : {}) };
  });
  const cond = {};
  if (ed.from) cond.after = ed.from + ":00";
  if (ed.to) cond.before = ed.to + ":00";
  if (ed.days.length && ed.days.length < 7) cond.weekday = ed.days;
  const conditions = [
    ...(Object.keys(cond).length ? [{ condition: "time", ...cond }] : []),
    ...ed.conditions.map(c => c.k === "num"
      ? { condition: "numeric_state", entity_id: c.entity, ...(c.above != null ? { above: c.above } : {}), ...(c.below != null ? { below: c.below } : {}) }
      : { condition: "state", entity_id: c.entity, state: c.to })
  ];
  const actions = ed.actions.map(a => a.k === "delay" ? { delay: { minutes: a.minutes } }
    : { action: `${domainOf(a.entity)}.${a.service}`, target: { entity_id: a.entity }, ...(Object.keys(a.data || {}).length ? { data: a.data } : {}) });
  return { alias: ed.name.trim(), description: describe(ed), triggers, conditions, actions };
}

// ---------------------------------------------------------------- plain-language lines

const BIN_WORDS = { occupancy: ["有人", "无人"], motion: ["有人", "无人"], door: ["打开", "关闭"], window: ["打开", "关闭"], moisture: ["漏水", "恢复正常"] };
function stateWord(eid, value) {
  const dom = domainOf(eid), dc = st(eid).a.device_class;
  if (dom === "binary_sensor") return (BIN_WORDS[dc] || ["是", "否"])[value === "on" ? 0 : 1];
  if (["switch", "light", "fan", "humidifier", "water_heater"].includes(dom)) return value === "on" ? "被打开" : "被关闭";
  return value == null ? "变化" : `变为 ${value}`;
}
function triggerText(t) {
  if (t.k === "time") return `每天 ${t.at}`;
  if (t.k === "num") return `${entName(t.entity)} ${t.above != null ? "高于 " + t.above : "低于 " + t.below}${st(t.entity).a.unit_of_measurement || ""}`;
  if (domainOf(t.entity) === "event") return entName(t.entity);
  return `${entName(t.entity)} ${stateWord(t.entity, t.to)}`;
}
function conditionText(c) {
  if (c.k === "num") return `${entName(c.entity)} 当前${c.above != null ? "高于 " + c.above : "低于 " + c.below}${st(c.entity).a.unit_of_measurement || ""}`;
  const word = stateWord(c.entity, c.to).replace(/^被/, "");
  return `${entName(c.entity)} 当前${word}`;
}
function ruleIcon(x) { return icon(ownerOf(x.entity)?.kind || (domainOf(x.entity) === "binary_sensor" ? "presence" : "smart")); }
const SVC_WORDS = { turn_on: "打开", turn_off: "关闭", toggle: "切换开关", press: "按下", trigger: "执行" };
function actionText(a) {
  if (a.k === "delay") return `等 ${a.minutes} 分钟`;
  if (a.service === "set_temperature") return `${entName(a.entity)} 设为 ${a.data.temperature}°`;
  if (a.service === "select_option") return `${entName(a.entity)} 设为 ${a.data.option}`;
  if (a.service === "set_value") return `${entName(a.entity)} 设为 ${a.data.value}`;
  return `${SVC_WORDS[a.service] || a.service} ${entName(a.entity)}`;
}
function windowText(ed) {
  const time = ed.from || ed.to ? `${ed.from || "00:00"}–${ed.to || "24:00"}` : "全天";
  const days = !ed.days.length || ed.days.length === 7 ? "每天" : "每周" + ed.days.map(d => WEEK_CN[WEEK.indexOf(d)]).join("、");
  return `${days} ${time}`;
}
function describe(ed) {
  const w = ed.from || ed.to || (ed.days.length && ed.days.length < 7) ? `（${windowText(ed)}）` : "";
  return `如果 ${ed.triggers.map(triggerText).join(" 或 ")}${ed.conditions.length ? "，且" + ed.conditions.map(conditionText).join("、") : ""}${w} → ${ed.actions.map(actionText).join("，")}`;
}

// ---------------------------------------------------------------- what a device can trigger / do

function deviceEntities(d) {
  const ids = new Set([...d.toggles.map(t => t.entity_id), ...d.features.map(f => f.entity_id), ...Object.values(d.roles)]);
  return [...ids].filter(id => id && id in S.states);
}

// Ordered like Mijia: presence / door / on-off first, then button presses, then the "持续…" states,
// then thresholds. Fault states (功能异常, 故障) are left out: nobody automates on those.
function triggerChoices(d) {
  const first = [], events = [], enums = [], nums = [];
  for (const id of deviceEntities(d)) {
    const dom = domainOf(id), s = st(id), name = d.labels?.[id] || "";
    if (/故障|异常/.test(name)) continue;
    const pre = name && name !== d.name && name !== d.kind_name ? name + " " : "";
    if (dom === "binary_sensor") {
      const [yes, no] = BIN_WORDS[s.a.device_class] || ["是", "否"];
      first.push({ text: pre + yes, t: { k: "state", entity: id, to: "on" } }, { text: pre + no, t: { k: "state", entity: id, to: "off" } });
    } else if (["switch", "light", "fan", "humidifier", "water_heater"].includes(dom)) {
      first.push({ text: pre + "被打开", t: { k: "state", entity: id, to: "on" } }, { text: pre + "被关闭", t: { k: "state", entity: id, to: "off" } });
    } else if (dom === "sensor" && s.a.options?.length) {
      for (const o of s.a.options) enums.push({ text: `${pre}变为 ${o}`, t: { k: "state", entity: id, to: o } });
    } else if (dom === "sensor" && s.a.unit_of_measurement && Number.isFinite(parseFloat(s.s))) {
      nums.push({ text: `${pre}高于…`, t: { k: "num", entity: id, above: null }, ask: "above" }, { text: `${pre}低于…`, t: { k: "num", entity: id, below: null }, ask: "below" });
    }
  }
  for (const ev of d.events || []) events.push({ text: ev.label, t: { k: "state", entity: ev.entity_id, to: null } });
  return [...first, ...events, ...enums, ...nums];
}

function conditionChoices(d) {
  const out = [];
  for (const id of deviceEntities(d)) {
    const dom = domainOf(id), stt = st(id), name = d.labels?.[id] || "";
    const pre = name && name !== d.name && name !== d.kind_name ? name + " " : "";
    if (dom === "binary_sensor") {
      const [yes, no] = BIN_WORDS[stt.a.device_class] || ["是", "否"];
      out.push({ text: `${pre}当前${yes}`, t: { k: "state", entity: id, to: "on" } }, { text: `${pre}当前${no}`, t: { k: "state", entity: id, to: "off" } });
    } else if (["switch", "light", "fan", "humidifier", "water_heater", "climate"].includes(dom)) {
      out.push({ text: `${pre}当前已打开`, t: { k: "state", entity: id, to: "on" } }, { text: `${pre}当前已关闭`, t: { k: "state", entity: id, to: "off" } });
    } else if (["sensor", "select"].includes(dom) && stt.a.options?.length) {
      for (const o of stt.a.options) out.push({ text: `${pre}当前为 ${o}`, t: { k: "state", entity: id, to: o } });
    } else if (dom === "sensor" && stt.a.unit_of_measurement && Number.isFinite(parseFloat(stt.s))) {
      out.push({ text: `${pre}当前高于…`, t: { k: "num", entity: id, above: null }, ask: "above" }, { text: `${pre}当前低于…`, t: { k: "num", entity: id, below: null }, ask: "below" });
    }
  }
  return out;
}

function actionChoices(d) {
  const out = [];
  for (const id of deviceEntities(d)) {
    const dom = domainOf(id), s = st(id), name = d.labels?.[id] || "";
    const pre = name && name !== d.name && name !== d.kind_name ? name + " " : "";
    if (["switch", "light", "fan", "humidifier", "water_heater", "climate"].includes(dom)) {
      out.push({ text: `打开 ${pre}`.trim(), a: { k: "call", entity: id, service: "turn_on", data: {} } },
               { text: `关闭 ${pre}`.trim(), a: { k: "call", entity: id, service: "turn_off", data: {} } });
      if (dom !== "climate" && dom !== "water_heater") out.push({ text: `切换 ${pre}`.trim(), a: { k: "call", entity: id, service: "toggle", data: {} } });
      if (dom === "climate") out.push({ text: "设定温度…", a: { k: "call", entity: id, service: "set_temperature", data: {} }, ask: "temperature" });
    } else if (dom === "select") {
      for (const o of s.a.options || []) out.push({ text: `${pre}设为 ${o}`, a: { k: "call", entity: id, service: "select_option", data: { option: o } } });
    } else if (dom === "number") {
      out.push({ text: `${pre}设为…`, a: { k: "call", entity: id, service: "set_value", data: {} }, ask: "value" });
    } else if (dom === "button") {
      out.push({ text: `按下 ${pre}`.trim(), a: { k: "call", entity: id, service: "press", data: {} } });
    }
  }
  return out;
}

// ---------------------------------------------------------------- page

async function openAuto() {
  const target = autoTarget();
  if (S.ed?.target === target) return renderAutoEditor();
  if (target === "new") {
    S.ed = { target, name: "", triggers: [], conditions: [], actions: [], from: "", to: "", days: [], readonly: false, desc: "" };
    return renderAutoEditor();
  }
  $("#app").innerHTML = `<div class="page"><div class="bar"><button class="back" data-act="ed-back">${icon("back")}</button><div class="t">自动化</div></div><div class="empty">读取中…</div></div>`;
  try {
    const cfg = await api("/api/automations/" + encodeURIComponent(target));
    S.ed = { target, ...parseAuto(cfg) };
  } catch (e) {
    S.ed = { target, name: S.model.automation_names?.[target] || "", triggers: [], actions: [], from: "", to: "", days: [], readonly: true, desc: "读不到这条自动化：" + e.message };
  }
  renderAutoEditor();
}

function renderAutoEditor() {
  const ed = S.ed, isNew = ed.target === "new", s = isNew ? null : st(ed.target);
  const info = { t: ["触发条件", "当以下任一情况发生"], c: ["状态条件", "同时满足这些当前状态"], a: ["执行动作", "触发后按顺序执行"] };
  const rows = (items, kind) => items.map((x, i) => `<button class="ed-rule" data-act="ed-row" data-arg="${kind}:${i}">
      <span class="ed-rule-ico">${kind === "a" && x.k === "delay" ? icon("clock") : ruleIcon(x)}</span><span class="ed-rule-main"><small>${info[kind][0]}</small><b>${esc(kind === "t" ? triggerText(x) : kind === "c" ? conditionText(x) : actionText(x))}</b></span>${ed.readonly ? "" : `<span class="ed-x">${icon("trash")}</span>`}</button>`).join("");
  const section = (kind, items) => `<div class="panel ed-section"><div class="ed-section-head"><span>${kind === "t" ? "①" : kind === "c" ? "②" : "③"}</span><div><h3>${info[kind][0]}</h3><small>${info[kind][1]}</small></div></div>${rows(items, kind) || `<p class="sheet-note ed-empty">${kind === "c" ? "不设则任何状态下都可触发" : "还没有" + info[kind][0]}</p>`}${ed.readonly ? "" : `<button class="ed-add" data-act="ed-add-${kind}">${icon("plus")}添加${info[kind][0]}</button>`}</div>`;
  $("#app").innerHTML = `
    <div class="page ed">
      <div class="bar"><button class="back" data-act="ed-back">${icon("back")}</button><div class="t">${isNew ? "新建自动化" : "编辑自动化"}</div>${isNew ? "" : `<button class="sw ${s.s === "on" ? "on" : ""}" style="--c:#14a38b;margin-left:auto;margin-right:8px" data-act="auto-toggle" data-arg="${esc(ed.target)}"></button>`}</div>
      ${ed.readonly ? `<div class="panel"><p class="sheet-note" style="text-align:left;margin:0">这条自动化用了编辑器还不支持的设置，只能查看。${esc(ed.desc)}</p></div>` : ""}
      <div class="panel ed-name-card"><input id="ed-name" class="ed-name" maxlength="60" placeholder="给自动化起个名字" value="${esc(ed.name)}" ${ed.readonly ? "disabled" : ""}></div>
      ${section("t", ed.triggers)}${section("c", ed.conditions)}
      <div class="panel ed-time-card"><span class="ed-rule-ico">${icon("clock")}</span><button class="ed-time-open" data-act="ed-window" ${ed.readonly ? "disabled" : ""}><small>④ 生效时间</small><b>${esc(windowText(ed))}</b></button><span class="chev">›</span></div>
      ${section("a", ed.actions)}
      ${ed.readonly ? "" : `<div class="sheet-btns ed-btns">${isNew ? "" : `<button data-act="ed-delete">删除</button>`}<button class="pri" data-act="ed-save">保存</button></div>`}
    </div>`;
  $("#ed-name")?.addEventListener("input", e => { ed.name = e.target.value; });
}

// ---- pickers (bottom sheets)

function pickDevice(purpose) {
  const choices = d => purpose === "a" ? actionChoices(d) : purpose === "c" ? conditionChoices(d) : triggerChoices(d);
  const devs = S.model.devices.filter(d => d.home === S.home && !deviceOffline(d) && choices(d).length);
  const rooms = [...new Set(devs.map(d => d.room || "其他"))];
  const extra = purpose === "t" ? `<button class="ed-special" data-act="ed-time">${icon("clock")}<span>定时触发<small>每天指定时间</small></span><b>›</b></button>` : purpose === "a" ? `<button class="ed-special" data-act="ed-delay">${icon("clock")}<span>延时执行<small>等待一段时间</small></span><b>›</b></button>` : "";
  sheet(`<h3>${purpose === "t" ? "添加触发条件" : purpose === "c" ? "添加状态条件" : "添加执行动作"}</h3><div class="sheet-list"><div class="ed-specials">${extra}</div>
    ${rooms.map(r => `<div class="sheet-group">${esc(r)}</div><div class="ed-devices">` + devs.filter(d => (d.room || "其他") === r).map(d =>
      `<button class="ed-device" data-act="ed-dev" data-arg="${purpose}:${d.id}"><span class="ed-device-ico">${icon(d.kind || "switch")}</span><span>${esc(d.name)}</span></button>`).join("") + `</div>`).join("")}</div>`);
}

function pickChoice(purpose, devId) {
  const d = S.byId[devId], list = purpose === "a" ? actionChoices(d) : purpose === "c" ? conditionChoices(d) : triggerChoices(d);
  S.edChoices = list;
  sheet(`<h3>${esc(d.name)}</h3><div class="sheet-list">${list.map((c, i) =>
    `<button class="ed-option" data-act="ed-choice" data-arg="${purpose}:${i}"><span class="ed-option-ico">${icon(d.kind || "smart")}</span><span>${esc(c.text)}</span><b>›</b></button>`).join("")}</div>`);
}

function choose(purpose, i) {
  const c = S.edChoices[i];
  if (c.ask) {
    const label = { above: "高于多少", below: "低于多少", temperature: "设为多少度", value: "设为多少" }[c.ask];
    sheet(`<h3>${label}</h3><input id="ed-num" type="number" inputmode="decimal" step="any">
      <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri" data-act="ed-num-ok" data-arg="${purpose}:${i}">好</button></div>`);
    $("#ed-num").focus();
    return;
  }
  commit(purpose, structuredClone(purpose === "a" ? c.a : c.t));
}

function chooseNumber(purpose, i) {
  const v = parseFloat($("#ed-num").value);
  if (!Number.isFinite(v)) return toast("填一个数字");
  const c = S.edChoices[i], x = structuredClone(purpose === "a" ? c.a : c.t);
  if (purpose === "a") x.data = { [c.ask]: v }; else x[c.ask] = v;
  commit(purpose, x);
}

function commit(purpose, x) {
  const list = purpose === "t" ? S.ed.triggers : purpose === "c" ? S.ed.conditions : S.ed.actions;
  const replace = S.replaceRule;
  if (replace?.kind === purpose) { list[replace.i] = x; S.replaceRule = null; } else list.push(x);
  closeSheet();
  renderAutoEditor();
}

function timeSheet() {
  sheet(`<h3>每天几点</h3><input id="ed-at" type="time" value="22:00">
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri" data-act="ed-time-ok">好</button></div>`);
}
function delaySheet() {
  sheet(`<h3>等几分钟</h3><input id="ed-num" type="number" inputmode="decimal" value="5" min="0.1" step="any">
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri" data-act="ed-delay-ok">好</button></div>`);
}

function windowSheet() {
  const ed = S.ed;
  sheet(`<h3 class="ed-window-title">${icon("clock")}生效时间</h3><p class="sheet-note">选择什么时候这条自动化才会生效</p>
    <div class="ed-time-presets"><button data-act="ed-window-all">全天</button><button data-act="ed-window-preset" data-arg="day">白天</button><button data-act="ed-window-preset" data-arg="night">夜晚</button></div>
    <div class="ed-win"><label><small>开始</small><input id="ed-from" type="time" value="${ed.from}"></label><span>至</span><label><small>结束</small><input id="ed-to" type="time" value="${ed.to}"></label></div>
    <div class="ed-week-label">重复</div><div class="chips ed-days">${WEEK.map((w, i) => `<button class="chip ${!ed.days.length || ed.days.includes(w) ? "on" : ""}" style="--c:#14a38b" data-act="ed-day" data-arg="${w}">${WEEK_CN[i]}</button>`).join("")}</div>
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri" data-act="ed-window-ok">完成</button></div>`);
}

function windowOk() {
  const days = [...document.querySelectorAll(".ed-days .chip.on")].map(b => b.dataset.arg);
  if (!days.length) return toast("至少选一天");
  Object.assign(S.ed, { from: $("#ed-from").value, to: $("#ed-to").value, days: days.length === 7 ? [] : days });
  closeSheet(); renderAutoEditor();
}

function rowSheet(arg) {
  if (S.ed.readonly) return;
  const [kind, i] = arg.split(":"), list = kind === "t" ? S.ed.triggers : kind === "c" ? S.ed.conditions : S.ed.actions;
  const text = kind === "t" ? triggerText(list[i]) : kind === "c" ? conditionText(list[i]) : actionText(list[i]);
  sheet(`<h3>${esc(text)}</h3><div class="sheet-btns"><button data-act="ed-change" data-arg="${arg}">修改</button>${kind === "a" && i > 0 ? `<button data-act="ed-up" data-arg="${i}">往前移</button>` : ""}<button class="pri danger" data-act="ed-remove" data-arg="${arg}">删除</button></div>`);
}

async function saveAuto() {
  const ed = S.ed;
  if (!ed.name.trim()) return toast("给自动化起个名字");
  if (!ed.triggers.length || !ed.actions.length) return toast("至少要一个条件和一个动作");
  const btn = $("[data-act=ed-save]"); btn.disabled = true; btn.textContent = "保存中…";
  try {
    const r = await api("/api/automations/" + encodeURIComponent(ed.target), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(buildAuto(ed)) });
    toast("已保存");
    S.ed = null;
    await load();
    location.hash = "#/smart";
    return r;
  } catch (e) { toast("没保存上：" + e.message); btn.disabled = false; btn.textContent = "保存"; }
}

function deleteAuto() {
  sheet(`<h3>删除「${esc(S.ed.name)}」？</h3><p class="sheet-note">删掉后不能恢复。</p>
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri danger" data-act="ed-delete-ok">删除</button></div>`);
}
async function deleteAutoOk() {
  closeSheet();
  try {
    await api("/api/automations/" + encodeURIComponent(S.ed.target) + "/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast("已删除"); S.ed = null; await load(); location.hash = "#/smart";
  } catch (e) { toast("删除失败：" + e.message); }
}

// Editor buttons. Runs before app.js's click handler (registered first in the capture phase).
document.addEventListener("click", e => {
  const t = e.target.closest("[data-act]");
  if (!t || !t.dataset.act.startsWith("ed-")) return;
  e.stopImmediatePropagation();
  const act = t.dataset.act, arg = t.dataset.arg;
  if (act === "ed-back") { S.ed = null; location.hash = "#/smart"; }
  else if (act === "ed-add-t") pickDevice("t");
  else if (act === "ed-add-a") pickDevice("a");
  else if (act === "ed-add-c") pickDevice("c");
  else if (act === "ed-dev") { const [p, id] = arg.split(":"); pickChoice(p, id); }
  else if (act === "ed-choice") { const [p, i] = arg.split(":"); choose(p, +i); }
  else if (act === "ed-num-ok") { const [p, i] = arg.split(":"); chooseNumber(p, +i); }
  else if (act === "ed-time") timeSheet();
  else if (act === "ed-time-ok") { const v = $("#ed-at").value; if (v) commit("t", { k: "time", at: v }); }
  else if (act === "ed-delay") delaySheet();
  else if (act === "ed-delay-ok") { const v = parseFloat($("#ed-num").value); if (v > 0) commit("a", { k: "delay", minutes: v }); else toast("填一个大于 0 的数"); }
  else if (act === "ed-window") windowSheet();
  else if (act === "ed-day") t.classList.toggle("on");
  else if (act === "ed-window-all") { Object.assign(S.ed, { from: "", to: "", days: [] }); closeSheet(); renderAutoEditor(); }
  else if (act === "ed-window-preset") { const v = arg === "night" ? { from: "22:00", to: "06:00" } : { from: "07:00", to: "22:00" }; Object.assign(S.ed, v, { days: [] }); closeSheet(); renderAutoEditor(); }
  else if (act === "ed-window-ok") windowOk();
  else if (act === "ed-row") rowSheet(arg);
  else if (act === "ed-change") { const [kind, i] = arg.split(":"); S.replaceRule = { kind, i: +i }; closeSheet(); pickDevice(kind); }
  else if (act === "ed-remove") { const [k, i] = arg.split(":"); (k === "t" ? S.ed.triggers : k === "c" ? S.ed.conditions : S.ed.actions).splice(+i, 1); closeSheet(); renderAutoEditor(); }
  else if (act === "ed-up") { const i = +arg, a = S.ed.actions; [a[i - 1], a[i]] = [a[i], a[i - 1]]; closeSheet(); renderAutoEditor(); }
  else if (act === "ed-save") saveAuto();
  else if (act === "ed-delete") deleteAuto();
  else if (act === "ed-delete-ok") deleteAutoOk();
}, true);
