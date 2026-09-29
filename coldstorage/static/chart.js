/* 温度曲线：折线 + 清晰阈值带（非装饰面积图）+ 越界红点 + 十字线 tooltip。
   数据由页面通过 window.__CHART__ 注入。 */
(function () {
  const el = document.getElementById("tempChart");
  if (!el || !window.__CHART__) return;
  const data = window.__CHART__;          // {points:[{t,c,breach}], lo, hi, setLo, setHi}
  const W = Math.max(640, el.clientWidth || 900), H = 340;
  const M = { t: 18, r: 20, b: 34, l: 46 };
  const iw = W - M.l - M.r, ih = H - M.t - M.b;

  const pts = data.points;
  const temps = pts.map(p => p.c).concat([data.lo, data.hi, data.setLo, data.setHi]);
  let yMin = Math.min(...temps) - 1.5, yMax = Math.max(...temps) + 1.5;
  const t0 = pts.length ? new Date(pts[0].t) : new Date();
  const t1 = pts.length ? new Date(pts[pts.length - 1].t) : new Date();
  const span = Math.max(1, t1 - t0);

  const x = d => M.l + (new Date(d.t) - t0) / span * iw;
  const y = v => M.t + (yMax - v) / (yMax - yMin) * ih;

  const svg = [];
  const NS = "http://www.w3.org/2000/svg";
  function add(tag, attrs, text) {
    const s = `<${tag} ` + Object.entries(attrs).map(([k, v]) => `${k}="${v}"`).join(" ") +
      (text == null ? "/>" : `>${text}</${tag}>`);
    svg.push(s);
  }

  // 网格（浅色 recessive）
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const v = yMin + (yMax - yMin) * i / ticks;
    const yy = y(v);
    add("line", { x1: M.l, x2: M.l + iw, y1: yy, y2: yy, stroke: "#e7eef1", "stroke-width": 1 });
    add("text", { x: M.l - 8, y: yy + 4, "text-anchor": "end",
      "font-size": 11, fill: "#6f6880" }, v.toFixed(1));
  }
  // X 轴时间刻度
  const xtN = 6;
  for (let i = 0; i <= xtN; i++) {
    const dt = new Date(+t0 + span * i / xtN);
    const xx = M.l + iw * i / xtN;
    const lbl = String(dt.getMonth() + 1).padStart(2, "0") + "-" +
      String(dt.getDate()).padStart(2, "0") + " " +
      String(dt.getHours()).padStart(2, "0") + ":" +
      String(dt.getMinutes()).padStart(2, "0");
    add("text", { x: xx, y: H - 10, "text-anchor": "middle",
      "font-size": 10.5, fill: "#6f6880" }, lbl);
  }

  // 阈值带：目标温区带（冰青）清晰标识可接受区间；其外为越界区（暖带仅作警示）
  add("rect", { x: M.l, y: y(data.hi), width: iw,
    height: y(data.lo) - y(data.hi), fill: "rgba(13,148,136,0.10)" });
  add("rect", { x: M.l, y: M.t, width: iw, height: y(data.hi) - M.t,
    fill: "rgba(180,83,9,0.07)" });
  add("rect", { x: M.l, y: y(data.lo), width: iw,
    height: M.t + ih - y(data.lo), fill: "rgba(180,83,9,0.07)" });
  // 库区物理温区（虚线边）
  add("line", { x1: M.l, x2: M.l + iw, y1: y(data.setHi), y2: y(data.setHi),
    stroke: "#6d5bd0", "stroke-width": 1.4, "stroke-dasharray": "6 4" });
  add("line", { x1: M.l, x2: M.l + iw, y1: y(data.setLo), y2: y(data.setLo),
    stroke: "#6d5bd0", "stroke-width": 1.4, "stroke-dasharray": "6 4" });
  add("text", { x: M.l + 6, y: y(data.setHi) - 4, "font-size": 10.5, fill: "#5648b0" },
    `库区上限 ${data.setHi}℃`);
  add("text", { x: M.l + 6, y: y(data.setLo) + 13, "font-size": 10.5, fill: "#5648b0" },
    `库区下限 ${data.setLo}℃`);
  // 目标温区边界（实线、带标注）
  add("line", { x1: M.l, x2: M.l + iw, y1: y(data.hi), y2: y(data.hi),
    stroke: "#0f766e", "stroke-width": 1.2 });
  add("line", { x1: M.l, x2: M.l + iw, y1: y(data.lo), y2: y(data.lo),
    stroke: "#0f766e", "stroke-width": 1.2 });

  // 折线
  const path = pts.map((p, i) => `${i ? "L" : "M"}${x(p).toFixed(1)},${y(p.c).toFixed(1)}`).join("");
  add("path", { d: path, fill: "none", stroke: "#0d9488", "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round" });

  // 越界点（红点，不依赖颜色：加大半径 + 白描边）
  pts.forEach(p => {
    if (p.breach) {
      add("circle", { cx: x(p), cy: y(p.c), r: 4.5, fill: "#b91c1c",
        stroke: "#fff", "stroke-width": 1.5 });
    }
  });

  // 十字线
  add("line", { id: "cxV", x1: 0, x2: 0, y1: M.t, y2: M.t + ih,
    stroke: "#4a4458", "stroke-width": 1, "stroke-dasharray": "3 3", visibility: "hidden" });
  add("circle", { id: "cxH", cx: 0, cy: 0, r: 4.5, fill: "#0d9488",
    stroke: "#fff", "stroke-width": 1.5, visibility: "hidden" });

  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
    aria-label="温度曲线，含目标温区与库区温区阈值带">${svg.join("")}</svg>
    <div id="chartTip"></div>`;

  const svgEl = el.querySelector("svg");
  const vLine = el.querySelector("#cxV");
  const hDot = el.querySelector("#cxH");
  const tip = el.querySelector("#chartTip");

  svgEl.addEventListener("mousemove", (ev) => {
    const rect = svgEl.getBoundingClientRect();
    const mx = (ev.clientX - rect.left) * (W / rect.width);
    if (mx < M.l || mx > M.l + iw || !pts.length) { tip.style.display = "none"; return; }
    const ratio = (mx - M.l) / iw;
    const idx = Math.max(0, Math.min(pts.length - 1, Math.round(ratio * (pts.length - 1))));
    const p = pts[idx];
    const px = x(p);
    vLine.setAttribute("x1", px); vLine.setAttribute("x2", px);
    vLine.setAttribute("visibility", "visible");
    hDot.setAttribute("cx", px); hDot.setAttribute("cy", y(p.c));
    hDot.setAttribute("visibility", "visible");
    const d = new Date(p.t);
    const tstr = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-` +
      `${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:` +
      `${String(d.getMinutes()).padStart(2, "0")}`;
    tip.innerHTML = `<div class="tt-t">${tstr}</div>
      <div>温度 <b>${p.c.toFixed(2)}℃</b> ${p.breach ? '<span style="color:#f6a3a3">· 越界</span>' : ""}</div>
      <div class="tt-t">目标 ${data.lo}~${data.hi}℃ ｜ 库区 ${data.setLo}~${data.setHi}℃</div>`;
    tip.style.display = "block";
    const localX = px * (rect.width / W);
    tip.style.left = Math.min(rect.width - 190, localX + 14) + "px";
    tip.style.top = "10px";
  });
  svgEl.addEventListener("mouseleave", () => {
    tip.style.display = "none";
    vLine.setAttribute("visibility", "hidden");
    hDot.setAttribute("visibility", "hidden");
  });
})();
