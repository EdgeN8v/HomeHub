// Kitchen appliances, laid out like Mijia's: the air fryer with its device modes (each with its own
// temperature / time / amount ranges, as the unit and Mijia limit them), your own recipes as 扩展模式,
// a cooking card with a countdown, and status pages for the microwave, pressure cooker and induction cooker.

const KS = { flipWait: [], flipAt: 0, sel: null, vals: {}, editing: false, recipe: null };
const kx = (d, k) => (d.kx || {})[k];
const kv = (d, k) => (kx(d, k) ? st(kx(d, k)).s : undefined);
const kIdle = s => !s || /待机|关机|休眠|空闲|离线|unavailable|unknown/.test(s);

// ---- the air fryer's device modes. n: name on the page, k: the unit's option, g: its picture (food.js)
const FRY = [
  ["手动", "手动模式", "hand"], ["冷冻薯条", "薯条", "fries"], ["鸡翅", "鸡翅", "wing"], ["牛排", "牛排", "steak"],
  ["羊排", "羊排", "rib"], ["五花肉", "五花肉", "skewer"], ["鱼", "鱼", "fish"], ["蜜薯", "红薯", "potato"],
  ["蛋糕", "蛋糕", "cake"], ["蛋挞", "蛋挞", "tart"], ["解冻", "解冻", "drop"], ["烤果干", "果干", "leaf"], ["酸奶", "酸奶", "jar"],
].map(([n, k, g]) => ({ n, k, g }));

// per mode: temp / time = [min, max, step, default]; weight = grams [min, max, step, default]; flip = has 抽锅翻动提醒.
const KCFG = {
  "手动模式": { temp: [40, 230, 5, 180], time: [1, 60, 1, 15] },
  "薯条": { weight: [100, 1000, 100, 500], temp: [180, 200, 5, 200], time: [1, 60, 1, 18], flip: true },
  "鸡翅": { portion: true, texture: true, temp: [160, 200, 5, 180], time: [1, 60, 1, 20] },
  "牛排": { portion: true, texture: true, temp: [180, 230, 5, 230], time: [1, 60, 1, 8] },
  "羊排": { portion: true, texture: true, temp: [160, 200, 5, 180], time: [1, 60, 1, 18] },
  "五花肉": { texture: true, temp: [160, 200, 5, 180], time: [1, 60, 1, 25] },
  "鱼": { portion: true, texture: true, temp: [160, 200, 5, 180], time: [1, 60, 1, 18] },
  "红薯": { temp: [160, 200, 5, 200], time: [1, 60, 1, 30] },
  "蛋糕": { temp: [140, 180, 5, 160], time: [1, 60, 1, 25] },
  "蛋挞": { portion: true, temp: [140, 180, 5, 160], time: [1, 60, 1, 15] },
  "解冻": { temp: [40, 60, 5, 50], time: [1, 60, 1, 20] },
  "果干": { temp: [40, 80, 5, 40], time: [60, 600, 10, 300] },
  "酸奶": { temp: [40, 60, 5, 40], time: [60, 720, 10, 480] },
};
const KPORTION = ["单层", "双层"], KTEXTURE = ["Crispy Roast", "Tender Roast"], KTEXLABEL = { "Crispy Roast": "脆烤", "Tender Roast": "嫩烤" };
const RECIPE_CFG = { temp: [40, 230, 5, 180], time: [1, 120, 1, 20] };    // your own recipes: the unit's full range

// ---- the two lists of modes: 设备上的模式 and 扩展模式. A mode id is "p:<index>" (built in) or "r:<id>" (your recipe).
const kRecipes = d => (S.model.kitchen_recipes || {})[d.id] || [];
// a recipe gets the picture of what it is made of, by its name
const KFOODS = [["榴莲", "durian"], ["南瓜", "pumpkin"], ["红薯", "potato"], ["蜜薯", "potato"], ["地瓜", "potato"], ["土豆", "spud"], ["薯", "spud"], ["鸡柳", "nugget"], ["鸡块", "nugget"], ["鸡翅", "wing"], ["牛排", "steak"], ["排骨", "rib"], ["羊", "rib"], ["鱼", "fish"], ["蛋糕", "cake"], ["蛋挞", "tart"], ["肠", "sausage"], ["串", "skewer"]];
const kFood = n => (KFOODS.find(([w]) => n.includes(w)) || [0, "recipe"])[1];
function kItem(d, id) {
  if (id.startsWith("p:")) { const m = FRY[parseInt(id.slice(2), 10)]; return m && { id, n: m.n, food: m.g, cfgKey: m.k, mode: m.k }; }
  const r = kRecipes(d).find(x => "r:" + x.id === id);
  return r && { id, n: r.name, food: kFood(r.name), cfgKey: null, mode: "手动模式", recipe: r };
}
function kLists(d) {
  const all = [...FRY.map((_, i) => "p:" + i), ...kRecipes(d).map(r => "r:" + r.id)], ord = S.model.order || {};
  const on0 = (ord[`kit:${d.id}:on`] || []).filter(i => all.includes(i)), ext0 = (ord[`kit:${d.id}:ext`] || []).filter(i => all.includes(i) && !on0.includes(i));
  const left = all.filter(i => !on0.includes(i) && !ext0.includes(i));
  return { on: [...on0, ...left.filter(i => i.startsWith("p:"))], ext: [...ext0, ...left.filter(i => i.startsWith("r:"))] };
}

// ---- state of a fryer
function fryState(d) {
  const s = kv(d, "status"), left = parseInt(kv(d, "left"), 10) || 0, busy = !kIdle(s);
  const paused = /暂停/.test(s || ""), keeping = /保温/.test(s || "");
  const total = parseInt(kv(d, "time"), 10) || 0;
  return { s, left, busy, paused, keeping, total, mode: kv(d, "mode"), temp: kv(d, "temp"), fault: kv(d, "fault") };
}
const kFmt = m => m >= 60 ? `${Math.floor(m / 60)} 小时${m % 60 ? " " + m % 60 + " 分钟" : ""}` : `${m} 分钟`;

// ---- pages
function renderKitchen(d) {
  if (d.kind === "fryer") return renderFryer(d);
  return renderCookerStatus(d);
}

function kBar(d) {
  return `<div class="bar"><button class="back" data-act="back">${icon("back")}</button>
    <div><div class="t">${esc(d.name)}</div><div class="r">${esc([d.room, d.kind_name].filter(Boolean).join(" · "))}</div></div></div>`;
}

const kTile = it => `<div class="kmode" data-kid="${it.id}" data-kit="mode|${it.id}"><span class="kart">${FOOD[it.food]}</span><span class="kn">${esc(it.n)}</span><button class="kbadge" data-kit="badge|${it.id}" aria-label="移动"></button></div>`;

function kFlipCheck(d, f) {
  if (!f.paused || Date.now() - KS.flipAt < 4000) return;
  KS.flipAt = Date.now();
  api("/api/kitchen/flip").then(r => { const w = r.flip_waiting || [], ch = w.includes(d.id) !== KS.flipWait.includes(d.id); KS.flipWait = w; if (ch && currentDevice()?.id === d.id && !$("#kpage")) render(); }).catch(() => {});
}

function renderFryer(d) {
  const f = fryState(d);
  kFlipCheck(d, f);
  const off = deviceOffline(d), resv = (S.model.kitchen_resv || {})[d.id], L = kLists(d);
  const on = L.on.map(i => kItem(d, i)).filter(Boolean), ext = L.ext.map(i => kItem(d, i)).filter(Boolean);
  $("#app").innerHTML = `<div class="page kit ${KS.editing ? "kedit" : ""}" style="--c:${COLORS.fryer}" data-dev="${d.id}">
    ${kBar(d)}
    ${f.busy ? kCooking(d, f) : `<div class="khero"><div class="kbig">${deviceArt(d)}</div><div class="kstate">${off ? "离线" : esc(f.s || "待机")}</div>
      ${resv ? `<div class="kresv">预约 ${kClock(resv.at)} 开始 · ${esc(kModeName(d, resv.params))}<button data-kit="resv-cancel">取消</button></div>` : ""}</div>`}
    <div class="kcard"><div class="kh"><h3>设备上的模式</h3><button class="kedbtn" data-kit="edit-toggle">${KS.editing ? "完成" : "编辑"}</button></div>
      <div class="kgrid ksort kon" data-list="on">${on.map(kTile).join("")}</div></div>
    <div class="kcard"><div class="kh"><h3>扩展模式 <small class="kcount">${ext.length}</small></h3></div>
      <div class="kgrid ksort kext" data-list="ext"><div class="kmode knew" data-kit="recipe-new"><span class="kart kplus">+</span><span class="kn">新建食谱</span></div>${ext.map(kTile).join("")}</div>
      ${KS.editing ? `<p class="knote kdrop">长按拖动可以调顺序，也可以把模式拖到上面或下面；点角上的 − / + 也能移动。</p>` : ""}</div>
  </div>`;
}

const kModeName = (d, p) => (FRY.find(m => m.k === p?.mode) || {}).n || p?.name || p?.mode || "";
const kClock = iso => { const t = new Date(iso); return `${t.getDate() !== new Date().getDate() ? "明天 " : ""}${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`; };

// the cooking card: a ring that empties as the time runs out, pause / stop, and the numbers can be nudged
function kCooking(d, f) {
  const pct = f.total ? Math.max(0, Math.min(1, 1 - f.left / f.total)) : 0, R = 70, C = 2 * Math.PI * R;
  const name = (FRY.find(m => m.k === f.mode) || {}).n || f.mode;
  return `<div class="kcard kcook">
    <div class="kring"><svg viewBox="0 0 160 160"><circle cx="80" cy="80" r="${R}" class="bg"/><circle cx="80" cy="80" r="${R}" class="fg" stroke-dasharray="${(C * pct).toFixed(1)} ${C.toFixed(1)}"/></svg>
      <div class="kin"><b>${f.keeping ? "保温中" : f.left}</b><small>${f.keeping ? "" : "分钟"}</small><em>${esc(f.s)}</em></div></div>
    ${f.paused && KS.flipWait.includes(d.id) ? `<div class="kflip">该翻面了：拉出抽屉翻一下，推回去后点「翻好了，继续」</div>` : ""}
    <div class="kline">${esc(name)} · ${esc(f.temp)}°C</div>
    <div class="kadj"><div><button data-kit="adj|temp|-5">−</button><span>${esc(f.temp)}°C</span><button data-kit="adj|temp|5">+</button></div>
      <div><button data-kit="adj|time|-1">−</button><span>${esc(f.total)} 分钟</span><button data-kit="adj|time|1">+</button></div></div>
    <div class="kbtns"><button data-kit="${f.paused ? "resume" : "pause"}">${f.paused ? (KS.flipWait.includes(d.id) ? "翻好了，继续" : "继续") : "暂停"}</button><button class="stop" data-kit="stop">停止烹饪</button></div></div>`;
}

// ---- the mode page (full screen over the app, like Mijia's). Back (gesture / key / ←) closes it.
function kSetup(d, id) {
  const it = kItem(d, id);
  if (!it) return;
  const c = it.recipe ? RECIPE_CFG : KCFG[it.cfgKey] || KCFG["手动模式"], r = it.recipe;
  KS.sel = { d: d.id, id };
  KS.vals = { temp: r ? r.temp : c.temp[3], time: r ? r.time : c.time[3], weight: c.weight ? c.weight[3] : null, portion: c.portion ? "单层" : null, texture: c.texture ? "Crispy Roast" : null,
    flip_at: r ? r.flip_at || 0 : 0, keep: r ? r.keep !== false : true, flip: kv(d, "flip") === "on" };
  kPaintSetup();
}
function closeKPage() { KS.sel = null; KS.recipe = null; $("#kpage")?.remove(); }

function kPaintSetup() {
  const d = S.byId[KS.sel.d], it = kItem(d, KS.sel.id), c = it.recipe ? RECIPE_CFG : KCFG[it.cfgKey] || KCFG["手动模式"], v = KS.vals;
  const row = (label, val, act) => `<button class="krow" data-kit="${act}"><span>${label}</span><b>${esc(val)}</b><i></i></button>`;
  const tg = (label, sub, on, act) => `<div class="krow tg"><span>${label}<small>${sub}</small></span><button class="swt ${on ? "on" : ""}" data-kit="${act}"></button></div>`;
  $("#kpage")?.remove();
  document.body.insertAdjacentHTML("beforeend", `<div id="kpage" style="--c:${COLORS.fryer}"><div class="kph"><button class="back" data-kit="setup-close">${icon("back")}</button><h2>${esc(it.n)}</h2>${it.recipe ? `<button class="kphr" data-kit="recipe-edit">编辑</button>` : ""}</div>
    <div class="kpic"><span class="kbigart">${FOOD[it.food]}</span></div><div class="kest">大约需要 ${kFmt(v.time)}</div>
    <div class="kform">
      ${tg("自动保温", "烹饪完成后自动保温 20 分钟", v.keep, "set|keep")}
      ${it.recipe ? kFlipRows(v.flip_at, "setflip", "pick|flipat") : ""}
      ${c.flip ? tg("抽锅翻动提醒", `${it.n}抽锅翻动设置`, v.flip, "set|flip") : ""}
      ${c.weight ? row("食材量", v.weight + "g", "pick|weight") : ""}
      ${c.portion ? row("食材量", v.portion, "pick|portion") : ""}
      ${c.texture ? row("烹饪模式", KTEXLABEL[v.texture], "pick|texture") : ""}
      ${row("烹饪温度", v.temp + "°C", "pick|temp")}
      ${row("烹饪时间", v.time + "分钟", "pick|time")}
    </div>
    <div class="kfoot"><button data-kit="reserve">预约</button><button class="go" data-kit="start">开始</button></div></div>`);
}

// half-way flip-over for your own recipes: the hub pauses the fryer at that minute, you flip the food and press 继续
function kFlipRows(at, tact, pact) {
  return `<div class="krow tg"><span>中途翻面提醒<small>到点自动暂停，翻好后点继续</small></span><button class="swt ${at ? "on" : ""}" data-kit="${tact}"></button></div>` +
    (at ? `<button class="krow" data-kit="${pact}"><span>翻面时间</span><b>烹饪到第 ${at} 分钟</b><i></i></button>` : "");
}
const kFlipDefault = t => t > 1 ? Math.max(1, Math.round(t / 2)) : 0;
const kClampFlip = (at, t) => at && at >= t ? kFlipDefault(t) : at;

// ---- the recipe editor (new / change / delete)
function kEditRecipe(d, r) {
  KS.recipe = r ? { ...r } : { id: "", name: "", temp: RECIPE_CFG.temp[3], time: RECIPE_CFG.time[3], keep: true, flip_at: 0 };
  kPaintEditor(d);
}
function kPaintEditor(d) {
  const r = KS.recipe;
  const row = (label, val, act) => `<button class="krow" data-kit="${act}"><span>${label}</span><b>${esc(val)}</b><i></i></button>`;
  $("#kpage")?.remove();
  document.body.insertAdjacentHTML("beforeend", `<div id="kpage" style="--c:${COLORS.fryer}"><div class="kph"><button class="back" data-kit="setup-close">${icon("back")}</button><h2>${r.id ? "编辑食谱" : "新建自定义食谱"}</h2></div>
    <div class="kpic"><span class="kbigart">${FOOD.recipe}</span></div>
    <div class="kform">
      <div class="krow tg"><span>名称</span><input id="rname" type="text" maxlength="10" placeholder="给食谱起个名字" value="${esc(r.name)}" data-kit-input="name"></div>
      <div class="krow tg"><span>自动保温<small>烹饪完成后自动保温 20 分钟</small></span><button class="swt ${r.keep ? "on" : ""}" data-kit="redit|keep"></button></div>
      ${kFlipRows(r.flip_at, "redit|flip", "redit|flipat")}
      ${row("烹饪温度", r.temp + "°C", "redit|temp")}
      ${row("烹饪时间", r.time + "分钟", "redit|time")}
    </div>
    <div class="kfoot">${r.id ? `<button class="del" data-kit="recipe-delete">删除</button>` : `<button data-kit="setup-close">取消</button>`}<button class="go" data-kit="recipe-save">保存</button></div></div>`);
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
  const d = S.byId[KS.sel.d], it = kItem(d, KS.sel.id), v = KS.vals, c = it.recipe ? RECIPE_CFG : KCFG[it.cfgKey] || KCFG["手动模式"];
  const p = { mode: it.mode, temp: v.temp, time: v.time, keep: v.keep };
  if (c.weight) p.weight = v.weight;
  if (c.portion) p.portion = v.portion;
  if (c.texture) p.texture = v.texture;
  if (c.flip) p.flip = v.flip;
  if (it.recipe) { p.name = it.recipe.name; if (v.flip_at) p.flip_at = v.flip_at; }
  return p;
}

// ---- taps
document.addEventListener("click", async e => {
  const t = e.target.closest("[data-kit]");
  if (!t) return;
  if (KD.swallow) { KD.swallow = false; e.stopPropagation(); return; }              // the finger lifting from a drag
  e.stopPropagation();
  const [act, a1, a2] = t.dataset.kit.split("|");
  const d = KS.sel ? S.byId[KS.sel.d] : currentDevice();
  if (act === "mode") { if (KS.editing) return; return kSetup(currentDevice(), a1); }
  if (act === "edit-toggle") { KS.editing = !KS.editing; $(".kit")?.classList.toggle("kedit", KS.editing); t.textContent = KS.editing ? "完成" : "编辑"; return render(); }
  if (act === "badge") return kBadge(a1);
  if (act === "setup-close") return closeKPage();
  if (act === "setflip") { KS.vals.flip_at = KS.vals.flip_at ? 0 : kFlipDefault(KS.vals.time); return kPaintSetup(); }
  if (act === "set") { KS.vals[a1] = !KS.vals[a1]; return kPaintSetup(); }
  if (act === "pick") {
    const it = kItem(d, KS.sel.id), c = it.recipe ? RECIPE_CFG : KCFG[it.cfgKey] || KCFG["手动模式"];
    const set = v => { KS.vals[a1] = v; if (a1 === "time") KS.vals.flip_at = kClampFlip(KS.vals.flip_at, v); kPaintSetup(); };
    if (a1 === "flipat") return kPick("翻面时间（烹饪到第几分钟）", kRange([1, Math.max(1, KS.vals.time - 1), 1]), KS.vals.flip_at, "分", v => { KS.vals.flip_at = v; kPaintSetup(); });
    if (a1 === "temp") return kPick("烹饪温度", kRange(c.temp), KS.vals.temp, "℃", set);
    if (a1 === "time") return kPick("烹饪时间", kRange(c.time), KS.vals.time, "分", set);
    if (a1 === "portion") return kPick("食材量", KPORTION, KS.vals.portion, "", set);
    if (a1 === "texture") return kPick("烹饪模式", KTEXTURE, KS.vals.texture, "", set, KTEXLABEL);
    if (a1 === "weight") return kPick("食材量", kRange(c.weight), KS.vals.weight, "g", set);
  }
  if (act === "start") {
    try { await kPost("/api/kitchen/fryer", { device: KS.sel.d, action: "start", params: kParams() }); toast("已开始烹饪"); const id = KS.sel.d; closeKPage(); setTimeout(() => pollStates(S.byId[id].entities), 1500); }
    catch (err) { toast("没能开始：" + err.message); }
    return;
  }
  if (act === "reserve") return kReserveSheet();
  if (act === "resv-cancel") { const cd = currentDevice(); const r = await kPost("/api/kitchen/fryer", { device: cd.id, action: "cancel_resv" }); S.model.kitchen_resv = r.kitchen_resv; render(); return; }
  // ---- recipes
  if (act === "recipe-new") return kEditRecipe(currentDevice(), null);
  if (act === "recipe-edit") return kEditRecipe(d, kItem(d, KS.sel.id).recipe);
  if (act === "redit") {
    const r = KS.recipe;
    if (a1 === "keep") { r.keep = !r.keep; return kPaintEditor(d); }
    if (a1 === "flip") { r.flip_at = r.flip_at ? 0 : kFlipDefault(r.time); return kPaintEditor(d); }
    if (a1 === "flipat") return kPick("翻面时间（烹饪到第几分钟）", kRange([1, Math.max(1, r.time - 1), 1]), r.flip_at, "分", v => { r.flip_at = v; kPaintEditor(d); });
    const c = RECIPE_CFG, set = v => { r[a1] = v; if (a1 === "time") r.flip_at = kClampFlip(r.flip_at, v); kPaintEditor(d); };
    if (a1 === "temp") return kPick("烹饪温度", kRange(c.temp), r.temp, "℃", set);
    if (a1 === "time") return kPick("烹饪时间", kRange(c.time), r.time, "分", set);
  }
  if (act === "recipe-save") {
    const r = KS.recipe, name = ($("#rname")?.value || r.name || "").trim();
    if (!name) return toast("先给食谱起个名字");
    try {
      const res = await kPost("/api/kitchen/recipe", { device: currentDevice().id, action: "save", recipe: { ...r, name } });
      S.model.kitchen_recipes = res.kitchen_recipes; closeKPage(); render(); toast("已保存");
    } catch (err) { toast("没保存上：" + err.message); }
    return;
  }
  if (act === "recipe-delete") {
    try {
      const res = await kPost("/api/kitchen/recipe", { device: currentDevice().id, action: "delete", recipe: { id: KS.recipe.id } });
      S.model.kitchen_recipes = res.kitchen_recipes; closeKPage(); render(); toast("已删除");
    } catch (err) { toast("没删掉：" + err.message); }
    return;
  }
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

document.addEventListener("input", e => { if (e.target.matches("[data-kit-input=name]") && KS.recipe) KS.recipe.name = e.target.value; });

function kReserveSheet() {
  const now = new Date(), opts = [];
  for (let m = 10; m <= 48 * 60; m += 10) { const t = new Date(now.getTime() + m * 60000); opts.push(t); }
  const label = t => `${t.getDate() !== now.getDate() ? "明天 " : "今天 "}${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
  const labels = {}; opts.forEach((t, i) => { labels[i] = label(t); });
  kPick("预约开始时间", opts.map((_, i) => i), 5, "", async i => {
    const t = opts[i], iso = `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}T${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
    try {
      const r = await kPost("/api/kitchen/fryer", { device: KS.sel.d, action: "reserve", at: iso, params: kParams() });
      S.model.kitchen_resv = r.kitchen_resv; closeKPage(); render(); toast("已预约，到点会自动开始");
    } catch (err) { toast("预约失败：" + err.message); }
  }, labels);
}

// ---- moving modes between the two lists, and long-press drag to reorder (everything glides, nothing jumps)
const KD = { lock: 0, timer: 0, x0: 0, y0: 0, x: 0, y: 0, el: null, ph: null, active: false, dx: 0, dy: 0, swallow: false, css: "" };
const kTiles = () => [...document.querySelectorAll(".kit .ksort .kmode")];

function kSaveLists() {
  const dev = $(".kit")?.dataset.dev;
  if (!dev) return;
  const ids = l => [...document.querySelectorAll(`.kit .ksort[data-list="${l}"] .kmode[data-kid]`)].map(e => e.dataset.kid);
  saveOrder(`kit:${dev}:on`, ids("on")); saveOrder(`kit:${dev}:ext`, ids("ext"));
  const n = $(".kcount"); if (n) n.textContent = ids("ext").length;
}

function kBadge(id) {
  const el = document.querySelector(`.kit .kmode[data-kid="${id}"]`);
  if (!el) return;
  const to = document.querySelector(`.kit .ksort[data-list="${el.parentElement.dataset.list === "on" ? "ext" : "on"}"]`);
  flipAnimate(kTiles(), () => to.appendChild(el));
  kSaveLists();
}

document.addEventListener("touchstart", e => {
  const it = e.target.closest(".kit .kmode[data-kid]");
  if (!it || e.touches.length !== 1 || e.target.closest(".kbadge")) return;
  KD.x0 = KD.x = e.touches[0].clientX; KD.y0 = KD.y = e.touches[0].clientY; KD.el = it;
  clearTimeout(KD.timer);
  KD.timer = setTimeout(() => kStartDrag(it), KS.editing ? 260 : 380);
}, { passive: true });

function kStartDrag(it) {
  KD.timer = 0;
  if (!KD.el) return;
  if (!KS.editing) { KS.editing = true; $(".kit")?.classList.add("kedit"); const b = $(".kedbtn"); if (b) b.textContent = "完成"; }
  navigator.vibrate?.(15);
  const r = it.getBoundingClientRect();
  KD.active = true; KD.css = it.style.cssText; KD.dx = KD.x0 - r.left; KD.dy = KD.y0 - r.top;
  KD.ph = document.createElement("div"); KD.ph.className = "kmode kgap"; KD.ph.style.height = r.height + "px";
  it.parentElement.insertBefore(KD.ph, it);
  document.body.appendChild(it);
  Object.assign(it.style, { position: "fixed", left: r.left + "px", top: r.top + "px", width: r.width + "px", zIndex: 160, margin: 0 });
  it.classList.add("klift");
}

document.addEventListener("touchmove", e => {
  if (!KD.el) return;
  const t = e.touches[0];
  KD.x = t.clientX; KD.y = t.clientY;
  if (!KD.active) {
    if (KD.timer && Math.hypot(KD.x - KD.x0, KD.y - KD.y0) > 10) { clearTimeout(KD.timer); KD.timer = 0; KD.el = null; }   // moved before the hold: it is a scroll
    if (!KD.active) return;
  }
  e.preventDefault();
  KD.el.style.left = KD.x - KD.dx + "px"; KD.el.style.top = KD.y - KD.dy + "px";
  if (KD.y > innerHeight - 90) window.scrollBy(0, 10); else if (KD.y < 110) window.scrollBy(0, -10);
  const under = document.elementFromPoint(KD.x, KD.y);
  const over = under?.closest(".kit .kmode[data-kid]"), box = under?.closest(".kit .ksort");
  const tiles = kTiles();
  if (performance.now() < KD.lock) return;                 // let the tiles finish sliding first, else the one under the finger keeps swapping
  if (over && over !== KD.ph) {
    const same = over.parentElement === KD.ph.parentElement, kids = [...over.parentElement.children];
    const after = same && kids.indexOf(KD.ph) < kids.indexOf(over);
    KD.lock = performance.now() + 260;
    flipAnimate(tiles, () => over.parentElement.insertBefore(KD.ph, after ? over.nextSibling : over));
  } else if (!over && box && !box.contains(KD.ph)) {
    KD.lock = performance.now() + 260;
    flipAnimate(tiles, () => box.appendChild(KD.ph));
  }
}, { passive: false });

function kEndDrag() {
  clearTimeout(KD.timer); KD.timer = 0;
  const el = KD.el; KD.el = null;
  if (!KD.active || !el) return;
  KD.active = false;
  const r = KD.ph.getBoundingClientRect();
  el.classList.remove("klift");
  el.style.transition = "left .2s cubic-bezier(.2,.8,.25,1), top .2s cubic-bezier(.2,.8,.25,1), transform .2s";   // glide into the gap
  el.style.left = r.left + "px"; el.style.top = r.top + "px";
  KD.swallow = true;
  const ph = KD.ph;
  setTimeout(() => {
    if (!ph.isConnected) return;
    ph.parentElement.insertBefore(el, ph); ph.remove();
    el.style.cssText = KD.css;
    kSaveLists();
    setTimeout(() => { KD.swallow = false; }, 50);
  }, 210);
}
document.addEventListener("touchend", kEndDrag);
document.addEventListener("touchcancel", kEndDrag);

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
