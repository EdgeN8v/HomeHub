// Washer detail page, laid out like the Mijia plugin for the 米家双区洗衣机 (mibx5.x13): a 上筒/下筒
// switch with each drum's common modes, a mode page with its settings and 预约 / 开始, and the
// finish-time wheel. Live state comes from the HA entities; starting and booking go through
// /api/washer/start, which writes the whole program first (the washer keeps its own stale copy).
"use strict";

const W = { drum: "lower", info: null, infoAt: 0, drafts: {}, sending: false };
const W_SIID = { upper: 12, lower: 13 };
const W_NAME = { upper: "上筒", lower: "下筒" };
// The five shown on the main page, as in Mijia; the rest are under 更多.
const W_TOP = { upper: ["智能", "快速", "特渍", "内衣", "随心洗烘"], lower: ["智能", "快速", "防串色", "白衣净", "随心洗烘"] };
const W_DESC = {
  "智能": "按衣物重量自动调整，适合大多数日常衣物", "快速": "少量轻污衣物，快速洗完", "自定义": "你常用的那一套参数，一键预约也用它",
  "筒清洁": "清洁内筒，建议每月一次", "单脱水": "只脱水", "漂+脱": "只漂洗和脱水", "轻柔": "轻薄、易变形的衣物",
  "强力": "重污衣物", "随心洗烘": "洗完接着烘干", "特渍": "顽固污渍", "内衣": "贴身衣物", "蕾丝": "蕾丝等精细面料",
  "防串色": "深浅混洗，减少串色", "白衣净": "白色衣物", "除菌螨": "高温除菌除螨", "羊毛": "可机洗羊毛", "羽绒服": "羽绒服",
  "真丝": "真丝面料", "棉麻": "棉麻衣物", "婴童": "宝宝衣物，加强漂洗", "大件": "床单被罩等大件", "混合": "混合面料",
  "香薰洗": "配合香薰", "速干衣": "运动速干面料", "除毛洗": "去除毛絮", "轻干洗": "免水洗的轻护理",
};
const W_ICON = {
  "智能": '<path d="M12 3c-3 4-6 6.5-6 10a6 6 0 0 0 4 5.7"/><path d="M13.5 13c-1 1.2-1 2.5 0 3.5s1 2.3 0 3.5M17 13c-1 1.2-1 2.5 0 3.5s1 2.3 0 3.5M12 3c1.6 2.1 3.2 3.8 4.4 5.6"/>',
  "快速": '<circle cx="12" cy="12" r="9"/><path d="M13 6.5 9.5 12.5h5L11 17.5"/>',
  "自定义": '<rect x="5" y="3.5" width="14" height="17" rx="3"/><path d="M14.5 3.5V9"/>',
  "随心洗烘": '<path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 1.2-.3 2.3-.8 3.3"/><path d="M14 16.5c1-.8 2-.8 3 0s2 .8 3 0M14 19.5c1-.8 2-.8 3 0s2 .8 3 0"/>',
  "筒清洁": '<path d="M5 5v9a7 7 0 0 0 14 0V5"/><path d="M12 9.5l1 2 2 .5-2 .5-1 2-1-2-2-.5 2-.5z"/>',
  "更多": '<circle cx="12" cy="12" r="9"/><path d="M8 12h.01M12 12h.01M16 12h.01"/>',
  "特渍": '<path d="M4 7h16l-3 6h-3l-2 4-2-4H7z"/>',
  "内衣": '<path d="M7 4v5c0 2-2 3-2 6v5h14v-5c0-3-2-4-2-6V4M7 9c2 1.5 4 1.5 5 0 1 1.5 3 1.5 5 0"/>',
  "白衣净": '<path d="M9 4 4.5 7l2 3.5L8 10v10h8V10l1.5.5 2-3.5L15 4c-.5 1.5-1.7 2.3-3 2.3S9.5 5.5 9 4z"/><circle cx="15.5" cy="16" r="3"/>',
  "防串色": '<path d="M9 4 4.5 7l2 3.5L8 10v10h5M15 4l4.5 3-2 3.5L16 10v2"/><circle cx="16.5" cy="17" r="3.5"/><path d="M16.5 13.5v7"/>',
  "单脱水": '<path d="M5 5v9a7 7 0 0 0 14 0V5"/><path d="M9 12a3 3 0 1 0 3-3"/>',
  "漂+脱": '<circle cx="12" cy="12" r="9"/><path d="M8 10c1.3 1 2.7 1 4 0s2.7-1 4 0M8 14.5c1.3 1 2.7 1 4 0s2.7-1 4 0"/>',
};
const wIcon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${W_ICON[name] || W_ICON["白衣净"]}</svg>`;

// Settings rows per drum, in Mijia's order. kind: sw (switch) | pick (bottom-sheet list).
const W_ROWS = {
  lower: ["dry_only", "blue_oxygen", "steam", "soak", "temp", "rinse", "spin", "drying", "high_water", "sleep_mode"],
  upper: ["dry_only", "soak", "temp", "rinse", "spin", "drying", "steam_time", "sleep_mode"],
};
const W_WASH_ONLY = new Set(["blue_oxygen", "steam", "soak", "temp", "rinse", "spin", "high_water", "steam_time"]);
const W_LABEL = { dry_only: "单烘干", blue_oxygen: "蓝氧除菌", steam: "蒸汽除菌", soak: "浸泡时长", temp: "洗涤温度", rinse: "漂洗次数",
  spin: "脱水转速", drying: "烘干时长", high_water: "高水位", sleep_mode: "夜间模式", steam_time: "蒸汽除菌时长" };
const W_PICK = {
  soak: [[0, "不浸泡"], [10, "10 分钟"], [20, "20 分钟"], [30, "30 分钟"], [45, "45 分钟"], [60, "60 分钟"]],
  temp: [["常温", "常温", "最省电，冬天水太凉时不建议"], ["30", "30°C", "帮洗涤剂溶解，适合冬天"], ["40", "40°C", "去污力强，适合大多数衣物"],
         ["60", "60°C", "杀灭大部分细菌，加热时间较长"], ["95", "95°C", "高温煮洗，只适合耐高温的棉织物"]],
  rinse: [[1, "1次"], [2, "2次"], [3, "3次"], [4, "4次"], [5, "5次"]],
  spin: [["无", "不脱水"], ["400", "400"], ["600", "600"], ["800", "800"], ["1000", "1000"], ["1200", "1200"]],
  steam_time: [["0", "不开启"], ["12", "12 分钟"], ["18", "18 分钟"]],
};
const W_DEGREE = [["Store", "收纳干", "最干，适合直接收进衣柜"], ["Wear Immediately", "即穿", "干到可以直接穿"], ["Ironing", "熨烫干", "微潮，方便熨烫"]];
// What a built-in mode starts from (Mijia shows the same for 智能); 自定义 uses the saved program.
const W_DEFAULT = { dry_only: false, blue_oxygen: false, steam: false, soak: 0, temp: "40", rinse: 2, spin: "800",
  drying_level: "不烘干", drying_degree: "Wear Immediately", drying_time: 60, high_water: false, sleep_mode: false, steam_time: "0" };

function wPrefix(d) {
  const e = d.entities.find(x => /_status_p_12_28$/.test(x));
  return e ? e.slice(7, -"_status_p_12_28".length) : "";
}
function wIds(d, drum) {
  const P = wPrefix(d), s = W_SIID[drum];
  return {
    power: `switch.${P}_on_p_2_1`, status: `sensor.${P}_status_p_${s}_28`, stage: `sensor.${P}_run_status_p_${s}_11`,
    left: `sensor.${P}_left_time_p_${s}_10`, mode: `select.${P}_mode_p_${s}_3`, door: `sensor.${P}_door_state_p_${s}_9`,
    fault: `sensor.${P}_fault_p_${s}_4`, start: `button.${P}_start_wash_a_${s}_2`, pause: `button.${P}_pause_a_${s}_3`,
    stop: `button.${P}_stop_washing_a_${s}_1`, beads: drum === "lower" ? `switch.${P}_laundry_beads_p_13_27` : null,
    autoOff: `switch.${P}_self_delivery_auto_turnoff_p_2_5`,
    dosing: [["洗涤剂", `select.${P}_detergent_self_delivery_level_p_8_5`, `sensor.${P}_detergent_left_level_p_8_1`],
             ["柔顺剂", `select.${P}_softener_self_delivery_level_p_9_5`, `sensor.${P}_fabric_softener_left_level_p_9_1`],
             ["专用洗涤剂", `select.${P}_specialized_detergent_self_delivery_level_p_10_2`, `sensor.${P}_specialized_detergent_left_level_p_10_1`]],
  };
}

function wRoute() {
  const m = location.hash.match(/^#\/d\/[^/]+\/(m|more)\/(upper|lower)(?:\/(.+))?$/);
  return m ? { page: m[1], drum: m[2], mode: m[3] && decodeURIComponent(m[3]) } : { page: "home" };
}

async function wLoadInfo(force) {
  if (!force && W.info && Date.now() - W.infoAt < 60000) return;
  W.infoAt = Date.now();
  try { W.info = await api("/api/washer"); scheduleRender(); } catch (e) { /* page still works from live states */ }
}

function renderWasher(d) {
  wLoadInfo();
  const r = wRoute();
  if (r.page === "m") return wModePage(d, r.drum, r.mode);
  if (r.page === "more") return wMorePage(d, r.drum);
  wHome(d);
}

const wBusy = s => s === "工作中" || s === "暂停中" || s === "预约中";

function wHome(d) {
  const ids = wIds(d, W.drum), on = isOn(ids.power), off = deviceOffline(d);
  const s = val(ids.status), stage = val(ids.stage), info = W.info?.drums?.[W.drum];
  const other = W.drum === "upper" ? "lower" : "upper", os = val(wIds(d, other).status);
  let state = off ? "离线" : !on ? "已关机" : s, sub = "";
  if (on && s === "工作中") sub = [stage && stage !== "未运行" ? stage : "", num(ids.left) ? `还剩 ${minutes(ids.left)}` : "", info?.finish_eta ? `约 ${info.finish_eta} 洗完` : ""].filter(Boolean).join(" · ");
  else if (on && s === "预约中") sub = info?.finish_eta ? `预约 ${info.finish_eta} 洗完` : "已预约";
  else if (on && s === "暂停中") sub = stage && stage !== "未运行" ? `停在「${stage}」` : "";
  if (on && wBusy(os)) sub += `${sub ? "　" : ""}${W_NAME[other]}${os}`;
  const fault = val(ids.fault);
  if (on && fault && fault !== "No Faults") sub = `故障：${fault}`;
  const ctl = !on || off ? "" : s === "工作中" ? `<button class="wbtn" data-w="pause">暂停</button><button class="wbtn" data-w="stop">结束</button>`
    : s === "暂停中" ? `<button class="wbtn pri" data-w="resume">继续</button><button class="wbtn" data-w="stop">结束</button>`
    : s === "预约中" ? `<button class="wbtn" data-w="stop">取消预约</button>` : "";
  const tab = k => { const ts = val(wIds(d, k).status); return `<button class="wtab ${W.drum === k ? "on" : ""}" data-w="tab" data-arg="${k}">${W_NAME[k]}${on && wBusy(ts) ? `<small>${esc(ts.replace("中", ""))}</small>` : ""}</button>`; };
  const mode = m => `<button class="wmode" data-w="${m === "更多" ? "more" : "mode"}" data-arg="${esc(m)}"><span class="mi">${wIcon(m)}</span>${esc(m)}</button>`;
  const dosing = ids.dosing.map(([, sel]) => val(sel)).filter(v => v && !dead(v));
  $("#app").innerHTML = `
    <div class="page wpage" style="--c:${COLORS.washer}">
      <div class="bar"><button class="back" data-act="back">${icon("back")}</button>
        <div><div class="t">${esc(d.name)}</div><div class="r">${esc(d.room || "")}</div></div>
        ${on && !off ? `<button class="wpow" data-w="power" aria-label="关机">${icon("power")}</button>` : ""}</div>
      <div class="hero ${on && (wBusy(s) || wBusy(os)) ? "active" : ""}">
        <div class="whero-row">${wCleanBtn("upper")}<div class="big-ico">${icon("washer")}</div>${wCleanBtn("lower")}</div>
        <div class="state">${esc(state)}</div>
        <div class="state-sub">${esc(sub)}</div>
        <div class="wctl">${on || off ? ctl : `<button class="big-pw" data-w="power" aria-label="开机">${icon("power")}</button>`}</div>
      </div>
      <div class="panel">
        <div class="wtabs" style="--i:${W.drum === "upper" ? 0 : 1}"><span class="wpill"></span>${tab("upper")}${tab("lower")}</div>
        <div class="wmodes">${[...W_TOP[W.drum], "更多"].map(mode).join("")}</div>
        ${ids.beads ? `<div class="row"><div class="l">洗衣凝珠模式<div class="rs">手动放洗衣凝珠或洗涤剂时打开</div></div>
          <button class="sw ${isOn(ids.beads) ? "on" : ""}" data-toggle="${ids.beads}"></button></div>` : ""}
        <div class="row" data-w="dosing"><div class="l">洗涤剂智能投放<div class="rs">${dosing.length ? "投放量：" + esc(dosing.map(wDose).join(" / ")) : "投放筒和投放量"}</div></div><span class="chev">›</span></div>
      </div>
      <div class="panel"><div class="row"><div class="l">缺液关闭自动投放<div class="rs">连续 6 次开机都低液位时，自动关掉投放</div></div>
        <button class="sw ${isOn(ids.autoOff) ? "on" : ""}" data-toggle="${ids.autoOff}"></button></div></div>
      <div class="wlinks">
        <button class="wlink" data-w="custom">${wIcon("自定义")}<span>我的自定义<small>${esc(W.info?.drums?.[W.drum]?.program_summary || "")}</small></span><span class="chev">›</span></button>
        <button class="wlink" data-act="washer">${icon("clock")}<span>一键预约<small>两桶都用自定义，在谷电时段洗完</small></span><span class="chev">›</span></button>
        <button class="wlink" data-act="washer">${icon("list")}<span>洗衣记录<small>每次实际用了多久</small></span><span class="chev">›</span></button>
      </div>
    </div>`;
}

// ---- 筒清洁: one button per drum at the top (left = 上桶, right = 下桶), 60 degrees, the washer's own program.
// The card's colour fills with the days since the last clean that RAN TO THE END (see hub/app/cleaning.py).

function wCleanBtn(k) {
  const c = W.info?.drums?.[k]?.clean;
  return `<button class="wcb ${c?.due ? "due" : ""}" data-w="clean-ask" data-arg="${k}" style="--p:${Math.round((c?.progress ?? 0) * 100)}%">
    <span class="wcb-i">${wIcon("筒清洁")}</span><b>${W_NAME[k]}清洁</b><small>${esc(c?.label ?? "")}</small></button>`;
}

function wCleanAsk(d, k) {
  const c = W.info?.drums?.[k]?.clean, ids = wIds(d, k), s = val(ids.status);
  const free = !isOn(ids.power) || ["待机中", "洗涤完成"].includes(s);
  sheet(`<h3>${W_NAME[k]}筒清洁</h3>
    <p class="fl-desc" style="text-align:center">${free ? `空筒，放 <b>${esc(c?.dose_text ?? "")}</b> ${esc(c?.product ?? "泡腾片")}，${esc(c?.temp ?? "60")}℃` : `${W_NAME[k]}现在是「${esc(s)}」，空下来才能开始`}</p>
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri wpri" data-w="clean-go" data-arg="${k}|now" ${free ? "" : "disabled"}>开始</button></div>
    ${free ? `<a class="wc-a" data-w="clean-go" data-arg="${k}|tonight">改成今晚 22:30 开始</a>` : ""}`);
}

async function wCleanGo(d, drum, tonight) {
  const name = W_NAME[drum];
  closeSheet();
  try {
    const r = await wJob("/api/washer/clean", { drum, tonight }, () => {
      toast(tonight ? `${name}已约好，今晚 22:30 开始筒清洁` : `${name}开始筒清洁了`);
      wShowBooked(d, drum, tonight ? "预约中" : "工作中");
    }, () => wBookedNow(d, drum));
    if (!r.ok) wFailSheet(`${name}没开始`, r);
    else if (r.notes?.length) toast(r.notes[r.notes.length - 1]);
  } catch (e) { toast(`${name}：${e.message}`); }
  wLoadInfo(true);
}

const wDose = v => ({ Few: "少量", Standard: "标准", Many: "多量", "Lots Of": "大量" }[v] || v);

function wMorePage(d, drum) {
  const modes = (st(wIds(d, drum).mode).a.options || []).filter(m => !W_TOP[drum].includes(m))
    .sort((a, b) => (b === "自定义") - (a === "自定义"));
  $("#app").innerHTML = `
    <div class="page wpage" style="--c:${COLORS.washer}">
      <div class="bar"><button class="back" data-act="back">${icon("back")}</button><div><div class="t">更多模式</div><div class="r">${W_NAME[drum]}</div></div></div>
      <div class="panel wlist">${modes.map(m => `<button class="wli" data-w="mode" data-arg="${esc(m)}" data-drum="${drum}"><span class="mi">${wIcon(m)}</span>
        <span class="l">${esc(m)}<small>${esc(W_DESC[m] || "")}</small></span><span class="chev">›</span></button>`).join("")}</div>
    </div>`;
}

function wDraft(drum, mode) {
  const k = drum + "/" + mode;
  if (!W.drafts[k]) {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem("washer-draft:" + k) || "null"); } catch (e) { /* ignore */ }
    W.drafts[k] = mode === "自定义" ? null : { ...W_DEFAULT, ...(saved || {}) };
  }
  if (mode === "自定义") return W.info?.drums?.[drum]?.program ? { ...W_DEFAULT, ...W.info.drums[drum].program } : null;
  return W.drafts[k];
}

function wSetDraft(drum, mode, patch) {
  if (mode === "自定义") {
    const p = { ...W.info.drums[drum].program, ...patch };
    W.info.drums[drum].program = p;
    api("/api/washer/program", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ drum, program: p }) })
      .then(r => { W.info.drums[drum].program = r.program; }).catch(e => toast("没保存上：" + e.message));
  } else {
    const k = drum + "/" + mode;
    W.drafts[k] = { ...wDraft(drum, mode), ...patch };
    try { localStorage.setItem("washer-draft:" + k, JSON.stringify(W.drafts[k])); } catch (e) { /* ignore */ }
  }
  scheduleRender();
}

function wValText(key, p) {
  if (key === "drying") {
    if (p.drying_level === "不烘干") return "不烘干";
    if (p.drying_level === "定时烘干") return `定时 ${p.drying_time} 分钟`;
    return W_DEGREE.find(x => x[0] === p.drying_degree)?.[1] || "智能烘干";
  }
  const opt = W_PICK[key]?.find(o => String(o[0]) === String(p[key]));
  return opt ? opt[1] : String(p[key] ?? "—");
}

function wEstimate(drum, mode, p) {
  if (p?.dry_only && p.drying_level === "定时烘干") return p.drying_time;
  if (mode === "自定义" && p?.estimate_min) return p.estimate_min;
  return null;
}

function wModePage(d, drum, mode) {
  const p = wDraft(drum, mode);
  const est = wEstimate(drum, mode, p);
  const rows = !p ? `<div class="sheet-note">正在读取…</div>` : W_ROWS[drum].filter(k => !(p.dry_only && W_WASH_ONLY.has(k)) && !(mode === "自定义" && k === "dry_only")).map(k => {
    if (["dry_only", "blue_oxygen", "steam", "high_water", "sleep_mode"].includes(k))
      return `<div class="row"><div class="l">${W_LABEL[k]}</div><button class="sw ${p[k] ? "on" : ""}" data-w="sw" data-arg="${k}"></button></div>`;
    return `<div class="row" data-w="pick" data-arg="${k}"><div class="l">${W_LABEL[k]}</div><div class="rv">${esc(wValText(k, p))}</div><span class="chev">›</span></div>`;
  }).join("");
  const ids = wIds(d, drum), s = val(ids.status), busyNow = wBusy(s);
  $("#app").innerHTML = `
    <div class="page wpage wmodepage" style="--c:${COLORS.washer}" data-drum="${drum}" data-mode="${esc(mode)}">
      <div class="bar"><button class="back" data-act="back">${icon("back")}</button><div><div class="t">${esc(mode)}</div><div class="r">${W_NAME[drum]}</div></div></div>
      <div class="whero"><div class="mi big">${wIcon(mode)}</div>
        <div class="wt">${est ? `大约需要 ${est} 分钟` : "所需时间需称重计算"}</div>
        <div class="ws">${esc(W_DESC[mode] || "")}</div></div>
      <div class="panel">${rows}</div>
      <div class="wbottom">${busyNow ? `<div class="sheet-note">${W_NAME[drum]}现在是「${esc(s)}」，结束后才能开始新的</div>` : ""}
        <button class="wbig" data-w="book" ${busyNow || W.sending ? "disabled" : ""}>预约</button>
        <button class="wbig pri" data-w="go" ${busyNow || W.sending ? "disabled" : ""}>${W.sending ? "正在启动…" : "开始"}</button></div>
    </div>`;
}

// ---- pickers

function wPick(drum, mode, key) {
  const p = wDraft(drum, mode);
  if (key === "drying") return wPickDrying(drum, mode, { ...p });
  const opts = W_PICK[key];
  let cur = p[key];
  const draw = () => {
    sheet(`<h3>${W_LABEL[key]}</h3><div class="wopts">${opts.map(([v, t, desc]) =>
      `<button class="wopt ${String(v) === String(cur) ? "on" : ""}" data-w="opt" data-arg="${esc(v)}"><span>${esc(t)}${desc ? `<small>${esc(desc)}</small>` : ""}</span><i>✓</i></button>`).join("")}</div>
      <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri wpri" data-w="opt-ok">确定</button></div>`);
  };
  W.picker = { pick: v => { cur = typeof opts[0][0] === "number" ? Number(v) : v; draw(); }, ok: () => { closeSheet(); wSetDraft(drum, mode, { [key]: cur }); } };
  draw();
}

function wPickDrying(drum, mode, p) {
  const levels = [["不烘干", "不烘干", "只洗不烘"], ["智能烘干", "智能烘干", "检测湿度，衣服干了就停"], ["定时烘干", "定时烘干", "自己定烘多久"]];
  const draw = () => {
    const sub = p.drying_level === "智能烘干"
      ? `<div class="sheet-group">烘干程度</div><div class="wopts">${W_DEGREE.map(([v, t, desc]) => `<button class="wopt ${p.drying_degree === v ? "on" : ""}" data-w="opt2" data-arg="${v}"><span>${t}<small>${desc}</small></span><i>✓</i></button>`).join("")}</div>`
      : p.drying_level === "定时烘干"
        ? `<div class="sheet-group">烘干时长</div><div class="chips">${[20, 30, 40, 50, 60, 70, 80, 90].map(m => `<button class="chip ${p.drying_time === m ? "on" : ""}" data-w="opt2" data-arg="${m}">${m} 分钟</button>`).join("")}</div>` : "";
    sheet(`<h3>烘干时长</h3><div class="wopts">${levels.map(([v, t, desc]) =>
      `<button class="wopt ${p.drying_level === v ? "on" : ""}" data-w="opt" data-arg="${v}"><span>${t}<small>${desc}</small></span><i>✓</i></button>`).join("")}</div>${sub}
      <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri wpri" data-w="opt-ok">确定</button></div>`);
  };
  W.picker = {
    pick: v => { p.drying_level = v; draw(); },
    pick2: v => { if (p.drying_level === "定时烘干") p.drying_time = Number(v); else p.drying_degree = v; draw(); },
    ok: () => { closeSheet(); wSetDraft(drum, mode, { drying_level: p.drying_level, drying_degree: p.drying_degree, drying_time: p.drying_time }); },
  };
  draw();
}

// ---- 预约: three snapping wheels (今天/明天, 时, 分) like Mijia's 预计完成时间

const W_ITEM = 44;

function wBookSheet(drum, mode) {
  const p = wDraft(drum, mode);
  const need = wEstimate(drum, mode, p) || 60;
  const now = new Date(), earliest = new Date(now.getTime() + need * 60000);
  // Default to the usual 05:50 when that is still reachable, otherwise the earliest possible.
  let pick = new Date(now); pick.setHours(5, 50, 0, 0);
  if (pick <= now) pick.setDate(pick.getDate() + 1);
  if (pick < earliest || pick - now > 24 * 3600000) pick = new Date(Math.ceil(earliest.getTime() / 60000) * 60000);
  const today = new Date(now); today.setHours(0, 0, 0, 0);
  const sel = { day: Math.round((new Date(pick).setHours(0, 0, 0, 0) - today) / 86400000), h: pick.getHours(), m: pick.getMinutes() };
  const col = (key, items, unit) => `<div class="wwheel" data-key="${key}"><div class="wpad"></div>${items.map((t, i) =>
    `<div class="wit" data-i="${i}">${t}${unit ? `<sup>${unit}</sup>` : ""}</div>`).join("")}<div class="wpad"></div></div>`;
  sheet(`<h3>预计完成时间</h3><div class="wwarn" id="wwarn"></div>
    <div class="wwheels"><div class="wband"></div>${col("day", ["今天", "明天"], "")}${col("h", [...Array(24).keys()].map(x => String(x).padStart(2, "0")), "时")}${col("m", [...Array(60).keys()].map(x => String(x).padStart(2, "0")), "分")}</div>
    <div class="sheet-note" id="wbooknote">${mode === "自定义" ? `程序约 ${need} 分钟` : "按洗衣机预估的时长倒推开始时间"}</div>
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri wpri" id="wbookgo" data-w="book-go">开始</button></div>`);
  const target = () => { const t = new Date(today); t.setDate(t.getDate() + sel.day); t.setHours(sel.h, sel.m, 0, 0); return t; };
  const check = () => {
    const t = target(), bad = t < earliest ? `早于最早可完成时间 ${earliest.toTimeString().slice(0, 5)}` : t - now > 24 * 3600000 ? "最多只能预约 24 小时内" : "";
    $("#wwarn").textContent = bad;
    $("#wbookgo").disabled = !!bad;
    W.bookAt = bad ? null : t;
  };
  document.querySelectorAll("#sheet .wwheel").forEach(w => {
    const key = w.dataset.key;
    const paint = () => {
      const c = w.scrollTop / W_ITEM;
      w.querySelectorAll(".wit").forEach(it => {
        const dist = Math.abs(it.dataset.i - c);
        it.style.opacity = Math.max(.12, 1 - dist * .3);
        it.classList.toggle("on", dist < .5);
      });
    };
    w.scrollTop = sel[key] * W_ITEM;
    paint();
    let t = 0;
    w.addEventListener("scroll", () => {
      paint();
      clearTimeout(t);
      t = setTimeout(() => { sel[key] = Math.max(0, Math.round(w.scrollTop / W_ITEM)); check(); }, 90);
    }, { passive: true });
    w.addEventListener("click", e => { const it = e.target.closest(".wit"); if (it) w.scrollTo({ top: it.dataset.i * W_ITEM, behavior: "smooth" }); });
  });
  check();
  W.booking = { drum, mode };
}

// Bookings run on the hub in the background (they outlast the app's 30 s request limit): start, then
// poll the job until it reports back.
// onBooked(partial) fires as soon as the washer says 预约中 (a few seconds); the full result, with the
// check that it took the right program, comes ~10 s later.
// flipped(): true once the live state already shows the drum booked - often before the job says so,
// and the moment the washer beeps; that counts as booked too.
async function wJob(path, body, onBooked, flipped) {
  const { job } = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let told = false;
  const tell = b => { if (!told) { told = true; onBooked?.(b); } };
  for (let i = 0; i < 1500; i++) {
    await new Promise(r => setTimeout(r, 200));
    if (flipped?.()) tell({ ok: true });
    if (i % 3 !== 2) continue;   // ask the hub every 0.6 s
    try {
      const j = await api("/api/washer/job/" + job);
      if (j.booked?.ok) tell(j.booked);
      if (j.done) return j.result;
    } catch (e) { if (/no such job/.test(e.message)) throw new Error("服务重启了，结果丢了，请看洗衣机状态"); }
  }
  throw new Error("等了 5 分钟还没结果，请看洗衣机状态");
}

const wBookedNow = (d, drum) => { const x = wIds(d, drum); return isOn(x.power) && ["预约中", "工作中"].includes(val(x.status)); };

// Show the drum as booked right away, then re-read it.
function wShowBooked(d, drum, status) {
  const ids = wIds(d, drum);
  S.states[ids.status] = { ...st(ids.status), s: status };
  S.pins[ids.status] = { s: status, until: Date.now() + 5000 };
  pollStates([ids.status, ids.stage, ids.left, ids.fault, ids.power]);
  wLoadInfo(true);
  scheduleRender();
}

const wReasons = r => (r.notes || []).filter(n => !/实测记录|余量|不另留/.test(n));

function wFailSheet(title, r) {
  sheet(`<h3>${esc(title)}</h3><div class="wnotes">${(wReasons(r).length ? wReasons(r) : r.notes || ["原因不明"]).map(n => `<p>${esc(n)}</p>`).join("")}</div>
    <div class="sheet-btns"><button class="pri wpri" data-act="sheet-close">知道了</button></div>`);
}

// ---- home-page card: 「上」「下」 each books that drum on 自定义, done before 05:50

W.quick = {};

function washerCardButtons(d) {
  if (!wPrefix(d)) return "";
  return `<div class="pws">${["upper", "lower"].map(k => {
    const s = val(wIds(d, k).status), on = isOn(wIds(d, k).power) && (s === "预约中" || s === "工作中");
    return `<button class="pw wq ${on ? "on" : ""} ${W.quick[k] && !on ? "pend" : ""}" data-wq="${k}" aria-label="约${W_NAME[k]}">${k === "upper" ? "上" : "下"}</button>`;
  }).join("")}</div>`;
}

// The washer card fills from the left, orange, as the days (or washes) since the last finished 筒清洁 pass:
// full at 30. Whichever drum is further along sets it.
function washerCleanFill() {
  const p = Math.max(...["upper", "lower"].map(k => W.info?.drums?.[k]?.clean?.progress ?? 0));
  if (!p) return "";
  const pct = Math.round(p * 100);
  return `;background-image:linear-gradient(90deg, rgba(255,138,40,${(.16 + .26 * p).toFixed(2)}) ${pct}%, transparent ${pct}%)`;
}

function washerSummary(d) {
  wLoadInfo();
  const ids = wIds(d, "upper");
  if (!isOn(ids.power)) return Object.values(W.quick).some(Boolean) ? "正在预约…" : "已关机";
  const parts = ["upper", "lower"].map(k => {
    const x = wIds(d, k), s = val(x.status), eta = W.info?.drums?.[k]?.finish_eta, n = k === "upper" ? "上" : "下";
    if (W.quick[k]) return `${n}正在约`;
    if (s === "预约中") return `${n}约${eta || "好了"}`;
    if (s === "工作中") return `${n}剩${minutes(x.left) || "…"}`;
    if (s === "暂停中") return `${n}暂停`;
    if (s === "洗涤完成") return `${n}洗好了`;
    return "";
  }).filter(Boolean);
  const due = ["lower", "upper"].some(k => W.info?.drums?.[k]?.clean?.due);
  return (parts.join(" · ") || "待机中") + (due && !parts.length ? " · 该清洁了" : "");
}

async function wQuick(d, drum) {
  const ids = wIds(d, drum), s = val(ids.status), name = W_NAME[drum];
  if (W.quick[drum]) return toast(`${name}正在预约，稍等`);
  if (isOn(ids.power) && s === "预约中") return toast(`${name}已经约好了${W.info?.drums?.[drum]?.finish_eta ? "，" + W.info.drums[drum].finish_eta + " 前洗完" : ""}`);
  if (isOn(ids.power) && (s === "工作中" || s === "暂停中")) return toast(`${name}正在洗，洗完才能再约`);
  W.quick[drum] = true; scheduleRender();   // the button pulses; no "正在约" message
  let told = false;
  try {
    const r = await wJob("/api/washer/quick", { drum }, b => {
      told = true; W.quick[drum] = false;
      toast(b.plan?.washer_finish ? `${name}约好了，${b.plan.washer_finish} 左右洗完烘干` : `${name}约好了`);
      wShowBooked(d, drum, b.result_status || val(wIds(d, drum).status) || "预约中");
    }, () => wBookedNow(d, drum));
    if (!r.ok) wFailSheet(told ? `${name}刚才约上了，但核对没通过` : `${name}没约上`, r);
    else if (!told) toast(`${name}约好了，${r.plan?.washer_finish || ""} 左右洗完烘干`);
  } catch (e) {
    toast(`${name}：${e.message}`);
  }
  W.quick[drum] = false;
  wLoadInfo(true); scheduleRender();
}

// Registered before app.js's handler (this file loads first), so the card's own tap → detail page is skipped.
document.addEventListener("click", e => {
  const b = e.target.closest("[data-wq]");
  if (!b || S.edit || drag.swallowClick) return;
  e.stopImmediatePropagation();
  const d = S.byId[b.closest("[data-dev]")?.dataset.dev];
  if (d) wQuick(d, b.dataset.wq);
});

async function wSend(drum, mode, finishAt) {
  if (W.sending) return;
  const p = wDraft(drum, mode);
  if (!p) return;
  W.sending = true; scheduleRender();
  const iso = t => { const z = -t.getTimezoneOffset(), pad = n => String(Math.abs(n)).padStart(2, "0");
    return `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())}T${pad(t.getHours())}:${pad(t.getMinutes())}:00${z >= 0 ? "+" : "-"}${pad(Math.trunc(z / 60))}:${pad(z % 60)}`; };
  try {
    const { estimate_min, ...program } = p;
    const d = currentDevice();
    const done = () => {
      W.sending = false;
      toast(finishAt ? `${W_NAME[drum]}已预约，${finishAt.toTimeString().slice(0, 5)} 前洗完` : `${W_NAME[drum]}开始洗了`);
      W.drum = drum;
      if (d) wShowBooked(d, drum, finishAt ? "预约中" : "工作中");
      if (/\/(m|more)\//.test(location.hash)) history.back();
    };
    let told = false;
    const r = await wJob("/api/washer/start", { drum, mode, program, finish_at: finishAt ? iso(finishAt) : null },
      b => { if (b.ok) { told = true; done(); } }, () => !!d && wBookedNow(d, drum));
    W.sending = false;
    if (!r.ok) {
      wFailSheet(told ? `${W_NAME[drum]}刚才约上了，但核对没通过` : `${W_NAME[drum]}没${finishAt ? "约" : "开始"}上`, r);
      scheduleRender();
    } else if (!told) done();
  } catch (e) {
    W.sending = false; scheduleRender();
    toast("出错了：" + e.message);
  }
}

function wConfirm(title, note, label, fn) {
  W.confirm = fn;
  sheet(`<h3>${esc(title)}</h3><p class="sheet-note">${esc(note)}</p>
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri danger" data-w="confirm">${esc(label)}</button></div>`);
}

function wDosingSheet(d) {
  const ids = wIds(d, W.drum);
  sheet(`<h3>洗涤剂智能投放</h3>${ids.dosing.map(([name, sel, left]) => {
    const s = st(sel), lv = val(left);
    if (dead(s.s)) return "";
    return `<div class="sheet-group">${name}${lv && !dead(lv) ? `　<span class="${/低|Low/.test(lv) ? "wlow" : ""}">${esc({ Low: "余量低", Normal: "余量正常" }[lv] || lv)}</span>` : ""}</div>
      <div class="chips">${(s.a.options || []).map(o => `<button class="chip ${s.s === o ? "on" : ""}" data-svc="${sel}|select_option|option|${esc(o)}" data-w="dose">${esc(wDose(o))}</button>`).join("")}</div>`;
  }).join("")}<div class="sheet-btns"><button class="pri wpri" data-act="sheet-close">完成</button></div>`);
}

document.addEventListener("click", e => {
  const t = e.target.closest("[data-w]");
  if (!t) return;
  const d = currentDevice();
  if (!d || d.kind !== "washer") return;
  const w = t.dataset.w, arg = t.dataset.arg, r = wRoute();
  const page = $(".wmodepage"), drum = page?.dataset.drum, mode = page?.dataset.mode;
  const base = "#/d/" + encodeURIComponent(d.id);
  if (w === "tab") {
    if (arg === W.drum) return;
    // let the pill slide over first (as in Mijia), then redraw with the other drum's modes
    $(".wtabs")?.style.setProperty("--i", arg === "upper" ? 0 : 1);
    document.querySelectorAll(".wtab").forEach(b => b.classList.toggle("on", b.dataset.arg === arg));
    W.drum = arg;
    setTimeout(render, 200);
    return;
  }
  if (w === "mode") { location.hash = `${base}/m/${t.dataset.drum || W.drum}/${encodeURIComponent(arg)}`; return; }
  if (w === "custom") { location.hash = `${base}/m/${W.drum}/${encodeURIComponent("自定义")}`; return; }
  if (w === "more") { location.hash = `${base}/more/${W.drum}`; return; }
  if (w === "dosing") { wDosingSheet(d); return; }
  if (w === "dose") { setTimeout(() => wDosingSheet(d), 50); return; }   // app.js sends it; redraw the sheet
  if (w === "clean-ask") { wCleanAsk(d, arg); return; }
  if (w === "clean-go") { const [k, when] = arg.split("|"); wCleanGo(d, k, when === "tonight"); return; }
  const ids = wIds(d, r.drum || W.drum);
  if (w === "power") {
    if (!isOn(ids.power)) { toggle(ids.power); return; }
    const lost = ["upper", "lower"].map(k => [k, val(wIds(d, k).status)]).filter(([, s]) => wBusy(s))
      .map(([k, s]) => `${W_NAME[k]}${s === "预约中" ? "的预约会被取消" : "正在洗的程序会停止"}`);
    return wConfirm("关掉整台洗衣机？", lost.length ? lost.join("，") + "。" : "两个筒都会关掉。", "关机", () => toggle(ids.power));
  }
  // Washer buttons show their result at once and then re-read the drum (see call() in app.js).
  const watch = [ids.status, ids.stage, ids.left, ids.fault, ids.power];
  if (w === "pause") { call(ids.pause, "press", {}, null, watch, { [ids.status]: "暂停中" }); return; }
  if (w === "resume") { call(ids.start, "press", {}, null, watch, { [ids.status]: "工作中" }); return; }
  if (w === "stop") {
    if (!isOn(ids.power)) { toast("洗衣机已经关机了，预约也已经没有了"); return; }
    const s = val(ids.status);
    return wConfirm(s === "预约中" ? `取消${W_NAME[W.drum]}的预约？` : `结束${W_NAME[W.drum]}？`,
      s === "预约中" ? "洗衣机会回到待机。" : "会停止当前程序，衣服可能还是湿的。", s === "预约中" ? "取消预约" : "结束",
      () => call(ids.stop, "press", {}, null, watch, { [ids.status]: "待机中" }));
  }
  if (w === "confirm") { const f = W.confirm; W.confirm = null; closeSheet(); f?.(); return; }
  if (w === "sw") {
    const p = wDraft(drum, mode);
    if (!p) return;
    // 单烘干 with 不烘干 would run nothing: start it at the usual 30 minutes
    const patch = arg === "dry_only" && !p.dry_only && p.drying_level === "不烘干"
      ? { dry_only: true, drying_level: "定时烘干", drying_time: 30 } : { [arg]: !p[arg] };
    wSetDraft(drum, mode, patch);
    return;
  }
  if (w === "pick") { wPick(drum, mode, arg); return; }
  if (w === "opt") { W.picker?.pick(arg); return; }
  if (w === "opt2") { W.picker?.pick2?.(arg); return; }
  if (w === "opt-ok") { W.picker?.ok(); return; }
  if (w === "book") { wBookSheet(drum, mode); return; }
  if (w === "book-go") { const at = W.bookAt, b = W.booking; if (!at || !b) return; closeSheet(); wSend(b.drum, b.mode, at); return; }
  if (w === "go") { wSend(drum, mode, null); return; }
});
