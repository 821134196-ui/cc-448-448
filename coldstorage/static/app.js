/* 共享冷库 SPA — 原生 JS，无依赖。 */
"use strict";

// ---------------- 基础 ----------------
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const state = { me: null, view: "map", map: null, detailCell: null };

async function api(method, url, body) {
  const opt = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(url, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmt(isoStr) {
  if (!isoStr) return "—";
  const d = new Date(isoStr.endsWith("Z") || isoStr.includes("+") ? isoStr : isoStr + "Z");
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fmtShort(isoStr) {
  if (!isoStr) return "—";
  const d = new Date(isoStr.endsWith("Z") || isoStr.includes("+") ? isoStr : isoStr + "Z");
  const p = n => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function localInputToIso(v) {
  if (!v) return null;
  const d = new Date(v);
  return d.toISOString().slice(0, 19);
}
function toLocalInput(isoStr) {
  if (!isoStr) return "";
  const d = new Date(isoStr + "Z");
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}
function money(v) {
  const n = Number(v || 0);
  return (n < 0 ? "−" : "") + "¥" + Math.abs(n).toFixed(2);
}
function toast(msg, isErr) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.className = ""), 2600);
}
function modal(title, bodyHtml, footHtml = "") {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-mask">
    <div class="modal">
      <div class="modal-head"><h3>${esc(title)}</h3>
        <button class="btn sm" id="m-close">✕</button></div>
      <div class="modal-body">${bodyHtml}</div>
      ${footHtml ? `<div class="modal-foot">${footHtml}</div>` : ""}
    </div></div>`;
  $("#m-close").onclick = closeModal;
  $(".modal-mask").addEventListener("click", e => { if (e.target.classList.contains("modal-mask")) closeModal(); });
  return root;
}
function closeModal() { $("#modal-root").innerHTML = ""; }

const STATUS_PILL = {
  pending: ["待确认", "warn"], confirmed: ["已确认·锁格", "info"],
  checked_in: ["在租", "occupied"], checked_out: ["已退库", "plain"],
  settled: ["已结清", "ok"], cancelled: ["已取消", "plain"],
  open: ["在租", "occupied"], closed: ["已关闭", "plain"],
  requested: ["待审批", "warn"], approved: ["已批准", "ok"],
  rejected: ["已驳回", "bad"], done: ["已执行", "ok"],
  issued: ["待财务复核", "warn"], reviewed: ["已复核", "ok"], paid: ["已支付", "ok"],
};
function pill(code) {
  const [txt, cls] = STATUS_PILL[code] || [code, "plain"];
  return `<span class="pill ${cls}">${esc(txt)}</span>`;
}
const KIND_LABEL = {
  overcapacity: "超容", temp_pull: "临时降温", excursion: "温度越界",
  outage: "断电", door_open: "门未关", leak: "货品渗漏", early_exit: "提前退租",
};

// ---------------- 登录 ----------------
$("#login-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    state.me = await api("POST", "/api/login", { user: f.get("user"), password: f.get("password") });
    enterApp();
  } catch (err) { toast(err.message, true); }
});
$("#logout-btn").onclick = async () => { await api("POST", "/api/logout"); location.reload(); };

function enterApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  $("#who-name").textContent = `${state.me.name}（${{ admin: "管理员", finance: "财务", tenant: "租户" }[state.me.role]}）`;
  buildNav();
  go("map");
}

const NAV = [
  ["map", "库区格位图", ["admin", "finance", "tenant"]],
  ["calendar", "预约日历", ["admin", "finance", "tenant"]],
  ["reservations", "预约与入库复核", ["admin", "tenant"]],
  ["leases", "租期与移格", ["admin", "tenant"]],
  ["incidents", "异常补偿", ["admin", "finance", "tenant"]],
  ["bills", "账单明细", ["admin", "finance", "tenant"]],
];
function buildNav() {
  $("#nav").innerHTML = NAV.filter(([, , roles]) => roles.includes(state.me.role))
    .map(([id, label]) => `<button data-v="${id}">${label}</button>`).join("");
  $$("#nav button").forEach(b => b.onclick = () => go(b.dataset.v));
}
async function go(view) {
  state.view = view;
  $$("#nav button").forEach(b => b.classList.toggle("active", b.dataset.v === view));
  const fn = {
    map: renderMap, calendar: renderCalendar, reservations: renderReservations,
    leases: renderLeases, incidents: renderIncidents, bills: renderBills,
  }[view];
  try { await fn(); } catch (e) { $("#view").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

// ---------------- 库区格位图 ----------------
async function renderMap() {
  state.map = await api("GET", "/api/map");
  const zonesHtml = state.map.map(z => {
    const pct = z.total_m3 ? Math.round(z.occupied_m3 / z.total_m3 * 100) : 0;
    const cells = z.cells.map(c => `
      <div class="cell ${c.status}" data-cell="${esc(c.id)}">
        <div class="cid">${esc(c.id)}</div>
        <div>${c.status === "free"
          ? `<span class="pill free">空格</span>`
          : c.status === "reserved"
            ? `<span class="pill reserved">已预约</span><div class="who">${esc(c.who || "")}</div>`
            : `<span class="pill occupied">在租</span><div class="who">${esc(c.who || "")}</div>`}
          ${c.band_override ? `<div class="small kind-temp_pull">⚑ 手工温区 ${c.band_override[0]}~${c.band_override[1]}℃</div>` : ""}
        </div>
        <div class="vol">${c.status === "free" ? "剩 " : ""}${c.remaining_m3.toFixed(1)} m³
          <div class="muted small">总 ${c.volume_m3.toFixed(1)}</div></div>
      </div>`).join("");
    return `<div class="zone-col">
      <div class="zone-head">
        <div class="zname">${esc(z.name)} <span class="small">${esc(z.code)}</span></div>
        <div class="zmeta">温区 ${z.temp_min}~${z.temp_max}℃ · 租金 ${z.rate} 元/m³/h</div>
        <div class="zone-remain"><b>${z.remaining_m3.toFixed(1)}</b><span>m³ 剩余 / 共 ${z.total_m3.toFixed(1)} m³ · ${pct}% 占用</span></div>
        <div class="zone-bar"><i style="width:${pct}%"></i></div>
      </div>
      <div class="cells">${cells}</div>
    </div>`;
  }).join("");
  $("#view").innerHTML = `
    <div class="page-head"><h2>库区格位图</h2>
      <span class="desc">按库区纵向分组；点击任意货格查看温度曲线与占用明细。所有数据为虚构。</span></div>
    <div class="zones">${zonesHtml}</div>`;
  $$(".cell").forEach(el => el.onclick = () =>
    openCellModal(el.dataset.cell).catch(e => toast(e.message, true)));
}

async function ensureMap() {
  if (!state.map) state.map = await api("GET", "/api/map");
  return state.map;
}

async function openCellModal(cid) {
  await ensureMap();
  const data = await api("GET", `/api/cells/${encodeURIComponent(cid)}/samples?from=${encodeURIComponent(localInputToIso(toLocalInput(new Date(Date.now() - 30 * 3600e3).toISOString().slice(0, 16))))}&to=${encodeURIComponent(new Date().toISOString().slice(0, 19))}`);
  const z = state.map.flatMap(z => z.cells).find(c => c.id === cid);
  const body = `
    <div id="chart-slot"></div>
    <div class="card" style="box-shadow:none;margin-top:12px">
      <h3>货格信息</h3>
      <dl class="kv">
        <dt>状态</dt><dd>${pill(z.status)} ${esc(z.who || "")}</dd>
        <dt>容积</dt><dd>${z.volume_m3} m³（剩余 ${z.remaining_m3} m³）</dd>
        <dt>温区带</dt><dd>${data.band.min}~${data.band.max}℃，目标 ${data.band.target}℃
          ${data.overrides.length ? `<br><span class="kind-temp_pull">⚑ 有手工改温记录 ${data.overrides.length} 次（最新：${esc(data.overrides[data.overrides.length - 1].reason)}）</span>` : ""}</dd>
        <dt>异常</dt><dd>${data.incidents.length ? data.incidents.map(i =>
          `<span class="pill bad">${esc(KIND_LABEL[i.kind])}</span> `).join("") : '<span class="muted">区间内无</span>'}</dd>
      </dl>
    </div>`;
  modal(`货格 ${cid} · 温度曲线`, body);
  renderTempChart($("#chart-slot"), data);
}

// ---------------- 温度曲线（清晰阈值带） ----------------
function renderTempChart(slot, data) {
  const W = 920, H = 340, M = { t: 24, r: 56, b: 38, l: 52 };
  const iw = W - M.l - M.r, ih = H - M.t - M.b;
  const samples = data.samples;
  slot.innerHTML = `<div class="chart-wrap">
    <div class="chart-legend">
      <span><i class="sw" style="background:#13726f"></i>温度读数</span>
      <span><i class="sw" style="background:rgba(27,138,134,.16);border:1px solid #1b8a86"></i>允许阈值带 ${data.band.min}~${data.band.max}℃</span>
      <span><i class="sw" style="background:#d03b3b;border-radius:50%"></i>越界点（形状+颜色标记）</span>
      <span><i class="sw" style="background:repeating-linear-gradient(45deg,#898781 0 3px,#eef1f2 3px 6px)"></i>断电（无读数）</span>
      <span><i class="sw" style="background:rgba(201,133,0,.25);border:1px solid #c98500"></i>门开启时段</span>
      <span><i class="sw" style="background:#5a6079;height:2px"></i>手工改温时刻</span>
    </div>
    <svg class="temp" viewBox="0 0 ${W} ${H}" id="tsvg"></svg>
    <div class="tip" id="ttip"></div>
    <div style="margin-top:8px"><button class="btn sm" id="tbl-toggle">查看数据表</button></div>
    <div id="tbl-slot" class="hidden" style="margin-top:10px;max-height:220px;overflow:auto"></div>
  </div>`;
  const svg = $("#tsvg");
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs = {}, txt) => {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (txt != null) n.textContent = txt;
    svg.appendChild(n);
    return n;
  };

  if (!samples.length) {
    el("text", { x: W / 2, y: H / 2, fill: "#6b7186", "font-size": 13, "text-anchor": "middle" }, "该时间窗内暂无采样读数");
    return;
  }
  const t0 = new Date(samples[0].ts + "Z").getTime();
  const t1 = new Date(samples[samples.length - 1].ts + "Z").getTime();
  const X = ts => M.l + (new Date(ts + "Z").getTime() - t0) / Math.max(1, t1 - t0) * iw;
  let lo = data.band.min, hi = data.band.max;
  samples.forEach(s => { if (s.temp_c != null) { lo = Math.min(lo, s.temp_c); hi = Math.max(hi, s.temp_c); } });
  data.overrides.forEach(o => { lo = Math.min(lo, o.new_min); hi = Math.max(hi, o.new_max); });
  const pad = Math.max(1.5, (hi - lo) * 0.12);
  const yMin = lo - pad, yMax = hi + pad;
  const Y = v => M.t + (yMax - v) / (yMax - yMin) * ih;

  // 网格与 y 轴
  const ticks = 6;
  for (let i = 0; i <= ticks; i++) {
    const v = yMin + (yMax - yMin) * i / ticks;
    const y = Y(v);
    el("line", { x1: M.l, x2: W - M.r, y1: y, y2: y, stroke: "#e6edee", "stroke-width": 1 });
    const t = el("text", { x: M.l - 7, y: y + 4, "text-anchor": "end", "font-size": 10.5, fill: "#6b7186" }, v.toFixed(1));
  }
  // x 轴时间刻度（约 6 个）
  for (let i = 0; i <= 6; i++) {
    const tt = t0 + (t1 - t0) * i / 6;
    const x = M.l + i / 6 * iw;
    el("line", { x1: x, x2: x, y1: H - M.b, y2: H - M.b + 4, stroke: "#9aa2b1" });
    el("text", { x, y: H - M.b + 17, "text-anchor": "middle", "font-size": 10.5, fill: "#6b7186" },
      fmtShort(new Date(tt).toISOString().slice(0, 19)));
  }

  // 门开启竖带（先画在最底层）
  data.doors.forEach(d => {
    if (!d.to) return;
    const xa = X(d.from), xb = Math.max(xa + 2, X(d.to));
    if (xb < M.l || xa > W - M.r) return;
    el("rect", { x: Math.max(M.l, xa), y: M.t, width: Math.min(W - M.r, xb) - Math.max(M.l, xa), height: ih, fill: "rgba(201,133,0,.13)" });
  });

  // 阈值带（默认带；改温时刻之后改画覆盖带）
  const bandAt = ts => {
    let band = [data.band.min, data.band.max];
    data.overrides.forEach(o => { if (o.created_at <= ts) band = [o.new_min, o.new_max]; });
    return band;
  };
  // 按覆盖时刻分段绘制带
  const cuts = [samples[0].ts, ...data.overrides.map(o => o.created_at).filter(c => c > samples[0].ts && c < samples[samples.length - 1].ts), samples[samples.length - 1].ts];
  for (let i = 0; i < cuts.length - 1; i++) {
    const [bLo, bHi] = bandAt(cuts[i]);
    const xa = X(cuts[i]), xb = X(cuts[i + 1]);
    el("rect", { x: xa, y: Y(bHi), width: Math.max(1, xb - xa), height: Math.max(1, Y(bLo) - Y(bHi)), fill: "rgba(27,138,134,.13)" });
  }
  [["max", bandAt(samples[samples.length - 1].ts)[1]], ["min", bandAt(samples[samples.length - 1].ts)[0]]].forEach(([k, v]) => {
    el("line", { x1: M.l, x2: W - M.r, y1: Y(v), y2: Y(v), stroke: "#1b8a86", "stroke-width": 1.3, "stroke-dasharray": "6 4" });
    el("text", { x: W - M.r + 5, y: Y(v) + 3.5, "font-size": 10.5, fill: "#13726f", "font-weight": 600 },
      (k === "max" ? "上限 " : "下限 ") + v + "℃");
  });
  const targetY = Y(data.band.target);
  el("line", { x1: M.l, x2: W - M.r, y1: targetY, y2: targetY, stroke: "#5a6079", "stroke-width": 1, "stroke-dasharray": "2 4", opacity: .7 });
  el("text", { x: W - M.r + 5, y: targetY + 3.5, "font-size": 10, fill: "#5a6079" }, "目标 " + data.band.target + "℃");

  // 断电窗（斜纹遮罩）
  let outStart = null;
  const outRanges = [];
  samples.forEach((s, i) => {
    if (!s.power_on && outStart === null) outStart = s.ts;
    if ((s.power_on || i === samples.length - 1) && outStart !== null) {
      outRanges.push([outStart, s.power_on ? s.ts : s.ts]);
      outStart = null;
    }
  });
  outRanges.forEach(([a, b]) => {
    const xa = X(a), xb = Math.max(xa + 4, X(b));
    const g = el("pattern", { id: "hatch", patternUnits: "userSpaceOnUse", width: 7, height: 7, patternTransform: "rotate(45)" });
    el_inside(g, "rect", { width: 7, height: 7, fill: "#fff" });
    el_inside(g, "line", { x1: 0, y1: 0, x2: 0, y2: 7, stroke: "#898781", "stroke-width": 2.5 });
    el("rect", { x: xa, y: M.t, width: xb - xa, height: ih, fill: "url(#hatch)", opacity: .55 });
    if (xb - xa > 40) el("text", { x: (xa + xb) / 2, y: M.t + 15, "text-anchor": "middle", "font-size": 10.5, fill: "#5a6079", "font-weight": 600 }, "断电");
  });

  // 手工改温时刻竖线
  data.overrides.forEach(o => {
    if (o.created_at < samples[0].ts || o.created_at > samples[samples.length - 1].ts) return;
    const x = X(o.created_at);
    el("line", { x1: x, x2: x, y1: M.t, y2: H - M.b, stroke: "#5a6079", "stroke-width": 1.2, "stroke-dasharray": "3 3" });
    el("text", { x, y: M.t - 8, "text-anchor": "middle", "font-size": 10, fill: "#5a6079" }, "改温");
  });

  // 温度折线（断电处断开）
  let dPath = "", pen = false;
  samples.forEach(s => {
    if (s.power_on && s.temp_c != null) {
      dPath += `${pen ? "L" : "M"}${X(s.ts).toFixed(1)},${Y(s.temp_c).toFixed(1)}`;
      pen = true;
    } else pen = false;
  });
  el("path", { d: dPath, fill: "none", stroke: "#13726f", "stroke-width": 2, "stroke-linejoin": "round" });

  // 越界点：红圆+白环（形状+颜色双编码）
  samples.forEach(s => {
    if (!s.power_on || s.temp_c == null) return;
    const [bLo, bHi] = bandAt(s.ts);
    if (s.temp_c < bLo || s.temp_c > bHi) {
      el("circle", { cx: X(s.ts), cy: Y(s.temp_c), r: 4.6, fill: "#d03b3b", stroke: "#fff", "stroke-width": 1.6 });
    }
  });

  // 十字线 + tooltip
  const crossV = el("line", { y1: M.t, y2: H - M.b, stroke: "#2d314d", "stroke-width": 1, opacity: 0 });
  const crossD = el("circle", { r: 4.5, fill: "#2d314d", stroke: "#fff", "stroke-width": 1.5, opacity: 0 });
  const tip = $("#ttip");
  el("rect", { x: M.l, y: M.t, width: iw, height: ih, fill: "transparent" })
    .addEventListener("mousemove", ev => {
      const rect = svg.getBoundingClientRect();
      const mx = (ev.clientX - rect.left) / rect.width * W;
      if (mx < M.l || mx > W - M.r) { tip.style.opacity = 0; crossV.setAttribute("opacity", 0); crossD.setAttribute("opacity", 0); return; }
      const tm = t0 + (mx - M.l) / iw * (t1 - t0);
      let best = samples[0], bd = Infinity;
      samples.forEach(s => {
        const dd = Math.abs(new Date(s.ts + "Z").getTime() - tm);
        if (dd < bd) { bd = dd; best = s; }
      });
      const x = X(best.ts);
      crossV.setAttribute("x1", x); crossV.setAttribute("x2", x); crossV.setAttribute("opacity", .5);
      const [bLo, bHi] = bandAt(best.ts);
      if (best.power_on && best.temp_c != null) {
        crossD.setAttribute("cx", x); crossD.setAttribute("cy", Y(best.temp_c)); crossD.setAttribute("opacity", 1);
      } else crossD.setAttribute("opacity", 0);
      const oob = best.temp_c != null && (best.temp_c < bLo || best.temp_c > bHi);
      tip.innerHTML = `<b>${esc(fmt(best.ts))}</b><br>` +
        (best.power_on && best.temp_c != null
          ? `温度 ${best.temp_c.toFixed(2)}℃ ${oob ? '<span style="color:#ff9d9d">· 越界</span>' : ""}<br><span class="muted">生效带 ${bLo}~${bHi}℃</span>`
          : `<span style="color:#c7c7d6">断电 · 无读数</span>`);
      tip.style.opacity = 1;
      let left = ev.clientX + 14, topEv = ev.clientY + 12;
      if (left + 230 > innerWidth) left = ev.clientX - 224;
      tip.style.left = left + "px"; tip.style.top = topEv + "px";
    });
  svg.addEventListener("mouseleave", () => {
    tip.style.opacity = 0; crossV.setAttribute("opacity", 0); crossD.setAttribute("opacity", 0);
  });

  function el_inside(parent, tag, attrs) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    parent.appendChild(n);
    return n;
  }

  // 数据表（可访问性退路）
  $("#tbl-toggle").onclick = () => {
    const t = $("#tbl-slot");
    t.classList.toggle("hidden");
    if (!t.dataset.built) {
      t.innerHTML = `<table><thead><tr><th>时间</th><th>温度℃</th><th>状态</th></tr></thead><tbody>${
        samples.map(s => `<tr><td>${esc(fmt(s.ts))}</td><td class="num">${s.temp_c == null ? "—" : s.temp_c.toFixed(2)}</td>
          <td>${s.power_on ? "正常" : '<span class="pill bad">断电</span>'}</td></tr>`).join("")}</tbody></table>`;
      t.dataset.built = "1";
    }
  };
}

// ---------------- 预约日历 ----------------
async function renderCalendar() {
  const from = new Date(Date.now() - 2 * 864e5).toISOString().slice(0, 19);
  const to = new Date(Date.now() + 10 * 864e5).toISOString().slice(0, 19);
  const rows = await api("GET", `/api/calendar?from=${from}&to=${to}`);
  const days = {};
  rows.forEach(r => {
    const k = r.start_ts.slice(0, 10);
    (days[k] = days[k] || []).push(r);
  });
  const keys = Object.keys(days).sort();
  $("#view").innerHTML = `
    <div class="page-head"><h2>预约日历</h2><span class="desc">已确认即锁定货格容量与温区；共 ${rows.length} 笔进行中/未来预约</span></div>
    <div class="card"><div class="cal-wrap">${keys.length ? keys.map(k => `
      <div class="cal-day"><div class="cal-date">${esc(fmt(k + "T00:00:00"))}</div>
        <div class="cal-items">${days[k].map(r => `
          <div class="cal-item">
            <span class="tag">${esc(r.cell_id || (r.status === "confirmed" ? "已占用" : "未锁格"))}</span>
            <span>${esc(r.tenant_name)} · ${esc(config.ZONE_NAMES[r.zone_code] || r.zone_code)}
              ${r.need_volume_m3 == null ? "" : `· ${r.need_volume_m3}m³`} · ${esc(fmtShort(r.start_ts))} → ${esc(fmtShort(r.end_ts))}</span>
            <span>${pill(r.status)}</span>
          </div>`).join("")}</div></div>`).join("") : '<div class="empty">日历区间内无预约</div>'}
    </div></div>`;
}
const config = {
  ZONE_NAMES: { F: "冷冻区", C: "冷藏区", AC: "恒温区" },
  ZONES: [["F", "冷冻区 -25~-18℃"], ["C", "冷藏区 0~4℃"], ["AC", "恒温区 12~18℃"]],
};

// ---------------- 预约与入库复核 ----------------
async function renderReservations() {
  const rows = await api("GET", "/api/reservations");
  const isAdmin = state.me.role === "admin";
  $("#view").innerHTML = `
    <div class="page-head"><h2>预约与入库复核</h2>
      <div>${isAdmin ? `<button class="btn primary" id="new-res">+ 新建预约</button>` : `<button class="btn primary" id="new-res">+ 我的预约</button>`}</div></div>
    <div class="card"><table><thead><tr>
      <th>#</th><th>租户</th><th>温区</th><th>货格</th><th>需求容量</th>
      <th>租用时段</th><th>状态</th><th>操作</th></tr></thead><tbody>
      ${rows.map(r => `<tr>
        <td class="num">${r.id}</td><td>${esc(r.tenant_name)}</td>
        <td>${esc(config.ZONE_NAMES[r.zone_code])}<div class="small muted">${r.target_temp_min}~${r.target_temp_max}℃</div></td>
        <td>${esc(r.cell_id || "—")}</td><td class="num">${r.need_volume_m3} m³</td>
        <td class="small">${esc(fmt(r.start_ts))}<br>→ ${esc(fmt(r.end_ts))}</td>
        <td>${pill(r.status)}</td>
        <td><div class="row-actions">
          <button class="btn sm" data-act="detail" data-id="${r.id}">详情/复核</button>
          ${r.status === "pending" && isAdmin ? `<button class="btn sm primary" data-act="confirm" data-id="${r.id}">确认锁格</button>` : ""}
          ${["pending", "confirmed"].includes(r.status) ? `<button class="btn sm danger" data-act="cancel" data-id="${r.id}">取消</button>` : ""}
        </div></td></tr>`).join("") || '<tr><td colspan="8" class="empty">暂无预约</td></tr>'}
    </tbody></table></div>`;
  $("#new-res").onclick = openReservationForm;
  $$("[data-act]").forEach(b => b.onclick = () => resAction(b.dataset.act, Number(b.dataset.id)));
}

async function resAction(act, id) {
  if (act === "detail") return openReservationDetail(id);
  if (act === "confirm") {
    try { const r = await api("POST", `/api/reservations/${id}/confirm`);
      toast(r.idempotent ? "该预约此前已确认" : `已锁定货格 ${r.cell_id}`); renderReservations();
    } catch (e) { toast(e.message, true); }
  }
  if (act === "cancel") {
    if (!confirm("确认取消该预约？")) return;
    try { await api("POST", `/api/reservations/${id}/cancel`); toast("已取消"); renderReservations(); }
    catch (e) { toast(e.message, true); }
  }
}

function openReservationForm() {
  const isAdmin = state.me.role === "admin";
  const start = toLocalInput(new Date(Date.now() + 3600e3).toISOString());
  const end = toLocalInput(new Date(Date.now() + 25 * 3600e3).toISOString());
  const body = `<div class="form-grid">
    ${isAdmin ? `<label class="wide">租户<select name="tenant_id">
      ${["t_lvyuan|绿源生鲜", "t_beiji|北极渔业", "t_tianshan|天山乳品"].map(x => {
        const [v, n] = x.split("|"); return `<option value="${v}">${n}</option>`; }).join("")}
    </select></label>` : ""}
    <label>温区<select name="zone_code">${config.ZONES.map(([v, n]) => `<option value="${v}">${n}</option>`).join("")}</select></label>
    <label>需求容量 m³<input name="need_volume_m3" type="number" step="0.1" min="0.1" value="5"></label>
    <label>开始时间<input name="start_ts" type="datetime-local" value="${start}"></label>
    <label>结束时间<input name="end_ts" type="datetime-local" value="${end}"></label>
    <label class="wide">备注<input name="note" placeholder="货品与用途（虚构）"></label>
  </div>`;
  const m = modal("新建预约", body, `<button class="btn" id="m-cancel">取消</button><button class="btn primary" id="m-save">提交（待确认）</button>`);
  $("#m-save").onclick = async () => {
    // 直接读模态框内控件
    const get = n => m.querySelector(`[name="${n}"]`).value;
    try {
      const payload = {
        zone_code: get("zone_code"), need_volume_m3: Number(get("need_volume_m3")),
        start_ts: localInputToIso(get("start_ts")), end_ts: localInputToIso(get("end_ts")),
        note: get("note"),
      };
      if (isAdmin) payload.tenant_id = get("tenant_id");
      const r = await api("POST", "/api/reservations", payload);
      closeModal(); toast(`预约 #${r.id} 已创建，等待管理员确认锁格`); renderReservations();
    } catch (e) { toast(e.message, true); }
  };
  $("#m-cancel").onclick = closeModal;
}

async function openReservationDetail(id) {
  const r = await api("GET", `/api/reservations/${id}`);
  const isAdmin = state.me.role === "admin";
  const body = `
    <dl class="kv">
      <dt>预约 #</dt><dd>${r.id} ${pill(r.status)}</dd>
      <dt>温区</dt><dd>${esc(config.ZONE_NAMES[r.zone_code])}（${r.target_temp_min}~${r.target_temp_max}℃）</dd>
      <dt>锁定货格</dt><dd>${esc(r.cell_id || "确认前未锁定")}</dd>
      <dt>需求容量</dt><dd>${r.need_volume_m3} m³</dd>
      <dt>租用时段</dt><dd>${esc(fmt(r.start_ts))} → ${esc(fmt(r.end_ts))}</dd>
      <dt>备注</dt><dd>${esc(r.note || "—")}</dd>
    </dl>
    <div class="card" style="box-shadow:none;border-color:var(--line)">
      <h3>货品批次与入库复核</h3>
      <table><thead><tr><th>货品</th><th>申报 kg</th><th>实称 kg</th><th>体积 m³</th><th>照片</th></tr></thead>
      <tbody>${r.batches.length ? r.batches.map(b => `<tr>
        <td>${esc(b.name)}<div class="small muted">${esc(b.inbound_note || "")}</div></td>
        <td class="num">${b.declared_weight_kg ?? "—"}</td>
        <td class="num">${b.actual_weight_kg ?? "—"}</td>
        <td class="num">${b.volume_m3 ?? "—"}</td>
        <td>${b.photo_path ? `<img class="thumb" src="/photos/${esc(b.photo_path)}" alt="货品照片">` : "—"}</td></tr>`).join("")
        : '<tr><td colspan="5" class="empty">尚未登记批次</td></tr>'}</tbody></table>
      ${isAdmin && r.status === "confirmed" ? `
      <div style="margin-top:12px"><button class="btn" id="add-batch">+ 登记批次（实称）</button>
        <button class="btn primary" id="pass-in">复核通过并开始计费</button>
        <button class="btn danger" id="fail-in">复核不通过</button></div>` : ""}
    </div>`;
  modal(`预约 #${id} 详情`, body);
  if (isAdmin && r.status === "confirmed") {
    $("#add-batch").onclick = () => openBatchForm(id, () => openReservationDetail(id));
    $("#pass-in").onclick = async () => {
      try { const res = await api("POST", `/api/reservations/${id}/checkin`, { passed: true });
        closeModal();
        toast(`复核通过：${res.cell_id}，实称 ${res.weight_kg}kg${res.overcapacity_m3 ? `，超容 ${res.overcapacity_m3}m³ 已留痕` : ""}，计费开始`);
        renderReservations();
      } catch (e) { toast(e.message, true); }
    };
    $("#fail-in").onclick = async () => {
      const note = prompt("复核不通过原因：", "包装破损，暂缓入库");
      if (!note) return;
      await api("POST", `/api/reservations/${id}/checkin`, { passed: false, reviewer_note: note });
      toast("已记录复核不通过，不放行、不计费"); openReservationDetail(id);
    };
  }
}

function openBatchForm(rid, onDone) {
  const body = `<div class="form-grid">
    <label class="wide">货品名称<input name="name" value="虚构货品"></label>
    <label>申报重量 kg<input name="declared" type="number" step="0.1" value="900"></label>
    <label>实称重量 kg<input name="actual" type="number" step="0.1" value="940"></label>
    <label>实测体积 m³<input name="volume" type="number" step="0.01" value="2.6"></label>
    <label class="wide">货品照片（本地文件，可选）<input name="photo" type="file" accept="image/*"></label>
    <label class="wide">备注<input name="note" value="入库复核已称重"></label>
  </div>`;
  const m = modal(`预约 #${rid} · 登记批次`, body,
    `<button class="btn" id="bc">取消</button><button class="btn primary" id="bs">保存</button>`);
  $("#bc").onclick = closeModal;
  $("#bs").onclick = async () => {
    const get = n => m.querySelector(`[name="${n}"]`).value;
    const file = m.querySelector('[name="photo"]').files[0];
    const payload = {
      name: get("name"), declared_weight_kg: Number(get("declared")),
      actual_weight_kg: Number(get("actual")), volume_m3: Number(get("volume")),
      inbound_note: get("note"),
    };
    if (file) {
      const b64 = await new Promise(res => {
        const fr = new FileReader();
        fr.onload = () => res(fr.result.split(",")[1]);
        fr.readAsDataURL(file);
      });
      payload.photo_base64 = b64;
      payload.photo_type = file.type || "image/png";
    }
    try { await api("POST", `/api/reservations/${rid}/batches`, payload);
      closeModal(); toast("批次已登记"); onDone();
    } catch (e) { toast(e.message, true); }
  };
}

// ---------------- 租期与移格 ----------------
async function renderLeases() {
  const rows = await api("GET", "/api/leases");
  const isAdmin = state.me.role === "admin";
  $("#view").innerHTML = `
    <div class="page-head"><h2>租期 · 移格 · 退库</h2>
      <span class="desc">计费自入库复核通过起；同温区空格可直接移位，跨温区需管理员重新确认。</span></div>
    <div class="card"><table><thead><tr>
      <th>#</th><th>租户</th><th>当前货格</th><th>重量 kg</th><th>起算</th>
      <th>结束</th><th>状态</th><th>操作</th></tr></thead><tbody>
      ${rows.map(l => `<tr>
        <td class="num">${l.id}</td><td>${esc(l.tenant_name)}</td>
        <td>${esc(l.cell_id)}<div class="small muted">${esc(config.ZONE_NAMES[l.zone_code])}</div></td>
        <td class="num">${l.weight_kg}</td>
        <td class="small">${esc(fmt(l.started_at))}</td>
        <td class="small">${esc(fmt(l.ended_at))}</td>
        <td>${pill(l.status)}</td>
        <td><div class="row-actions">
          <button class="btn sm" data-act="temp" data-id="${l.id}">温度曲线</button>
          ${l.status === "open" ? `<button class="btn sm" data-act="move" data-id="${l.id}">申请移格</button>` : ""}
          ${l.status === "open" && isAdmin ? `<button class="btn sm danger" data-act="out" data-id="${l.id}">退库清点</button>` : ""}
          ${l.status === "open" && isAdmin ? `<button class="btn sm" data-act="more" data-id="${l.id}">登记异常</button>` : ""}
        </div></td></tr>`).join("") || '<tr><td colspan="8" class="empty">暂无租期</td></tr>'}
    </tbody></table></div>
    <div class="card"><h3>移格申请</h3><table><thead><tr>
      <th>#</th><th>租期</th><th>从</th><th>到</th><th>类型</th><th>原因</th><th>状态</th><th>操作</th></tr></thead><tbody>
      ${rows.flatMap(l => l.moves).map(mv => `<tr>
        <td class="num">${mv.id}</td><td class="num">#${mv.lease_id}</td>
        <td>${esc(mv.from_cell)}</td><td>${esc(mv.to_cell)}</td>
        <td>${mv.same_zone ? '<span class="pill info">同温区</span>' : '<span class="pill warn">跨温区·重确认</span>'}</td>
        <td class="small">${esc(mv.reason)}</td><td>${pill(mv.status)}</td>
        <td>${mv.status === "requested" && !mv.same_zone && isAdmin
          ? `<button class="btn sm primary" data-mv="${mv.id}" data-ap="1">批准并重新确认温区</button>
             <button class="btn sm danger" data-mv="${mv.id}" data-ap="0">驳回</button>` : "—"}</td>
      </tr>`).join("") || '<tr><td colspan="8" class="empty">暂无移格记录</td></tr>'}
    </tbody></table></div>`;
  $$("[data-act]").forEach(b => b.onclick = () => leaseAction(b.dataset.act, Number(b.dataset.id), rows));
  $$("[data-mv]").forEach(b => b.onclick = async () => {
    try { await api("POST", `/api/moves/${b.dataset.mv}/decide`, { approve: b.dataset.ap === "1" });
      toast(b.dataset.ap === "1" ? "已批准并执行，温区重新确认" : "已驳回"); renderLeases();
    } catch (e) { toast(e.message, true); }
  });
}

async function leaseAction(act, id, rows) {
  const lease = rows.find(l => l.id === id);
  if (act === "temp") return openCellModal(lease.cell_id);
  if (act === "move") return openMoveForm(lease);
  if (act === "out") {
    if (!confirm("退库清点结束？将关闭租期并生成不可重开的费用快照。")) return;
    try { const r = await api("POST", `/api/leases/${id}/checkout`, {});
      toast(r.idempotent ? `该租期已结账，账单 #${r.bill_id} 不可重开` : `已生成账单 #${r.bill_id}，合计 ${money(r.total)}`);
      go("bills");
    } catch (e) { toast(e.message, true); }
  }
  if (act === "more") openMoreIncidentForm(id);
}

async function openMoveForm(lease) {
  await ensureMap();
  const free = state.map.flatMap(z => z.cells.filter(c => c.status === "free").map(c => ({ ...c, zone: z.code, zoneName: z.name })));
  const body = `<dl class="kv"><dt>当前货格</dt><dd>${esc(lease.cell_id)}（${esc(config.ZONE_NAMES[lease.zone_code])}）</dd></dl>
    <div class="form-grid">
      <label class="wide">目标空格<select name="to">
        ${free.map(c => `<option value="${c.id}">${c.id} · ${c.zoneName} · ${c.volume_m3}m³ ${c.zone === lease.zone_code ? "（同温区，立即执行）" : "（跨温区，需管理员重新确认）"}</option>`).join("")}
      </select></label>
      <label class="wide">移位原因<input name="reason" value="风机检修，需要平移"></label>
    </div>`;
  const m = modal(`租期 #${lease.id} · 移格`, body,
    `<button class="btn" id="mc">取消</button><button class="btn primary" id="ms">提交</button>`);
  $("#mc").onclick = closeModal;
  $("#ms").onclick = async () => {
    try {
      const r = await api("POST", `/api/leases/${lease.id}/move`, {
        to_cell: m.querySelector('[name="to"]').value,
        reason: m.querySelector('[name="reason"]').value,
      });
      closeModal();
      toast(r.same_zone ? (r.executed ? "同温区空格，已直接移位" : "已申请") : "跨温区移格已提交，等待管理员重新确认");
      renderLeases();
    } catch (e) { toast(e.message, true); }
  };
}

function openMoreIncidentForm(id) {
  const body = `<div class="form-grid">
    <label>类型<select name="kind">
      <option value="pull">临时降温（按小时加收）</option>
      <option value="leak">货品渗漏（清理费）</option>
      <option value="door">登记一次门未关</option>
      <option value="override">手工修改温区（需填原因）</option>
    </select></label>
    <label>数值<input name="num" type="number" step="0.1" value="3"></label>
    <label class="wide">说明 / 改温格式<input name="note" value="临时降温 3 小时"></label>
    ${""}
  </div>
  <div id="ov-fields" class="hidden form-grid" style="margin-top:10px">
    <label>新下限 ℃<input name="nmin" type="number" step="0.1" value="-22"></label>
    <label>新上限 ℃<input name="nmax" type="number" step="0.1" value="-16"></label>
    <label class="wide">改温原因（必填）<input name="reason" placeholder="例如：租户工艺要求，管理员现场确认"></label>
  </div>`;
  const m = modal(`租期 #${id} · 登记操作`, body,
    `<button class="btn" id="ic">取消</button><button class="btn primary" id="is">提交</button>`);
  const kindSel = m.querySelector('[name="kind"]');
  kindSel.onchange = () => $("#ov-fields").classList.toggle("hidden", kindSel.value !== "override");
  $("#ic").onclick = closeModal;
  $("#is").onclick = async () => {
    const kind = kindSel.value, num = Number(m.querySelector('[name="num"]').value);
    const note = m.querySelector('[name="note"]').value;
    try {
      if (kind === "pull") { await api("POST", `/api/leases/${id}/temp-pull`, { hours: num, note }); toast("临时降温已留痕"); }
      if (kind === "leak") { await api("POST", `/api/leases/${id}/leak`, { detail: note }); toast("渗漏清理费已留痕"); }
      if (kind === "door") {
        const lease = (await api("GET", `/api/leases/${id}`));
        await api("POST", `/api/cells/${lease.cell_id}/door`, { minutes: num, note });
        toast("门未关已登记并自动计费");
      }
      if (kind === "override") {
        const lease = await api("GET", `/api/leases/${id}`);
        await api("POST", "/api/admin/override", {
          cell_id: lease.cell_id, new_min: Number(m.querySelector('[name="nmin"]').value),
          new_max: Number(m.querySelector('[name="nmax"]').value),
          reason: m.querySelector('[name="reason"]').value || note,
        });
        toast("手工改温已记录原因");
      }
      closeModal(); renderLeases();
    } catch (e) { toast(e.message, true); }
  };
}

// ---------------- 异常留痕 ----------------
async function renderIncidents() {
  const rows = await api("GET", "/api/incidents");
  $("#view").innerHTML = `
    <div class="page-head"><h2>异常与操作留痕</h2>
      <span class="desc">超容 · 临时降温 · 越界 · 断电 · 门未关 · 渗漏 · 提前退租；负数为给租户的补偿，需财务复核。</span></div>
    <div class="card"><table><thead><tr>
      <th>时间</th><th>货格</th>${state.me.role !== "tenant" ? "<th>租户</th>" : ""}
      <th>类型</th><th>说明</th><th>时长/区间</th><th>金额</th><th>复核</th></tr></thead><tbody>
      ${rows.map(i => `<tr>
        <td class="small">${esc(fmt(i.ts))}</td><td>${esc(i.cell_id || "")}</td>
        ${state.me.role !== "tenant" ? `<td>${esc(i.tenant_name || "")}</td>` : ""}
        <td class="tag-kind kind-${i.kind}">${esc(KIND_LABEL[i.kind])}</td>
        <td class="small">${esc(i.detail)}</td>
        <td class="small">${i.start_ts ? `${esc(fmtShort(i.start_ts))}${i.end_ts ? " → " + esc(fmtShort(i.end_ts)) : ""}` : "—"}</td>
        <td class="num" style="color:${i.amount < 0 ? "var(--teal-700)" : i.amount > 0 ? "var(--serious)" : "inherit"};font-weight:700">${money(i.amount)}</td>
        <td>${i.amount < 0 ? (i.reviewed ? '<span class="pill ok">已复核</span>' : '<span class="pill warn">待财务复核</span>') : "—"}</td>
      </tr>`).join("") || '<tr><td colspan="8" class="empty">暂无异常记录</td></tr>'}
    </tbody></table></div>`;
}

// ---------------- 账单 ----------------
async function renderBills() {
  const rows = await api("GET", "/api/bills");
  const role = state.me.role;
  $("#view").innerHTML = `
    <div class="page-head"><h2>账单明细</h2>
      <span class="desc">退库清点生成费用快照；计费区间按时间轴拆开，异常减免紧邻对应区间；已结账租期不可重新打开。</span></div>
    <div class="card"><table><thead><tr>
      <th>#</th>${role !== "tenant" ? "<th>租户</th>" : ""}<th>租期</th>
      <th>计费区间</th><th>基础租金</th><th>加收</th><th>减免</th><th>合计</th><th>状态</th><th></th></tr></thead><tbody>
      ${rows.map(b => `<tr>
        <td class="num">${b.id}</td>${role !== "tenant" ? `<td>${esc(b.tenant_name)}</td>` : ""}
        <td class="num">#${b.lease_id}</td>
        <td class="small">${esc(fmtShort(b.period_from))}<br>→ ${esc(fmtShort(b.period_to))}</td>
        <td class="num">${money(b.subtotal)}</td>
        <td class="num" style="color:var(--serious)">${money(b.surcharge_total)}</td>
        <td class="num" style="color:var(--teal-700)">−${money(b.discount_total).replace("−", "")}</td>
        <td class="num" style="font-weight:700">${money(b.total)}</td>
        <td>${pill(b.status)}</td>
        <td><button class="btn sm" data-bill="${b.id}">明细</button></td>
      </tr>`).join("") || '<tr><td colspan="9" class="empty">暂无账单</td></tr>'}
    </tbody></table></div>
    <div id="bill-slot"></div>`;
  $$("[data-bill]").forEach(b => b.onclick = () => openBill(Number(b.dataset.bill)));
}

async function openBill(id) {
  const b = await api("GET", `/api/bills/${id}`);
  const d = b.detail;
  const isFinance = state.me.role === "finance";
  const isAdmin = state.me.role === "admin";
  const segs = d.segments.map(s => {
    const lines = [];
    lines.push(`<div class="line-item"><span class="li-label">基础租金</span>
      <span class="li-detail">${esc(s.zone_name)} · ${s.rate} 元/m³/h × ${s.volume_m3}m³ × ${s.hours.toFixed(2)}h</span>
      <span class="li-amount">${money(s.base_rent)}</span></div>`);
    if (s.weight_surcharge > 0) lines.push(`<div class="line-item add"><span class="li-label">重量附加</span>
      <span class="li-detail">实称 ${s.weight_kg}kg 超过标准密度部分（300kg/m³×${s.volume_m3}m³），0.35 元/吨·时 × ${s.hours.toFixed(2)}h</span>
      <span class="li-amount">${money(s.weight_surcharge)}</span></div>`);
    s.items.forEach(it => {
      const cls = it.amount < 0 ? "cut" : "add";
      const sign = it.amount < 0 ? "−" : "";
      lines.push(`<div class="line-item ${cls}">
        <span class="li-label">${esc(it.label)}</span>
        <span class="li-detail">${esc(it.detail)}
          ${it.amount < 0 ? (it.reviewed ? `<span class="pill ok">财务已${it.review_action === "reject" ? "驳回" : "核准"}</span>` : '<span class="pill warn">待财务复核</span>') : ""}</span>
        <span class="li-amount">${sign}${money(Math.abs(it.amount)).replace("−", "")}
          ${isFinance && it.amount < 0 && !it.reviewed ? `
            <button class="btn sm primary" data-rv="${it.incident_id}" data-ap="1">核准减免</button>
            <button class="btn sm danger" data-rv="${it.incident_id}" data-ap="0">驳回</button>` : ""}
        </span></div>`);
    });
    return `<div class="seg"><div class="seg-card">
      <div class="seg-head"><span class="seg-time">${esc(fmt(s.from))} → ${esc(fmt(s.to))}</span>
        <span class="seg-zone">${esc(s.zone_name)} · ${s.hours.toFixed(2)} 小时</span></div>
      <div class="seg-lines">${lines.join("")}</div>
    </div></div>`;
  }).join("");
  const body = `
    <div class="timeline">${segs}</div>
    <div class="totals">
      <div class="stat"><div class="k">占格时长</div><div class="v">${d.total_hours.toFixed(2)} h</div></div>
      <div class="stat"><div class="k">基础租金</div><div class="v">${money(d.subtotal)}</div></div>
      <div class="stat"><div class="k">加收合计</div><div class="v add">${money(d.surcharge_total)}</div></div>
      <div class="stat"><div class="k">异常减免</div><div class="v cut">−${money(d.discount_total).replace("−", "")}</div></div>
    </div>
    <div class="totals" style="grid-template-columns:1fr 1fr">
      <div class="stat"><div class="k">账单状态</div><div class="v" style="font-size:16px">${pill(b.status)}</div></div>
      <div class="stat"><div class="k">应付合计</div><div class="v grand">${money(b.total)}</div></div>
    </div>
    ${d.needs_review ? '<p class="small" style="color:var(--warning);margin-top:10px">⚠ 含负数减免项，财务逐项复核通过后方可支付。</p>' : ""}`;
  const foot = (isFinance || isAdmin) && b.status !== "paid"
    ? `<button class="btn" id="m-close2">关闭</button><button class="btn primary" id="pay-btn">标记已支付</button>`
    : `<button class="btn" id="m-close2">关闭</button>`;
  modal(`账单 #${id} · 费用快照`, body, foot);
  $("#m-close2").onclick = closeModal;
  $$("[data-rv]").forEach(btn => btn.onclick = async () => {
    const note = prompt(btn.dataset.ap === "1" ? "核准说明（可空）：" : "驳回原因：", btn.dataset.ap === "1" ? "情况属实，准予补偿" : "");
    if (note === null) return;
    try {
      await api("POST", `/api/bills/${id}/review`, {
        incident_id: Number(btn.dataset.rv), approve: btn.dataset.ap === "1", note,
      });
      toast(btn.dataset.ap === "1" ? "减免已核准" : "已驳回并重新计算账单");
      openBill(id); renderBills();
    } catch (e) { toast(e.message, true); }
  });
  const pay = $("#pay-btn");
  if (pay) pay.onclick = async () => {
    try { await api("POST", `/api/bills/${id}/pay`, {}); closeModal(); toast("账单已支付，租期结清"); renderBills(); }
    catch (e) { toast(e.message, true); }
  };
}

// ---------------- 启动 ----------------
(async function init() {
  try {
    state.me = await api("GET", "/api/me");
    if (state.me.role && state.me.role !== "anonymous") enterApp();
    else $("#login-view").classList.remove("hidden");
  } catch { $("#login-view").classList.remove("hidden"); }
})();
