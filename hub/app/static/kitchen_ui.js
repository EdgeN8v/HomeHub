// Kitchen appliances, laid out like Mijia's: the air fryer with its device modes (each with its own
// temperature / time / amount ranges, as the unit and Mijia limit them), a cooking card with a countdown,
// and status pages for the microwave, pressure cooker and induction cooker.

const KS = { sel: null, vals: {}, tick: 0 };
const kx = (d, k) => (d.kx || {})[k];
const kv = (d, k) => (kx(d, k) ? st(kx(d, k)).s : undefined);
const kIdle = s => !s || /待机|关机|休眠|空闲|离线|unavailable|unknown/.test(s);

// ---- the air fryer's device modes. n: name on the page, k: the unit's option, c: colours, g: glyph,
// temp/time: [min, max, step, default], weight/portions/textures when the mode has that row.
const FRY = [
  ["手动", "手动模式", ["#35bfc6", "#69dcd6"], "hand"],
  ["冷冻薯条", "薯条", ["#ff7a45", "#ffb08f"], "fries"],
  ["鸡翅", "鸡翅", ["#ffb02e", "#ffc94d"], "wing"],
  ["牛排", "牛排", ["#f77f7f", "#f9a1a1"], "steak"],
  ["羊排", "羊排", ["#dc5878", "#f093ab"], "rib"],
  ["五花肉", "五花肉", ["#f07c80", "#f4a0a4"], "skewer"],
  ["鱼", "鱼", ["#22b8f0", "#6fd0f7"], "fish"],
  ["蜜薯", "红薯", ["#3dbbff", "#7ad2ff"], "potato"],
  ["蛋糕", "蛋糕", ["#8b5cf6", "#b08cf9"], "cake"],
  ["蛋挞", "蛋挞", ["#ffb02e", "#ffcf5c"], "tart"],
  ["解冻", "解冻", ["#d9467f", "#f29ab8"], "drop"],
  ["烤果干", "果干", ["#14b8c9", "#4fd6e0"], "leaf"],
  ["酸奶", "酸奶", ["#2f7cf6", "#6aa7fa"], "jar"],
].map(([n, k, c, g]) => ({ n, k, c, g }));

// per mode: temp / time = [min, max, step, default]; weight = grams [min, max, step, default]; portions = the amount
// choices (the unit's "设定食材份量"); textures = 烹饪模式 (the unit's 口感); flip = has 抽锅翻动提醒.
const KCFG = {
  "手动模式": { temp: [40, 230, 5, 180], time: [1, 60, 1, 15] },
  "薯条": { weight: [100, 1000, 100, 500], temp: [180, 200, 5, 200], time: [1, 60, 1, 18], flip: true },
  "鸡翅": { temp: [160, 200, 5, 180], time: [1, 60, 1, 20] },
  "牛排": { temp: [180, 230, 5, 230], time: [1, 60, 1, 8] },
  "羊排": { temp: [160, 200, 5, 180], time: [1, 60, 1, 18] },
  "五花肉": { temp: [160, 200, 5, 180], time: [1, 60, 1, 25] },
  "鱼": { temp: [160, 200, 5, 180], time: [1, 60, 1, 18] },
  "红薯": { temp: [160, 200, 5, 200], time: [1, 60, 1, 30] },
  "蛋糕": { temp: [140, 180, 5, 160], time: [1, 60, 1, 25] },
  "蛋挞": { temp: [140, 180, 5, 160], time: [1, 60, 1, 15] },
  "解冻": { temp: [40, 60, 5, 50], time: [1, 60, 1, 20] },
  "果干": { temp: [40, 80, 5, 40], time: [60, 600, 10, 300] },
  "酸奶": { temp: [40, 60, 5, 40], time: [60, 720, 10, 480] },
};

const KG = {   // white glyphs on the coloured disc, 24x24
  hand: '<path d="M9 11V5a1.5 1.5 0 0 1 3 0v5l1-.5a1.5 1.5 0 0 1 2 .8l.3 1 1-.2a1.5 1.5 0 0 1 1.7 1.5V17a4 4 0 0 1-4 4h-2a4 4 0 0 1-3-1.4L6 16.5a1.4 1.4 0 0 1 2-2l1 1.2"/>',
  fries: '<path d="M7 11 6 4M10 11 9.5 3.5M13 11l.5-7.5M16 11l1.5-6.5M5.5 11h13l-1.3 9h-10.4z"/>',
  wing: '<path d="M6 15c-2-3 0-7 4.5-7.5 3-.3 5.5 1.5 7 4l-3.5 7.5C11 21 7.5 18.5 6 15zM18 12.5l2.5-2"/>',
  steak: '<path d="M4 13c0-4 3-7 8-7 5 0 8 3 8 6.5 0 3.5-3 6-8 6-5 0-8-2-8-5.5zM14 11a2.2 2.2 0 1 0 0 .1"/>',
  rib: '<path d="M7 6l11 11M5 9l3-3 11 11-3 3L5 9zM3.5 7.5 6 5M17 19.5l2.5-2.5"/>',
  skewer: '<path d="M4 20 20 4M7 11l4-4 3 3-4 4zM12 16l3-3 3 3-3 3z"/>',
  fish: '<path d="M3 12c3-5 9-6 14-3l3-3v12l-3-3c-5 3-11 2-14-3zM8.5 11.5h.01"/>',
  potato: '<path d="M5 17C3 12 6 6 12 5c4-.6 8 1 8 5 0 5-5 9-10 9-2 0-4-.5-5-2zM9 11c2 0 3 1 3 3"/>',
  cake: '<path d="M5 20h14v-6a3 3 0 0 0-3-3H8a3 3 0 0 0-3 3zM8 11V8M12 11V6M16 11V8M5 15c2 1.5 4 1.5 7 0s5 1.5 7 0"/>',
  tart: '<path d="M4 12h16l-2 7H6zM6 12c0-3 2.5-5 6-5s6 2 6 5M12 9v3"/>',
  drop: '<path d="M12 3s6 6 6 10.5a6 6 0 0 1-12 0C6 9 12 3 12 3zM9.5 14.5a2.5 2.5 0 0 0 2.5 2.5"/>',
  leaf: '<path d="M5 19C4 11 9 5 20 4c0 10-5 15-14 15zM5 19l9-9"/>',
  jar: '<path d="M8 4h8v3H8zM7 7h10l1 12a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 19zM9 12h6"/>',
};
const kDisc = (m, size = 56) => `<span class="kdisc" style="width:${size}px;height:${size}px;background:linear-gradient(135deg,${m.c[0]},${m.c[1]})"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" style="width:${size * .5}px;height:${size * .5}px">${KG[m.g]}</svg></span>`;

// ---- state of a fryer
function fryState(d) {
  const s = kv(d, "status"), left = parseInt(kv(d, "left"), 10) || 0, busy = !kIdle(s);
  const paused = /暂停/.test(s || ""), keeping = /保温/.test(s || "");
  const total = parseInt(kv(d, "time"), 10) || 0;
  return { s, left, busy, paused, keeping, total, mode: kv(d, "mode"), temp: kv(d, "temp"), fault: kv(d, "fault") };
}
const kFmt = m => m >= 60 ? `${Math.floor(m / 60)} 小时 ${m % 60 ? m % 60 + " 分钟" : ""}` : `${m} 分钟`;

// ---- pages
function renderKitchen(d) {
  if (d.kind === "fryer") return renderFryer(d);
  return renderCookerStatus(d);
}

function kBar(d) {
  return `<div class="bar"><button class="back" data-act="back">${icon("back")}</button>
    <div><div class="t">${esc(d.name)}</div><div class="r">${esc([d.room, d.kind_name].filter(Boolean).join(" · "))}</div></div></div>`;
}

function renderFryer(d) {
  const f = fryState(d), off = deviceOffline(d), resv = (S.model.kitchen_resv || {})[d.id];
  const grid = FRY.map((m, i) => `<button class="kmode" data-kit="mode|${i}">${kDisc(m)}<span>${m.n}</span></button>`).join("");
  $("#app").innerHTML = `<div class="page kit" style="--c:${COLORS.fryer}">
    ${kBar(d)}
    ${f.busy ? kCooking(d, f) : `<div class="khero"><div class="kbig">${deviceArt(d)}</div><div class="kstate">${off ? "离线" : esc(f.s || "待机")}</div>
      ${resv ? `<div class="kresv">预约 ${kClock(resv.at)} 开始 · ${esc(kModeName(resv.params.mode))}<button data-kit="resv-cancel">取消</button></div>` : ""}</div>`}
    <div class="kcard"><h3>设备上的模式</h3><div class="kgrid">${grid}</div></div>
    ${d.features.length ? `<details class="more" ${S.openMore ? "open" : ""}><summary>更多设置（${d.features.length + d.more.length}）</summary><div class="panel">${[...d.features, ...d.more].map(x => entityRow(x)).join("")}</div></details>` : ""}
  </div>`;
  $("details.more")?.addEventListener("toggle", e => { S.openMore = e.target.open; });
}

const kModeName = k => (FRY.find(m => m.k === k) || {}).n || k;
const kClock = iso => { const t = new Date(iso); return `${t.getDate() !== new Date().getDate() ? "明天 " : ""}${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`; };

// the cooking card: a ring that empties as the time runs out, pause / stop, and the numbers can be nudged
function kCooking(d, f) {
  const pct = f.total ? Math.max(0, Math.min(1, 1 - f.left / f.total)) : 0, R = 70, C = 2 * Math.PI * R;
  return `<div class="kcard kcook">
    <div class="kring"><svg viewBox="0 0 160 160"><circle cx="80" cy="80" r="${R}" class="bg"/><circle cx="80" cy="80" r="${R}" class="fg" stroke-dasharray="${(C * pct).toFixed(1)} ${C.toFixed(1)}"/></svg>
      <div class="kin"><b>${f.keeping ? "保温中" : f.left}</b><small>${f.keeping ? "" : "分钟"}</small><em>${esc(f.s)}</em></div></div>
    <div class="kline">${esc(kModeName(f.mode))} · ${esc(f.temp)}°C</div>
    <div class="kadj"><div><button data-kit="adj|temp|-5">−</button><span>${esc(f.temp)}°C</span><button data-kit="adj|temp|5">+</button></div>
      <div><button data-kit="adj|time|-1">−</button><span>${esc(f.total)} 分钟</span><button data-kit="adj|time|1">+</button></div></div>
    <div class="kbtns"><button data-kit="${f.paused ? "resume" : "pause"}">${f.paused ? "继续" : "暂停"}</button><button class="stop" data-kit="stop">停止烹饪</button></div></div>`;
}

// ---- the mode page (full screen over the page, like Mijia's)
function kSetup(d, i) {
  const m = FRY[i], c = KCFG[m.k] || KCFG["手动模式"];
  KS.sel = { d: d.id, i }; KS.vals = { temp: c.temp[3], time: c.time[3], weight: c.weight ? c.weight[3] : null, portion: c.portions ? c.portions[1] : null,
    texture: c.textures ? c.textures[0] : null, keep: kv(d, "keep") !== "off", flip: kv(d, "flip") === "on" };
  kPaintSetup();
}

function kPaintSetup() {
  const d = S.byId[KS.sel.d], m = FRY[KS.sel.i], c = KCFG[m.k] || KCFG["手动模式"], v = KS.vals;
  const row = (label, val, act) => `<button class="krow" data-kit="${act}"><span>${label}</span><b>${esc(val)}</b><i></i></button>`;
  const sw = (label, sub, on, act) => `<div class="krow tg"><span>${label}<small>${sub}</small></span><button class="swt ${on ? "on" : ""}" data-kit="${act}"></button></div>`;
  $("#kpage")?.remove();
  document.body.insertAdjacentHTML("beforeend", `<div id="kpage" style="--c:${COLORS.fryer}"><div class="kph"><button class="back" data-kit="setup-close">${icon("back")}</button><h2>${m.n}</h2></div>
    <div class="kpic">${kDisc(m, 150)}</div><div class="kest">大约需要 ${kFmt(v.time)}</div>
    <div class="kform">
      ${sw("自动保温", "烹饪完成后将开启自动保温", v.keep, "set|keep")}
      ${c.flip ? sw("抽锅翻动提醒", `${m.n}抽锅翻动设置`, v.flip, "set|flip") : ""}
      ${c.weight ? row("食材量", v.weight + "g", "pick|weight") : ""}
      ${c.portions ? row("食材量", v.portion, "pick|portion") : ""}
      ${c.textures ? row("烹饪模式", v.texture, "pick|texture") : ""}
      ${row("烹饪温度", v.temp + "°C", "pick|temp")}
      ${row("烹饪时间", v.time + "分钟", "pick|time")}
    </div>
    <div class="kfoot"><button data-kit="reserve">预约</button><button class="go" data-kit="start">开始</button></div></div>`);
  void d;
}

// ---- a wheel picker: scroll to a value, 确定
function kPick(title, values, cur, unit, done, labels) {
  const items = values.map(v => `<div class="wi" data-v="${v}">${labels ? labels[v] ?? v : String(v).padStart(unit === "分" ? 2 : 0, "0")}</div>`).join("");
  sheet(`<h3>${title}</h3><div class="wheel"><div class="wbar"></div><div class="wl">${items}</div></div>
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri" id="wok">确定</button></div>`, "kpick");
  const el = $("#sheet .wl"), idx = Math.max(0, values.findIndex(x => String(x) === String(cur)));
  el.scrollTop = idx * 44;
  const mark = () => { const i = Math.round(el.scrollTop / 44); [...el.children].forEach((c, j) => c.classList.toggle("on", j === i)); return i; };
  mark(); el.addEventListener("scroll", () => { mark(); });
  $("#wok").onclick = () => { const i = mark(); closeSheet(); done(values[Math.max(0, Math.min(values.length - 1, i))]); };
}
const kRange = ([lo, hi, step]) => { const a = []; for (let v = lo; v <= hi + 1e-9; v += step) a.push(v); return a; };

async function kPost(path, body) { return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }

function kParams() {
  const m = FRY[KS.sel.i], v = KS.vals, c = KCFG[m.k] || KCFG["手动模式"];
  const p = { mode: m.k, temp: v.temp, time: v.time, keep: v.keep };
  if (c.weight) p.weight = v.weight;
  if (c.flip) p.flip = v.flip;
  return p;
}

document.addEventListener("click", async e => {
  const t = e.target.closest("[data-kit]");
  if (!t) return;
  e.stopPropagation();
  const [act, a1, a2] = t.dataset.kit.split("|");
  const d = KS.sel ? S.byId[KS.sel.d] : currentDevice();
  if (act === "mode") return kSetup(currentDevice(), parseInt(a1, 10));
  if (act === "setup-close") { KS.sel = null; $("#kpage")?.remove(); return; }
  if (act === "set") { KS.vals[a1] = !KS.vals[a1]; return kPaintSetup(); }
  if (act === "pick") {
    const m = FRY[KS.sel.i], c = KCFG[m.k] || KCFG["手动模式"];
    const set = v => { KS.vals[a1] = v; kPaintSetup(); };
    if (a1 === "temp") return kPick("烹饪温度", kRange(c.temp), KS.vals.temp, "℃", set);
    if (a1 === "time") return kPick("烹饪时间", kRange(c.time), KS.vals.time, "分", set);
    if (a1 === "weight") return kPick("食材量", kRange(c.weight), KS.vals.weight, "g", set);
    if (a1 === "portion") return kPick("食材量", c.portions, KS.vals.portion, "", set, null);
    if (a1 === "texture") return kPick("烹饪模式", c.textures, KS.vals.texture, "", set, null);
  }
  if (act === "start") {
    try { await kPost("/api/kitchen/fryer", { device: KS.sel.d, action: "start", params: kParams() }); toast("已开始烹饪"); KS.sel = null; $("#kpage")?.remove(); setTimeout(() => pollStates(S.byId[currentDevice().id].entities), 1500); }
    catch (err) { toast("没能开始：" + err.message); }
    return;
  }
  if (act === "reserve") return kReserveSheet();
  if (act === "resv-cancel") { const cd = currentDevice(); const r = await kPost("/api/kitchen/fryer", { device: cd.id, action: "cancel_resv" }); S.model.kitchen_resv = r.kitchen_resv; render(); return; }
  const cd = currentDevice();
  if (!cd || !cd.kx) return;
  if (act === "stop") return call(kx(cd, "stop"), "press", {});
  if (act === "pause") return call(kx(cd, "pause"), "press", {});
  if (act === "resume") return call(kx(cd, "resume"), "press", {});
  if (act === "adj") {
    const key = a1, e0 = kx(cd, key), a = st(e0).a, nv = Math.min(a.max ?? 9999, Math.max(a.min ?? 0, parseFloat(st(e0).s) + parseFloat(a2)));
    return call(e0, "set_value", { value: nv }, { s: String(nv) });
  }
}, true);

function kReserveSheet() {
  const now = new Date(), opts = [];
  for (let m = 10; m <= 48 * 60; m += 10) { const t = new Date(now.getTime() + m * 60000); opts.push(t); }
  const label = t => `${t.getDate() !== now.getDate() ? "明天 " : "今天 "}${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
  const labels = {}; opts.forEach((t, i) => { labels[i] = label(t); });
  kPick("预约开始时间", opts.map((_, i) => i), 5, "", async i => {
    const t = opts[i], iso = `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}T${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
    try {
      const r = await kPost("/api/kitchen/fryer", { device: KS.sel.d, action: "reserve", at: iso, params: kParams() });
      S.model.kitchen_resv = r.kitchen_resv; KS.sel = null; $("#kpage")?.remove(); render(); toast("已预约，到点会自动开始");
    } catch (err) { toast("预约失败：" + err.message); }
  }, labels);
}

// ---- microwave / pressure cooker / induction cooker: status and the safe controls (they cannot be started from here)
function renderCookerStatus(d) {
  const s = kv(d, "status"), busy = !kIdle(s), left = parseInt(kv(d, "left"), 10) || 0;
  const rows = [];
  if (d.kind === "pressure_cooker") rows.push(["锅内温度", `${kv(d, "temp") ?? "—"}°C`], ["阶段", kv(d, "phase")], ["锅盖", kv(d, "lid")], ["口感", kv(d, "taste")]);
  if (d.kind === "microwave") rows.push(["模式", kv(d, "mode")], ["火力", kv(d, "heat")]);
  if (d.kind === "ihcooker") rows.push(["模式", kv(d, "mode")], ["火力", kv(d, "fire")], ["温度", kv(d, "temp") != null ? kv(d, "temp") + "°C" : "—"], ["已工作", kv(d, "worked") != null ? kv(d, "worked") + " 分钟" : "—"]);
  const sw = (k, label) => kx(d, k) ? `<div class="row"><div class="l">${label}</div><button class="sw ${isOn(kx(d, k)) ? "on" : ""}" data-toggle="${kx(d, k)}"></button></div>` : "";
  const fault = kv(d, "fault"), faultText = fault && !/无|No Fault|^0$/.test(fault) ? fault : "";
  $("#app").innerHTML = `<div class="page kit" style="--c:${COLORS[d.kind]}">${kBar(d)}
    <div class="khero"><div class="kbig">${deviceArt(d) || icon(d.kind)}</div><div class="kstate">${deviceOffline(d) ? "离线" : esc(s || "待机")}</div>
      ${busy && left ? `<div class="kresv">还剩 ${left} 分钟</div>` : ""}${faultText ? `<div class="kresv warn">故障：${esc(faultText)}</div>` : ""}</div>
    ${busy && !deviceOffline(d) ? `<div class="kcard"><div class="kbtns">${kx(d, "pause") ? `<button data-kit="pause">暂停</button>` : ""}${kx(d, "resume") && /暂停/.test(s || "") ? `<button data-kit="resume">继续</button>` : ""}<button class="stop" data-kit="stop">停止烹饪</button></div></div>` : ""}
    ${rows.length ? `<div class="kcard"><div class="kinfo">${rows.filter(r => r[1]).map(([k, v]) => `<div><small>${k}</small><b>${esc(v)}</b></div>`).join("")}</div></div>` : ""}
    <div class="kcard">${sw("lock", "童锁")}${sw("beep", "提示音")}${sw("push", "烹饪完成时推送")}${sw("buzzer", "提示音")}</div>
    <div class="kcard"><p class="knote">${d.kind === "pressure_cooker" ? "压力锅的菜谱要在米家里选；做的时候这里会显示进度，也能随时停止。" : d.kind === "microwave" ? "微波炉要人在旁边、放好食物才能开始，所以这里只看进度、暂停和停止。" : "电磁炉的菜单在米家里选，这里显示火力和温度，也能暂停、停止。"}</p></div>
  </div>`;
}

// ---- home card: busy kitchen appliances show what they are doing
function kitchenSummary(d) {
  const s = kv(d, "status");
  if (deviceOffline(d)) return "离线";
  if (kIdle(s)) return s && !/unavailable|unknown/.test(s) ? s : "待机";
  const left = parseInt(kv(d, "left"), 10);
  return `${s}${left ? " · 剩 " + left + " 分" : ""}`;
}
