// Baseboard heater page (kind "heater", xiaomi.heater.ma9), laid out like Mijia's: a power card, 目标温度 (slider +/−),
// 延时关闭, 安睡模式 (the unit's own custom-sleep programme), 定时 (kept as Home Assistant automations, so they keep
// running without the cloud) and 加湿模式 (the humidifier module: on/off + gear 1/2). No fan / swing / AC extras.
// The unit's own properties come in d.ac (HEATER_EXTRA in home.py). Same cards and sheets as the AC and water-heater pages.

const HT_TAG = /\[ht:([^\]]*)\]/;
const HT_WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], HT_WEEK_CN = ["一", "二", "三", "四", "五", "六", "日"];
const htHM = m => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

// ---- drawing without flicker: a page is built as HTML, then MORPHED into what is already on screen, so the elements stay
// the same nodes and CSS transitions run (a switch slides, an arc grows, a number ticks) instead of everything being replaced.
function htMorph(a, b) {
  if (a.classList.contains("hs-in")) b.classList.add("hs-in");      // an entrance animation still running must not be cut
  for (const at of [...a.attributes]) if (!b.hasAttribute(at.name)) a.removeAttribute(at.name);
  for (const at of b.attributes) if (a.getAttribute(at.name) !== at.value) a.setAttribute(at.name, at.value);
  if (a.tagName === "INPUT" && a.value !== b.value) a.value = b.value;
  const ac = [...a.childNodes], bc = [...b.childNodes];
  bc.forEach((y, i) => {
    const x = ac[i];
    if (!x) a.appendChild(y);
    else if (x.nodeType !== y.nodeType || x.nodeName !== y.nodeName) a.replaceChild(y, x);
    else if (x.nodeType === 3) {
      if (x.data !== y.data) {
        x.data = y.data;
        const el = a.closest("[data-tick]");
        if (el) { el.classList.remove("tick"); void el.getBoundingClientRect(); el.classList.add("tick"); }
      }
    } else if (x.nodeType === 1) htMorph(x, y);
  });
  for (let i = ac.length - 1; i >= bc.length; i--) a.removeChild(ac[i]);
}
// Sub pages slide in from the right (and out to the right when going back); the page underneath drifts away. depth tells which way.
const HT_DEPTH = { main: 0, sleep: 1, timer: 1, zones: 2, tedit: 2 }, HT_SCROLL = {};
let htDir = 1;
function htMount0(html, key) {
  const app = $("#app"), tpl = document.createElement("template");
  tpl.innerHTML = html.trim();
  const next = tpl.content.firstElementChild, cur = app.querySelector(":scope > [data-hpage]");
  if (cur && cur.dataset.hpage === key) return htMorph(cur, next);
  if (!cur) { next.classList.add("hs-in"); app.replaceChildren(next); return; }       // coming from the home page: the page's own entrance
  // the app's own page transition (app.js pageGhost / slidePages, the one the home page uses): a picture of the old page slides away
  // or drifts left while the new page slides in. Reusing it keeps one proven motion everywhere.
  const dir = (HT_DEPTH[key] ?? 0) >= (HT_DEPTH[cur.dataset.hpage] ?? 0) ? 1 : -1;
  HT_SCROLL[cur.dataset.hpage] = window.scrollY;
  const animate = window.pageGhost && window.slidePages && !matchMedia("(prefers-reduced-motion: reduce)").matches;
  $("#ghost")?.remove();
  const ghost = animate ? pageGhost() : null;
  app.replaceChildren(next);
  const y = dir > 0 ? 0 : (HT_SCROLL[key] || 0);
  window.scrollTo(0, y);
  requestAnimationFrame(() => window.scrollTo(0, y));                                  // after the app's own scroll restore
  if (ghost) slidePages(ghost, dir > 0);
}
// the + button of the 定时 list lives in #app itself (not in the sliding page: a transformed parent would pull a fixed button along)
function htFabSync(show) {
  const app = $("#app"), f = app.querySelector(":scope > .trFab");
  if (show && !f) app.insertAdjacentHTML("beforeend", `<button class="trFab" data-tr="add" aria-label="添加定时"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/></svg></button>`);
  else if (show && f) f.classList.remove("out");
  else if (!show && f && !f.classList.contains("out")) { f.classList.add("out"); setTimeout(() => f.remove(), 300); }
}
function htMount(html, key) { htMount0(html, key); htFabSync(key === "timer"); }
const HT_PARENT = { zones: "sleep", tedit: "timer" };
function htBack() {
  const pg = S.htPage;
  if (!pg) return false;
  const par = HT_PARENT[pg.view];
  if (par) pg.view = par; else S.htPage = null;
  render();
  return true;
}

function heaterState(d) {
  const s = st(d.roles.main), a = s.a, on = s.s !== "off", cur = a.current_temperature, tgt = a.temperature;
  if (!on) return { on, text: "已关机" + (cur != null ? ` · 室温 ${fmtNum(cur)}°` : "") };
  const heating = a.hvac_action === "heating" || (a.hvac_action == null && cur != null && tgt != null && cur < tgt);
  return { on, heating, text: heating ? "加热中" : "已到温 · 保温中" };
}

// ---- 安睡模式: "{sleepStage:[{t:24,d:1},...],postAction:{a:off,d:0},repeatA:[...],repeat:8,startTime:23-00}" (not JSON)
function htParseSleep(text) {
  const t = text || "";
  const stages = [...t.matchAll(/\{t:(-?\d+),d:([\d.]+)\}/g)].map(m => ({ t: +m[1], d: +m[2] }));
  const post = (t.match(/postAction:\{a:(\w+),d:(\d+)\}/) || [])[1] || "none";
  const s0 = t.match(/startTime:(\d+)-(\d+)/);
  const rep = (t.match(/repeatA:\[([\d,]*)\]/) || [])[1];
  const once = (t.match(/,repeat:(\d+)/) || [])[1];
  return { raw: t, stages, post, start: s0 ? +s0[1] * 60 + +s0[2] : 23 * 60, days: rep ? rep.split(",").filter(Boolean).map(Number) : [1, 2, 3, 4, 5, 6, 7], repeat: once != null ? +once : 8 };
}
function htWriteSleep(d, p) {
  const pad = n => String(n).padStart(2, "0");
  const t = p.raw.replace(/sleepStage:\[.*?\](?=,postAction)/, `sleepStage:[${p.stages.map(s => `{t:${s.t},d:${s.d}}`).join(",")}]`)
    .replace(/postAction:\{a:\w+,d:\d+\}/, `postAction:{a:${p.post},d:0}`)
    .replace(/repeatA:\[[\d,]*\]/, `repeatA:[${p.days.join(",")}]`).replace(/,repeat:\d+/, `,repeat:${p.repeat}`)
    .replace(/startTime:\d+-\d+/, `startTime:${pad(Math.floor(p.start / 60))}-${pad(p.start % 60)}`);
  call(d.ac.sleep_cfg, "set_value", { value: t }, { s: t });
}
// ---- the page
function renderHeater(d) {
  if (S.htPage && S.htPage.id === d.roles.main && window.htSleepPage) {      // sub pages (heater_sleep.js, heater_timer.js)
    const v = S.htPage.view;
    return v === "zones" ? htZonesPage(d) : v === "timer" ? htTimerPage(d) : v === "tedit" ? htTimerEdit(d) : htSleepPage(d);
  }
  const x = d.ac || {}, id = d.roles.main, s = st(id), a = s.a, h = heaterState(d), on = h.on;
  const step = a.target_temp_step || 1, lo = a.min_temp ?? 16, hi = a.max_temp ?? 28, tgt = a.temperature, cur = a.current_temperature;
  const dim = on ? "" : "idle";
  const delayOn = x.delay_on && isOn(x.delay_on), delayH = x.delay_time ? num(x.delay_time) : null;
  const sleepOn = x.sleep_on && isOn(x.sleep_on), sleepLive = x.sleep_active && isOn(x.sleep_active);
  const timers = htTimers(d), tOn = timers.filter(t => t.on);
  const humOn = x.hum_on && isOn(x.hum_on), gears = x.hum_gear ? (st(x.hum_gear).a.options || []) : [], gear = x.hum_gear ? st(x.hum_gear).s : "";
  const row = (ico, title, sub, tail, attrs = "") => `<div class="row pv" style="--c:${COLORS.heater}" ${attrs}><div class="pv-ico">${ico}</div><div class="l">${title}${sub ? `<div class="rs">${sub}</div>` : ""}</div>${tail}</div>`;
  htMount(`<div data-hpage="main" class="page ac ${on ? "heat" : "off"}" style="--c:${COLORS.heater}">
      <div class="bar"><button class="back" data-act="back">${icon("back")}</button>
        <div class="actitle"><div class="t">${esc(d.name)}</div><div class="r">${esc([d.room, d.kind_name].filter(Boolean).join(" · "))}</div></div></div>
      <div class="hero ${on && h.heating ? "active" : ""}">
        ${window.deviceArt && deviceArt(d) ? `<div class="big-ico art">${deviceArt(d)}</div>` : `<div class="big-ico">${icon("heater")}</div>`}
        <div class="state">${esc(h.text)}</div>
        <div class="state-sub">${cur != null ? `室温 ${fmtNum(cur)}°` : ""}</div>
      </div>
      <div class="accard htpower" data-ht="power">
        <button class="htpw ${on ? "on" : "off"}" aria-label="${on ? "关闭" : "开启"}">${AI.power}</button>
        <div class="htpt"><b>${on ? "关闭" : "开启"}</b><small>${on ? "点一下关机" : "点一下开机"}</small></div></div>
      <div class="accard acctl ${dim}">
        <div class="htt"><b>目标温度</b><span>${tgt != null ? `加热到 ${fmtNum(tgt)}° 后进入保温` : ""}</span></div>
        <div class="htset"><button class="pm" ${!on || tgt == null || tgt <= lo ? "disabled" : ""} data-temp="${id}" data-d="${-step}">−</button>
          <div class="fanbar" style="--p:${tgt != null ? ((tgt - lo) / (hi - lo)) * 100 : 0}%"><input type="range" min="${lo}" max="${hi}" step="${step}" value="${tgt ?? lo}" data-ht-range ${on ? "" : "disabled"}><span>${tgt != null ? fmtNum(tgt) : "--"}</span></div>
          <button class="pm" ${!on || tgt == null || tgt >= hi ? "disabled" : ""} data-temp="${id}" data-d="${step}">+</button></div>
      </div>
      ${x.delay_on ? `<div class="accard ${dim}">
        ${row(AI.clock, "延时关闭", "", `<button class="sw ${delayOn ? "on" : ""}" style="--c:${COLORS.heater}" data-ht="delay-sw"></button>`)}
        ${x.delay_time ? row(AI.clock, "延迟时间", "", `<div class="rv">${delayH != null ? delayH : "--"} 小时</div><span class="chev">›</span>`, `data-ht="delay-time"`) : ""}</div>` : ""}
      ${x.sleep_cfg ? `<div class="accard ${dim}">${row(AI.moon, "安睡模式", sleepLive ? "正在运行" : sleepOn ? "已开启" : "未开启", `<span class="chev">›</span>`, `data-ht="sleep"`)}</div>` : ""}
      <div class="accard">${row(AI.clock, "定时", tOn.length ? `${tOn.length} 个定时开着` : "未设置", `<span class="chev">›</span>`, `data-ht="timer"`)}</div>
      ${x.hum_on ? `<div class="accard ${dim}"><div class="htt"><b>加湿模式</b>
          <button class="htsw ${humOn ? "on" : ""}" data-ht="hum-sw" aria-label="加湿开关">${AI.power}</button></div>
        <div class="htgear">${gears.map((g, i) => `<button class="${humOn && gear === g ? "on" : ""}" data-ht="hum-gear|${esc(g)}">${i + 1}</button>`).join("")}</div></div>` : ""}
    </div>`, "main");
}

document.addEventListener("click", e => {
  const t = e.target.closest("[data-ht]");
  if (!t) return;
  const d = currentDevice();
  if (!d || d.kind !== "heater" || !d.ac) return;
  e.stopPropagation();
  const [act, arg] = t.dataset.ht.split("|");
  const x = d.ac, id = d.roles.main, a = st(id).a, on = st(id).s !== "off";
  const sw = (eid, v) => call(eid, v ? "turn_on" : "turn_off", {}, { s: v ? "on" : "off" });
  const rng = () => [a.min_temp ?? 16, a.max_temp ?? 28, 1];
  switch (act) {
    case "power": return call(id, on ? "turn_off" : "turn_on", {}, { s: on ? "off" : "heat" });
    case "delay-sw": return sw(x.delay_on, !isOn(x.delay_on));
    case "delay-time": {
      const n = st(x.delay_time).a;
      return kPick("延迟时间", kRange([n.min ?? 0, n.max ?? 12, n.step || 1]), num(x.delay_time) ?? 1, "小时", v => call(x.delay_time, "set_value", { value: v }, { s: String(v) }));
    }
    case "hum-sw": return sw(x.hum_on, !isOn(x.hum_on));
    case "hum-gear":
      if (!isOn(x.hum_on)) sw(x.hum_on, true);
      return call(x.hum_gear, "select_option", { option: arg }, { s: arg });
    case "sleep": S.htPage = { id, view: "sleep" }; return render();
    case "timer": S.htPage = { id, view: "timer" }; return render();
  }
}, true);

// the target-temperature pill: the label follows the finger, sent on release
document.addEventListener("input", e => {
  const r = e.target.closest("[data-ht-range]");
  if (!r) return;
  busy = 1;
  r.parentElement.style.setProperty("--p", ((r.value - r.min) / (r.max - r.min)) * 100 + "%");
  r.nextElementSibling.textContent = r.value;
});
document.addEventListener("change", e => {
  const r = e.target.closest("[data-ht-range]");
  if (!r) return;
  busy = 0;
  const d = currentDevice(); if (!d) return;
  const id = d.roles.main, v = parseFloat(r.value);
  call(id, "set_temperature", { temperature: v }, { a: { ...st(id).a, temperature: v } });
});
