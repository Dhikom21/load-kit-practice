/* health — green / amber / red strip per load over time. Every load is checked the same way: no fixed victim. */
LK.healthStrips = (S, w, opts = {}) => {
  const rowH = opts.rowH || 22, top = 4;
  const loads = S.loads;
  const h = top + loads.length * rowH + (opts.ticks === false ? 4 : 20);
  const ax = LK.axis(w, opts.left || 92);
  let svg = LK.phaseBands(ax, 0, h - 18, false) + (opts.ticks === false ? "" : LK.timeTicks(ax, h - 16, h - 18));
  loads.forEach((load, i) => {
    const y = top + i * rowH;
    const p = S.prio[load] || "";
    svg += `<text class="lbl" x="4" y="${y + 15}">${LK.esc(load)}</text>`;
    if (p) svg += `<text x="${ax.left - 8}" y="${y + 15}" text-anchor="end" font-size="9" fill="${p === "high" ? "#C4B5FD" : "var(--muted)"}">${p}</text>`;
    const rows = S.h[load] || [];
    rows.forEach((r, j) => {
      const t2 = rows[j + 1] ? rows[j + 1].t : r.t + 15;
      const warm = r.phase === "warmup";
      const col = !r.healthy ? "var(--danger)" : r.threshold && r.value > 0.8 * r.threshold ? "var(--amber)" : "var(--ok)";
      svg += `<rect x="${ax.x(r.t)}" y="${y + 3}" width="${Math.max(1, ax.x(t2) - ax.x(r.t) + .3)}" height="${rowH - 8}" fill="${col}" fill-opacity="${warm ? .25 : .75}"><title>${LK.fmtT(r.t)} · ${r.metric} ${LK.num(r.value, 2)} (limit ${r.threshold})${r.healthy ? "" : " — unhealthy"}${warm ? " · warm-up: not counted" : ""}</title></rect>`;
    });
  });
  svg += LK.nowLine(ax, 0, h - 18);
  return `<svg width="${w}" height="${h}">${svg}</svg>`;
};
LK.register({
  id: "health", title: "Health of every load", hint: "green ok · amber near the limit · red unhealthy",
  teach: "Shop: p95 response time and errors. Stream: age of the oldest waiting event. Batch: run time vs its normal estimate (and overlaps, kills).",
  render(el, S) {
    if (S.mode === "preview") { el.innerHTML = `<div class="muted">Health is measured during a run.</div>`; return; }
    el.innerHTML = LK.healthStrips(S, el.clientWidth || 900, { left: 132 });
  },
});
