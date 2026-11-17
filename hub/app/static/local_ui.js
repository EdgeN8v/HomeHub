// 直连: how much of the house can be controlled directly (no Home Assistant, no Xiaomi cloud) and what is switched to it.
// Numbers come from /api/local: every device with a token is asked on the LAN, so "能直连" is what answered just now.
const LC = { data: null, busy: false };

// last answer is kept (in memory and on the phone), so the page opens at once with it and updates when the new one arrives
try { LC.data = JSON.parse(localStorage.getItem("hh-local") || "null"); } catch (e) { /* none yet */ }

async function loadLocal(force) {
  if (LC.busy) return LC.data;
  LC.busy = true;
  try {
    LC.data = await api("/api/local" + (force ? "?fresh=1" : ""));
    try { localStorage.setItem("hh-local", JSON.stringify(LC.data)); } catch (e) { /* ignore */ }
  } catch (e) { /* keep what we had */ }
  LC.busy = false;
  return LC.data;
}

// the HomeHub device (room, name) that belongs to a Xiaomi device id: its entity ids carry _cn_<did>_
const lcDev = did => Object.values(S.byId || {}).find(d => (d.entities || []).some(e => e.includes(`_cn_${did}_`)));

// a tab page like 家 / 智能 / NAS: drawn at once from what was seen last, refreshed behind it
function renderLocal() {
  if (!$("#lbody")) {
    $("#app").innerHTML = `<div class="page lpg"><div class="top"><div class="top-row"><div class="home-name"><span>直连</span></div></div></div><div id="lbody"></div></div>${bottomNav("local")}`;
  }
  paintLocal();
  if (!LC.busy && Date.now() - (LC.at || 0) > 15000) { LC.at = Date.now(); loadLocal(false).then(() => { if (isLocal()) paintLocal(); }); }
}

function lcRow(r) {
  const d = lcDev(r.did), st = r.stats, room = d?.room || "";
  const sub = [room, r.model.split(".").slice(-1)[0], r.ip].filter(Boolean).join(" · ");
  const use = st ? `直发 ${st.local} 次${st.fallback ? `，设备没接受 ${st.fallback} 次` : ""}` : "";
  let tail;
  if (r.away) tail = `<span class="lcnote">${esc(r.away)}</span>`;
  else if (r.no_props) tail = `<span class="lcnote">没有可控属性</span>`;
  else if (r.direct) tail = `<button class="lcbtn on" data-lc="off|${r.did}">直连中</button>`;
  else if (r.local && r.spec) tail = `<button class="lcbtn" data-lc="on|${r.did}">开启</button>`;
  else if (r.local) tail = `<span class="lcnote">缺规格</span>`;
  else tail = `<span class="lcnote">没应答</span>`;
  if (r.model.startsWith("chuangmi.camera.") && r.local) tail = `<button class="lcbtn on" data-lc="cam|c700">看画面</button>`;
  return `<div class="lcrow"><i class="${r.direct ? "g" : r.local ? "y" : "n"}"></i><div class="lcn"><b>${esc(d?.name || r.name)}</b><small>${esc(sub)}${use ? " · " + esc(use) : ""}</small></div>${tail}</div>`;
}

function paintLocal() {
  const body = $("#lbody"), d = LC.data;
  if (!body) return;
  if (!d) { body.innerHTML = `<div class="enempty">正在逐台问设备…</div>`; return; }
  const here = d.devices.filter(r => !r.away), away = d.devices.filter(r => r.away);
  const direct = here.filter(r => r.direct), ready = here.filter(r => !r.direct && r.local && !r.no_props), off = here.filter(r => !r.direct && !r.local), other = here.filter(r => !r.direct && r.local && r.no_props);
  const total = here.length, localN = here.filter(r => r.local).length;
  const pct = n => (total ? n / total * 100 : 0).toFixed(1);
  body.innerHTML = `
    <div class="kcard lchero"><small>能不经云直接控制</small>
      <div class="enbig">${localN}<span> / ${total} 台</span></div>
      <div class="lcbar"><i class="g" style="width:${pct(direct.length)}%"></i><i class="y" style="width:${pct(ready.length)}%"></i><i class="n" style="width:${pct(off.length)}%"></i></div>
      <div class="lcleg"><span><i class="g"></i>直连中 ${direct.length}</span><span><i class="y"></i>能直连、还没开 ${ready.length}</span><span><i class="n"></i>没应答 ${off.length}</span></div></div>
    <div class="lcnums"><div><b>${d.account.devices}</b><small>账号里的设备</small></div><div><b>${d.account.tokens}</b><small>已备份密钥</small></div><div><b>${d.account.ble.length}</b><small>蓝牙密钥</small></div><div><b>${d.used.local}</b><small>直发次数</small></div></div>
    ${direct.length ? `<div class="lcsec">直连中</div><div class="kcard">${direct.map(lcRow).join("")}</div>` : ""}
    ${ready.length ? `<div class="lcsec">能直连，还没开启</div><div class="kcard">${ready.map(lcRow).join("")}</div>` : ""}
    ${other.length ? `<div class="lcsec">没有可控属性</div><div class="kcard">${other.map(lcRow).join("")}</div>` : ""}
    ${off.length ? `<div class="lcsec">本机没应答</div><div class="kcard">${off.map(lcRow).join("")}</div>` : ""}
    ${away.length ? `<div class="lcsec">不能在 Wi-Fi 局域网直连（另一个家、蓝牙或经网关）</div><div class="kcard">${away.map(lcRow).join("")}</div>` : ""}
    ${d.account.ble.length ? `<div class="lcsec">蓝牙设备（已有密钥，还没接管）</div><div class="kcard">${d.account.ble.map(b => `<div class="lcrow"><i class="n"></i><div class="lcn"><b>${esc(b.name)}</b><small>${esc(b.model)}</small></div><span class="lcnote">待接管</span></div>`).join("")}</div>` : ""}
    <div class="kcard"><p class="knote">「直连中」的设备，HomeHub 的指令直接发到设备，不经过 Home Assistant 和小米云。设备没应答或拒绝时，会直接告诉你没成功，不会悄悄改走 Home Assistant。个别还没做翻译的指令（比如灯的色温）暂时仍由 Home Assistant 代发。<br>
    「没应答」是设备现在不回应本机（关机、换了地址，或只能走云），不代表坏了。<br>
    设备的状态显示目前还是读 Home Assistant 的，它对洗衣机的部分设置是过期的，直连读取接上以后会更准。</p></div>`;
}

document.addEventListener("click", async e => {
  const t = e.target.closest("[data-lc]");
  if (!t) return;
  e.stopPropagation();
  const [act, did] = t.dataset.lc.split("|");
  if (act === "cam" && window.HomeHubApp?.openCamera) { HomeHubApp.openCamera(did, ""); return; }
  if (act === "cam") {      // live picture from go2rtc, passed through the hub. The app's request relay cannot carry a websocket and the
    // phone cannot play the camera's H.265, so it is shown as motion-JPEG in an <img>; if that does not start the snapshot (every ~3 s) takes over.
    const box = document.createElement("div");
    box.style.cssText = "position:fixed;inset:0;z-index:99;background:#000;display:flex;flex-direction:column";
    box.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;padding:10px 14px;color:#fff"><b>摄像头</b><small id="camst" style="opacity:.7">连接中…</small><button class="lcbtn" id="camx">关闭</button></div>
      <div style="flex:1;display:flex;align-items:center;justify-content:center;min-height:0"><img id="cami" style="max-width:100%;max-height:100%"></div>
      <div id="campad" style="display:grid;grid-template-columns:repeat(3,64px);grid-template-rows:repeat(2,52px);gap:8px;justify-content:center;padding:10px 0 calc(16px + var(--sab,0px))">
        <span></span><button class="lcbtn" data-mv="up">▲</button><span></span>
        <button class="lcbtn" data-mv="left">◀</button><button class="lcbtn" data-mv="down">▼</button><button class="lcbtn" data-mv="right">▶</button></div>`;
    document.body.appendChild(box);
    const im = box.querySelector("#cami"), st = box.querySelector("#camst");
    let alive = true, snap = false, ctl = null, shown = 0, last = "", why = "";
    // the phone's back gesture (the app asks window.hhBack first) closes the picture instead of leaving the app
    const close = () => { if (!alive) return; alive = false; if (ctl) ctl.abort(); im.removeAttribute("src"); box.remove(); window.hhCamClose = null; };
    window.hhCamClose = close;
    box.querySelector("#camx").onclick = close;
    // direction pad: a tap turns the head a step, holding keeps turning (repeat), letting go stops it
    const mv = (dir) => api(`/api/cam/move?dir=${dir}&src=${did}`, { method: "POST" }).catch(() => {});
    box.querySelectorAll("[data-mv]").forEach((b) => {
      let tm = 0, busy = false;
      const go = async () => { if (busy) return; busy = true; await mv(b.dataset.mv); busy = false; };
      const down = (e) => { e.preventDefault(); go(); tm = setInterval(go, 700); };
      const up = () => { if (!tm) return; clearInterval(tm); tm = 0; setTimeout(() => mv("stop"), 150); };
      b.addEventListener("pointerdown", down);
      ["pointerup", "pointercancel", "pointerleave"].forEach((ev) => b.addEventListener(ev, up));
    });
    const snapshots = () => {
      if (snap || !alive) return;
      snap = true; if (ctl) ctl.abort(); st.textContent = "静态画面，约 5 秒刷新";
      const tick = () => { if (!alive) return; im.onload = im.onerror = () => setTimeout(tick, 800); im.src = `/cam/api/frame.jpeg?src=${did}&t=${Date.now()}`; };
      tick();
    };
    // The app's request relay hands the stream over as one body without the multipart boundary, so an <img> cannot show it:
    // read the body here and cut the JPEG frames out of it (--frame / Content-Type / Content-Length / data).
    const live = async () => {
      ctl = new AbortController();
      try {
        const r = await fetch(`/cam/api/stream.mjpeg?src=${did}m`, { signal: ctl.signal });
        if (!r.ok) throw new Error("HTTP " + r.status);
        if (!r.body) throw new Error("no body stream");
        const rd = r.body.getReader();
        let buf = new Uint8Array(0);
        const dec = new TextDecoder();
        const t0 = Date.now();
        for (;;) {
          const { value, done } = await rd.read();
          if (done || !alive) break;
          const nb = new Uint8Array(buf.length + value.length); nb.set(buf); nb.set(value, buf.length); buf = nb;
          for (;;) {
            let h = -1;
            for (let i = 0; i + 3 < buf.length && i < 400; i++) if (buf[i] === 13 && buf[i + 1] === 10 && buf[i + 2] === 13 && buf[i + 3] === 10) { h = i; break; }
            if (h < 0) break;
            const m = /Content-Length:\s*(\d+)/i.exec(dec.decode(buf.subarray(0, h)));
            if (!m) { buf = buf.subarray(h + 4); continue; }
            const n = +m[1], end = h + 4 + n;
            if (buf.length < end) break;
            const url = URL.createObjectURL(new Blob([buf.slice(h + 4, end)], { type: "image/jpeg" }));
            if (last) URL.revokeObjectURL(last);
            last = url; im.src = url; shown++;
            if (shown === 1) st.textContent = "实时";
            buf = buf.subarray(end);
          }
        }
      } catch (e) { why = String(e && e.message || e); }
      if (alive && !snap && shown < 3) { snapshots(); if (why) st.textContent += "（" + why.slice(0, 40) + "）"; }
    };
    live();
    setTimeout(() => { if (alive && !snap && shown === 0) snapshots(); }, 10000);
    return;
  }
  if (act === "on" || act === "off") {
    const on = new Set(LC.data.enabled);
    act === "on" ? on.add(did) : on.delete(did);
    try { await kPost("/api/local/enable", { dids: [...on] }); await loadLocal(true); paintLocal(); toast(act === "on" ? "已切到直连" : "已切回 Home Assistant"); }
    catch (err) { toast("没成功：" + err.message); }
  }
}, true);
