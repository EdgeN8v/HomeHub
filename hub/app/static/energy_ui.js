// 用电: today / this month at a glance on the home page, and a full page with the daily bars, the monthly totals and
// who used what. The numbers come from the hub (/api/energy): the metered plug's watts shared between the fridge and
// the washer, plus the air conditioners' and water heater's own counters.
const EN = { data: null, at: 0, tab: "day", sel: null, busy: false };
const EN_NAMES = { fridge: "冰箱", washer: "洗衣机", mop: "洗地机" };
const EN_COLORS = ["#2f80ed", "#14a38b", "#f2711c", "#8e5cf0", "#e5484d", "#f5a623", "#2fb4d9"];
const enName = k => EN_NAMES[k] || S.byId?.[k]?.name || "其他";
const enFmt = v => v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2);
const enMoney = v => "¥" + (v >= 100 ? v.toFixed(0) : v.toFixed(2));

// the chip on the home page: "今日 3.2 度", filled in as soon as the numbers are here
function enChipText() { return EN.data ? `${EN.data.today.kwh.toFixed(1)}度` : "用电"; }
async function loadEnergy(force) {
  if (EN.busy || (!force && EN.data && Date.now() - EN.at < 60000)) return EN.data;
  EN.busy = true;
  try { EN.data = await api("/api/energy"); EN.at = Date.now(); } catch (e) { /* keep what we had */ }
  EN.busy = false;
  document.querySelectorAll(".enow").forEach(el => { el.textContent = enChipText(); });
  return EN.data;
}

const enWhoOrder = d => { const t = {}; d.forEach(x => Object.entries(x.who).forEach(([k, v]) => { t[k] = (t[k] || 0) + v.kwh; })); return Object.keys(t).sort((a, b) => t[b] - t[a]); };
const enColor = (order, k) => EN_COLORS[Math.max(0, order.indexOf(k)) % EN_COLORS.length];

function enPeriods() {
  const d = EN.data;
  if (EN.tab === "day") return d.days.slice(-30).map(x => ({ key: x.day, label: x.day.slice(8) + "日", sub: x.day.slice(5).replace("-", "月") + "日", ...x }));
  return d.cycles.map(c => {
    const m = c.start.slice(5, 7) * 1, bd = d.bill_day;
    return { key: c.key, label: m + "月", sub: bd === 1 ? `${c.key.slice(0, 4)}年${m}月` : `${m}月${bd}日起`, ...c };
  });
}

function enChart(ps, order, selKey) {
  if (!ps.length) return `<div class="enempty">还没有数据，过一会儿就有了</div>`;
  const max = Math.max(...ps.map(p => p.kwh), 0.01), H = 120, slots = Math.max(ps.length, EN.tab === "day" ? 14 : 6), W = 100 / slots, off = slots - ps.length;
  const bars = ps.map((p0, j) => {
    const p = p0, i = j + off;
    let y = H, segs = "";
    order.forEach(k => { const v = p.who[k]?.kwh || 0, h = v / max * H; if (h > 0) { y -= h; segs += `<rect x="${i * W + W * .18}" y="${y.toFixed(1)}" width="${W * .64}" height="${h.toFixed(1)}" fill="${enColor(order, k)}"/>`; } });
    return `<g data-en="${p.key}" class="${p.key === selKey ? "on" : ""}"><rect x="${i * W}" y="0" width="${W}" height="${H}" fill="transparent"/>${segs}</g>`;
  }).join("");
  const step = Math.ceil(ps.length / 7), labels = ps.map((p, i) => (ps.length - 1 - i) % step === 0 ? `<span style="left:${(i + off + .5) * W}%">${p.label}</span>` : "").join("");
  return `<div class="enchart"><svg viewBox="0 0 100 ${H}" preserveAspectRatio="none">${bars}</svg><div class="enlabels">${labels}</div></div>`;
}

function openEnergy() {
  if ($("#epage")) return;
  document.body.insertAdjacentHTML("beforeend", `<div id="epage"><div class="kph"><button class="back" data-en="close">${icon("back")}</button><h2>用电</h2><button class="kphr" data-en="settings">设置</button></div><div id="ebody"><div class="enempty">加载中…</div></div></div>`);
  loadEnergy(true).then(paintEnergy);
}
function closeEnergy() { $("#epage")?.remove(); }

function paintEnergy() {
  const body = $("#ebody"), d = EN.data;
  if (!body) return;
  if (!d) { body.innerHTML = `<div class="enempty">没取到数据，稍后再试</div>`; return; }
  const ps = enPeriods(), order = enWhoOrder(ps.length ? ps : []);
  const sel = ps.find(p => p.key === EN.sel) || ps[ps.length - 1];
  const cur = d.cycles[d.cycles.length - 1];
  const rows = sel ? Object.entries(sel.who).sort((a, b) => b[1].kwh - a[1].kwh) : [];
  body.innerHTML = `
    <div class="kcard enhero"><small>今天</small><div class="enbig">${enFmt(d.today.kwh)}<span> 度</span></div><div class="ensub">${enMoney(d.today.cost)}${cur ? ` · 本${d.bill_day === 1 ? "月" : "期"}累计 ${enFmt(cur.kwh)} 度 ${enMoney(cur.cost)}` : ""}</div></div>
    <div class="kcard">
      <div class="entabs"><button class="${EN.tab === "day" ? "on" : ""}" data-en="tab|day">按天</button><button class="${EN.tab === "month" ? "on" : ""}" data-en="tab|month">按月</button></div>
      ${enChart(ps, order, sel?.key)}
      ${sel ? `<div class="enpick"><b>${sel.sub}</b><span>${enFmt(sel.kwh)} 度 · ${enMoney(sel.cost)}</span></div>` : ""}
      <div class="enrows">${rows.map(([k, v]) => `<div class="enrow"><i style="background:${enColor(order, k)}"></i><span class="n">${esc(enName(k))}</span><span class="v">${enFmt(v.kwh)} 度</span><span class="m">${enMoney(v.cost)}</span></div>`).join("") || `<div class="enempty">这段时间没有数据</div>`}</div>
    </div>
    <div class="kcard"><p class="knote">合计只包含能计量的设备：插座上的冰箱和洗衣机、两台空调、热水器。灯、空气炸锅、微波炉等没有读数，没有算进来。<br>
    冰箱和洗衣机是插座的总功率拆开的：洗衣机没在工作时全算冰箱（冰箱平均约 ${enFmt(d.fridge_w)} W），洗衣机工作时，冰箱按这个平均值，其余算洗衣机。<br>
    电价：峰时（6:00–22:00）${d.prices.peak} 元，谷时（22:00–次日 6:00）${d.prices.valley} 元；空调${d.since ? d.since.slice(5).replace("-", "月") + "日" : ""}之前的天数没有分时，按两者平均价算。${d.since ? `<br>从 ${d.since.slice(5).replace("-", "月")}日开始记录。` : ""}</p></div>`;
}

function enSettings() {
  const d = EN.data;
  if (!d) return;
  sheet(`<h3>电价设置</h3>
    <div class="krow tg"><span>峰时电价<small>6:00–22:00，元/度</small></span><input id="enp" type="text" inputmode="decimal" value="${d.prices.peak}"></div>
    <div class="krow tg"><span>谷时电价<small>22:00–次日 6:00，元/度</small></span><input id="env" type="text" inputmode="decimal" value="${d.prices.valley}"></div>
    <div class="krow tg"><span>每月起算日<small>1 号就是自然月</small></span><input id="enb" type="text" inputmode="numeric" value="${d.bill_day}"></div>
    <div class="sheet-btns"><button data-act="sheet-close">取消</button><button class="pri" data-en="save">保存</button></div>`);
}

document.addEventListener("click", async e => {
  const t = e.target.closest("[data-en]");
  if (!t) return;
  e.stopPropagation();
  const [act, v] = t.dataset.en.split("|");
  if (act === "close") return closeEnergy();
  if (act === "settings") return enSettings();
  if (act === "tab") { EN.tab = v; EN.sel = null; return paintEnergy(); }
  if (act === "save") {
    try {
      EN.data = await kPost("/api/energy/settings", { prices: { peak: parseFloat($("#enp").value), valley: parseFloat($("#env").value) }, bill_day: parseInt($("#enb").value, 10) });
      closeSheet(); paintEnergy(); toast("已保存");
    } catch (err) { toast("没保存上：" + err.message); }
    return;
  }
  if (t.closest("g")) { EN.sel = act; paintEnergy(); }
}, true);
