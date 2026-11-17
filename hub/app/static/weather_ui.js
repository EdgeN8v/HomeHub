// 天气: tap the weather at the top of the home page. Now, the next 24 hours, the next days (HA's weather entity, met.no).
const WX = { data: null };
const WX_CN = { sunny: "晴", "clear-night": "晴", partlycloudy: "多云", cloudy: "阴", fog: "雾", rainy: "雨", pouring: "大雨", lightning: "雷电",
                "lightning-rainy": "雷阵雨", snowy: "雪", "snowy-rainy": "雨夹雪", hail: "冰雹", windy: "大风", "windy-variant": "多云有风", exceptional: "异常天气" };
const wxIcon = (c, hour) => WEATHER[c === "sunny" && hour != null && (hour < 6 || hour >= 18) ? "clear-night" : c] ?? "🌡️";
const wxBeaufort = kmh => [1, 5, 11, 19, 28, 38, 49, 61, 74, 88, 102, 117].findIndex(v => kmh < v);       // km/h -> 风力级 (0..12)
const wxLevel = kmh => { const i = wxBeaufort(kmh); return i < 0 ? 12 : i; };
const wxDir = d => d == null ? "" : ["北", "东北", "东", "东南", "南", "西南", "西", "西北"][Math.round(d / 45) % 8] + "风";
const wxN = v => v == null ? "–" : Math.round(v);

function openWeather() {
  if ($("#wpage")) return;
  document.body.insertAdjacentHTML("beforeend", `<div id="wpage"><div class="kph"><button class="back" data-wx="close">${icon("back")}</button><h2>天气</h2></div><div id="wbody"><div class="enempty">加载中…</div></div></div>`);
  api("/api/weather").then(d => { WX.data = d; paintWeather(); }).catch(() => { const b = $("#wbody"); if (b) b.innerHTML = `<div class="enempty">没取到天气，稍后再试</div>`; });
}
function closeWeather() { $("#wpage")?.remove(); }

function paintWeather() {
  const body = $("#wbody"), d = WX.data;
  if (!body || !d) return;
  const n = d.now, now = new Date(), hr = now.getHours();
  const hours = d.hourly.filter(h => new Date(h.datetime).getTime() > Date.now() - 3600e3).slice(0, 24);
  const temps = d.daily.flatMap(x => [x.temperature, x.templow]).filter(v => v != null), lo = Math.min(...temps), hi = Math.max(...temps), span = Math.max(1, hi - lo);
  const day = i => i === 0 ? "今天" : i === 1 ? "明天" : "周" + "日一二三四五六"[new Date(now.getTime() + i * 864e5).getDay()];
  const cell = (k, v) => `<div><small>${k}</small><b>${v}</b></div>`;
  body.innerHTML = `
    <div class="kcard wxnow"><div class="wxbig"><span>${wxIcon(n.condition, hr)}</span>${wxN(n.temperature)}°</div>
      <div class="wxcond">${WX_CN[n.condition] ?? n.condition}${d.daily[0] ? ` · ${wxN(d.daily[0].templow)}° / ${wxN(d.daily[0].temperature)}°` : ""}</div></div>
    <div class="kcard"><div class="kh"><h3>未来 24 小时</h3></div>
      <div class="wxhours">${hours.map(h => { const t = new Date(h.datetime), hh = t.getHours(); return `<div><small>${t.getTime() - Date.now() < 3600e3 ? "现在" : hh + "时"}</small><span>${wxIcon(h.condition, hh)}</span><b>${wxN(h.temperature)}°</b><em>${h.precipitation >= 0.1 ? h.precipitation.toFixed(1) : ""}</em></div>`; }).join("")}</div></div>
    <div class="kcard"><div class="kh"><h3>未来 ${d.daily.length} 天</h3></div>
      <div class="wxdays">${d.daily.map((x, i) => `<div class="wxday"><span class="d">${day(i)}<small>${x.precipitation >= 0.1 ? x.precipitation.toFixed(1) + "mm" : ""}</small></span><span class="i">${wxIcon(x.condition, 12)}</span><span class="lo">${wxN(x.templow)}°</span><span class="wxr"><i style="left:${Math.min(88, (x.templow - lo) / span * 100).toFixed(0)}%;width:${Math.min(100 - Math.min(88, (x.templow - lo) / span * 100), Math.max(12, (x.temperature - x.templow) / span * 100)).toFixed(0)}%"></i></span><span class="hi">${wxN(x.temperature)}°</span></div>`).join("")}</div></div>
    <div class="kcard"><div class="wxgrid">${cell("湿度", wxN(n.humidity) + "%")}${cell("风", `${wxDir(n.wind_bearing)} ${wxLevel(n.wind_speed)} 级`)}${cell("气压", wxN(n.pressure) + " hPa")}${cell("紫外线", n.uv_index != null ? n.uv_index.toFixed(1) : "–")}${cell("云量", wxN(n.cloud_coverage) + "%")}${cell("露点", wxN(n.dew_point) + "°")}</div>
      <p class="knote">数据来自挪威气象局（Met.no）对你家位置的预报，是模型预报，不是实测。${d.at ? "更新于 " + new Date(d.at * 1000).toTimeString().slice(0, 5) + "。" : ""}</p></div>`;
}

document.addEventListener("click", e => {
  const t = e.target.closest("[data-wx]");
  if (!t) return;
  e.stopPropagation();
  if (t.dataset.wx === "close") closeWeather();
}, true);
