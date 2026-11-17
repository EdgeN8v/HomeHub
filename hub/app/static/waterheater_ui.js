// Water heater extras laid out like Mijia's page: 除菌 (tap to start, tap again to stop), 定时除菌
// (switch + day-cycle + start time), 错峰用电 (up to 3 time windows kept in one text property: add,
// retime, delete, switch each on/off), 屏幕亮度, plus the 热水量 bars and 防冻 state the device reports.
// See WATER_HEATER_EXTRA in home.py for which entities these are.

const WH_MAX_WINS = 3;
const whHM = m => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
const whMinToHHMM = m => whHM(m).replace(":", "");
const whHHMMToMin = t => parseInt((t || "0000").slice(0, 2), 10) * 60 + parseInt((t || "0000").slice(2, 4), 10);

function whTimerText(wh) {
  const cyc = wh.sterilize_cycle ? num(wh.sterilize_cycle) : null;
  const sm = wh.sterilize_start ? num(wh.sterilize_start) : null;
  return [cyc != null ? `每${cyc}天` : "", sm != null ? whHM(sm) : ""].filter(Boolean).join(" · ");
}

// "1&2200&0100&1&0" -> {rep, start, end, on, id, text:"22:00–01:00"}. A window may cross midnight.
function whParseSchedule(text) {
  if (!text || dead(text)) return [];
  return text.split("-").filter(Boolean).map(seg => {
    const [rep, start, end, on, id] = seg.split("&");
    return { rep, start, end, on: on === "1", id, text: `${whHM(whHHMMToMin(start))}–${whHM(whHHMMToMin(end))}` };
  });
}
function whSerializeSchedule(wins) {
  return wins.map(w => [w.rep, w.start, w.end, w.on ? "1" : "0", w.id].join("&")).join("-");
}
function whInWindow(w, nowMin) {
  const s = whHHMMToMin(w.start), e = whHHMMToMin(w.end);
  return s <= e ? (nowMin >= s && nowMin < e) : (nowMin >= s || nowMin < e);
}
const whNow = () => new Date().getHours() * 60 + new Date().getMinutes();

// ---- phone notification switches, kept on the hub (/api/notify, see notify.py); the phone app shows the notices.
// Read once per page load, then redrawn; until the answer comes the switch shows the last known value (localStorage).
let WH_NOTIFY = (() => { try { return JSON.parse(localStorage.getItem("wh-notify") || "null"); } catch (e) { return null; } })();
let whNotifyAsked = false;
function whNotifyOn(kind) {
  if (!whNotifyAsked) {
    whNotifyAsked = true;
    api("/api/notify").then(r => { whNotifySave(r.on); render(); }).catch(() => { whNotifyAsked = false; });
  }
  return WH_NOTIFY ? WH_NOTIFY[kind] !== false : true;
}
function whNotifySave(on) {
  WH_NOTIFY = on;
  try { localStorage.setItem("wh-notify", JSON.stringify(on)); } catch (e) { /* private mode */ }
}
async function whNotifyToggle(kind) {
  const was = whNotifyOn(kind);
  whNotifySave({ ...(WH_NOTIFY || {}), [kind]: !was });
  render();
  try {
    const r = await api("/api/notify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind, on: !was }) });
    whNotifySave(r.on);
    toast(!was ? "已开启，防冻开始工作时手机会收到通知" : "已关闭防冻提醒");
  } catch (e) {
    whNotifySave({ ...(WH_NOTIFY || {}), [kind]: was });
    toast("没保存上：" + e.message);
  }
  render();
}

// ---- the rows under the target-temperature dial on the device page (see kindPanel in app.js)
function whPanel(d) {
  const wh = d.wh || {};
  const sterOn = wh.sterilize && isOn(wh.sterilize);
  const timerOn = wh.sterilize_timer && isOn(wh.sterilize_timer);
  const onWins = whParseSchedule(val(wh.staggered)).filter(w => w.on);
  const active = onWins.some(w => whInWindow(w, whNow()));
  const level = parseInt(val(wh.water_level), 10);
  const bright = wh.brightness ? st(wh.brightness) : null;
  const ico = name => `<div class="pv-ico">${icon(name)}</div>`;
  return `
    ${Number.isFinite(level) ? `<div class="row pv">${ico("water_heater")}<div class="l">热水量<div class="rs">屏幕上的水量格数，格越多热水越多</div></div>
      <span class="pv-v">${level} / 4 格</span></div>` : ""}
    ${wh.anti_icing && val(wh.anti_icing) === "Antifreeze" ? `<div class="row pv">${ico("snow")}<div class="l">防冻<div class="rs">天冷时热水器自动加热，防止管路结冰</div></div><div class="rv">防冻中</div></div>` : ""}
    ${wh.anti_icing ? `<div class="row pv">${ico("bell")}<div class="l">防冻提醒<div class="rs">防冻功能开始工作时，发送提醒通知</div></div>
      <button class="sw ${whNotifyOn("antifreeze") ? "on" : ""}" style="--c:#2F80ED" data-wh="notify|antifreeze"></button></div>` : ""}
    ${wh.sterilize ? `<div class="row pv">${ico("shield")}<div class="l">除菌<div class="rs">${sterOn ? "正在除菌，水温会升高，再点一下停止" : "加热内胆杀菌，点一下开始"}</div></div>
      <button class="act" data-wh="steri">${sterOn ? "停止" : "开始"}</button></div>` : ""}
    ${wh.sterilize_timer ? `<div class="row pv" data-wh="timer">${ico("clock")}<div class="l">定时除菌<div class="rs">${timerOn ? whTimerText(wh) : "未开启"}</div></div><span class="chev">›</span></div>` : ""}
    ${wh.staggered ? `<div class="row pv" data-wh="staggered">${ico("bolt")}<div class="l">错峰用电<div class="rs">${onWins.length ? (active ? "现在正是错峰时段 · " : "") + onWins.map(w => w.text).join("、") : "未设置时段"}</div></div><span class="chev">›</span></div>` : ""}
    ${bright && !dead(bright.s) ? `<div class="row pv col"><div class="pv-head">${ico("sun")}<div class="l">屏幕亮度<div class="rs">3 分钟无操作时屏幕的亮度</div></div></div>
      <div class="chips">${(bright.a.options || []).map(o => `<button class="chip ${bright.s === o ? "on" : ""}" data-svc="${wh.brightness}|select_option|option|${esc(o)}">${esc(o)}</button>`).join("")}</div></div>` : ""}`;
}

// ---- a 时 / 分 wheel pair; done(minutes of the day)
function whHMPick(title, cur, done) {
  const sel = { h: Math.floor(cur / 60), m: cur % 60 };
  const hs = [...Array(24).keys()].map(x => String(x).padStart(2, "0"));
  const ms = [...Array(60).keys()].map(x => String(x).padStart(2, "0"));
  sheet(`<h3>${title}</h3>
    <div class="wwheels wtwo"><div class="wband"></div>${wWheelCol("h", hs, "时")}${wWheelCol("m", ms, "分")}</div>
    <div class="sheet-btns"><button id="whhmno">取消</button><button class="pri" id="whhmok">确定</button></div>`, "whs");
  document.querySelectorAll("#sheet .wwheel").forEach(w => wWheelInit(w, sel[w.dataset.key], i => { sel[w.dataset.key] = i; }));
  $("#whhmok").onclick = () => done(sel.h * 60 + sel.m);
  $("#whhmno").onclick = () => done(null);
}

// ---- 定时除菌: switch + 周期(天) + 启动时间(时:分), each the device's own entity
function whTimerSheet(d) {
  const wh = d.wh, timerOn = isOn(wh.sterilize_timer);
  sheet(`<h3>定时除菌</h3>
    <div class="row" style="--c:${COLORS.water_heater}"><div class="l">定时除菌<div class="rs">到时间自动开始一次除菌</div></div><button class="sw ${timerOn ? "on" : ""}" data-toggle="${wh.sterilize_timer}"></button></div>
    <div class="row" data-wh="cycle"><div class="l">除菌周期</div><div class="rv">每 ${num(wh.sterilize_cycle) ?? 30} 天</div></div>
    <div class="row" data-wh="start"><div class="l">启动时间</div><div class="rv">${whHM(num(wh.sterilize_start) ?? 60)}</div></div>
    <div class="sheet-btns"><button class="pri" data-act="sheet-close">完成</button></div>`, "whs");
}

// ---- 错峰用电: the list, and an editor for one window (WHD = the window being edited, i = null for a new one)
let WHD = null;
function whStaggeredSheet(d) {
  const wins = whParseSchedule(val(d.wh.staggered)), now = whNow();
  sheet(`<h3>错峰用电</h3>
    <p class="sub">最多 ${WH_MAX_WINS} 个时段，点时段修改或删除</p>
    ${wins.length ? wins.map((w, i) => `<div class="row whwin" style="--c:${COLORS.water_heater}"><div class="l" data-wh="win-edit|${i}"><b>${esc(w.text)}</b>${w.on && whInWindow(w, now) ? "<small>现在正是错峰时段</small>" : ""}</div>
      <button class="sw ${w.on ? "on" : ""}" data-wh="win-toggle|${i}"></button></div>`).join("") : `<p class="sub">还没有时段</p>`}
    <div class="sheet-btns">${wins.length < WH_MAX_WINS ? `<button data-wh="win-add">添加时段</button>` : ""}<button class="pri" data-act="sheet-close">完成</button></div>`, "whs");
}

function whWinSheet() {
  const w = WHD, bad = w.start === w.end;
  sheet(`<h3>${w.i == null ? "添加时段" : "修改时段"}</h3>
    <div class="row" data-wh="win-pick|start"><div class="l">开始时间</div><div class="rv">${whHM(w.start)}</div></div>
    <div class="row" data-wh="win-pick|end"><div class="l">结束时间</div><div class="rv">${whHM(w.end)}${w.end <= w.start && !bad ? "（次日）" : ""}</div></div>
    ${bad ? `<p class="sub">开始和结束不能是同一时间</p>` : ""}
    <div class="sheet-btns">${w.i == null ? `<button data-wh="staggered">取消</button>` : `<button class="pri danger" data-wh="win-del">删除</button>`}
      <button class="pri" data-wh="win-save" ${bad ? "disabled" : ""}>保存</button></div>`, "whs");
}

function whWrite(d, wins) {
  const text = whSerializeSchedule(wins);
  call(d.wh.staggered, "set_value", { value: text }, { s: text });
}

document.addEventListener("click", e => {
  const t = e.target.closest("[data-wh]");
  if (!t) return;
  e.stopPropagation();
  const d = currentDevice();
  if (!d || !d.wh) return;
  const [act, arg] = t.dataset.wh.split("|");
  const wins = () => whParseSchedule(val(d.wh.staggered));
  switch (act) {
    case "notify": return whNotifyToggle(arg);
    case "steri": { const on = isOn(d.wh.sterilize); return call(d.wh.sterilize, on ? "turn_off" : "turn_on", {}, { s: on ? "off" : "on" }); }
    case "timer": return whTimerSheet(d);
    case "cycle":
      return kPick("除菌周期", kRange([1, 120, 1]), num(d.wh.sterilize_cycle) ?? 30, "天", v => {
        call(d.wh.sterilize_cycle, "set_value", { value: v }, { s: String(v) });
        whTimerSheet(d);
      });
    case "start":
      return whHMPick("除菌启动时间", num(d.wh.sterilize_start) ?? 60, m => {
        if (m != null) call(d.wh.sterilize_start, "set_value", { value: m }, { s: String(m) });
        whTimerSheet(d);
      });
    case "staggered": return whStaggeredSheet(d);
    case "win-toggle": {
      const ws = wins(), i = parseInt(arg, 10);
      if (!ws[i]) return;
      ws[i].on = !ws[i].on;
      whWrite(d, ws);
      return whStaggeredSheet(d);
    }
    case "win-add": WHD = { i: null, start: 22 * 60, end: 23 * 60 }; return whWinSheet();
    case "win-edit": {
      const i = parseInt(arg, 10), w = wins()[i];
      if (!w) return;
      WHD = { i, start: whHHMMToMin(w.start), end: whHHMMToMin(w.end) };
      return whWinSheet();
    }
    case "win-pick":
      return whHMPick(arg === "start" ? "开始时间" : "结束时间", WHD[arg], m => { if (m != null) WHD[arg] = m; whWinSheet(); });
    case "win-save": {
      if (WHD.start === WHD.end) return;
      const ws = wins();
      if (WHD.i == null) {
        if (ws.length >= WH_MAX_WINS) return toast(`最多 ${WH_MAX_WINS} 个时段`);
        const used = new Set(ws.map(w => w.id));
        const id = String([...Array(WH_MAX_WINS).keys()].find(n => !used.has(String(n))) ?? ws.length);
        ws.push({ rep: ws[0]?.rep || "1", start: whMinToHHMM(WHD.start), end: whMinToHHMM(WHD.end), on: true, id });
      } else if (ws[WHD.i]) {
        Object.assign(ws[WHD.i], { start: whMinToHHMM(WHD.start), end: whMinToHHMM(WHD.end) });
      }
      whWrite(d, ws);
      return whStaggeredSheet(d);
    }
    case "win-del": {
      const ws = wins();
      ws.splice(WHD.i, 1);
      whWrite(d, ws);
      return whStaggeredSheet(d);
    }
  }
}, true);
