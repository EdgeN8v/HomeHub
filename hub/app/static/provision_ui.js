// Adding a Xiaomi Wi-Fi device without Mijia: the new device opens its own setup hotspot, the phone (app 2.0+)
// joins it and carries UDP packets, the hub runs the protocol (hub/app/miio_prov.py). The home Wi-Fi name and
// password are typed here by the person; they go to the hub only inside one request and are not stored.
"use strict";

const PROV = { sid: null, gateway: "192.168.99.1", cb: {}, wifiDone: null };

window.hhSetup = (id, res) => { const f = PROV.cb[id]; delete PROV.cb[id]; if (f) f(res); };
const native = (op, args = {}) => new Promise(resolve => {
  const id = Math.random().toString(36).slice(2);
  PROV.cb[id] = resolve;
  HomeHubApp.setup(op, id, JSON.stringify(args));
});

const provPost = async (path, body) => {
  // the phone's own network flips while it joins the hotspot; a request may need a second go
  for (let i = 0; ; i++) {
    try { return await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }
    catch (e) { if (i >= 3) throw e; await new Promise(r => setTimeout(r, 1500)); }
  }
};

function provSheet(title, bodyHtml, buttons = "") {
  sheet(`<h3>${esc(title)}</h3>${bodyHtml}${buttons ? `<div class="sheet-btns">${buttons}</div>` : ""}`);
}
const provBusy = (title, text) => provSheet(title, `<p class="fl-desc" style="text-align:center">${esc(text)}</p>`);
function provFail(msg) {
  if (window.HomeHubApp?.hasSetup) native("leave");
  provSheet("没配成功", `<p class="fl-desc">${esc(msg)}</p>`, `<button data-act="add-device">返回</button><button class="pri" data-act="prov-start">重试</button>`);
}

async function provStart() {
  if (!window.HomeHubApp?.hasSetup) {
    provSheet("需要更新 App", `<p class="fl-desc">配网需要 HomeHub App 2.0 或更新的版本。点「检查更新」，装好后再来。</p>`,
      `<button data-act="add-device">返回</button><button class="pri" data-act="update">检查更新</button>`);
    return;
  }
  provBusy("正在找设备", "找附近正在配网状态的小米设备…");
  const r = await native("scan");
  if (!r.ok) return provFail(r.error);
  if (!r.list.length) {
    provSheet("没找到设备", `<p class="fl-desc">先让设备进入配网状态：通常是接通电源后长按它的按钮，直到指示灯闪烁（具体看说明书）。已经在米家里配过的设备要先在米家里删除。</p>`,
      `<button data-act="add-device">返回</button><button class="pri" data-act="prov-start">重新扫描</button>`);
    return;
  }
  provSheet("选择要配网的设备", `<div class="sheet-list">${r.list.map(d =>
    `<div class="sheet-row"><span>${esc(d.model || d.ssid)}<small>${esc(d.ssid)} · 信号 ${d.level}</small></span><button data-act="prov-pick" data-arg="${esc(d.ssid)}">配网</button></div>`).join("")}</div>`,
    `<button data-act="add-device">返回</button>`);
}

const provUdp = async (hex, final) => {
  for (let i = 0; i < 3; i++) {
    const r = await native("udp", { host: PROV.gateway, port: 54321, hex, timeout: final ? 5000 : 3000 });
    if (r.ok || final) return r;   // the last message is allowed to go unanswered
    if (!r.timeout) return r;
  }
  return { ok: false, error: "设备没有回应" };
};

async function provRun(ssid) {
  provBusy("正在连接设备", "手机会弹出「连接到设备」的提示，点「连接」。");
  const j = await native("join", { ssid });
  if (!j.ok) return provFail(j.error);
  PROV.gateway = j.gateway || "192.168.99.1";
  provBusy("正在和设备握手", "和设备建立加密连接…");
  let action;
  try {
    action = await provPost("/api/provision/start", {});
    PROV.sid = action.session;
    while (true) {
      if (action.error) return provFail(action.error);
      if (action.send) {
        const r = await provUdp(action.send.hex, action.send.final);
        if (!r.ok && !action.send.final) return provFail(r.error || "设备没有回应");
        action = await provPost("/api/provision/step", { session: PROV.sid, reply: r.ok ? r.hex : null });
      } else if (action.need === "wifi") {
        const wifi = await provAskWifi(action.model, action.ssid_hint);
        if (!wifi) { native("leave"); return; }
        provBusy("正在发送 Wi-Fi 设置", "把家里的 Wi-Fi 信息加密发给设备…");
        action = await provPost("/api/provision/step", { session: PROV.sid, wifi });
      } else if (action.done) break;
      else return provFail("配网的回复看不懂，重试一次");
    }
  } catch (e) { return provFail("和家里的服务连不上：" + e.message); }

  await native("leave");
  provBusy("等设备上线", "设备正在连家里的 Wi-Fi，最多等一分钟…");
  for (let i = 0; i < 3; i++) {
    let f;
    try { f = await provPost("/api/provision/find", { session: PROV.sid }); } catch (e) { continue; }
    if (f.error) return provFail(f.error);
    if (f.found) {
      provSheet("设备上线了", `<p class="fl-desc">${esc(f.model)} 已经连上家里的 Wi-Fi（${esc(f.ip)}）。</p>`, `<button class="pri" data-act="sheet-close">好的</button>`);
      return;
    }
  }
  provFail("设备没有出现在家里的网络上。多半是 Wi-Fi 名称或密码输错了，或者设备只认 2.4G 的 Wi-Fi。长按设备的按钮让它回到配网状态，再试一次。");
}

// The Wi-Fi the person used last time is filled in (password as dots); "记住" keeps it only on this phone.
const provSaved = () => { try { return JSON.parse(localStorage.getItem("prov-wifi") || "null"); } catch (e) { return null; } };

function provAskWifi(model, ssidHint) {
  const saved = provSaved();
  const ssid = saved?.ssid || ssidHint || "";
  return new Promise(resolve => {
    PROV.wifiDone = resolve;
    provSheet("家里的 Wi-Fi", `<p class="fl-desc">${esc(model)} 要连上哪个 Wi-Fi？设备只认 2.4G 的 Wi-Fi。</p>
      <label class="fl-row"><span>Wi-Fi 名称</span><input data-p="ssid" value="${esc(ssid)}" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"></label>
      <label class="fl-row"><span>Wi-Fi 密码</span><input data-p="password" type="password" value="${esc(saved?.ssid === ssid ? saved.password : "")}" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
      <label class="fl-row inline"><span>记住，下次不用再输（只存在这台手机上）</span><input data-p="remember" type="checkbox" ${saved || !localStorage.getItem("prov-asked") ? "checked" : ""}></label>`,
      `<button data-act="prov-cancel">取消</button><button class="pri" data-act="prov-wifi">连接</button>`);
  });
}

document.addEventListener("click", e => {
  const t = e.target.closest("[data-act]");
  if (!t) return;
  const act = t.dataset.act;
  if (act === "prov-start") { e.stopImmediatePropagation(); provStart(); }
  else if (act === "prov-pick") { e.stopImmediatePropagation(); provRun(t.dataset.arg); }
  else if (act === "prov-wifi") {
    e.stopImmediatePropagation();
    const v = k => document.querySelector(`#sheet [data-p="${k}"]`).value;
    if (!v("ssid")) { toast("填一下 Wi-Fi 名称"); return; }
    try {
      localStorage.setItem("prov-asked", "1");
      if (document.querySelector('#sheet [data-p="remember"]').checked) localStorage.setItem("prov-wifi", JSON.stringify({ ssid: v("ssid"), password: v("password") }));
      else localStorage.removeItem("prov-wifi");
    } catch (err) { /* storage unavailable: just no memory */ }
    const f = PROV.wifiDone; PROV.wifiDone = null;
    if (f) f({ ssid: v("ssid"), password: v("password") });
  } else if (act === "prov-cancel") {
    e.stopImmediatePropagation();
    const f = PROV.wifiDone; PROV.wifiDone = null;
    if (f) f(null);
    closeSheet();
  }
}, true);
