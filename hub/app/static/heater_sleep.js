// 安睡模式 pages of the baseboard heater, laid out like Mijia's: a switch card, 就寝 / 起床时间 (hour + minute wheels), 重复
// (每天 / 执行一次 / 自定义 weekdays), a ring of the four stages (tap an arc: it grows and its temperature wheel opens),
// 时间分区设置 (move the stage boundaries) and 设备状态 (what happens at the end).
// The unit keeps it all in one text property (parsed by htParseSleep in heater_ui.js); the page is a route-like view
// (S.htPage), so it redraws with every state change and the switch always shows what the unit says.

const HS_COLORS = ["#ff7a30", "#ff9a3c", "#ffb74d", "#ffd166"];
const HS_POST = { none: "保持开机", off: "立即关机" };
const HS_MIN_STAGE = 30, HS_MAX_TOTAL = 690;     // stages of at least 30 min, at most 11.5 h in all

const hsBounds = p => { const b = [0]; p.stages.forEach(s => b.push(b[b.length - 1] + Math.round(s.d * 60))); return b; };   // minutes from 就寝
const hsClock = m => htHM(((Math.round(m) % 1440) + 1440) % 1440);

function hsPolar(r, deg) { const a = (deg - 90) * Math.PI / 180; return [150 + r * Math.cos(a), 150 + r * Math.sin(a)]; }
const HS_R = 134, HS_r = 88;
function hsArc(m0, m1) {       // a donut piece between two minutes-of-day, inset so the round stroke makes soft corners; same command shape always (so it can animate)
  const gap = 2.6, a0 = ((m0 % 720) / 720) * 360 + gap, a1 = ((m0 % 720) / 720) * 360 + ((m1 - m0) / 720) * 360 - gap, k = 6, big = a1 - a0 > 180 ? 1 : 0;
  const f = n => n.toFixed(1), P = (r, deg) => hsPolar(r, deg).map(f).join(" ");
  return `M${P(HS_R - k, a0 + 2)}A${HS_R - k} ${HS_R - k} 0 ${big} 1 ${P(HS_R - k, a1 - 2)}L${P(HS_r + k, a1 - 2)}A${HS_r + k} ${HS_r + k} 0 ${big} 0 ${P(HS_r + k, a0 + 2)}Z`;
}
const HS_GRAD = [["#ff9158", "#ff6a1f"], ["#ffac5c", "#ff8a2b"], ["#ffc470", "#ffa535"], ["#ffe08a", "#ffc23d"]];
function hsRing(p, withTemp, faint, sel) {
  const b = hsBounds(p), s0 = p.start, total = b[b.length - 1], id = withTemp ? "m" : "z";
  const defs = `<defs>${HS_GRAD.map((g, i) => `<linearGradient id="hg${id}${i}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${g[0]}"/><stop offset="1" stop-color="${g[1]}"/></linearGradient>`).join("")}
    <radialGradient id="hf${id}" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#fff6ee"/></radialGradient></defs>`;
  const arc = (m0, m1, fill, extra = "") => { const d = hsArc(m0, m1); return `<path d="${d}" style="d:path('${d}')" fill="${fill}" stroke="${fill}" stroke-width="12" stroke-linejoin="round" ${extra}/>`; };
  const gray = arc(s0 + total, s0 + 720, "#eeeef2", 'class="hsRest"');
  const segs = p.stages.map((s, i) => {
    const m0 = s0 + b[i], m1 = s0 + b[i + 1], [lx, ly] = hsPolar((HS_R + HS_r) / 2, (((m0 + m1) / 2 % 720) / 720) * 360), fill = `url(#hg${id}${i})`;
    return `<g class="hsG ${i === sel ? "sel" : ""}" style="--k:${i}" ${withTemp ? `data-hs="seg|${i}"` : ""} ${faint ? 'opacity=".6"' : ""}>${arc(m0, m1, fill, 'class="hsSeg"')}` +
      (withTemp ? `<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="middle" dominant-baseline="central" class="hsT" data-tick>${s.t}<tspan class="hsU">°C</tspan></text>` : "") + `</g>`;
  }).join("");
  const face = `<circle cx="150" cy="150" r="${HS_r - 2}" fill="url(#hf${id})"/>`;
  const nums = [...Array(12).keys()].map(i => { const n = i || 12, [x, y] = hsPolar(63, i * 30); return (withTemp ? n % 3 === 0 : true) ? `<text x="${x.toFixed(1)}" y="${y.toFixed(1)}" text-anchor="middle" dominant-baseline="central" class="hsN ${n % 3 === 0 ? "big" : ""}">${n}</text>` : ""; }).join("");
  const ticks = [...Array(60).keys()].map(i => { const long = i % 5 === 0, [x0, y0] = hsPolar(long ? 80 : 83, i * 6), [x1, y1] = hsPolar(86, i * 6); return `<path d="M${x0.toFixed(1)} ${y0.toFixed(1)}L${x1.toFixed(1)} ${y1.toFixed(1)}" stroke="${long ? "#c9c9d1" : "#e2e2e8"}" stroke-width="${long ? 1.5 : 1}"/>`; }).join("");
  return `<svg class="hsRing" viewBox="0 0 300 300">${defs}${gray}${face}${ticks}${nums}${segs}</svg>`;
}
const hsRepeat = p => p.repeat === 0 ? "执行一次" : p.days.length >= 7 ? "每天" : "周" + [...p.days].sort().map(n => HT_WEEK_CN[n - 1]).join("、");

function htSleepPage(d) {
  const x = d.ac, on = isOn(x.sleep_on), live = x.sleep_active && isOn(x.sleep_active), p = htParseSleep(st(x.sleep_cfg).s);
  const b = hsBounds(p), wake = p.start + b[b.length - 1], sel = S.htPage?.sel ?? -1, tot = b[b.length - 1], hrs = `${Math.floor(tot / 60)} 小时${tot % 60 ? " " + (tot % 60) + " 分" : ""}`;
  htMount(`<div data-hpage="sleep" class="page hs ${on ? "hs-on" : ""} ${sel >= 0 ? "selecting" : ""}" style="--c:${COLORS.heater}">
      <div class="bar"><button class="back" data-hs="back">${icon("back")}</button></div>
      <h1 class="hsH">安睡模式</h1>
      <div class="accard hsCard"><div class="l"><b>安睡模式</b><div class="rs">${live ? "正在按设置的温度运行" : "开机状态下，电暖器将在设置时段按照您设定的温度工作"}</div></div>
        <button class="sw ${on ? "on" : ""}" style="--c:${COLORS.heater}" data-hs="sw" aria-label="安睡模式开关"></button></div>
      <div class="hsLab">时间设置</div>
      <div class="hsTimes">
        <button class="accard" data-hs="start"><small><i class="hsMoon"></i>就寝时间</small><b data-tick>${htHM(p.start)}</b></button>
        <button class="accard" data-hs="wake"><small><i class="hsSun"></i>起床时间</small><b data-tick>${hsClock(wake)}</b></button></div>
      <div class="accard hsRow" data-hs="repeat"><span>重复</span><em data-tick>${hsRepeat(p)}</em><span class="chev">›</span></div>
      <div class="accard hsRingCard" id="hsRingCard">${hsRing(p, true, false, sel)}<div class="hsHint">点一下圆弧，调整这个时段的温度<br>一共睡 <b data-tick>${hrs}</b></div></div>
      <div class="accard hsRow" data-hs="zones"><span>时间分区设置</span><em>${p.stages.length} 个时段</em><span class="chev">›</span></div>
      <div class="accard hsRow" data-hs="post"><span>设备状态</span><em data-tick>${HS_POST[p.post] || "保持开机"}</em><span class="chev">›</span></div>
    </div>`, "sleep");
}

function htZonesPage(d) {
  const p = htParseSleep(st(d.ac.sleep_cfg).s), b = hsBounds(p);
  htMount(`<div data-hpage="zones" class="page hs" style="--c:${COLORS.heater}">
      <div class="bar"><button class="back" data-hs="back">${icon("back")}</button><div class="actitle"><div class="t">时间分区设置</div></div></div>
      <p class="hsNote">可根据自己的睡眠习惯，自定义安睡模式中不同时段的长度。时段最少为 30 分钟，睡眠总时长不支持设置超过 11.5 个小时。</p>
      <div class="hsRingBig">${hsRing(p, false, true)}</div>
      <div class="hsLine"><i class="hsMoon"></i>${[1, 2, 3].map(() => "<u></u>").join("")}<i class="hsSun"></i></div>
      <div class="hsChips">${b.map((m, i) => `<button data-hs="bnd|${i}" data-tick>${hsClock(p.start + m)}</button>`).join("")}</div>
      <div class="hsCap"><span>就寝时间</span><span>起床时间</span></div>
    </div>`, "zones");
}

// 就寝 / 起床 are exactly what was picked: the stages are stretched or squeezed (proportionally, at least 30 min each) to fill the night
function hsRetime(d, p, start, wake) {
  const total = (((wake - start) % 1440) + 1440) % 1440, old = hsBounds(p), oldTotal = old[old.length - 1] || 1;
  if (total < HS_MIN_STAGE * p.stages.length || total > HS_MAX_TOTAL) return toast("睡眠时长要在 2 到 11.5 小时之间");
  const mins = p.stages.map(s => Math.max(HS_MIN_STAGE, Math.round(Math.round(s.d * 60) * total / oldTotal)));
  let diff = total - mins.reduce((x, y) => x + y, 0), guard = 400;
  while (diff !== 0 && guard--) {          // put the rounding leftovers on the longest stage that can take them
    const i = diff > 0 ? mins.indexOf(Math.max(...mins)) : mins.indexOf(Math.max(...mins.map(m => (m > HS_MIN_STAGE ? m : 0))));
    if (diff < 0 && mins[i] <= HS_MIN_STAGE) break;
    const step = diff > 0 ? 1 : -1; mins[i] += step; diff -= step;
  }
  p.start = start;
  p.stages.forEach((s, i) => { s.d = Math.round((mins[i] / 60) * 10000) / 10000; });
  htWriteSleep(d, p);
}

function hsPickBoundary(d, k) {          // k = 0: 就寝时间, 1-3: where a stage ends, 4: 起床时间
  const p = htParseSleep(st(d.ac.sleep_cfg).s), b = hsBounds(p), wake = (p.start + b[4]) % 1440;
  if (k === 0) return whHMPick("就寝时间", p.start, m => { closeSheet(); if (m != null && m !== p.start) hsRetime(d, p, m, wake); });
  if (k === 4) return whHMPick("起床时间", wake, m => { closeSheet(); if (m != null && m !== wake) hsRetime(d, p, p.start, m); });
  const lo = b[k - 1] + HS_MIN_STAGE, hi = k === 4 ? HS_MAX_TOTAL : b[k + 1] - HS_MIN_STAGE;
  whHMPick(k === 4 ? "起床时间" : `第 ${k} 段结束时间`, (((p.start + b[k]) % 1440) + 1440) % 1440, m => {
    closeSheet();
    if (m == null) return;
    const off = Math.round((((m - p.start) % 1440 + 1440) % 1440) / 30) * 30;   // half-hour steps, counted from 就寝
    if (off < lo || off > hi) return toast(`这个时间不行：要在 ${hsClock(p.start + lo)} 到 ${hsClock(p.start + hi)} 之间`);
    const cut = b.slice(); cut[k] = off;
    p.stages.forEach((s, i) => { s.d = (cut[i + 1] - cut[i]) / 60; });
    htWriteSleep(d, p);
  });
}

// ---- 重复: 每天 / 执行一次 / 自定义 (周一…周日 with a round tick each)
let HSR = null;
function hsRepeatSheet() {
  const mode = HSR.mode, row = (k, text) => `<div class="row hsOpt ${mode === k ? "on" : ""}" data-hs="rep-mode|${k}"><div class="l">${text}</div>${mode === k ? `<span class="hsOk">✓</span>` : ""}</div>`;
  sheet(`<h3>重复</h3>${row("daily", "每天")}${row("once", "执行一次")}${row("custom", "自定义")}
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri" data-hs="rep-ok">确定</button></div>`, "whs");
}
function hsDaysSheet() {
  sheet(`<h3>自定义</h3>${HT_WEEK_CN.map((w, i) => `<div class="row hsOpt" style="--c:${COLORS.heater}" data-hs="rep-day|${i + 1}"><div class="l">周${w}</div><span class="hsCk ${HSR.days.includes(i + 1) ? "on" : ""}"></span></div>`).join("")}
    <div class="sheet-btns"><button data-hs="rep-back">取消</button><button class="pri" data-hs="rep-days-ok">确定</button></div>`, "whs");
}

// ---- tapping an arc of the ring: it grows while its temperature wheel is open
function hsSegTemp(d, i) {
  const p = htParseSleep(st(d.ac.sleep_cfg).s), a = st(d.roles.main).a, b = hsBounds(p);
  const temps = kRange([a.min_temp ?? 16, a.max_temp ?? 28, 1]), page = document.querySelector(".page.hs");
  const mark = on => { page?.classList.toggle("selecting", on); document.querySelectorAll(".hsG").forEach((g, j) => g.classList.toggle("sel", on && j === i)); };
  S.htPage.sel = i; mark(true);
  requestAnimationFrame(() => { const card = $("#hsRingCard"); if (card) window.scrollTo({ top: card.getBoundingClientRect().top + window.scrollY - 76, behavior: "smooth" }); });   // above the wheel
  const gone = new MutationObserver(() => { if (!$("#sheet")) { gone.disconnect(); if (S.htPage) S.htPage.sel = -1; mark(false); } });
  gone.observe(document.body, { childList: true });
  kPick(`第 ${i + 1} 阶段<small>${hsClock(p.start + b[i])} - ${hsClock(p.start + b[i + 1])}</small>`, temps, p.stages[i].t, "°C",
    v => { p.stages[i].t = v; htWriteSleep(d, p); }, Object.fromEntries(temps.map(v => [v, v + "°C"])));
}

document.addEventListener("click", e => {
  const t = e.target.closest("[data-hs]");
  if (!t) return;
  const d = currentDevice();
  if (!d || d.kind !== "heater" || !d.ac) return;
  e.stopPropagation();
  const [act, arg] = t.dataset.hs.split("|");
  const x = d.ac, p = htParseSleep(st(x.sleep_cfg).s);
  switch (act) {
    case "back": return htBack();
    case "sw": { const on = isOn(x.sleep_on); return call(x.sleep_on, on ? "turn_off" : "turn_on", {}, { s: on ? "off" : "on" }); }
    case "start": return hsPickBoundary(d, 0);
    case "wake": return hsPickBoundary(d, 4);
    case "zones": S.htPage = { id: d.roles.main, view: "zones" }; return render();
    case "bnd": return hsPickBoundary(d, +arg);
    case "seg": return hsSegTemp(d, +arg);
    case "repeat":
      HSR = { mode: p.repeat === 0 ? "once" : p.days.length >= 7 ? "daily" : "custom", days: p.days.length ? [...p.days] : [1, 2, 3, 4, 5, 6, 7] };
      return hsRepeatSheet();
    case "rep-mode": HSR.mode = arg; return arg === "custom" ? hsDaysSheet() : hsRepeatSheet();
    case "rep-back": return hsRepeatSheet();
    case "rep-day": { const n = +arg, i = HSR.days.indexOf(n); if (i >= 0) HSR.days.splice(i, 1); else HSR.days.push(n); return hsDaysSheet(); }
    case "rep-days-ok":
      if (!HSR.days.length) return toast("至少选一天");
      p.repeat = 8; p.days = HSR.days.slice().sort((a, b) => a - b);
      htWriteSleep(d, p);
      return closeSheet();
    case "rep-ok":
      if (HSR.mode === "custom" && !HSR.days.length) return toast("至少选一天");
      p.repeat = HSR.mode === "once" ? 0 : 8;
      if (HSR.mode === "daily") p.days = [1, 2, 3, 4, 5, 6, 7]; else if (HSR.mode === "custom") p.days = HSR.days.slice().sort((a, b) => a - b);
      htWriteSleep(d, p);
      return closeSheet();
    case "post":
      return sheet(`<h3>设备状态</h3><p class="sub">安睡时段结束后</p>
        ${Object.entries(HS_POST).map(([k, v]) => `<div class="row hsOpt ${p.post === k ? "on" : ""}" data-hs="post-set|${k}"><div class="l">${v}</div>${p.post === k ? `<span class="hsOk">✓</span>` : ""}</div>`).join("")}`, "whs");
    case "post-set": p.post = arg; htWriteSleep(d, p); return closeSheet();
  }
}, true);
