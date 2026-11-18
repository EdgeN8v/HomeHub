// HomeHub phone UI: a Mijia-style home page (rooms, device cards with power buttons) and a
// detail page per device. Data: /api/home (device model + states), live changes over /api/stream,
// controls through /api/call. Device-specific knowledge comes from the server's "kind" and "roles".
"use strict";

const COLORS = {
  ac: "#2f80ed", heater: "#f2711c", light: "#f5a623", switch: "#14a38b", plug: "#14a38b", fan: "#2fb4d9",
  humidifier: "#2fb4d9", water_heater: "#f2711c", bath_heater: "#f2711c", lock: "#7c5cff", camera: "#5b6cff",
  presence: "#14a38b", contact: "#7c5cff", flood: "#2f80ed", smoke: "#e5484d", gas: "#f2711c", thermo: "#14a38b",
  fridge: "#2f80ed", washer: "#2f80ed", dishwasher: "#2f80ed", cooker: "#f2711c", fryer: "#f2711c", microwave: "#7c5cff", pressure_cooker: "#14a38b", ihcooker: "#e5484d", speaker: "#7c5cff",
  vacuum: "#14a38b", treadmill: "#14a38b", gateway: "#14a38b", router: "#14a38b", remote: "#14a38b", massager: "#7c5cff", other: "#14a38b",
};
const VACUUM = { docked: "已回充", cleaning: "清扫中", returning: "回充中", paused: "已暂停", idle: "待命", error: "出错了", unknown: "—" };
const HVAC = { cool: "制冷", heat: "制热", dry: "除湿", fan_only: "送风", auto: "自动", heat_cool: "自动", off: "关闭" };
const ON_STATES = new Set(["on", "cool", "heat", "dry", "fan_only", "auto", "heat_cool", "playing", "open", "unlocked"]);

const S = { model: null, states: {}, byId: {}, home: null, room: "全部", toast: 0 };
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const st = id => S.states[id] || { s: "unavailable", a: {} };
const val = id => (id ? st(id).s : undefined);
const num = id => { const v = parseFloat(val(id)); return Number.isFinite(v) ? v : null; };
const isOn = id => ON_STATES.has(val(id));
const dead = s => s === "unavailable" || s === undefined;
const fmtNum = v => (Math.round(v * 10) / 10).toString();

// ---------------------------------------------------------------- data

// Inside the app (1.8+) every request is relayed by the app, which can't read request bodies, so a
// POST also carries its JSON body base64'd in X-HH-Body. Harmless for the server and older apps.
function withBody(opts) {
  if (!opts || String(opts.method || "GET").toUpperCase() !== "POST") return opts;
  const body = opts.body ?? "";
  const b64 = btoa(String.fromCharCode(...new TextEncoder().encode(body)));
  return { ...opts, headers: { ...(opts.headers || {}), "X-HH-Body": b64 } };
}

async function api(path, opts) {
  const r = await fetch(path, withBody(opts));
  if (!r.ok) throw new Error((await r.text()) || r.status);
  return r.json();
}

// The camera is not an HA device: it is added to the model here (only inside the phone app, which has the native player) so it is a card
// like any other - long-press, drag, move to another room, rename, delete. Its name / room / hidden live in /api/cam/prefs.
const CAM_ID = "cam:c700";
function injectCam(m) {
  if (!window.HomeHubApp?.openCamera) return;
  if (!S.camPrefs) { try { S.camPrefs = JSON.parse(localStorage.getItem("cam-prefs") || "{}"); } catch (e) { S.camPrefs = {}; } }
  const p = S.camPrefs, home = m.homes.find(h => h.rooms.includes(p.room || "卧室")) || m.homes[0];
  // the same camera also comes in from Mijia's cloud list: one card only, this one
  m.devices = m.devices.filter(d => d.id !== CAM_ID && !(d.kind === "camera" && /chuangmi\.camera/.test(d.model || "")));
  m.hidden = (m.hidden || []).filter(i => i !== CAM_ID);
  if (!home) return;
  m.devices.unshift({ id: CAM_ID, name: p.name || "小米智能摄像机 C700", model: "chuangmi.camera.81ac1", kind: "camera", kind_name: "摄像头",
    home: home.name, room: home.rooms.includes(p.room) ? p.room : (home.rooms.includes("卧室") ? "卧室" : home.rooms[0]),
    roles: {}, toggles: [], features: [], more: [], events: [], entities: [] });
  if (p.hidden) m.hidden.push(CAM_ID);
}
async function camPref(patch) {
  S.camPrefs = { ...(S.camPrefs || {}), ...patch };
  try { localStorage.setItem("cam-prefs", JSON.stringify(S.camPrefs)); } catch (e) { /* ignore */ }
  try { await api("/api/cam/prefs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) }); }
  catch (e) { toast("没保存上：" + e.message); }
}

function apply(m) {
  injectCam(m);
  S.model = m;
  S.states = m.states;
  S.byId = Object.fromEntries(m.devices.map(d => [d.id, d]));
  // Offline devices sink to the bottom of their page - decided when the data is (re)loaded, not live, so cards
  // don't jump around under a finger when something drops off for a moment.
  S.offIds = new Set(m.devices.filter(deviceOffline).map(d => d.id));
  if (!S.home || !m.homes.find(h => h.name === S.home)) S.home = m.homes[0]?.name ?? "";
  render();
}

// Each phone keeps its own card order (2026-10: two people, different habits). The phone model from the
// WebView user agent survives reinstalls; a random id kept in storage covers browsers that hide it.
const PHONE = (() => {
  const m = navigator.userAgent.match(/Android [\d.]+; (?:[a-z]{2}[-_][a-z]{2}; )?([^;)]+?)(?: Build\/|\))/i);
  if (m && m[1] !== "K" && /^[\w .+-]{1,40}$/.test(m[1])) return m[1];
  try {
    let id = localStorage.getItem("phone-id");
    if (!id) { id = "web-" + Math.random().toString(36).slice(2, 10); localStorage.setItem("phone-id", id); }
    return id;
  } catch (e) { return ""; }
})();

async function load(fresh) {
  const q = [PHONE ? "phone=" + encodeURIComponent(PHONE) : "", fresh ? "fresh=1" : ""].filter(Boolean).join("&");
  const [m, prefs] = await Promise.all([api("/api/home" + (q ? "?" + q : "")),
    window.HomeHubApp?.openCamera ? api("/api/cam/prefs").catch(() => null) : null]);
  if (prefs) { S.camPrefs = prefs; try { localStorage.setItem("cam-prefs", JSON.stringify(prefs)); } catch (e) { /* ignore */ } }
  apply(m);
  try { localStorage.setItem("home-snapshot", JSON.stringify(m)); } catch (e) { /* storage full or disabled: just no instant start */ }
}

// Open on the last snapshot right away (like Mijia) and let load() swap in fresh data a moment later.
function showCached() {
  try {
    const m = JSON.parse(localStorage.getItem("home-snapshot") || "null");
    if (m?.devices) { apply(m); return true; }
  } catch (e) { /* corrupt cache: ignore */ }
  return false;
}

let es;
function listen() {
  es?.close();
  es = new EventSource("/api/stream");
  es.onmessage = ev => mergeStates(JSON.parse(ev.data));
}

// A control shows its result at once (optimistic) and the shown state is "pinned" for a few seconds,
// so an older state still on its way can't flip it back before the device has reported.
S.pins = {};
function mergeStates(obj) {
  const now = Date.now();
  for (const [id, v] of Object.entries(obj)) {
    const pin = S.pins[id];
    if (pin && now < pin.until && v.s !== pin.s) continue;
    delete S.pins[id];
    S.states[id] = v;
  }
  scheduleRender();
}

// The live stream can arrive seconds late inside the app, so after a control ask for the touched
// entities directly a few times as well; whichever comes first wins.
async function pollStates(ids) {
  for (const wait of [400, 600, 900, 1500, 2500]) {
    await new Promise(r => setTimeout(r, wait));
    try { mergeStates(await api("/api/states?ids=" + ids.map(encodeURIComponent).join(","))); } catch (e) { return; }
  }
}

let renderPending = false;
function scheduleRender() {
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(() => { renderPending = false; render(); });
}

let busy = 0;   // while a slider is being dragged, don't re-render under the finger
// optimistic: {s, a} shown for entity_id right away. watch: entities to re-read after the call (default
// entity_id); with pinState: {id: state} also shown at once for other entities (e.g. a washer's status
// when its stop button is pressed).
async function call(entity_id, service, data = {}, optimistic, watch, pinState) {
  const prev = S.states[entity_id];
  const until = Date.now() + 5000;
  if (optimistic) {
    S.states[entity_id] = { ...st(entity_id), ...optimistic };
    if (optimistic.s) S.pins[entity_id] = { s: optimistic.s, until };
  }
  for (const [id, s] of Object.entries(pinState || {})) { S.states[id] = { ...st(id), s }; S.pins[id] = { s, until }; }
  if (optimistic || pinState) render();
  try {
    await api("/api/call", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entity_id, service, data }) });
    pollStates(watch || [entity_id]);
  } catch (e) {
    for (const id of Object.keys(pinState || {})) delete S.pins[id];
    delete S.pins[entity_id];
    if (prev) S.states[entity_id] = prev;
    if (pinState) pollStates(Object.keys(pinState));   // put back what the device really shows
    toast("操作失败：" + e.message);
    render();
  }
}

function toggle(id) {
  const d = id.split(".")[0];
  const on = isOn(id);
  if (d === "climate") return call(id, on ? "turn_off" : "turn_on", {}, { s: on ? "off" : (st(id).a.hvac_mode || "auto") });
  if (d === "water_heater") return call(id, on ? "turn_off" : "turn_on", {}, { s: on ? "off" : "on" });
  call(id, on ? "turn_off" : "turn_on", {}, { s: on ? "off" : "on" });
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(S.toast); S.toast = setTimeout(() => t.classList.remove("show"), 2600);
}

// ---------------------------------------------------------------- status text

function deviceOffline(d) {
  const ids = d.toggles.length ? d.toggles.map(t => t.entity_id) : Object.values(d.roles);
  return ids.length > 0 && ids.every(id => dead(val(id)));
}

// FLIP: remember where each element is, make the change, then glide every element from where it was to where it is
// now. This is what makes reordering look like cards sliding aside instead of jumping.
function flipAnimate(els, mutate, ms = 280) {
  const before = new Map(els.map(e => [e, e.getBoundingClientRect()]));
  mutate();
  for (const e of els) {
    if (!e.isConnected) continue;
    const a = before.get(e), b = e.getBoundingClientRect(), dx = a.left - b.left, dy = a.top - b.top;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
    e.style.transition = "none"; e.style.transform = `translate(${dx}px, ${dy}px)`;
    void e.offsetWidth;
    e.style.transition = `transform ${ms}ms cubic-bezier(.2, .8, .25, 1)`; e.style.transform = "";
    const done = () => { e.style.transition = ""; e.removeEventListener("transitionend", done); };
    e.addEventListener("transitionend", done);
  }
}

const KITCHEN_KINDS = { fryer: 1, microwave: 1, pressure_cooker: 1, ihcooker: 1 };

function deviceActive(d) {
  if (d.toggles.length) return d.toggles.some(t => isOn(t.entity_id));
  const r = d.roles;
  if (r.main) return isOn(r.main);
  if (d.kind === "presence") return val(r.occupancy) === "on";
  if (d.kind === "contact") return val(r.door) === "on";
  if (d.kind === "flood") return val(r.moisture) === "on";
  if (d.kind === "vacuum") return ["cleaning", "returning"].includes(val(r.main));
  if (d.kind === "washer") return busyStatus(washerStatus(d));   // both drums' own status, not the door sensor
  if (["dishwasher", "cooker", "treadmill", "massager"].includes(d.kind)) return busyStatus(val(r.status) ?? washerStatus(d));
  if (d.kind in KITCHEN_KINDS) return !kIdle(kv(d, "status")) && !dead(kv(d, "status"));
  return false;
}

const busyStatus = s => !!s && /中|烹饪|工作|运动|加热|保温/.test(s) && !/待机|关机|停止|空闲|故障/.test(s);

function washerStatus(d) {
  const up = d.entities.find(e => e.endsWith("_status_p_12_28")), lo = d.entities.find(e => e.endsWith("_status_p_13_28"));
  if (!up && !lo) return null;
  const a = val(up), b = val(lo);
  const busyOne = [a, b].find(busyStatus);
  return busyOne ?? a ?? b;
}

function minutes(id) {
  const v = num(id);
  if (!v) return "";
  const unit = st(id).a.unit_of_measurement;
  const m = unit === "s" ? Math.round(v / 60) : unit === "h" ? v * 60 : v;
  return m >= 60 ? `${Math.floor(m / 60)}小时${m % 60 ? (m % 60) + "分" : ""}` : `${m}分钟`;
}

function sensorText(id) {
  const s = st(id);
  if (dead(s.s)) return "离线";
  if (s.s === "unknown") return "—";
  const u = s.a.unit_of_measurement;
  const n = parseFloat(s.s);
  if (Number.isFinite(n) && u) return `${fmtNum(n)}${u === "°C" ? "°" : u === "%" ? "%" : " " + u}`;
  return s.s;
}

function binaryText(id) {
  const s = val(id), dc = st(id).a.device_class;
  if (dead(s)) return "离线";
  const on = s === "on";
  return ({ door: on ? "已打开" : "已关闭", window: on ? "已打开" : "已关闭", occupancy: on ? "有人" : "无人",
            motion: on ? "有人" : "无人", moisture: on ? "漏水！" : "正常" }[dc]) ?? (on ? "是" : "否");
}

function summary(d, short = false) {
  const r = d.roles, k = d.kind;
  if (deviceOffline(d)) return { text: "离线" };
  if (k === "ac" || k === "heater") {
    const c = st(r.main);
    const due = k === "ac" && S.model.ac_clean?.[r.main]?.due;
    if (c.s === "off") return { text: due ? "已关闭 · 该清洁了" : "已关闭" + (!short && c.a.current_temperature != null ? ` · 室温${fmtNum(c.a.current_temperature)}°` : "") };
    const t = c.a.temperature != null ? ` ${fmtNum(c.a.temperature)}°` : "";
    if (k === "heater") return { text: `${c.a.hvac_action === "idle" ? "保温" : "加热"}${t}` };
    return { text: `${HVAC[c.s] ?? c.s}${t}${c.a.preset_mode ? " · " + c.a.preset_mode : ""}` };
  }
  if (k in KITCHEN_KINDS && window.kitchenSummary) return { text: kitchenSummary(d) };
  if (k === "light") {
    const l = st(r.main);
    if (l.s !== "on") return { text: "已关闭" };
    return { text: l.a.brightness != null ? `亮度 ${Math.round(l.a.brightness / 2.55)}%` : "已开启" };
  }
  if (k === "fan") { const f = st(r.main); return { text: f.s !== "on" ? "已关闭" : (f.a.preset_mode || (f.a.percentage != null ? `风速 ${f.a.percentage}%` : "已开启")) }; }
  if (k === "humidifier") { const h = st(r.main); return { text: h.s !== "on" ? "已关闭" : `目标湿度 ${h.a.humidity ?? "—"}%` }; }
  if (k === "water_heater") {
    const a = st(r.main).a, x = d.wh || {};
    return { text: [val(r.status), x.sterilize && isOn(x.sterilize) && "除菌中", x.anti_icing && val(x.anti_icing) === "Antifreeze" && "防冻中",
      a.temperature != null ? `${fmtNum(a.temperature)}°` : "",
      !short && a.current_temperature != null ? `水温${fmtNum(a.current_temperature)}°` : ""].filter(Boolean).join(" · ") || "—" };
  }
  if (k === "bath_heater") { const on = d.toggles.filter(t => isOn(t.entity_id)).map(t => t.label); return { text: on.length ? on.join(" · ") : "已关闭" }; }
  if (k === "switch" || k === "plug") {
    if (d.toggles.length > 1) { const n = d.toggles.filter(t => isOn(t.entity_id)).length; return { text: n ? `${n} 路开着` : "已关闭" }; }
    const on = d.toggles[0] && isOn(d.toggles[0].entity_id);
    return { text: (on ? "已开启" : "已关闭") + (on && r.power && num(r.power) ? ` · ${sensorText(r.power)}` : "") };
  }
  if (k === "lock") {
    // The lock reports "门虚掩" right after a lock/unlock but never says the door got closed later, so the
    // warning only lasts 10 minutes from that report; after that it is just a note.
    const ajar = val(r.status) === "门虚掩", since = ajar ? Date.parse(st(r.status).t) : NaN;
    const fresh = ajar && !(Date.now() - since > 10 * 60 * 1000);
    return { text: [fresh || !ajar ? val(r.status) : "门虚掩（已超 10 分钟，不再报警）", r.battery && `电量 ${sensorText(r.battery)}`].filter(Boolean).join(" · "), warn: fresh };
  }
  if (k === "presence") return { text: binaryText(r.occupancy) + (r.illuminance ? ` · ${sensorText(r.illuminance)}` : "") };
  if (k === "contact") return { text: binaryText(r.door) };
  if (k === "flood") return { text: binaryText(r.moisture), warn: val(r.moisture) === "on" };
  if (k === "smoke") return { text: val(r.status) ?? "—", warn: !!val(r.status) && !/监测中|正常/.test(val(r.status)) };
  if (k === "gas") {
    // The sensor reports a concentration that is rarely exactly 0 (1 = trace); what counts is its own verdict
    // (监测正常 / 报警...), or a clearly high reading when it gives no verdict.
    const v = val(r.status);
    return { text: `${v ?? "—"}${r.gas ? " · " + sensorText(r.gas) : ""}`, warn: v ? !/监测|正常/.test(v) : num(r.gas) >= 10 };
  }
  if (k === "thermo") return { text: [r.temperature && sensorText(r.temperature), r.humidity && sensorText(r.humidity)].filter(Boolean).join(" · ") };
  if (k === "fridge") {
    const zones = Object.entries(r).filter(([key]) => key.startsWith("zone:")).map(([key, id]) => `${key.slice(5).replace("室", "")} ${sensorText(id)}`);
    return { text: zones.join(" · ") };
  }
  if (k === "washer") {
    if (window.washerSummary && wPrefix(d)) return { text: washerSummary(d) };
    const s = washerStatus(d); return { text: s ?? "—" };
  }
  if (k === "vacuum") {
    const v = st(r.main), bat = v.a.battery_level != null ? ` · 电量${Math.round(v.a.battery_level)}%` : "";
    return { text: (VACUUM[v.s] ?? v.s) + (v.s === "error" ? "" : bat), warn: v.s === "error" };
  }
  if (k === "speaker") { const s = val(r.main); return { text: s === "playing" ? "播放中" : s === "off" ? "已关闭" : "空闲" }; }
  if (r.status) {
    const s = val(r.status), left = r.left && busyStatus(s) ? minutes(r.left) : "";
    return { text: s + (left ? ` · 剩${left}` : "") };
  }
  if (d.toggles.length) return { text: isOn(d.toggles[0].entity_id) ? "已开启" : "已关闭" };
  if (r.battery) return { text: `电量 ${sensorText(r.battery)}` };
  return { text: "在线" };
}

// ---------------------------------------------------------------- home page

function homeDevices() {
  const hidden = new Set(S.model.hidden || []);
  return S.model.devices.filter(d => d.home === S.home && !hidden.has(d.id));
}

// The stat chips and alarm banner for a room (Mijia shows the selected room's numbers; temperature and
// humidity fall back to the whole home when the room has no thermometer).
function headerStats(room, devs = homeDevices()) {
  const shown = room === "全部" ? devs : devs.filter(d => d.room === room);
  const thermosIn = ds => ds.filter(d => d.kind === "thermo" && num(d.roles.temperature) != null);
  const thermos = thermosIn(shown).length ? thermosIn(shown) : thermosIn(devs);
  const avg = k => thermos.length ? fmtNum(thermos.reduce((a, d) => a + num(d.roles[k]), 0) / thermos.length) : null;
  const lightsOn = shown.filter(d => !deviceOffline(d)).reduce((n, d) =>
    n + (d.kind === "light" ? (isOn(d.roles.main) ? 1 : 0) : d.toggles.filter(t => /灯/.test(t.label) && isOn(t.entity_id)).length), 0);
  const alarms = devs.filter(d => ["smoke", "gas", "flood", "lock"].includes(d.kind)).filter(d => summary(d).warn);
  const guards = devs.filter(d => ["smoke", "gas", "flood"].includes(d.kind));
  const stats = [
    avg("temperature") && `<div class="stat tap" data-act="temps" role="button">${icon("thermo")}${avg("temperature")}°</div>`,
    avg("humidity") && `<div class="stat">${icon("humidifier")}${avg("humidity")}%</div>`,
    `<div class="stat tap" data-act="energy" role="button">${icon("bolt")}<span class="enow">${typeof enChipText === "function" ? enChipText() : "用电"}</span></div>`,
    `<div class="stat tap" data-act="lights" role="button">${icon("light")}亮灯 ${lightsOn}</div>`,
    guards.length && `<div class="stat ${alarms.length ? "warn" : ""}">${icon(alarms.length ? "alert" : "shield")}${alarms.length ? "有报警" : "安全"}</div>`,
  ].filter(Boolean).join("");
  const alertHtml = alarms.length
    ? `<div class="alert">${icon("alert")}<span>${alarms.map(d => `${esc(d.name)}：${esc(summary(d).text)}`).join("；")}</span></div>` : "";
  return { stats, alertHtml };
}

function renderHome() {
  const devs = homeDevices();
  const home = S.model.homes.find(h => h.name === S.home) || { rooms: [] };
  const rooms = home.rooms;
  if (S.room !== "全部" && !rooms.includes(S.room)) S.room = "全部";

  const { stats, alertHtml } = headerStats(S.room, devs);

  const homes = S.model.homes;
  const pages = roomList(), idx = pages.indexOf(S.room);

  $("#app").innerHTML = `
    <div class="page">
      <div class="top">
        <div class="top-row">
          <div class="home-name" ${homes.length > 1 ? 'data-act="switch-home"' : ""}><span>${esc(S.home || "我的家")}</span>${homes.length > 1 ? `<span class="caret">${icon("caret")}</span>` : ""}</div>
          <div class="top-right">${weatherHtml()}<button class="add-btn" data-act="add-device" aria-label="添加设备">${icon("plus")}</button></div>
        </div>
        ${S.offline ? `<div class="offline-note">连不上家里，显示的是上次的状态，正在重连…</div>` : ""}
        <div class="stats">${stats}</div>
        ${alertHtml}
      </div>
      <div class="tabs">${["全部", ...rooms].map(r => `<button class="tab ${r === S.room ? "on" : ""}" data-room="${esc(r)}">${esc(r)}</button>`).join("")}<span class="tab-ind"></span></div>
      <div class="pager"><div class="track" style="transform: translateX(${-idx * 100}%)">
        ${pages.map(r => `<div class="roomview ${r === S.room ? "cur" : ""}">${roomPage(r, devs, rooms)}</div>`).join("")}
      </div></div>
    </div>${bottomNav("home")}`;
}

// One page of the room pager: "全部" is one flat grid (no room titles - the tabs already say which room is
// which), with its own saved order so favourites from any room can be pinned to the top; a room tab is
// just that room's grid.
// Until "全部" has an order of its own, it lists the rooms in turn, each room's devices in the order
// arranged on that room's tab.
const allDevices = (devs, rooms) => rooms.flatMap(r => ordered(devs.filter(d => d.room === r), r));

// The camera card, like Mijia's: the last picture with a play button (plays right here), and the latest movements (tap = that clip, here too).
// The picture itself is the phone's native player laid over this card (see camInline); the events and the still come from the hub's recorder.
// The last answers are kept on the phone, so the card comes up at once as it was (power, latest movements) and is then updated.
function camSave() { try { localStorage.setItem("cam-last", JSON.stringify({ cam: S.cam, power: S.camPower })); } catch (e) { /* ignore */ } }
function camLoad(force) {
  if (S.cam === undefined) {
    try { const c = JSON.parse(localStorage.getItem("cam-last") || "null"); if (c) { S.cam = c.cam; S.camPower = null; S.camState = "checking"; } } catch (e) { /* ignore */ }
  }
  if (!force && S.camAt && Date.now() - S.camAt < 15000) return;
  S.camAt = Date.now();
  const redraw = () => { if (!S.edit && !drag.active && !S.camInl) scheduleRender(); };
  api("/api/cam/events").then(r => {
    const same = JSON.stringify((S.cam?.events || []).slice(0, 3)) === JSON.stringify((r.events || []).slice(0, 3));
    S.cam = r; camSave(); if (!same) redraw();
  }).catch(() => {});
  api("/api/cam/power").then(r => {
    const state = r.online === false ? "offline" : typeof r.on !== "boolean" ? "unknown" : r.on ? "online" : "sleeping";
    if (S.camPower !== r.on || S.camState !== state) { S.camPower = r.on; S.camState = state; camSave(); redraw(); }
  }).catch(() => { S.camPower = null; S.camState = "unknown"; redraw(); });
  // the newest picture (the hub takes one every 30 s while the camera is on): fetched off-screen, then swapped in - no flash
  camPicFetch("latest", "/api/cam/file/latest.jpg?t=" + Math.floor(Date.now() / 15000), 720, true);
}

// The card's pictures are kept in the page itself, small (a few dozen KB), as data: URLs - so the card is drawn complete in its very
// first frame, never grey. A movement's thumbnail is fetched once, when the movement is new; the big picture is renewed in the background.
const CAM_PICS = (() => { try { return JSON.parse(localStorage.getItem("cam-pics") || "{}"); } catch (e) { return {}; } })();
const camPicBusy = {};
function camPic(key, url, w) {
  if (CAM_PICS[key]) return CAM_PICS[key];
  camPicFetch(key, url, w, false);
  return url;
}
function camPicFetch(key, url, w, swap) {
  if (camPicBusy[key]) return;
  camPicBusy[key] = true;
  const im = new Image();
  im.onload = () => {
    camPicBusy[key] = false;
    try {
      const c = document.createElement("canvas"), k = Math.min(1, w / im.naturalWidth);
      c.width = Math.round(im.naturalWidth * k); c.height = Math.round(im.naturalHeight * k);
      c.getContext("2d").drawImage(im, 0, 0, c.width, c.height);
      const data = c.toDataURL("image/jpeg", 0.72);
      if (CAM_PICS[key] === data) return;
      CAM_PICS[key] = data;
      // keep only what the card shows: the big picture and the three latest movements
      const keep = new Set(["latest", ...(S.cam?.events || []).slice(0, 3).map(e => e.id)]);
      for (const k2 of Object.keys(CAM_PICS)) if (!keep.has(k2)) delete CAM_PICS[k2];
      try { localStorage.setItem("cam-pics", JSON.stringify(CAM_PICS)); } catch (e) { /* full: next time */ }
      if (S.camInl) return;
      document.querySelectorAll(`[data-pic="${key}"]`).forEach(el => { el.src = data; });
    } catch (e) { /* ignore */ }
  };
  im.onerror = () => { camPicBusy[key] = false; };
  im.src = url;
}
const camHM = ts => { const t = new Date(ts * 1000); return String(t.getHours()).padStart(2, "0") + ":" + String(t.getMinutes()).padStart(2, "0"); };

function camCard(d) {
  camLoad(false);
  const evs = (S.cam?.events || []).slice(0, 3);
  // like Mijia: the picture fills the card edge to edge with the name and 进入 on it; the latest movements under it, 〉 = all of them
  return `<div class="card camcard" data-dev="${d.id}" role="button">
    ${S.camPower !== true
      ? `<div class="cam-view cam-off" data-act="cam-wake"><img data-pic="latest" src="${camPic("latest", "/api/cam/file/latest.jpg", 720)}" onerror="this.style.display='none'">
          <div class="cam-hd"><b>${esc(d.name)}</b><span class="cam-go" data-act="cam-open">进入 ›</span></div>
          <div class="cam-offin">${S.camWaking ? `<span class="cam-wakebtn busy"></span><em>正在开启…</em>`
            : `<span class="cam-wakebtn">${icon("power")}</span><em>${S.camState === "sleeping" ? "摄像头已关闭，点按开启" : S.camState === "offline" ? "摄像头离线 · 点击重试" : S.camState === "unknown" ? "无法连接摄像头 · 点击重试" : "正在确认摄像头状态"}</em>`}</div></div>`
      : `<div class="cam-view cam-preview" data-act="cam-play"><img data-pic="latest" src="${camPic("latest", "/api/cam/file/latest.jpg", 720)}" onerror="this.style.display='none'">
          <div class="cam-hd"><b>${esc(d.name)}</b><span class="cam-go" data-act="cam-open">进入 ›</span></div>
          <span class="cam-play">▶</span><span class="cam-still-label">上次画面 · 点击播放</span></div>`}
    <div class="cam-ev">${evs.map(e => `<div class="cam-th" data-act="cam-clip" data-arg="${esc(e.id)}|${esc(e.clip || e.id + ".mp4")}">
      ${e.thumb ? `<img data-pic="${esc(e.id)}" src="${camPic(e.id, "/api/cam/file/" + e.thumb, 360)}">` : ""}<span>${camHM(e.ts)}</span></div>`).join("") || (S.cam ? `<div class="cam-none">还没有移动记录</div>` : `<div class="cam-th"></div><div class="cam-th"></div><div class="cam-th"></div>`)}
      <div class="cam-more" data-act="cam-all">${evs.length ? "›" : ""}</div></div>
    <div class="selmark">${icon("check")}</div>
  </div>`;
}

// every movement the hub recorded, newest first, by day
function camAll() {
  const evs = S.cam?.events || [], days = [];
  const label = ts => { const t = new Date(ts * 1000), n = new Date(); const dd = Math.round((new Date(n.getFullYear(), n.getMonth(), n.getDate()) - new Date(t.getFullYear(), t.getMonth(), t.getDate())) / 864e5);
    return dd === 0 ? "今天" : dd === 1 ? "昨天" : `${t.getMonth() + 1}月${t.getDate()}日`; };
  for (const e of evs) { const l = label(e.ts); let g = days[days.length - 1]; if (!g || g.l !== l) days.push(g = { l, es: [] }); g.es.push(e); }
  sheet(`<h3>移动记录</h3><div class="cam-all">${days.map(g => `<div class="cam-day">${g.l}</div><div class="cam-grid">${g.es.map(e =>
    `<div class="cam-th" data-act="cam-clipfull" data-arg="${esc(e.id)}|${esc(e.clip || "")}">${e.thumb ? `<img src="/api/cam/file/${esc(e.thumb)}">` : ""}<span>${camHM(e.ts)} · ${e.dur || 1}秒</span></div>`).join("")}</div>`).join("")
    || `<p class="sheet-note">还没有记录。有人或宠物在画面里走动时，这里会出现。</p>`}</div>`);
}

// Plays the live picture or a clip inside the card: the native player is laid over the card's picture box and follows it while the page
// scrolls. Back / another room / leaving the page / the clip ending all put the still picture back.
// switching on from the card: the camera needs a few seconds to raise its head, then the live picture starts in the card
async function camWake() {
  if (S.camWaking) return;
  S.camWaking = true; scheduleRender();
  try {
    const state = await api("/api/cam/power");
    if (state.online === false || typeof state.on !== "boolean") { S.camState = "offline"; throw new Error("摄像头离线，请检查供电和网络"); }
    if (!state.on) {
      const result = await api("/api/cam/power?on=1", { method: "POST" });
      if (result.on !== true) throw new Error("摄像头未确认开启");
    }
    S.camPower = true; S.camState = "online"; camSave(); scheduleRender();
    setTimeout(() => { S.camWaking = false; camInline(""); }, 3500);
  } catch (e) { S.camWaking = false; scheduleRender(); toast("没开成：" + e.message); }
}
function camInline(clip) {
  if (!clip && S.camPower !== true) { camWake(); return; }
  const el = document.querySelector(".roomview.cur .cam-view");
  if (!el || !HomeHubApp.playInline) { HomeHubApp.openCamera("c700", clip || ""); return; }
  if (S.camInl) { if (clip) { camStop(); } else { HomeHubApp.camTap(); return; } }     // playing already: a tap on the picture shows / hides the buttons
  const r = el.getBoundingClientRect(), card = el.closest(".card");
  S.camInl = { room: S.room, key: "" };
  HomeHubApp.playInline("c700", clip || "", r.left, r.top, r.width, r.height, S.byId[CAM_ID]?.name || "", parseFloat(getComputedStyle(card).borderTopLeftRadius) || 20);
  requestAnimationFrame(camTrack);
}
function camTrack() {
  if (!S.camInl) return;
  const el = document.querySelector(".roomview.cur .cam-view");
  if (!el || S.room !== S.camInl.room || S.edit || !document.querySelector("#app .page .tabs")) return camStop();
  const r = el.getBoundingClientRect(), top = (parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--sat")) || 0) + 8;
  const vis = r.top >= top && r.bottom <= innerHeight - 70;
  const key = [r.left, r.top, r.width, r.height, vis].join();
  if (key !== S.camInl.key) { S.camInl.key = key; HomeHubApp.moveInline(r.left, r.top, r.width, r.height, vis); }
  requestAnimationFrame(camTrack);
}
function camStop() {
  if (!S.camInl) return;
  S.camInl = null;
  try { HomeHubApp.stopInline(); } catch (e) { /* older app */ }
}
window.hhCamEnded = camStop;

function roomPage(room, devs, rooms) {
  const list = room === "全部" ? allDevices(devs, rooms) : devs.filter(d => d.room === room);
  const offlineLast = ds => [...ds.filter(d => !S.offIds?.has(d.id)), ...ds.filter(d => S.offIds?.has(d.id))];
  return (list.length ? `<div class="grid" data-order="${esc(orderKey(room))}">${offlineLast(ordered(list, room)).map(card).join("")}</div>`
                      : `<div class="empty">这个房间还没有设备</div>`)
    + ((S.model.hidden || []).length && room === "全部"
      ? `<div class="foot"><a href="#" data-act="show-hidden">已删除的设备（${S.model.hidden.length}）</a></div>` : "");
}

// ---------------------------------------------------------------- card order (long-press drag)

// One saved order per page: each room tab has its own, and "全部" has one for the whole home. Devices
// not in the saved list keep the server's default order after the saved ones.
const orderKey = room => `${S.home}/${room}`;
function ordered(ds, room) {
  const saved = S.model.order?.[orderKey(room)];
  if (!saved) return ds;
  const pos = new Map(saved.map((id, i) => [id, i]));
  return [...ds].sort((a, b) => (pos.get(a.id) ?? (a.kind === "camera" ? -1 : 1e6 + ds.indexOf(a))) - (pos.get(b.id) ?? (b.kind === "camera" ? -1 : 1e6 + ds.indexOf(b))));
}

// ---------------------------------------------------------------- edit mode (long press), like Mijia
//
// Long-press a card: the page enters edit mode with that card selected, in place. Keep the finger
// down and move to carry it; tap other cards to add them to the selection. Top bar: count and ✓;
// bottom bar: 移到顶部 / 移动设备 / 重命名 / 删除设备 (= remove from the home page).
// Edit mode lives in S.edit (a Set of device ids) and as classes on the page, so entering it never
// re-renders under the finger.

const drag = { timer: 0, x0: 0, y0: 0, x: 0, y: 0, pending: null, active: false, card: null, ph: null, grid: null,
               dx: 0, dy: 0, raf: 0, swallowClick: false };

document.addEventListener("touchstart", e => {
  const c = e.target.closest(".roomview .card[data-dev]");
  if (!c || e.touches.length !== 1 || e.target.closest("[data-toggle]")) return;
  drag.x0 = drag.x = e.touches[0].clientX; drag.y0 = drag.y = e.touches[0].clientY;
  drag.pending = c;
  clearTimeout(drag.timer);
  if (!S.edit) drag.timer = setTimeout(() => enterEdit(c), 450);
}, { passive: true });

function enterEdit(c) {
  drag.timer = 0;
  S.edit = new Set([c.dataset.dev]);
  swipe.t = 0;                 // this touch is not a room swipe any more
  drag.swallowClick = true;    // the finger lifting from the long press must not toggle the selection
  navigator.vibrate?.(15);
  decorateEdit();
}

function exitEdit() {
  S.edit = null;
  decorateEdit();
  scheduleRender();
}

// Selection marks, dimming and the two bars, applied to whatever is on screen.
function decorateEdit() {
  const on = !!S.edit;
  document.body.classList.toggle("editing", on);
  document.querySelectorAll(".card[data-dev]").forEach(c => c.classList.toggle("sel", on && S.edit.has(c.dataset.dev)));
  $("#edit-top")?.remove(); $("#edit-bottom")?.remove();
  if (!on) return;
  const n = S.edit.size;
  document.body.insertAdjacentHTML("beforeend", `
    <div id="edit-top"><button class="eb-x" data-act="edit-cancel">取消</button><div>已选择 ${n}</div>
      <button class="eb-ok" data-act="edit-done">${icon("check")}</button></div>
    <div id="edit-bottom">
      <button data-act="edit-top">${icon("totop")}<span>移到顶部</span></button>
      <button data-act="edit-move">${icon("move")}<span>移动设备</span></button>
      <button data-act="edit-rename" ${n === 1 ? "" : "disabled"}>${icon("rename")}<span>重命名</span></button>
      <button data-act="edit-delete">${icon("trash")}<span>删除设备</span></button>
    </div>`);
}

function toggleSelect(id) {
  S.edit.has(id) ? S.edit.delete(id) : S.edit.add(id);
  if (!S.edit.size) return exitEdit();
  decorateEdit();
}

// Non-passive so the page doesn't scroll while a card is carried. Selected cards also have
// touch-action: none (CSS), so a drag that starts on one is never taken over by scrolling.
document.addEventListener("touchmove", e => {
  const t = e.touches[0];
  drag.x = t.clientX; drag.y = t.clientY;
  const moved = Math.hypot(drag.x - drag.x0, drag.y - drag.y0) > 8;
  if (!drag.active) {
    if (drag.timer && moved) { clearTimeout(drag.timer); drag.timer = 0; }
    if (S.edit && moved && drag.pending && S.edit.has(drag.pending.dataset.dev)) startDrag(drag.pending);
    if (!drag.active) return;
  }
  e.preventDefault();
  drag.card.style.left = drag.x - drag.dx + "px";
  drag.card.style.top = drag.y - drag.dy + "px";
  const over = document.elementFromPoint(drag.x, drag.y)?.closest(".card");
  if (!over || over === drag.ph || over.parentElement !== drag.grid || performance.now() < (drag.lock || 0)) return;
  drag.lock = performance.now() + 260;                    // let the cards finish sliding first, else the one under the finger keeps swapping
  const kids = [...drag.grid.children];
  flipAnimate(kids.filter(c => c !== drag.card), () => drag.grid.insertBefore(drag.ph, kids.indexOf(drag.ph) < kids.indexOf(over) ? over.nextSibling : over));
}, { passive: false });

// The card leaves its grid for <body> while carried: the room view is transformed (swipe animation),
// which would make position: fixed relative to it and throw the card away from the finger.
function startDrag(c) {
  const r = c.getBoundingClientRect();
  drag.active = true; drag.card = c; drag.grid = c.parentElement; drag.css = c.style.cssText;
  drag.dx = drag.x0 - r.left; drag.dy = drag.y0 - r.top;   // grab point inside the card, from where the finger went down
  drag.ph = document.createElement("div");
  drag.ph.className = "card ph";
  drag.ph.style.height = r.height + "px";
  drag.ph.style.gridColumn = getComputedStyle(c).gridColumn;
  drag.grid.insertBefore(drag.ph, c);
  document.body.appendChild(c);
  Object.assign(c.style, { position: "fixed", left: r.left + "px", top: r.top + "px", width: r.width + "px", height: r.height + "px", zIndex: 100, margin: 0 });
  c.classList.add("lifted");
  autoScroll();
}

document.addEventListener("touchend", endTouch);
document.addEventListener("touchcancel", endTouch);

function endTouch() {
  clearTimeout(drag.timer); drag.timer = 0;
  if (drag.pending && !drag.active && S.stale) scheduleRender();   // catch up on updates held back while the finger was down - only if there were any
  drag.pending = null;
  if (!drag.active) return;
  cancelAnimationFrame(drag.raf);
  const { card, ph, grid } = drag, css = drag.css;
  drag.active = false; drag.swallowClick = true;
  const r = ph.getBoundingClientRect();
  card.classList.remove("lifted");
  card.style.transition = "left .2s cubic-bezier(.2, .8, .25, 1), top .2s cubic-bezier(.2, .8, .25, 1), transform .2s";   // glide into the gap
  card.style.left = r.left + "px"; card.style.top = r.top + "px";
  setTimeout(() => {
    if (!ph.isConnected) return;
    grid.insertBefore(card, ph); ph.remove();
    card.style.cssText = css;
    saveOrder(grid.dataset.order, [...grid.querySelectorAll(".card[data-dev]")].map(c => c.dataset.dev));
  }, 210);
}

function saveOrder(key, ids) {
  S.model.order = { ...(S.model.order || {}), [key]: ids };
  api("/api/order", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key, ids, phone: PHONE }) })
    .catch(e => toast("排序没保存上：" + e.message));
}

// Carrying a card near the top / bottom edge scrolls the page.
function autoScroll() {
  if (!drag.active) return;
  const edge = 90, top = (parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--sat")) || 0) + 60;
  const v = drag.y < top + edge ? -Math.ceil((top + edge - drag.y) / 8) : drag.y > innerHeight - edge ? Math.ceil((drag.y - innerHeight + edge) / 8) : 0;
  if (v) window.scrollBy(0, v);
  drag.raf = requestAnimationFrame(autoScroll);
}

// ---- edit actions

function editMoveToTop() {
  const ids = [...S.edit];
  const pin = (room, list) => saveOrder(orderKey(room), [...ids.filter(id => list.some(d => d.id === id)),
    ...ordered(list, room).map(d => d.id).filter(id => !ids.includes(id))]);
  const devs = homeDevices(), rooms = (S.model.homes.find(h => h.name === S.home) || { rooms: [] }).rooms;
  if (S.room === "全部") {
    pin("全部", allDevices(devs, rooms));
  } else {
    pin(S.room, devs.filter(d => d.room === S.room));
  }
  exitEdit();
}

function editMove() {
  const rooms = roomList().slice(1).filter(r => r !== "未分配");   // 未分配 is not an HA area to move into
  sheet(`<h3>移动到哪个房间</h3><div class="sheet-list">${rooms.map(r =>
    `<button data-act="move-to" data-arg="${esc(r)}">${esc(r)}</button>`).join("")}</div>`);
}

async function editMoveTo(room) {
  const ids = [...S.edit];
  closeSheet();
  try {
    if (ids.includes(CAM_ID)) { await camPref({ room }); apply(S.model); }
    const rest = ids.filter(i => i !== CAM_ID);
    if (rest.length) await api("/api/device/move", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ device_ids: rest, home: S.home, room }) });
    toast(`已移到${room}`);
  } catch (e) { toast("移动失败：" + e.message); }
  exitEdit(); load().catch(() => {});
}

function editRename() {
  const d = S.byId[[...S.edit][0]];
  sheet(`<h3>重命名</h3><input id="rename-input" maxlength="40" value="${esc(d.name)}">
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri" data-act="rename-save">保存</button></div>
    <p class="sheet-note">只改 HomeHub 里的名字，米家里的名字不变。</p>`);
  const i = $("#rename-input"); i.focus(); i.select();
}

async function editRenameSave() {
  const id = [...S.edit][0], name = $("#rename-input").value.trim();
  if (!name) return toast("名字不能为空");
  closeSheet();
  try {
    if (id === CAM_ID) { await camPref({ name }); apply(S.model); }
    else await api("/api/device/rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ device_id: id, name }) });
  } catch (e) { toast("重命名失败：" + e.message); }
  exitEdit(); load().catch(() => {});
}

function editDelete() {
  const n = S.edit.size;
  sheet(`<h3>从首页删除 ${n} 个设备？</h3>
    <p class="sheet-note">小米设备只能在米家里解绑，这里只是不在 HomeHub 首页显示。之后可以在「全部」页最下面的「已删除的设备」里恢复。</p>
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri danger" data-act="delete-confirm">删除</button></div>`);
}

async function setHidden(ids, hidden) {
  const set = new Set(S.model.hidden || []);
  ids.forEach(id => hidden ? set.add(id) : set.delete(id));
  S.model.hidden = [...set];
  try {
    if (ids.includes(CAM_ID)) { await camPref({ hidden }); ids = ids.filter(i => i !== CAM_ID); }
    if (ids.length) await api("/api/device/hide", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ device_ids: ids, hidden }) });
  } catch (e) { toast("没保存上：" + e.message); }
}

function showHidden() {
  const ds = (S.model.hidden || []).map(id => S.byId[id]).filter(Boolean);
  sheet(`<h3>已删除的设备</h3><div class="sheet-list">${ds.map(d =>
    `<div class="sheet-row"><span>${esc(d.name)}<small>${esc(d.room)}</small></span><button data-act="unhide" data-arg="${d.id}">恢复</button></div>`).join("") || "<p class='sheet-note'>没有</p>"}</div>`);
}

// ---- 添加设备 (the + in the header)

// What HA found, in plain words (integration domain -> 类型).
const FOUND_KINDS = { homekit_controller: "HomeKit 设备，可走局域网", ipp: "打印机", dlna_dms: "媒体服务器（NAS）", dlna_dmr: "投屏设备",
  roborock: "石头扫地机", nut: "UPS 不间断电源", esphome: "自研设备（ESPHome）", matter: "Matter 设备", zha: "Zigbee 接收棒",
  cast: "投屏设备", yeelight: "Yeelight 灯", xiaomi_miio: "小米设备（局域网）", tuya: "涂鸦设备", midea_ac_lan: "美的设备" };

function publicAddressSheet() {
  sheet(`<h3>公网地址</h3>
    <p class="sheet-note" style="text-align:left">不在家时 App 用这个地址连家里。写域名就行，端口不写就沿用现在的；改完会马上重新测一次连不连得上。</p>
    <input id="pubin" type="text" inputmode="url" autocapitalize="off" autocomplete="off" spellcheck="false" value="${esc(HomeHubApp.publicUrl().replace("https://", ""))}" style="margin-top:12px">
    <div id="pubres" class="pubres"></div>
    <div class="sheet-btns"><button data-act="pub-reset">恢复默认</button><button data-act="pub-test">测试连接</button><button class="pri" data-act="pub-save">保存</button></div>
    <p class="sheet-note" style="text-align:left">在家里用 Wi-Fi 测，有些路由器不让手机从里面绕出去再连回来，会显示失败；用 5G 测最准。</p>`);
}
window.hhTest = (id, r) => { const el = $("#pubres"); if (el) { el.textContent = r.msg; el.className = "pubres " + (r.ok ? "ok" : "bad"); } };

async function addDeviceSheet() {
  sheet(`<h3>添加设备</h3>
    <div class="add-sec"><div class="add-t">米家设备</div>
      <p class="sheet-note">小米设备只能在米家里配网。配好后点「同步新设备」，几秒后就会出现在这里（米家里选的房间也会带过来）。</p>
      <div class="sheet-btns"><button data-act="open-mijia">打开米家去添加</button><button class="pri" data-act="sync-xiaomi">同步新设备</button></div></div>
    <div class="add-sec"><div class="add-t">附近发现的设备</div><div id="discovered" class="sheet-list"><p class="sheet-note">正在查找…</p></div></div>
    <div class="add-sec"><div class="add-t">小米 Wi-Fi 设备（不用米家）</div>
      <p class="sheet-note">新买的或刚复位的小米 Wi-Fi 设备（灯、插座、净化器…），直接在这里配网。</p>
      <div class="sheet-btns"><button class="pri" data-act="prov-start">开始配网</button></div></div>
    ${window.HomeHubApp ? `<div class="add-sec"><div class="add-t">应用</div>
      <div class="sheet-row"><span>HomeHub ${esc(HomeHubApp.versionName())}<small>线路：${esc(HomeHubApp.route())}</small></span><button data-act="update">检查更新</button></div>
      ${HomeHubApp.publicUrl ? `<div class="sheet-row"><span>公网地址<small>${esc(HomeHubApp.publicUrl().replace("https://", ""))}</small></span><button data-act="pub-edit">修改</button></div>` : ""}</div>` : ""}
    <div class="add-sec"><div class="add-t">Zigbee / Matter 设备</div>
      <p class="sheet-note">还没启用：需要香橙派插上 Zigbee 接收棒，或装好 Matter 服务。买了这类设备告诉 Claude 来开通。</p></div>`);
  try {
    const list = await api("/api/devices/discovered");
    const el = $("#discovered");
    if (el) el.innerHTML = list.map(d => `<div class="sheet-row"><span>${esc(d.title === d.domain ? (FOUND_KINDS[d.domain] ?? d.title) : d.title)}<small>${esc(d.title === d.domain ? d.domain : (FOUND_KINDS[d.domain] ?? d.domain))}</small></span>
      <button data-act="add-found" data-arg="${esc(d.flow_id)}">添加</button></div>`).join("") || `<p class="sheet-note">没有发现新的设备</p>`;
  } catch (e) {
    const el = $("#discovered"); if (el) el.innerHTML = `<p class="sheet-note">查不到：${esc(e.message)}</p>`;
  }
}

const OPTION_NAMES = { auto: "自动", us: "美国", eu: "欧洲", ru: "俄罗斯", cn: "中国大陆", custom: "自定义" };

async function addFound(flowId, btn) {
  btn.disabled = true; btn.textContent = "连接中…";
  try { flowStep(await api("/api/devices/add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ flow_id: flowId }) }), flowId); }
  catch (e) { toast("添加失败：" + e.message); btn.disabled = false; btn.textContent = "添加"; }
}

// What HA answers while a device is being added: a form to fill (account, code, pairing code ...),
// a menu, or the outcome. The person types their own details in the form; nothing is stored here.
function flowStep(s, flowId) {
  if (s.type === "done") { toast(s.message); closeSheet(); load().catch(() => {}); return; }
  if (s.type === "abort" || s.type === "error") { toast(s.message); addDeviceSheet(); return; }
  S.flow = { id: s.flow_id || flowId };
  if (s.title === s.domain) s.title = FOUND_KINDS[s.domain] || s.domain;
  if (s.type === "menu") {
    sheet(`<h3>${esc(s.title)}</h3><div class="sheet-list">${s.options.map(([id, name]) =>
      `<button data-act="flow-menu" data-arg="${esc(id)}">${esc(name)}</button>`).join("")}</div>
      <div class="sheet-btns"><button data-act="add-device">返回</button></div>`);
    return;
  }
  const input = f => {
    const v = f.default ?? "";
    if (f.kind === "bool") return `<input type="checkbox" data-f="${esc(f.name)}" ${f.default ? "checked" : ""}>`;
    if (f.kind === "select") return `<select data-f="${esc(f.name)}">${f.options.map(([val, lab]) => `<option value="${esc(val)}" ${val === f.default ? "selected" : ""}>${esc(OPTION_NAMES[lab] ?? lab)}</option>`).join("")}</select>`;
    return `<input data-f="${esc(f.name)}" data-kind="${f.kind}" type="${f.kind === "password" ? "password" : f.kind === "number" ? "text" : "text"}" ${f.kind === "number" ? 'inputmode="decimal"' : ""}
      value="${esc(v)}" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">`;
  };
  const errs = Object.entries(s.errors || {}).map(([k, v]) => `<div class="fl-err">${esc(v)}</div>`).join("");
  sheet(`<h3>${esc(s.title)}</h3>
    ${s.description ? `<p class="fl-desc">${esc(s.description)}</p>` : ""}
    ${s.fields.map(f => `<label class="fl-row ${f.kind === "bool" ? "inline" : ""}"><span>${esc(f.label)}${f.required ? "" : "（可选）"}</span>${input(f)}${f.hint ? `<small>${esc(f.hint)}</small>` : ""}${(s.errors || {})[f.name] ? `<div class="fl-err">${esc(s.errors[f.name])}</div>` : ""}</label>`).join("")}
    ${errs}
    <div class="sheet-btns"><button data-act="add-device">返回</button><button class="pri" data-act="flow-submit">${s.fields.length ? "提交" : "确认"}</button></div>`);
  $("#sheet input:not([type=checkbox])")?.focus();
}

async function flowSend(input, btn) {
  const label = btn?.textContent;
  if (btn) { btn.disabled = true; btn.textContent = "处理中…"; }
  try {
    flowStep(await api("/api/devices/add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ flow_id: S.flow.id, input }) }), S.flow.id);
  } catch (e) {
    toast("没成功：" + e.message);
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

function flowSubmit(btn) {
  const input = {};
  for (const el of document.querySelectorAll("#sheet [data-f]")) {
    const k = el.dataset.f;
    input[k] = el.type === "checkbox" ? el.checked : el.dataset.kind === "number" && el.value !== "" && !isNaN(el.value) ? Number(el.value) : el.value;
  }
  flowSend(input, btn);
}

async function syncXiaomi(btn) {
  btn.disabled = true; btn.textContent = "同步中，约 20 秒…";
  try {
    const r = await api("/api/devices/sync", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(r.added > 0 ? `新增了 ${r.added} 个设备` : "已同步，没有新设备");
    closeSheet(); load().catch(() => {});
  } catch (e) { toast("同步失败：" + e.message); btn.disabled = false; btn.textContent = "同步新设备"; }
}

// ---- bottom sheet

function sheet(html, cls = "") {
  // A sheet that is already up is updated where it stands (picking 20 instead of 10 minutes redraws the same card) - it must not
  // slide up again. Only a sheet opened from nothing animates in.
  const old = $("#sheet");
  if (old && $("#sheet-mask")) {
    const title = old.querySelector("h3")?.textContent, top = old.scrollTop;
    S.pop = null;
    old.className = cls; old.style.animation = "none"; old.style.transform = ""; old.style.transition = "";
    old.innerHTML = html;
    old.scrollTop = old.querySelector("h3")?.textContent === title ? top : 0;
    return;
  }
  closeSheet();
  document.body.insertAdjacentHTML("beforeend", `<div id="sheet-mask" data-act="sheet-close"></div><div id="sheet" class="${cls}">${html}</div>`);
}
// The big pop-ups close by pulling them down: the drag starts when the list is at its top and the finger moves down.
(() => {
  let y0 = 0, dy = 0, t0 = 0, on = false, el = null;
  document.addEventListener("touchstart", e => {
    el = e.target.closest("#sheet.big"); on = false; dy = 0;
    if (el && e.touches.length === 1) { y0 = e.touches[0].clientY; t0 = Date.now(); }
  }, { passive: true });
  document.addEventListener("touchmove", e => {
    if (!el) return;
    const d = e.touches[0].clientY - y0;
    if (!on && d > 6 && el.scrollTop <= 0) { on = true; el.style.animation = "none"; el.style.transition = "none"; y0 = e.touches[0].clientY; return; }
    if (!on) return;
    dy = Math.max(0, e.touches[0].clientY - y0);
    el.style.transform = `translateY(${dy}px)`;
    e.preventDefault();
  }, { passive: false });
  document.addEventListener("touchend", () => {
    if (!el || !on) { el = null; return; }
    const quick = dy / Math.max(1, Date.now() - t0) > 0.5;
    if (dy > 110 || (quick && dy > 30)) {
      el.style.transition = "transform .2s ease-in"; el.style.transform = "translateY(105%)";
      const m = $("#sheet-mask"), e2 = el; if (m) { m.style.transition = "opacity .2s"; m.style.opacity = 0; }
      setTimeout(() => { if ($("#sheet") === e2) closeSheet(); }, 200);
    } else { el.style.transition = "transform .18s ease-out"; el.style.transform = ""; }
    el = null; on = false;
  });
})();
function closeSheet() { S.pop = null; $("#sheet")?.remove(); $("#sheet-mask")?.remove(); }

// ---- 灯光 / 温度 pop-ups (the 亮灯 and temperature chips). Laid out like Mijia's: a big sheet, the rooms one
// after another, white cards with a round power button, a tap on a card opens the device, a tap on the room
// name (with its current temperature) jumps to that room.
function popSheet(kind) {
  sheet(popHtml(kind), "big");
  S.pop = kind;     // sheet() closes any open sheet first, which clears the flag
}
function popHtml(kind) { return kind === "lights" ? lightsHtml() : tempHtml(); }
function refreshPop() {
  const el = $("#sheet");
  if (!el) { S.pop = null; return; }
  const y = el.scrollTop;
  el.innerHTML = popHtml(S.pop);
  el.scrollTop = y;
}

function popRooms(items) {
  const home = S.model.homes.find(h => h.name === S.home) || { rooms: [] };
  const names = [...home.rooms, ...new Set(items.map(i => i.room))].filter((r, i, a) => a.indexOf(r) === i);
  return names.map(r => [r, items.filter(i => i.room === r)]).filter(([, l]) => l.length);
}
function scopedDevices() {
  const devs = homeDevices();
  return S.room === "全部" ? devs : devs.filter(d => d.room === S.room);
}
function thermoAvg(room) {
  const t = homeDevices().filter(d => d.kind === "thermo" && d.room === room && num(d.roles.temperature) != null);
  return t.length ? t.reduce((a, d) => a + num(d.roles.temperature), 0) / t.length : null;
}
function roomHead(room, right) {
  return `<div class="m-room" data-room-jump="${esc(room)}" role="button"><span>${esc(room)}</span>${right ? `<span class="m-cur">${right}</span>` : ""}<i>${icon("caret")}</i></div>`;
}

function lightItems() {
  const out = [];
  for (const d of scopedDevices()) {
    const lamps = d.kind === "light" ? [{ id: d.roles.main, name: d.name }]
      : d.toggles.filter(t => /灯/.test(t.label)).map(t => ({ id: t.entity_id, name: d.toggles.length > 1 ? t.label : d.name }));
    for (const l of lamps) out.push({ ...l, dev: d.id, room: d.room, offline: deviceOffline(d) });
  }
  return out;
}

function lightsHtml() {
  const items = lightItems(), online = items.filter(l => !l.offline), on = online.filter(l => isOn(l.id)).length;
  const card = l => {
    const a = st(l.id).a, pct = isOn(l.id) && a.brightness != null ? Math.round(a.brightness / 2.55) + "%" : "";
    return `<div class="mc ${l.offline ? "offline" : isOn(l.id) ? "active" : ""}" style="--c:#14c8a0" data-dev="${l.dev}" role="button">
      ${l.offline ? "" : `<button class="pw ${isOn(l.id) ? "on" : ""}" data-toggle="${l.id}" aria-label="${esc(l.name)}">${icon("power")}</button>`}
      <div class="pi art">${(window.deviceArt && deviceArt(S.byId[l.dev])) || icon("light")}</div>
      <div class="mn">${esc(l.name)}</div><div class="ms">${esc(l.offline ? l.room + " | 离线" : [l.room, pct].filter(Boolean).join(" | "))}</div></div>`;
  };
  return `<div class="m-top"><div><h3>灯光</h3><div class="m-sub">${online.length ? `${on}个灯亮着` : "这里没有灯"}</div></div>
      ${online.length ? `<button class="m-all ${on ? "off" : "on"}" data-act="${on ? "lights-off" : "lights-on"}">${icon("light")}${on ? "全部关闭" : "全部打开"}</button>` : ""}</div>
    ${popRooms(items).map(([r, l]) => roomHead(r) + `<div class="m-grid">${l.map(card).join("")}</div>`).join("")}`;
}

function tempItems() {
  return scopedDevices().filter(d => ["ac", "heater", "fan"].includes(d.kind)).map(d => ({ d, room: d.room }));
}

function tempHtml() {
  const th = scopedDevices().filter(d => d.kind === "thermo" && num(d.roles.temperature) != null).map(d => num(d.roles.temperature));
  const all = th.length ? th : homeDevices().filter(d => d.kind === "thermo" && num(d.roles.temperature) != null).map(d => num(d.roles.temperature));
  const lo = all.length ? Math.min(...all) : null, hi = all.length ? Math.max(...all) : null;
  const head = lo == null ? "—" : lo === hi ? `${lo.toFixed(1)}°` : `${lo.toFixed(1)} - ${hi.toFixed(1)}°`;
  const card = ({ d }) => {
    const off = deviceOffline(d), on = !off && isOn(d.roles.main), a = st(d.roles.main).a, tg = d.toggles[0];
    const pwId = d.kind === "fan" ? (tg ? tg.entity_id : d.roles.main) : d.roles.main;
    const power = off ? "" : `<button class="pw ${on ? "on" : ""}" data-toggle="${pwId}" aria-label="开关">${icon("power")}</button>`;
    let ctl = "";
    if (d.kind !== "fan") {
      const step = a.target_temp_step || (d.kind === "ac" ? 1 : 0.5), has = a.temperature != null;
      ctl = `<div class="m-temp ${on ? "" : "dim"}"><button class="m-pm" ${on && has ? "" : "disabled"} data-temp="${d.roles.main}" data-d="${-step}">−</button>
        <div class="m-val">${has && !off ? fmtNum(a.temperature) + "<small>°</small>" : "--"}</div>
        <button class="m-pm" ${on && has ? "" : "disabled"} data-temp="${d.roles.main}" data-d="${step}">+</button></div>`;
    }
    return `<div class="mw ${off ? "offline" : on ? "active" : ""}" style="--c:${COLORS[d.kind]}" data-dev="${d.id}" role="button">
      <div class="mw-top"><div class="pi art">${(window.deviceArt && deviceArt(d)) || icon(d.kind)}</div><div class="mw-t"><div class="mn">${esc(d.name)}</div><div class="ms">${esc(d.room + (off ? " | 离线" : ""))}</div></div>${power}</div>${ctl}</div>`;
  };
  const items = tempItems(), rooms = popRooms(items);
  return `<div class="m-top"><div><h3>${head}</h3><div class="m-sub">${S.room === "全部" ? "全屋温度" : esc(S.room) + "温度"}</div></div></div>
    ${rooms.map(([r, l]) => { const t = thermoAvg(r); return roomHead(r, t == null ? "" : `当下${t.toFixed(1)}°`) + l.map(card).join(""); }).join("") || `<p class="sheet-note">这里没有空调或暖气</p>`}`;
}

function allLights(on) {
  const todo = lightItems().filter(l => !l.offline && isOn(l.id) !== on);
  todo.forEach(l => call(l.id, on ? "turn_on" : "turn_off", {}, { s: on ? "on" : "off" }));
  if (todo.length) toast(`${on ? "打开" : "关闭"}了 ${todo.length} 盏灯`);
}

function roomList() {
  const home = S.model.homes.find(h => h.name === S.home) || { rooms: [] };
  return ["全部", ...home.rooms];
}

// ---------------------------------------------------------------- room pager (swipe / tab)
//
// Every room is its own page side by side in .track, like Mijia's pager: the finger moves the strip
// 1:1, the next room shows up at the edge, and on release it settles on a page with an eased slide.
// The pager is as tall as the current page; when the list has been scrolled down, the other pages
// are shifted so their top sits just under the sticky tabs, where the eye already is.

const pager = { moving: false, w: 0, lift: 0, raf: 0, gen: 0, prepared: false };

// The pager's height follows the current page (otherwise a long "全部" leaves empty space under short rooms).
function fitPager() {
  const p = $(".pager"), cur = $(".roomview.cur");
  if (p && cur) p.style.height = cur.offsetHeight + "px";
}

// How far the lift is: when the list has been scrolled down, the pages that are not current sit just under
// the sticky tabs, where the eye already is. Called at every touch start - it is cheap.
function preparePager() {
  const p = $(".pager"), tabs = $(".tabs");
  if (!p) return false;
  pager.w = p.clientWidth;
  const lift = pager.lift = Math.max(0, (tabs ? tabs.getBoundingClientRect().bottom : 0) - p.getBoundingClientRect().top);
  document.querySelectorAll(".roomview:not(.cur)").forEach(v => { v.style.transform = lift ? `translateY(${lift}px)` : ""; });
  pager.prepared = true;
  return true;
}

function centerActiveTab() {
  const tabs = $(".tabs"), on = $(".tab.on");
  if (tabs && on) tabs.scrollTo({ left: on.offsetLeft - tabs.clientWidth / 2 + on.clientWidth / 2, behavior: "smooth" });
}

// ---- one source of truth: the strip's x position. Every frame sets the strip AND the tab bar from it, so they can
// never disagree; the animation is a plain requestAnimationFrame loop with a token, so an old one can never
// finish after a newer one has started.
const stripX = () => { const t = $(".track"); return t ? new DOMMatrixReadOnly(getComputedStyle(t).transform).m41 : 0; };
function setStrip(x) {
  const t = $(".track");
  if (!t) return;
  t.style.transition = "none";
  t.style.transform = `translate3d(${x}px, 0, 0)`;
  placeIndicator(-x / pager.w, false);               // the white bar under the tabs follows along
}
function stopAnim() { cancelAnimationFrame(pager.raf); pager.gen++; pager.moving = false; }

// The white bar under the room tabs is a single element: it follows the strip (pos is a fractional tab index).
function tabCenter(i) {
  const t = document.querySelectorAll(".tab")[i];
  return t ? t.offsetLeft + t.offsetWidth / 2 : 0;
}
function placeIndicator(pos, animate) {
  const ind = $(".tab-ind");
  if (!ind) return;
  const last = roomList().length - 1, p = Math.max(0, Math.min(last, pos)), i = Math.floor(p), f = p - i;
  const x = tabCenter(i) + (tabCenter(Math.min(i + 1, last)) - tabCenter(i)) * f;
  ind.style.transition = animate ? "transform .34s cubic-bezier(.2, .8, .25, 1)" : "none";
  ind.style.transform = `translateX(${x - ind.offsetWidth / 2}px)`;
}
function highlightTab(j) {
  document.querySelectorAll(".tab").forEach((t, k) => t.classList.toggle("on", k === j));
  const tabs = $(".tabs"), on = document.querySelectorAll(".tab")[j];
  if (tabs && on) tabs.scrollTo({ left: on.offsetLeft - tabs.clientWidth / 2 + on.clientWidth / 2, behavior: "smooth" });
}

// Making room j current is only bookkeeping - no re-render, so nothing on screen jumps. Idempotent.
function commitRoom(j) {
  const l = roomList(), room = l[j];
  if (!room || room === S.room) return;
  const pg = $(".pager"), lift = pager.lift;
  const anchor = pg.getBoundingClientRect().top + lift;   // where the new room's (lifted) top is on screen now
  S.room = room;
  document.querySelectorAll(".roomview").forEach((v, k) => { v.classList.toggle("cur", k === j); v.style.transform = ""; });
  fitPager();
  const now = pg.getBoundingClientRect().top;
  if (lift) window.scrollBy(0, now - anchor);             // take the lift off without the page moving
  const statsEl = $(".stats");
  if (statsEl) statsEl.innerHTML = headerStats(room).stats;
  highlightTab(j);
}

// Glide to page j from wherever the strip is now. v0: the finger's speed (px/ms) for a natural carry-over.
function animateTo(j, v0 = 0) {
  const l = roomList();
  j = Math.max(0, Math.min(l.length - 1, j));
  stopAnim();
  const gen = pager.gen, from = stripX(), to = -j * pager.w, dist = to - from;
  pager.moving = true;
  highlightTab(j);
  const statsEl = $(".stats");
  if (statsEl && l[j] !== S.room) statsEl.innerHTML = headerStats(l[j]).stats;
  const finish = () => {
    if (gen !== pager.gen) return;
    pager.moving = false;
    setStrip(to);
    commitRoom(j);
    preparePager();                                         // lifts for the pages that are not current now
    if (S.stale) { S.stale = false; scheduleRender(); }     // catch up on state changes held back meanwhile
  };
  if (Math.abs(dist) < 0.5) return finish();
  const dur = Math.max(160, Math.min(320, Math.abs(dist) / pager.w * 360 + 90 - Math.min(80, Math.abs(v0) * 60)));
  const t0 = performance.now();
  const step = now => {
    if (gen !== pager.gen) return;
    const p = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - p, 3);     // ease-out: fast start, soft landing
    setStrip(from + dist * e);
    if (p < 1) pager.raf = requestAnimationFrame(step); else finish();
  };
  pager.raf = requestAnimationFrame(step);
}

// Taking over a slide that is still running: freeze where it is, make the room it was heading for current.
function interruptPager() {
  const x = stripX();
  const heading = Math.round(-x / pager.w);                 // the page the strip is closest to
  stopAnim();
  setStrip(x);
  commitRoom(Math.max(0, Math.min(roomList().length - 1, heading)));
  preparePager();
}

function selectRoom(room) {
  const j = roomList().indexOf(room);
  if (j < 0) return;
  if (!preparePager()) { S.room = room; render(); return; }
  if (pager.moving) interruptPager();
  if (room === S.room && Math.abs(stripX() + j * pager.w) < 1) return;
  animateTo(j);
}

// ---- finger: 1:1 follow, rubber band past the ends, fling to the next room
const swipe = { x: 0, y: 0, t: 0, axis: null, lastX: 0, lastT: 0, vx: 0, base: 0 };
document.addEventListener("touchstart", e => {
  swipe.axis = null;
  if (S.edit || isSmart() || isAuto() || currentDevice() || e.touches.length !== 1 || e.target.closest(".tabs,.stats,input,select") || !$(".pager")) { swipe.t = 0; return; }
  preparePager();
  if (pager.moving) interruptPager();
  swipe.base = stripX();
  swipe.x = swipe.lastX = e.touches[0].clientX; swipe.y = e.touches[0].clientY; swipe.t = swipe.lastT = Date.now(); swipe.vx = 0;
}, { passive: true });

document.addEventListener("touchmove", e => {
  if (!swipe.t) return;
  const x = e.touches[0].clientX, dx = x - swipe.x, dy = e.touches[0].clientY - swipe.y, now = Date.now();
  if (!swipe.axis && Math.hypot(dx, dy) > 6) {
    swipe.axis = Math.abs(dx) > Math.abs(dy) * 1.2 ? "x" : "y";
    if (swipe.axis === "x") { clearTimeout(drag.timer); drag.timer = 0; }     // a swipe, not a long press
  }
  if (swipe.axis !== "x") return;
  if (now > swipe.lastT) { swipe.vx = swipe.vx * .4 + .6 * (x - swipe.lastX) / (now - swipe.lastT); swipe.lastX = x; swipe.lastT = now; }
  const last = roomList().length - 1, min = -last * pager.w;
  let nx = swipe.base + dx;
  if (nx > 0) nx *= .3; else if (nx < min) nx = min + (nx - min) * .3;       // rubber band past the ends
  setStrip(nx);
}, { passive: true });

function endSwipe() {
  if (!swipe.t) return;
  const wasX = swipe.axis === "x";
  swipe.t = 0;
  const x = stripX(), last = roomList().length - 1;
  if (!wasX) {                                              // a tap, or a vertical scroll: if the strip is mid-way, let it land
    if (Math.abs(x + Math.round(-x / pager.w) * pager.w) > 1) animateTo(Math.round(-x / pager.w));
    return;
  }
  const f = -x / pager.w;                                   // fractional page now
  const v = swipe.vx;                                       // px/ms, negative = towards the next room
  let j;
  if (Math.abs(v) > .3) j = v < 0 ? Math.ceil(f - 0.001) : Math.floor(f + 0.001);          // a fling goes one page on
  else j = Math.round(f);
  const from = Math.round(swipe.base / -pager.w);           // never skip more than one page from where the finger started
  j = Math.max(from - 1, Math.min(from + 1, j));
  animateTo(Math.max(0, Math.min(last, j)), v);
}
document.addEventListener("touchend", endSwipe, { passive: true });
document.addEventListener("touchcancel", endSwipe, { passive: true });

const WEATHER = { sunny: "☀️", "clear-night": "🌙", partlycloudy: "⛅", cloudy: "☁️", fog: "🌫️", rainy: "🌧️", pouring: "🌧️",
                  lightning: "⛈️", "lightning-rainy": "⛈️", snowy: "❄️", "snowy-rainy": "🌨️", hail: "🌨️", windy: "🍃", "windy-variant": "🍃", exceptional: "⚠️" };
function weatherHtml() {
  const w = S.model.weather && st(S.model.weather);
  if (!w || dead(w.s) || w.a.temperature == null) return "";
  return `<div class="weather tap" data-act="weather" role="button"><span class="wi">${WEATHER[w.s] ?? "🌡️"}</span>${Math.round(w.a.temperature)}°</div>`;
}

function card(d) {
  if (d.kind === "camera") return camCard(d);
  const sm = summary(d), off = deviceOffline(d), active = !off && deviceActive(d);
  const tg = off ? [] : d.toggles;
  const btns = d.kind === "washer" && !off && window.washerCardButtons ? washerCardButtons(d)   // 一键约上/下筒 (washer_ui.js)
    : tg.length === 1
    ? `<button class="pw ${isOn(tg[0].entity_id) ? "on" : ""}" data-toggle="${tg[0].entity_id}" aria-label="开关">${icon("power")}</button>`
    : tg.length > 1 ? `<div class="pws">${tg.map(t => `<button class="pw ${isOn(t.entity_id) ? "on" : ""}" data-toggle="${t.entity_id}" aria-label="${esc(t.label)}">${icon("power")}</button>`).join("")}</div>` : "";
  const fill = d.kind === "washer" && window.washerCleanFill ? washerCleanFill() : d.kind === "ac" && window.acCleanFill ? acCleanFill(d) : "";
  return `<div class="card ${active ? "active" : ""} ${off ? "offline" : ""}" style="--c:${COLORS[d.kind]}${fill}" data-dev="${d.id}" role="button">
      ${window.deviceArt && deviceArt(d) ? `<div class="art">${deviceArt(d)}</div>` : `<div class="ico">${icon(d.kind)}</div>`}${btns}<div class="selmark">${icon("check")}</div>
      <div class="name ${[...d.name].length > 7 ? "long" : ""}">${esc(d.name)}</div>
      <div class="st ${sm.warn ? "warn" : ""}">${esc(sm.text)}</div>
    </div>`;
}

// ---------------------------------------------------------------- 智能 page (HA automations)

function renderSmart() {
  const autos = S.model.automations || [];
  const miniFlow = desc => {
    const clean = (desc || "").replace(/^草稿[｜|]\s*/, "").replace(/^如果\s*/, "");
    const [trigger, ...actions] = clean.split(" → ");
    const target = actions.join(" → ");
    return `<div class="auto-mini-flow" title="${esc(clean)}"><span class="auto-mini-node source">${icon("smart")}</span><span class="auto-mini-copy">${esc(trigger || "等待条件")}</span>${target ? `<span class="auto-mini-arrow">→</span><span class="auto-mini-node target">${icon("power")}</span><span class="auto-mini-copy target-copy">${esc(target)}</span>` : ""}</div>`;
  };
  const cards = autos.map(id => {
    const s = st(id), on = s.s === "on", raw = S.model.automation_desc?.[id] || "";
    const draft = raw.startsWith("草稿");
    const name = S.model.automation_names?.[id] ?? id;
    return `<div class="auto auto-row ${on ? "" : "off"}" data-act="auto-open" data-arg="${id}">
      <div class="auto-row-main">
        <div class="auto-row-title">${draft ? `<span class="draft">草稿</span>` : ""}<b>${esc(name)}</b></div>
        ${miniFlow(raw)}
      </div>
      <button class="sw ${on ? "on" : ""}" style="--c:#14a38b" data-act="auto-toggle" data-arg="${id}" aria-label="${on ? "关闭" : "开启"}${esc(name)}"></button>
    </div>`;
  }).join("");
  $("#app").innerHTML = `
    <div class="page smart-page">
      <div class="top"><div class="top-row"><div class="home-name"><span>智能</span></div><button class="add-btn" data-act="auto-new" aria-label="新建自动化">${icon("plus")}</button></div><div class="smart-sub">自动化在家里的香橙派上运行，断网也照常执行</div></div>
      ${autos.some(id => (S.model.automation_desc?.[id] || "").startsWith("草稿")) ? `<div class="smart-tip">草稿默认关闭；确认后再开启，并在米家关闭同名规则，避免重复触发。</div>` : ""}
      <div class="autos auto-rows">${cards || `<div class="empty">还没有自动化。点右上角「+」创建第一条。</div>`}</div>
    </div>${bottomNav("smart")}`;
}
// ---------------------------------------------------------------- detail page

// ---------------------------------------------------------------- air conditioner page (Mijia layout)
// Big number, one card of round buttons (power / mode / fan / 恒温), one card of the unit's own on-off
// functions; everything else stays folded away under 更多设置.

const comfortOf = id => (S.model.comfort || {})[id];
function deskTemp(d) {
  const t = S.model.devices.filter(x => x.kind === "thermo" && x.home === d.home && x.room === d.room && num(x.roles.temperature) != null);
  return t.length ? t.reduce((n, x) => n + num(x.roles.temperature), 0) / t.length : null;
}

async function comfortSend(id, on, target) {
  try {
    const r = await api("/api/comfort", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ entity_id: id, on, target }) });
    S.model.comfort = { ...(S.model.comfort || {}), [id]: r.comfort };
    if (!on) delete S.model.comfort[id];
    render();
  } catch (e) { toast("恒温失败：" + e.message); }
}

function renderDetail(d) {
  if (d.kind === "washer" && window.renderWasher && wPrefix(d)) return renderWasher(d);   // washer_ui.js
  if (d.kind in KITCHEN_KINDS && d.kx && !deviceOffline(d) && window.renderKitchen) return renderKitchen(d);   // kitchen_ui.js
  if (d.kind === "heater" && window.renderHeater && d.roles.main?.startsWith("climate.") && !deviceOffline(d) && !dead(st(d.roles.main).s)) return renderHeater(d);   // heater_ui.js
  if ((d.kind === "ac" || d.kind === "heater") && d.roles.main?.startsWith("climate.") && !deviceOffline(d) && !dead(st(d.roles.main).s)) return renderAc(d);
  const sm = summary(d, true), off = deviceOffline(d), active = !off && deviceActive(d);
  const main = d.toggles[0];
  let heroCtl = "";
  if (!off && d.toggles.length === 1) {
    heroCtl = `<button class="big-pw ${isOn(main.entity_id) ? "on" : ""}" data-toggle="${main.entity_id}">${icon("power")}</button>`;
  } else if (!off && d.toggles.length > 1) {
    heroCtl = `<div class="keys">${d.toggles.map(t => `<button class="key ${isOn(t.entity_id) ? "on" : ""}" data-toggle="${t.entity_id}">${icon("power")}${esc(t.label)}</button>`).join("")}</div>`;
  }
  const washerLink = d.kind === "washer" ? `<div class="panel"><button class="chip on" style="width:100%" data-act="washer">一键约洗衣机 / 洗衣记录</button></div>` : "";

  $("#app").innerHTML = `
    <div class="page" style="--c:${COLORS[d.kind]}">
      <div class="bar"><button class="back" data-act="back">${icon("back")}</button>
        <div><div class="t">${esc(d.name)}</div><div class="r">${esc([d.room, d.kind_name].filter(Boolean).join(" · "))}</div></div></div>
      <div class="hero ${active ? "active" : ""}">
        ${window.deviceArt && deviceArt(d) ? `<div class="big-ico art">${deviceArt(d)}</div>` : `<div class="big-ico">${icon(d.kind)}</div>`}
        <div class="state" style="${sm.warn ? "color:var(--danger)" : ""}">${esc(sm.text)}</div>
        <div class="state-sub">${esc(heroSub(d))}</div>
        ${heroCtl}
      </div>
      ${washerLink}
      ${off ? "" : kindPanel(d)}
      ${tilesPanel(d)}
      ${d.features.length ? `<div class="panel"><h3>功能</h3>${d.features.map(f => entityRow(f)).join("")}</div>` : ""}
    </div>`;
}

function heroSub(d) {
  const r = d.roles;
  if ((d.kind === "ac" || d.kind === "heater") && st(r.main).a.current_temperature != null) return `室温 ${fmtNum(st(r.main).a.current_temperature)}°`;
  if (d.kind === "water_heater" && st(r.main).a.current_temperature != null) return `水温 ${fmtNum(st(r.main).a.current_temperature)}°`;
  if (r.left && busyStatus(val(r.status))) return `还剩 ${minutes(r.left)}`;
  return "";
}

function kindPanel(d) {
  const r = d.roles, id = r.main, s = st(id), a = s.a;
  if (!id || dead(s.s)) return "";
  const dom = id.split(".")[0];
  if (dom === "climate") {
    const step = a.target_temp_step || (d.kind === "ac" ? 1 : 0.5);
    const modes = (a.hvac_modes || []).filter(m => m !== "off");
    return `<div class="panel ${s.s === "off" ? "dim" : ""}">
      ${a.temperature != null ? `<div class="temp"><button class="circle" data-temp="${id}" data-d="${-step}">−</button>
        <div class="v">${fmtNum(a.temperature)}<small>°C</small></div>
        <button class="circle" data-temp="${id}" data-d="${step}">+</button></div>` : ""}
      ${modes.length > 1 ? `<div class="sub-h">模式</div><div class="chips">${modes.map(m => `<button class="chip ${s.s === m ? "on" : ""}" data-svc="${id}|set_hvac_mode|hvac_mode|${m}">${HVAC[m] ?? m}</button>`).join("")}</div>` : ""}
      ${a.fan_modes?.length ? `<div class="sub-h">风速</div><div class="chips">${a.fan_modes.map(m => `<button class="chip ${a.fan_mode === m ? "on" : ""}" data-svc="${id}|set_fan_mode|fan_mode|${esc(m)}">${esc(m)}</button>`).join("")}</div>` : ""}
      ${a.preset_modes?.length ? `<div class="sub-h">档位</div><div class="chips">${a.preset_modes.map(m => `<button class="chip ${a.preset_mode === m ? "on" : ""}" data-svc="${id}|set_preset_mode|preset_mode|${esc(m)}">${esc(m)}</button>`).join("")}</div>` : ""}
    </div>`;
  }
  if (dom === "light" && s.s === "on") {
    const modes = a.supported_color_modes || [];
    const dim = modes.some(m => m !== "onoff");
    const ct = modes.includes("color_temp");
    const b = a.brightness != null ? Math.round(a.brightness / 2.55) : 100;
    const kMin = a.min_color_temp_kelvin || 2700, kMax = a.max_color_temp_kelvin || 6500, k = a.color_temp_kelvin || kMin;
    if (!dim) return "";
    return `<div class="panel">
      <div class="slider-h"><span>亮度</span><b>${b}%</b></div>
      <input type="range" min="1" max="100" value="${b}" style="--p:${b}%" data-range="${id}|turn_on|brightness_pct">
      ${ct ? `<div class="ct"><div class="slider-h" style="margin-top:8px"><span>色温</span><b>${k}K</b></div>
        <input type="range" min="${kMin}" max="${kMax}" step="50" value="${k}" data-range="${id}|turn_on|color_temp_kelvin"></div>` : ""}
    </div>`;
  }
  if (dom === "fan" && s.s === "on") {
    return `<div class="panel">
      ${a.percentage != null ? `<div class="slider-h"><span>风速</span><b>${a.percentage}%</b></div>
        <input type="range" min="1" max="100" step="${a.percentage_step || 1}" value="${a.percentage}" style="--p:${a.percentage}%" data-range="${id}|set_percentage|percentage">` : ""}
      ${a.preset_modes?.length ? `<div class="sub-h">模式</div><div class="chips">${a.preset_modes.map(m => `<button class="chip ${a.preset_mode === m ? "on" : ""}" data-svc="${id}|set_preset_mode|preset_mode|${esc(m)}">${esc(m)}</button>`).join("")}</div>` : ""}
      ${a.oscillating != null ? `<div class="row"><div class="l">摇头</div><button class="sw ${a.oscillating ? "on" : ""}" data-svc="${id}|oscillate|oscillating|${!a.oscillating}"></button></div>` : ""}
    </div>`;
  }
  if (dom === "humidifier" && s.s === "on") {
    const lo = a.min_humidity ?? 30, hi = a.max_humidity ?? 80, h = a.humidity ?? lo;
    return `<div class="panel">
      <div class="slider-h"><span>目标湿度</span><b>${h}%</b></div>
      <input type="range" min="${lo}" max="${hi}" value="${h}" style="--p:${((h - lo) / (hi - lo)) * 100}%" data-range="${id}|set_humidity|humidity">
      ${a.available_modes?.length ? `<div class="sub-h">模式</div><div class="chips">${a.available_modes.map(m => `<button class="chip ${a.mode === m ? "on" : ""}" data-svc="${id}|set_mode|mode|${esc(m)}">${{ auto: "自动", normal: "普通", sleep: "睡眠" }[m] ?? esc(m)}</button>`).join("")}</div>` : ""}
    </div>`;
  }
  if (dom === "water_heater" && a.temperature != null) {
    return `<div class="panel"><div class="temp"><button class="circle" data-temp="${id}" data-d="-1">−</button>
      <div class="v">${fmtNum(a.temperature)}<small>°C</small></div><button class="circle" data-temp="${id}" data-d="1">+</button></div>
      ${window.whPanel ? whPanel(d) : ""}</div>`;
  }
  if (dom === "vacuum") {
    const go = (svc, text, on) => `<button class="chip ${on ? "on" : ""}" data-svc="${id}|${svc}">${text}</button>`;
    const speeds = a.fan_speed_list || [];
    return `<div class="panel">
      <div class="chips">${go("start", s.s === "paused" ? "继续清扫" : "开始清扫", s.s === "cleaning")}${s.s === "cleaning" ? go("pause", "暂停") : ""}${go("return_to_base", "回充", s.s === "returning")}${go("locate", "找机器")}</div>
      ${speeds.length ? `<div class="sub-h">吸力</div><div class="chips">${speeds.map(m => `<button class="chip ${a.fan_speed === m ? "on" : ""}" data-svc="${id}|set_fan_speed|fan_speed|${esc(m)}">${esc(m)}</button>`).join("")}</div>` : ""}
    </div>`;
  }
  if (dom === "media_player" && a.volume_level != null) {
    const v = Math.round(a.volume_level * 100);
    return `<div class="panel"><div class="slider-h"><span>音量</span><b>${v}%</b></div>
      <input type="range" min="0" max="100" value="${v}" style="--p:${v}%" data-range="${id}|volume_set|volume_level|0.01"></div>`;
  }
  return "";
}

function tilesPanel(d) {
  const r = d.roles;
  const keys = [["temperature", "温度"], ["humidity", "湿度"], ["illuminance", "光照"], ["power", "功率"], ["energy", "耗电量"], ["battery", "电量"]];
  // the water heater's kWh is already counted on the 能耗 page
  const tiles = keys.filter(([k]) => r[k] && !(k === "energy" && d.kind === "water_heater")).map(([k, label]) => [label, r[k]]);
  if (d.kind === "fridge") Object.entries(r).filter(([k]) => k.startsWith("zone:")).forEach(([k, id]) => tiles.push([k.slice(5), id]));
  if (r.left && num(r.left)) tiles.push(["剩余时间", r.left]);
  if (r.fault && !/无|No Faults|没有|null|^0$/.test(val(r.fault) || "")) tiles.push(["故障", r.fault]);
  const uniq = [...new Map(tiles.map(t => [t[1] + t[0], t])).values()];
  if (!uniq.length) return "";
  return `<div class="panel"><div class="tiles">${uniq.map(([label, id]) => {
    const t = id === r.left ? minutes(id) : id.startsWith("binary_sensor.") ? binaryText(id) : sensorText(id);
    return `<div class="tile"><div class="k">${esc(label)}</div><div class="val">${esc(t)}</div></div>`;
  }).join("")}</div></div>`;
}

function entityRow(f) {
  const id = f.entity_id, s = st(id), a = s.a, dom = id.split(".")[0], label = esc(f.label);
  if (dead(s.s)) return `<div class="row"><div class="l">${label}</div><div class="rv">离线</div></div>`;
  switch (dom) {
    case "switch": case "light":
      return `<div class="row"><div class="l">${label}</div><button class="sw ${s.s === "on" ? "on" : ""}" data-toggle="${id}"></button></div>`;
    case "select": {
      const opts = a.options || [];
      if (opts.length <= 6 && opts.join("").length < 40)
        return `<div class="row col"><div class="l">${label}</div><div class="chips">${opts.map(o => `<button class="chip ${s.s === o ? "on" : ""}" data-svc="${id}|select_option|option|${esc(o)}">${esc(o)}</button>`).join("")}</div></div>`;
      return `<div class="row"><div class="l">${label}</div><select data-select="${id}">${opts.map(o => `<option ${s.s === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></div>`;
    }
    case "number": {
      const u = a.unit_of_measurement ? ` ${a.unit_of_measurement}` : "";
      return `<div class="row"><div class="l">${label}</div><div class="stepper">
        <button data-num="${id}" data-d="-1">−</button><span>${fmtNum(parseFloat(s.s))}${esc(u)}</span><button data-num="${id}" data-d="1">+</button></div></div>`;
    }
    case "button":
      return `<div class="row"><div class="l">${label}</div><button class="act" data-svc="${id}|press">执行</button></div>`;
    case "binary_sensor":
      return `<div class="row"><div class="l">${label}</div><div class="rv">${esc(binaryText(id))}</div></div>`;
    case "sensor":
      return `<div class="row"><div class="l">${label}</div><div class="rv">${esc(sensorText(id))}</div></div>`;
    default:
      return `<div class="row"><div class="l">${label}</div><div class="rv">${esc(s.s)}</div></div>`;
  }
}

// ---------------------------------------------------------------- routing & events

const isSmart = () => location.hash === "#/smart";
const isServer = () => location.hash === "#/server";
const isLocal = () => location.hash === "#/local";

function bottomNav(on) {
  return `<nav id="bnav"><button class="${on === "home" ? "on" : ""}" data-act="nav-home">${icon("home")}<span>家</span></button>
    <button class="${on === "smart" ? "on" : ""}" data-act="nav-smart">${icon("smart")}<span>智能</span></button>
    <button class="${on === "nas" ? "on" : ""}" data-act="nav-nas">${icon("server")}<span>NAS</span></button>
    <button class="${on === "local" ? "on" : ""}" data-act="nav-local">${icon("link")}<span>直连</span></button></nav>`;
}

function currentDevice() {
  const m = location.hash.match(/^#\/d\/([^/]+)/);   // washer sub-pages: #/d/<id>/m/<drum>/<mode>
  return m ? S.byId[decodeURIComponent(m[1])] : null;
}

function render() {
  // never redraw under a finger that is on a card (long press pending) or carrying one
  if (!S.model || busy || drag.active || drag.pending || pager.moving || (swipe.t && swipe.axis === "x")) { if (S.model) S.stale = true; return; }   // held back: whoever lifts the hold catches up
  S.stale = false;
  const y = window.scrollY, tabsX = $(".tabs")?.scrollLeft ?? 0;
  if (isAuto()) {   // the editor redraws itself; live updates must not wipe what is being typed
    if (!S.ed || S.ed.target !== autoTarget()) openAuto();
    return;
  }
  const d = currentDevice();
  if (d && S.edit) S.edit = null;
  const rects = !d && !isSmart() && !isServer() && !isLocal() && !isAuto() && $(".roomview.cur") ? new Map([...document.querySelectorAll(".roomview.cur .card[data-dev]")].map(c => [c.dataset.dev, c.getBoundingClientRect()])) : null, room0 = S.room, scrollY0 = window.scrollY;
  d ? renderDetail(d) : isSmart() ? renderSmart() : isServer() ? renderServer() : isLocal() ? renderLocal() : renderHome();
  if (!d && !isSmart() && !isServer() && !isLocal() && typeof loadEnergy === "function") loadEnergy();
  window.scrollTo(0, y);
  if (rects && rects.size && room0 === S.room && window.scrollY === scrollY0) {
    for (const c of document.querySelectorAll(".roomview.cur .card[data-dev]")) {
      const a = rects.get(c.dataset.dev);
      if (!a) continue;
      const b = c.getBoundingClientRect(), dx = a.left - b.left, dy = a.top - b.top;
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
      c.style.transition = "none"; c.style.transform = `translate(${dx}px, ${dy}px)`;
      void c.offsetWidth;
      c.style.transition = "transform .3s cubic-bezier(.2, .8, .25, 1)"; c.style.transform = "";
      setTimeout(() => { c.style.transition = ""; }, 340);
    }
  }
  decorateEdit();
  const tabs = $(".tabs");
  if (tabs) { tabs.scrollLeft = tabsX; centerActiveTab(); placeIndicator(roomList().indexOf(S.room), false); }
  fitPager();
  markStuck();
  if (S.pop) refreshPop();
}

// Tabs / title bar get a frosted background only once they stick to the top over the cards.
function markStuck() {
  const tabs = $(".tabs"), bar = $(".bar");
  const sat = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--sat")) || 0;
  const stuck = tabs ? tabs.getBoundingClientRect().top <= sat + 1 && window.scrollY > 8
                     : !!bar && bar.getBoundingClientRect().top <= 0 && window.scrollY > 8;
  (tabs || bar)?.classList.toggle("stuck", stuck);
  document.body.classList.toggle("stuck", !!tabs && stuck);
}
window.addEventListener("scroll", markStuck, { passive: true });

// Page transitions like Mijia: going deeper, the new page slides in from the right over the old one
// (which drifts left); going back, the old page slides off to the right.
const pageDepth = h => !h || h === "#/smart" || h === "#/server" || h === "#/local" ? 0 : /^#\/d\/[^/]+\/(m|more)\//.test(h) ? 2 : 1;
let lastHash = location.hash;

function pageGhost() {
  const g = document.createElement("div");
  g.id = "ghost";
  const inner = document.createElement("div");
  inner.className = "gin";
  inner.style.top = -window.scrollY + "px";
  inner.innerHTML = $("#app").innerHTML;
  g.appendChild(inner);
  document.body.appendChild(g);
  return g;
}

function slidePages(ghost, forward) {
  const app = $("#app");
  ghost.classList.add(forward ? "under" : "over");
  app.classList.add(forward ? "pg-in" : "pg-under");
  const end = () => { ghost.remove(); app.classList.remove("pg-in", "pg-under"); };
  ghost.addEventListener("animationend", end, { once: true });
  setTimeout(end, 500);
}

window.addEventListener("hashchange", () => {
  const from = pageDepth(lastHash), to = pageDepth(location.hash);
  lastHash = location.hash;
  const animate = from !== to && S.model && !matchMedia("(prefers-reduced-motion: reduce)").matches;
  $("#ghost")?.remove();
  const ghost = animate ? pageGhost() : null;
  S.openMore = false; render(); window.scrollTo(0, 0);
  if (ghost) slidePages(ghost, to > from);
});

document.addEventListener("contextmenu", e => { if (e.target.closest(".card")) e.preventDefault(); });

document.addEventListener("click", e => {
  if (drag.swallowClick) { drag.swallowClick = false; return; }   // the lift that ended a long press / drag
  if (S.edit) {
    const c = e.target.closest(".card[data-dev]");
    if (c && !e.target.closest("#edit-top,#edit-bottom,#sheet")) { toggleSelect(c.dataset.dev); return; }
  }
  const t = e.target.closest("[data-toggle],[data-svc],[data-temp],[data-ctemp],[data-num],[data-room],[data-room-jump],[data-act],[data-dev]");
  if (!t) return;
  if (t.dataset.toggle) { e.stopPropagation(); toggle(t.dataset.toggle); return; }
  if (t.dataset.svc) {
    const [id, svc, key, v] = t.dataset.svc.split("|");
    const data = key ? { [key]: v === "true" ? true : v === "false" ? false : v } : {};
    const opt = key === "hvac_mode" ? { s: v } : key === "option" ? { s: v } : key ? { a: { ...st(id).a, [key]: data[key] } } : null;
    call(id, svc, data, opt);
    if (t.closest("#sheet")) closeSheet();
    return;
  }
  if (t.dataset.ctemp) {
    const id = t.dataset.ctemp, c = comfortOf(id), a = st(id).a;
    if (!c) return;
    const nv = Math.min(a.max_temp ?? 31, Math.max(a.min_temp ?? 16, c.target + parseFloat(t.dataset.d)));
    S.model.comfort[id] = { ...c, target: nv }; render();
    clearTimeout(S.ctT); S.ctT = setTimeout(() => comfortSend(id, true, nv), 600);
    return;
  }
  if (t.dataset.temp) {
    const id = t.dataset.temp, a = st(id).a, nv = Math.min(a.max_temp ?? 99, Math.max(a.min_temp ?? 0, (a.temperature ?? 24) + parseFloat(t.dataset.d)));
    call(id, "set_temperature", { temperature: nv }, { a: { ...a, temperature: nv } });
    return;
  }
  if (t.dataset.num) {
    const id = t.dataset.num, a = st(id).a, step = a.step || 1;
    const nv = Math.min(a.max ?? 1e9, Math.max(a.min ?? -1e9, parseFloat(val(id)) + step * parseFloat(t.dataset.d)));
    call(id, "set_value", { value: nv }, { s: String(nv) });
    return;
  }
  if (t.dataset.room) { selectRoom(t.dataset.room); return; }
  if (t.dataset.roomJump) { closeSheet(); selectRoom(t.dataset.roomJump); return; }
  const act = t.dataset.act;
  const arg = t.dataset.arg;
  if (act === "cam-open") { camStop(); HomeHubApp.openCamera("c700", ""); return; }
  if (act === "cam-play") { camInline(""); return; }
  if (act === "cam-wake") { camWake(); return; }
  if (act === "cam-clip") { camInline((arg || "").split("|")[1] || ""); return; }
  if (act === "cam-clipfull") { closeSheet(); const c = (arg || "").split("|")[1]; if (c) HomeHubApp.openCamera("c700", c); return; }
  if (act === "cam-all") { camAll(); return; }
  if (act === "edit-done" || act === "edit-cancel") return exitEdit();
  if (act === "edit-top") return editMoveToTop();
  if (act === "edit-move") return editMove();
  if (act === "move-to") return editMoveTo(arg);
  if (act === "edit-rename") return editRename();
  if (act === "rename-save") return editRenameSave();
  if (act === "edit-delete") return editDelete();
  if (act === "delete-confirm") { const ids = [...S.edit]; closeSheet(); exitEdit(); setHidden(ids, true).then(render); return; }
  if (act === "show-hidden") { e.preventDefault(); return showHidden(); }
  if (act === "unhide") { setHidden([arg], false).then(() => { render(); showHidden(); }); return; }
  if (act === "sheet-close") return closeSheet();
  if (act === "add-device") return addDeviceSheet();
  if (act === "add-found") return addFound(arg, t);
  if (act === "flow-submit") return flowSubmit(t);
  if (act === "flow-menu") return flowSend({ next_step_id: arg }, t);
  if (act === "sync-xiaomi") return syncXiaomi(t);
  if (act === "open-mijia") {
    if (window.HomeHubApp?.openApp) HomeHubApp.openApp("com.xiaomi.smarthome");
    else toast("请手动打开米家 App 添加设备");
    return;
  }
  if (act === "comfort") {
    if (st(arg).s === "off") return toast("先打开空调");
    return comfortSend(arg, !comfortOf(arg)?.on);
  }
  if (act === "pub-edit") return publicAddressSheet();
  if (act === "pub-test") {
    if (!HomeHubApp.testPublicUrl) return toast("要先更新到新版 App 才能测试");
    const el = $("#pubres"); if (el) { el.textContent = "正在测试…"; el.className = "pubres"; }
    return HomeHubApp.testPublicUrl("t", $("#pubin")?.value || "");
  }
  if (act === "pub-save" || act === "pub-reset") {
    const v = act === "pub-reset" ? "" : ($("#pubin")?.value || "");
    const err = HomeHubApp.setPublicUrl(v);
    if (err) return toast(err);
    toast(act === "pub-reset" ? "已恢复成默认地址" : "公网地址已保存，正在重新连接");
    return addDeviceSheet();
  }
  if (act === "lights") return popSheet("lights");
  if (act === "temps") return popSheet("temp");
  if (act === "lights-on") return allLights(true);
  if (act === "lights-off") return allLights(false);
  if (act === "nav-home") { if (isSmart() || isServer() || isLocal()) location.hash = ""; return; }
  if (act === "nav-smart") { if (location.hash !== "#/smart") location.hash = "#/smart"; return; }
  if (act === "nav-nas") { if (!isServer()) location.hash = "#/server"; return; }
  if (act === "nav-local") { if (!isLocal()) location.hash = "#/local"; return; }
  if (act === "auto-toggle") {
    const on = isOn(arg);
    call(arg, on ? "turn_off" : "turn_on", {}, { s: on ? "off" : "on" });
    if (isAuto()) renderAutoEditor();
    return;
  }
  if (act === "auto-open") { location.hash = "#/auto/" + encodeURIComponent(arg); return; }
  if (act === "auto-new") { S.ed = null; location.hash = "#/auto/new"; return; }
  if (act === "auto-run") { call(arg, "trigger").then(() => toast("已执行")); return; }
  if (act === "back") { history.length > 1 ? history.back() : (location.hash = ""); return; }
  if (act === "washer") { location.href = "/washer"; return; }
  if (act === "weather") return openWeather();
  if (act === "energy") return openEnergy();
  if (act === "monitor") { location.hash = "#/server"; return; }
  if (act === "update") { e.preventDefault(); HomeHubApp.checkUpdate(); return; }
  if (act === "switch-home") {
    const hs = S.model.homes.map(h => h.name), i = hs.indexOf(S.home);
    S.home = hs[(i + 1) % hs.length]; S.room = "全部"; render(); return;
  }
  if (t.dataset.dev === CAM_ID) return;     // the camera card has its own buttons
  if (t.dataset.dev) { closeSheet(); location.hash = "#/d/" + encodeURIComponent(t.dataset.dev); }
});

document.addEventListener("change", e => {
  const sel = e.target.closest("[data-select]");
  if (sel) call(sel.dataset.select, "select_option", { option: sel.value }, { s: sel.value });
});

// Sliders: live label while dragging, send on release; no re-render under the finger.
document.addEventListener("input", e => {
  const r = e.target.closest("[data-range]");
  if (!r) return;
  busy = 1;
  r.style.setProperty("--p", ((r.value - r.min) / (r.max - r.min)) * 100 + "%");
  const b = r.previousElementSibling?.querySelector("b");
  const unit = r.dataset.range.includes("kelvin") ? "K" : "%";
  if (b) b.textContent = r.value + unit;
});
document.addEventListener("change", e => {
  const r = e.target.closest("[data-range]");
  if (!r) return;
  busy = 0;
  const [id, svc, key, scale] = r.dataset.range.split("|");
  const v = parseFloat(r.value) * (scale ? parseFloat(scale) : 1);
  const opt = key === "brightness_pct" ? { s: "on", a: { ...st(id).a, brightness: Math.round(v * 2.55) } } : { a: { ...st(id).a, [key]: v } };
  call(id, svc, { [key]: v }, opt);
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") connect();
});

// The phone's back gesture / key (app 1.7+ asks the page first): close what's open, leave edit mode,
// leave a sub-page; false means "nothing left to undo here" and the app goes to the background.
window.hhBack = () => {
  if (S.camInl) { camStop(); return true; }
  if (window.hhCamClose) { window.hhCamClose(); return true; }
  if (drag.active) return true;
  if ($("#sheet")) { closeSheet(); return true; }
  if (S.htPage && window.htBack && window.htBack()) return true;   // heater sub pages
  if ($("#wpage")) { closeWeather(); return true; }
  if ($("#epage")) { closeEnergy(); return true; }
  if ($("#kpage")) { closeKPage(); return true; }
  if (S.edit) { exitEdit(); return true; }
  if (isAuto()) { S.ed = null; location.hash = "#/smart"; return true; }
  if (/^#\/d\/[^/]+\//.test(location.hash)) { location.hash = location.hash.replace(/\/(m|more)\/.*$/, ""); return true; }
  if (location.hash) { location.hash = ""; return true; }
  return false;
};

// App 1.3+ draws the page under the status bar and tells us how tall the bars are.
if (window.HomeHubApp?.insetTop) {
  document.documentElement.style.setProperty("--sat", HomeHubApp.insetTop() + "px");
  document.documentElement.style.setProperty("--sab", HomeHubApp.insetBottom() + "px");
}

// Like Mijia without network: the last known home stays on screen (marked offline), controls just
// fail with a toast, and we keep trying to reach home in the background - no 重试 button needed.
let retryTimer = 0, retryDelay = 2000;
async function connect() {
  clearTimeout(retryTimer);
  try {
    await load();
    listen();
    retryDelay = 2000;
    setOffline(false);
  } catch (e) {
    if (S.model) setOffline(true);
    else $("#app").innerHTML = `<div class="loading">正在连接家里…<br><small>${esc(e.message)}</small></div>`;
    retryTimer = setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 10000);
  }
}

function setOffline(off) {
  if (S.offline === off) return;
  S.offline = off;
  scheduleRender();
}

window.addEventListener("online", () => connect());
showCached();
connect();


// ---------------------------------------------------------------- pull down to refresh
// Starts only when the page is at its top and the finger moves mostly down; a long press, a drag of a card,
// a room swipe or an open pop-up are left alone.
(() => {
  let y0 = 0, x0 = 0, pull = 0, on = false, busyRefresh = false, el = null;
  const LIMIT = 70;
  const bar = () => el || (el = (document.body.insertAdjacentHTML("beforeend", `<div id="ptr"><i></i><span></span></div>`), $("#ptr")));
  const paint = (d, text, spin) => { const b = bar(); b.style.transform = `translateY(${d}px)`; b.style.opacity = Math.min(1, d / 40); b.classList.toggle("spin", !!spin); b.querySelector("span").textContent = text; };
  const reset = () => { if (el) { el.style.transition = "transform .25s, opacity .25s"; el.style.transform = "translateY(0)"; el.style.opacity = 0; setTimeout(() => { if (el) el.style.transition = ""; }, 260); } };
  document.addEventListener("touchstart", e => {
    on = false; pull = 0;
    if (busyRefresh || e.touches.length !== 1 || window.scrollY > 2 || S.edit || drag.active || S.pop || $("#sheet") || $("#kpage")) return;   // a finger that starts on a card counts too: moving cancels its long press
    if (e.target.closest("input,[data-range],.wheel,.tabs,.kmode")) return;
    y0 = e.touches[0].clientY; x0 = e.touches[0].clientX; on = null;       // null = not decided yet
  }, { passive: true });
  document.addEventListener("touchmove", e => {
    if (on === false || busyRefresh) return;
    const dy = e.touches[0].clientY - y0, dx = e.touches[0].clientX - x0;
    if (swipe.axis === "x") { if (on) reset(); on = false; return; }       // the room swipe owns this gesture
    if (on === null) {
      if (Math.abs(dy) < 12 && Math.abs(dx) < 12) return;
      on = dy > 12 && dy > Math.abs(dx) * 1.6 && window.scrollY <= 2;     // clearly downward, not a diagonal swipe
      if (!on) return;
    }
    if (window.scrollY > 2) { on = false; reset(); return; }
    pull = Math.max(0, dy) * 0.5;
    paint(Math.min(pull, 90), pull >= LIMIT ? "松开刷新" : "下拉刷新", false);
    clearTimeout(drag.timer); drag.timer = 0;                    // not a long press on a card
  }, { passive: true });          // passive: a listener that may cancel makes every swipe wait for the script
  let lastFresh = 0;
  const finish = async () => {
    if (!on) { on = false; return; }
    on = false;
    if (pull < LIMIT) return reset();
    busyRefresh = true; paint(56, "刷新中…", true);
    S.camAt = 0;
    try {
      const fresh = Date.now() - lastFresh > 30000;      // rebuilding the device list takes a few seconds: not on every pull
      await Promise.race([load(fresh), new Promise((_, no) => setTimeout(() => no(new Error("连接超时")), 12000))]);
      if (isLocal() && typeof loadLocal === "function") { await loadLocal(true); LC.at = Date.now(); paintLocal(); }   // 直连: ask every device again
      if (fresh) lastFresh = Date.now();
      toast("已刷新");
    } catch (e) { toast("刷新失败：" + e.message); }
    reset();
    setTimeout(() => { busyRefresh = false; }, 1500);   // a short pause so a second pull right after can't stack up
  };
  document.addEventListener("touchend", finish);
  document.addEventListener("touchcancel", () => { on = false; pull = 0; if (!busyRefresh) reset(); });   // the browser took the gesture: no refresh
})();

// A pop-up with a text field: while the field has focus the sheet moves up to the top part of the screen, so the
// keyboard (which the app's WebView does not shrink the page for) can never cover what is being typed.
(() => {
  let t = 0;
  document.addEventListener("focusin", e => {
    const sh = e.target.closest?.("#sheet");
    if (!sh || !e.target.matches("input[type=text],input:not([type]),input[type=search],input[type=url],input[type=number],textarea")) return;
    clearTimeout(t);
    if (!window.hhKeyboard) sh.classList.add("kb");             // an older app cannot tell the page how tall the keyboard is
    setTimeout(() => e.target.scrollIntoView?.({ block: "center" }), 60);
  });
  document.addEventListener("focusout", () => { clearTimeout(t); t = setTimeout(() => $("#sheet")?.classList.remove("kb"), 350); });
})();

setInterval(() => { if (!document.hidden && !S.camInl && document.querySelector(".camcard")) camLoad(true); }, 10000);
