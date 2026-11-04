// Air conditioner page, laid out like Mijia's: the big number, a card of round buttons (power / favourite /
// timer / mode), the unit's own functions in a grid, then the room-temperature chart, the electricity
// statistics and self-clean. Everything on it maps to what the unit really exposes (see AC_EXTRA in home.py).

const AC_FAN = ["一档", "二档", "三档", "四档", "五档", "六档", "七档", "Max档"];
const AC_LABEL = { cool: "制冷", heat: "制热", dry: "除湿", fan_only: "风扇", auto: "自动", heat_cool: "自动" };
const AC_SLEEP = { "Standard Sleep": "标准睡眠", "Lingyun Sleep": "灵云睡眠", Exit: "关闭" };
const AC_FAVTYPE = { "Cloud Recommendation": "云端推荐", Custom: "自定义" };
const acSvg = inner => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
const AI = {
  power: acSvg('<path d="M12 3.5v8"/><path d="M6.6 7a8 8 0 1 0 10.8 0"/>'),
  fav: acSvg('<circle cx="12" cy="12" r="8.5"/><path d="M8.6 16 12 7.8 15.4 16M9.9 13.6h4.2"/>'),
  clock: acSvg('<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3.2 2"/>'),
  cool: acSvg('<path d="M12 3v18M4.2 7.5l15.6 9M4.2 16.5l15.6-9M9.5 4.5 12 7l2.5-2.5M9.5 19.5 12 17l2.5 2.5"/>'),
  dry: acSvg('<path d="M12 4s5.8 5.8 5.8 9.6a5.8 5.8 0 0 1-11.6 0C6.2 9.8 12 4 12 4z"/><path d="M4.5 4.5l15 15"/>'),
  fan_only: acSvg('<circle cx="12" cy="12" r="1.5"/><path d="M12 10.5C11 6 12.6 3.8 14.6 4.6c1.9.8.8 4.6-2.6 5.9zM13.5 12c4.5-1 6.7.6 5.9 2.6-.8 1.9-4.6.8-5.9-2.6zM12 13.5c1 4.5-.6 6.7-2.6 5.9-1.9-.8-.8-4.6 2.6-5.9zM10.5 12C6 13 3.8 11.4 4.6 9.4c.8-1.9 4.6-.8 5.9 2.6z"/>'),
  heat: acSvg('<circle cx="12" cy="12" r="4"/><path d="M12 2.8v3M12 18.2v3M2.8 12h3M18.2 12h3M5.5 5.5l2.1 2.1M16.4 16.4l2.1 2.1M18.5 5.5l-2.1 2.1M7.6 16.4l-2.1 2.1"/>'),
  auto: acSvg('<circle cx="12" cy="12" r="8.5"/><path d="M8.6 16 12 7.8 15.4 16M9.9 13.6h4.2"/>'),
  eco: acSvg('<path d="M5 19C4.5 10.5 9.5 5 20 4.5 20 14.5 15 19.5 6.5 19.5zM5 19l8.5-8.5"/>'),
  wind: acSvg('<path d="M3 8.5h10.5a2.7 2.7 0 1 0-2.7-2.7M3 13h14.5a3 3 0 1 1-3 3M3 17.5h6"/>'),
  nodirect: acSvg('<path d="M5 4v9M12 4v12M19 4v9M3.5 20h17M9 17.5l3 2.5 3-2.5"/>'),
  moon: acSvg('<path d="M19.5 14.5A8 8 0 1 1 9.5 4.5 6.5 6.5 0 0 0 19.5 14.5z"/>'),
  swingH: acSvg('<rect x="3.5" y="3.5" width="17" height="6.5" rx="2"/><path d="M6 13.5l-1.5 6M10 13.5l-.8 6M14 13.5l.8 6M18 13.5l1.5 6"/>'),
  swingV: acSvg('<rect x="3" y="3.5" width="7.5" height="17" rx="2"/><path d="M14.5 6l5.5-1.2M14.5 10h6M14.5 14h6M14.5 18l5.5 1.2"/>'),
  fixH: acSvg('<rect x="3.5" y="3.5" width="17" height="6.5" rx="2"/><path d="M12 13.5v6M7.5 13.5l-2 6M16.5 13.5l2 6"/>'),
  fixV: acSvg('<rect x="3" y="3.5" width="7.5" height="17" rx="2"/><path d="M14.5 12h6M14.5 8l5-2M14.5 16l5 2"/>'),
  heater: acSvg('<path d="M4 7.5c2-2 3 2 5 0s3 2 5 0 3 2 5 0M4 12c2-2 3 2 5 0s3 2 5 0 3 2 5 0M7 16v4M12 16v4M17 16v4"/>'),
  thermo: acSvg('<path d="M10 14.5V5a2 2 0 0 1 4 0v9.5a4 4 0 1 1-4 0z"/>'),
  smart: acSvg('<path d="M12 3 4.5 7.5v9L12 21l7.5-4.5v-9z"/><path d="M12 12 4.7 7.7M12 12l7.3-4.3M12 12v9"/>'),
};

const acHist = {};   // entity -> { t, data }

function acVal(id) { return id ? st(id).s : ""; }

function acFlags(d) {
  const x = d.ac || {}, a = st(d.roles.main).a, dir = acVal(x.wind_dir), swing = a.swing_mode || "off";
  return {
    fav: !!x.favorite && isOn(x.favorite), eco: !!x.eco && isOn(x.eco), sleep: !!x.sleep && isOn(x.sleep),
    heater: !!x.heater && isOn(x.heater), wind: !!x.wind_sw && isOn(x.wind_sw),
    dir, nodirect: dir === "防直吹", swingH: swing === "horizontal" || swing === "both", swingV: swing === "vertical" || swing === "both",
    hfix: !!x.hpos && acVal(x.hpos) !== "定格关闭" && !dead(acVal(x.hpos)), vfix: !!x.vpos && acVal(x.vpos) !== "定格关闭" && !dead(acVal(x.vpos)),
  };
}

function acTimerLeft(d) {
  const t = acVal((d.ac || {}).timer);
  const p = (t || "").split(",");
  return p[0] === "1" ? parseInt(p[3] || p[1], 10) || 0 : 0;   // minutes left, 0 = off
}
const acHM = m => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

function renderAc(d) {
  const x = d.ac || {}, id = d.roles.main, s = st(id), a = s.a, on = s.s !== "off", f = on ? acFlags(d) : Object.fromEntries(Object.keys(acFlags(d)).map(k => [k, k === "dir" ? "关" : false]));
  const step = a.target_temp_step || 0.5, cf = comfortOf(id), cfOn = !!(cf && cf.on && on), desk = deskTemp(d);
  const timerMin = acTimerLeft(d);
  const lvl = a.fan_mode && AC_FAN.indexOf(a.fan_mode) >= 0 ? AC_FAN.indexOf(a.fan_mode) + 1 : parseInt((acVal(x.fan_lever) || "").replace(/\D/g, ""), 10) || 1;
  const fanAuto = !a.fan_mode || a.fan_mode === "自动";
  const tags = [f.fav && "喜好", f.eco && "灵云节能", f.wind && f.dir !== "关" && f.dir, f.nodirect && "防直吹", f.sleep && "灵云睡眠",
    f.swingH && "左右扫风", f.swingV && "上下扫风", f.hfix && "左右定格", f.vfix && "上下定格", f.heater && "辅热", cfOn && "恒温"].filter(Boolean);
  const big = on && cfOn ? cf.target : a.temperature;
  const cl = acCleanOf(d), run = acRun(d), gauge = run.live ? Math.max(run.progress, 3) : cl ? Math.max(Math.round(cl.progress * 100), 3) : 0;
  const gaugeText = run.live ? run.text : cl ? cl.text : "";
  const circ = (cls, ico, text, attrs, arrow) => `<button class="rb ${cls}" ${attrs}><span class="rc">${ico}</span><span class="rt">${text}${arrow ? "<i></i>" : ""}</span></button>`;
  const arrowBtn = (act, arg = "") => `<span class="rarrow" data-ac="${act}|${arg}"></span>`;
  const tile = (cls, ico, text, act, arrow, dis) => `<div class="rbw ${dis ? "dis" : ""}"><button class="rb ${cls}" data-ac="${act}">${`<span class="rc">${ico}</span><span class="rt">${text}</span>`}</button>${arrow ? arrowBtn(arrow) : ""}</div>`;
  const noWind = !x.wind_sw && !x.wind_dir;
  const grid = [
    x.eco && tile(f.eco ? "on" : "", AI.eco, "灵云节能", "eco"),
    !noWind && tile(f.wind ? "on" : "", AI.wind, "风感", "wind", "wind-pick"),
    x.wind_dir && tile(f.nodirect ? "on" : "", AI.nodirect, "防直吹", "nodirect", "", s.s !== "cool"),
    x.sleep && tile(f.sleep ? "on" : "", AI.moon, "灵云睡眠", "sleep", x.sleep_state ? "sleep-pick" : ""),
    tile(f.swingH ? "on" : "", AI.swingH, "左右扫风", "swing-h"),
    tile(f.swingV ? "on" : "", AI.swingV, "上下扫风", "swing-v"),
    x.hpos && tile(f.hfix ? "on" : "", AI.fixH, "左右定格", "fix-h", "fix-h"),
    x.vpos && tile(f.vfix ? "on" : "", AI.fixV, "上下定格", "fix-v", "fix-v"),
    x.heater && tile(f.heater ? "on" : "", AI.heater, "辅热", "heater"),
    desk != null && tile(cfOn ? "on" : "", AI.thermo, "恒温", "comfort"),
  ].filter(Boolean).join("");
  const tint = !on ? "off" : s.s === "cool" || s.s === "dry" ? "cool" : s.s === "heat" ? "heat" : "wind";
  $("#app").innerHTML = `
    <div class="page ac ${tint}" style="--c:${COLORS[d.kind]}">
      <div class="bar"><button class="back" data-act="back">${icon("back")}</button>
        <div class="actitle"><div class="t">${esc(d.name)}</div>${timerMin ? `<div class="r">${acHM(timerMin)}后关机</div>` : ""}</div></div>
      <div class="ac-hero">
        <div class="ac-pic ${on ? "on" : ""} ${run.live ? "cleaning" : cl?.due ? "due" : ""}">
          <div class="acbody"><i class="acfill" style="width:${gauge}%"></i><i class="aclogo"></i><b class="acdisp">${on && big != null ? fmtNum(big) : ""}</b>
            ${cl ? `<div class="acgauge"><small>${esc(gaugeText)}</small><button data-ac="${run.live ? "clean-stop" : "clean"}">${run.live ? "停止清洁" : "一键自清洁"}</button></div>` : ""}</div>
          <div class="acwind"></div></div>
      </div>
      <div class="accard acctl ${on && !run.live ? "" : "idle"}">
        <div class="actags">${(run.live ? [run.text] : on ? tags : [a.current_temperature != null ? `已关机 · 室温 ${fmtNum(a.current_temperature)}°` : "已关机"]).map(esc).join('<i>|</i>') || "&nbsp;"}</div>
        <div class="acset"><button class="pm" ${on ? "" : "disabled"} ${cfOn ? `data-ctemp="${id}"` : `data-temp="${id}"`} data-d="${-step}">−</button>
          <div class="acv">${big != null ? fmtNum(big) : "--"}<small>°C</small></div>
          <button class="pm" ${on ? "" : "disabled"} ${cfOn ? `data-ctemp="${id}"` : `data-temp="${id}"`} data-d="${step}">+</button></div>
        ${cfOn ? `<div class="accf">恒温：保持${esc(d.room)}桌面 ${fmtNum(cf.target)}°（现在 ${desk != null ? desk.toFixed(1) : "—"}°，空调设定 ${fmtNum(a.temperature)}°）</div>` : ""}
        <div class="acfan"><div class="fanbar" style="--p:${((lvl - 1) / 7) * 100}%"><input type="range" min="1" max="8" step="1" value="${lvl}" data-ac-fan ${on ? "" : "disabled"}>
            <span>风速 ${fanAuto ? "" : lvl === 8 ? "Max" : lvl}${fanAuto ? lvl : ""}</span></div>
          <button class="fanauto ${fanAuto && on ? "on" : ""}" data-ac="fan-auto">自动</button></div>
      </div>
      <div class="accard row4">
        ${tile(on ? "pwr on" : "", AI.power, on ? "关机" : "开机", "power")}
        ${x.favorite ? tile(f.fav ? "on" : "", AI.fav, "喜好", "fav", "fav-pick") : ""}
        ${x.timer ? tile(timerMin ? "on" : "", AI.clock, "定时", "timer") : ""}
        ${tile(on ? "on" : "", AI[s.s] || AI.auto, on ? AC_LABEL[s.s] || "模式" : "模式", "mode")}
      </div>
      <div class="accard grid4">${grid}</div>
      <div class="acrow2"><button data-act="nav-smart"><span class="blk">${AI.smart}</span>空调智能</button></div>
      <div class="accard filt"><span class="fl">${x.filter_life && acVal(x.filter_life) !== "101" && !dead(acVal(x.filter_life)) ? acVal(x.filter_life) + "%" : "--%"}</span>
        <div><b>米家空调健康滤网</b><small>${x.filter_life && acVal(x.filter_life) !== "101" && !dead(acVal(x.filter_life)) ? "剩余寿命" : "未安装"}</small></div></div>
      <div class="accard" id="ac-env"></div>
      <div class="accard" id="ac-kwh"></div>
    </div>`;
  acCharts(d);
}

// ---- charts (filled from /api/ac/history, kept for a minute)
async function acCharts(d) {
  const id = d.roles.main, h = acHist[id];
  const paint = () => {
    const env = $("#ac-env"), kwh = $("#ac-kwh");
    if (!env || !acHist[id]?.data) return;
    const { temp } = acHist[id].data, cur = st(id).a.current_temperature;
    const verdict = cur == null ? "" : cur > 26 ? "温度偏高" : cur < 20 ? "温度偏低" : "温度适宜";
    env.innerHTML = `<h3>室内环境</h3><div class="envk">室内温度</div>
      <div class="envv">${cur != null ? fmtNum(cur) : "--"}<small>℃</small><em>${verdict}</em></div>${acLine(temp)}
      <p>适宜的温度能稳定人体代谢功能、脉搏以及呼吸，避免因温度过低导致的皮下血管收缩，皮肤过度紧张，呼吸道粘膜的抵抗力减弱，从而避免诱发呼吸道疾病，化霜时温度偏低属于正常现象</p>`;
    kwh.innerHTML = acStats(acHist[id].data.daily || {});
  };
  paint();
  if (h && Date.now() - h.t < 60000) return;
  try {
    const data = await api("/api/ac/history?entity_id=" + encodeURIComponent(id));
    acHist[id] = { t: Date.now(), data };
    paint();
  } catch (e) { /* the page works without the charts */ }
}

function acLine(pts) {
  if (!pts || pts.length < 2) return `<div class="envc empty">还没有记录</div>`;
  const t1 = Date.now(), t0 = t1 - 24 * 36e5, vs = pts.map(p => p[1]), lo = Math.min(...vs) - 1, hi = Math.max(...vs) + 1;
  const X = t => 8 + ((t - t0) / (t1 - t0)) * 284, Y = v => 90 - ((v - lo) / (hi - lo)) * 80;
  const path = pts.map((p, i) => `${i ? "L" : "M"}${X(Math.max(t0, new Date(p[0]).getTime())).toFixed(1)} ${Y(p[1]).toFixed(1)}`).join("") + ` L292 ${Y(vs[vs.length - 1]).toFixed(1)}`;
  const ticks = [0, 4, 8, 12, 16, 20].map(h => { const t = new Date(t0 + h * 36e5); return `<span>${String(t.getHours()).padStart(2, "0")}:00</span>`; }).join("");
  return `<div class="envc"><svg viewBox="0 0 300 100" preserveAspectRatio="none"><path d="${path}" fill="none" stroke="#2aa7ff" stroke-width="2" vector-effect="non-scaling-stroke"/></svg><div class="envt">${ticks}</div></div>`;
}

// ---- actions
function acSheet(html) { sheet(html, "acs"); }

function acSelect(id, opt) { call(id, "select_option", { option: opt }, { s: opt }); }
function acSwitch(id, on) { call(id, on ? "turn_on" : "turn_off", {}, { s: on ? "on" : "off" }); }

function acModeSheet(d) {
  const id = d.roles.main, a = st(id).a, cur = st(id).s;
  const modes = ["cool", "dry", "fan_only", "heat"].filter(m => (a.hvac_modes || []).includes(m));
  acSheet(`<div class="acsh"><button class="x" data-act="sheet-close">✕</button><h3>模式选择</h3></div>
    <div class="cgrid">${modes.map(m => `<button class="ci ${cur === m ? "on" : ""}" data-svc="${id}|set_hvac_mode|hvac_mode|${m}"><span class="rc">${AI[m]}</span><span class="rt">${AC_LABEL[m]}</span></button>`).join("")}</div>`);
}

function acPickSheet(title, id, opts, cur, labels = {}, offOpt, ico) {
  acSheet(`<div class="acsh"><button class="x" data-act="sheet-close">✕</button><h3>${title}</h3></div>
    <div class="cgrid">${opts.map(o => `<button class="ci ${cur === o ? "on" : ""}" data-svc="${id}|select_option|option|${esc(cur === o && offOpt ? offOpt : o)}"><span class="rc">${ico || ""}</span><span class="rt">${esc(labels[o] || o)}</span></button>`).join("")}</div>`);
}

function acTimerSheet(d, hours) {
  const x = d.ac, left = acTimerLeft(d);
  hours = hours || 0.5;
  const opts = []; for (let h = 0.5; h <= 12; h += 0.5) opts.push(h);
  acSheet(`<div class="acsh"><h3>倒计时关机</h3><p class="sub">${left ? "已开启 · " + acHM(left) + "后关机" : "未开启"}</p></div>
    <div class="hpick">${opts.map(h => `<button class="${h === hours ? "on" : ""}" data-ac="timer-h|${h}">${h}<small>小时</small></button>`).join("")}</div>
    <div class="sheet-btns"><button data-act="sheet-close">返回</button>
      ${left ? `<button class="pri danger" data-ac="timer-off">关闭定时</button>` : ""}<button class="pri" data-ac="timer-ok|${hours}">确定</button></div>`);
  $("#sheet .hpick .on")?.scrollIntoView({ block: "center" });
}

function acFixSheet(d, axis) {
  const x = d.ac, id = axis === "h" ? x.hpos : x.vpos, cur = acVal(id), opts = (st(id).a.options || []).filter(o => o !== "定格关闭");
  acSheet(`<div class="acsh"><button class="x" data-act="sheet-close">✕</button><h3>${axis === "h" ? "左右定格" : "上下定格"}</h3></div>
    <div class="cgrid">${opts.map(o => `<button class="ci ${cur === o ? "on" : ""}" data-svc="${id}|select_option|option|${cur === o ? "定格关闭" : esc(o)}"><span class="rc">${axis === "h" ? AI.fixH : AI.fixV}</span><span class="rt">${esc(o)}</span></button>`).join("")}</div>`);
}

document.addEventListener("click", e => {
  const t = e.target.closest("[data-ac]");
  if (!t) return;
  e.stopPropagation();
  const d = currentDevice();
  if (!d || !d.ac) return;
  const [act, arg] = t.dataset.ac.split("|");
  const x = d.ac, id = d.roles.main, s = st(id), a = s.a, f = acFlags(d);
  const on = s.s !== "off";
  switch (act) {
    case "power": return toggle(id);
    case "mode": return acModeSheet(d);
    case "fav": return acSwitch(x.favorite, !f.fav);
    case "fav-pick": return acPickSheet("喜好", x.favorite_type, st(x.favorite_type).a.options || [], acVal(x.favorite_type), AC_FAVTYPE);
    case "eco": return acSwitch(x.eco, !f.eco);
    case "heater": return acSwitch(x.heater, !f.heater);
    case "wind":
      if (f.wind) { acSwitch(x.wind_sw, false); if (x.wind_dir) acSelect(x.wind_dir, "关"); }
      else { acSwitch(x.wind_sw, true); if (x.wind_dir && (f.dir === "关" || f.nodirect)) acSelect(x.wind_dir, "下吹风"); }
      return;
    case "wind-pick": return acPickSheet("风感", x.wind_dir, ["上吹风", "下吹风", "循环风"], f.dir);
    case "nodirect":
      if (s.s !== "cool") return toast("防直吹只在制冷模式下可用");
      return acSelect(x.wind_dir, f.nodirect ? "关" : "防直吹");
    case "sleep": return acSwitch(x.sleep, !f.sleep);
    case "sleep-pick": return acPickSheet("灵云睡眠", x.sleep_state, ["Standard Sleep", "Lingyun Sleep"], acVal(x.sleep_state), AC_SLEEP);
    case "swing-h": case "swing-v": {
      const h = act === "swing-h" ? !f.swingH : f.swingH, v = act === "swing-v" ? !f.swingV : f.swingV;
      const mode = h && v ? "both" : h ? "horizontal" : v ? "vertical" : "off";
      return call(id, "set_swing_mode", { swing_mode: mode }, { a: { ...a, swing_mode: mode } });
    }
    case "fix-h": return acFixSheet(d, "h");
    case "fix-v": return acFixSheet(d, "v");
    case "fan-auto": return call(id, "set_fan_mode", { fan_mode: "自动" }, { a: { ...a, fan_mode: "自动" } });
    case "timer": return acTimerSheet(d);
    case "timer-h": return acTimerSheet(d, parseFloat(arg));
    case "timer-ok": {
      const m = Math.round(parseFloat(arg) * 60);
      call(x.timer, "set_value", { value: `1,${m},0,${m}` }, { s: `1,${m},0,${m}` });
      closeSheet(); return;
    }
    case "timer-off": call(x.timer, "set_value", { value: "0" }, { s: "0" }); closeSheet(); return;
    case "comfort":
      if (!on) return toast("先打开空调");
      return comfortSend(id, !comfortOf(id)?.on);
  }
}, true);

document.addEventListener("change", e => {
  if (!e.target.matches("[data-ac-fan]")) return;
  const d = currentDevice(); if (!d) return;
  const id = d.roles.main, mode = AC_FAN[parseInt(e.target.value, 10) - 1];
  call(id, "set_fan_mode", { fan_mode: mode }, { a: { ...st(id).a, fan_mode: mode } });
});


// ---- 电量统计: 周 / 月 / 年, from the per-day totals HomeHub records (it keeps them longer than HA does)
function acStats(daily) {
  const tab = S.acTab || "week", now = new Date(), pad = n => String(n).padStart(2, "0");
  const ymd = t => `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())}`;
  let bars = [];
  if (tab === "year") {
    bars = [...Array(12)].map((_, i) => { const t = new Date(now.getFullYear(), now.getMonth() - 11 + i, 1), k = `${t.getFullYear()}-${pad(t.getMonth() + 1)}`;
      const rows = Object.entries(daily).filter(([d]) => d.startsWith(k)).map(([, v]) => v);
      return { label: `${t.getMonth() + 1}月`, kwh: rows.reduce((n, v) => n + v.kwh, 0), hours: rows.reduce((n, v) => n + v.hours, 0) }; });
  } else {
    const n = tab === "month" ? 30 : 7;
    bars = [...Array(n)].map((_, i) => { const t = new Date(now.getTime() - (n - 1 - i) * 864e5), v = daily[ymd(t)] || { kwh: 0, hours: 0 };
      return { label: tab === "month" && i % 5 !== 4 ? "" : `${pad(t.getMonth() + 1)}/${pad(t.getDate())}`, kwh: v.kwh, hours: v.hours }; });
  }
  const sum = k => bars.reduce((n, b) => n + b[k], 0), top = Math.max(...bars.map(b => b.kwh), 0.01);
  const tabs = [["week", "周"], ["month", "月"], ["year", "年"]].map(([k, l]) => `<span class="${tab === k ? "on" : ""}" data-ac="stat-tab|${k}">${l}</span>`).join("");
  return `<h3>电量统计</h3><div class="seg">${tabs}</div>
    <div class="kw2"><div><b>${sum("kwh").toFixed(2)}</b><small>运行用电(度)</small></div><div><b>${sum("hours").toFixed(1)}</b><small>累计运行时长(时)</small></div></div>
    <div class="bars ${bars.length > 12 ? "thin" : ""}">${bars.map(b => `<div><i style="height:${Math.round((b.kwh / top) * 100)}%"></i><span>${b.label}</span></div>`).join("")}</div>
    ${tab !== "week" ? `<p class="statnote">从 HomeHub 开始记录的那天起累计，更早的数据只在米家里。</p>` : ""}`;
}

// ---- 自清洁: a progress bar like the washer's, one tap to start
const acCleanOf = d => (S.model.ac_clean || {})[d.roles.main];
function acCleanFill(d) {   // the home card fills from the left as the 90 days pass
  const c = acCleanOf(d);
  if (!c || !c.progress) return "";
  const pct = Math.round(c.progress * 100);
  return `;background-image:linear-gradient(90deg, rgba(47,128,237,${(.14 + .26 * c.progress).toFixed(2)}) ${pct}%, transparent ${pct}%)`;
}
const AC_STAGES = ["内机凝露", "内机结霜", "内机化霜", "内机烘干", "外机清洁"];
function acRun(d) {   // what the unit says about a clean in progress ("1,35,2,1")
  const p = ((acVal((d.ac || {}).clean) || "").split(","));
  const live = p.length === 4 && p[0] === "1";
  const progress = live ? parseInt(p[1], 10) || 0 : 0, stage = live ? parseInt(p[2], 10) || 1 : 0, stopping = live && p[3] === "0";
  return { live, progress, stage, stopping, text: live ? (stopping ? "正在结束清洁…" : `清洁中 ${progress}% · ${AC_STAGES[stage - 1] || ""}`) : "" };
}
function acCleanSheet(d) {
  const c = acCleanOf(d) || {};
  acSheet(`<div class="acsh"><button class="x" data-act="sheet-close">✕</button><h3>一键自清洁</h3></div>
    <p class="clnote">自动清洁会让室内机先凝露、结霜、化霜、烘干，再清洁外机，把换热器上的灰冲掉，出风更干净。<br>
    <b>要先关机</b>，全程约 65 分钟，期间空调其他功能都不能用。夜里清洁室内机会有些风声和水声。<br>
    ${c.from_baseline ? "" : `按运行时间算，现在是 ${c.text}。`}</p>
    <div class="sheet-btns"><button data-ac="clean-done">我已在米家清洁过</button><button class="pri" data-ac="clean-go">开始清洁</button></div>`);
}
async function acCleanPost(d, action) {
  try {
    const r = await api("/api/ac/clean", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ entity_id: d.roles.main, action }) });
    if (r.ac_clean) S.model.ac_clean = r.ac_clean;
    closeSheet(); render();
    toast({ done: "已记下：刚刚清洁过", start: "已开始自清洁，约 65 分钟", stop: "正在停止清洁…" }[action]);
  } catch (e) { closeSheet(); toast(String(e.message || e).replace(/^.*?:\s*/, "")); }
}

document.addEventListener("click", e => {
  const t = e.target.closest("[data-ac]");
  if (!t) return;
  const [act, arg] = t.dataset.ac.split("|");
  if (act === "stat-tab") { e.stopPropagation(); S.acTab = arg; const d = currentDevice(); if (d) acCharts(d); return; }
  const d = currentDevice();
  if (!d || !d.ac) return;
  if (act === "clean") { e.stopPropagation(); return acCleanSheet(d); }
  if (act === "clean-go") { e.stopPropagation(); return acCleanPost(d, "start"); }
  if (act === "clean-done") { e.stopPropagation(); return acCleanPost(d, "done"); }
  if (act === "clean-stop") { e.stopPropagation(); return acCleanPost(d, "stop"); }
}, true);
