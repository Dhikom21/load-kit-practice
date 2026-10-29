/* replicas — pods per scaled load; one marker per scaling decision with what each signal wanted. */
LK.register({
  id: "replicas", title: "Replicas and scaling decisions", hint: "hover a marker: what each signal wanted",
  teach: "Scale up at once, scale down only after the stabilization window — so extra pods stay on the node for minutes.",
  render(el, S) {
    if (S.mode === "preview") { el.innerHTML = `<div class="muted">Starts with the run.</div>`; return; }
    const loads = S.loads.filter((l) => S.types[l] !== "batch");
    if (!loads.length) { el.innerHTML = `<div class="muted">No scaled loads in this scenario.</div>`; return; }
    const w = el.clientWidth || 600, h = 210, top = 8, ch = h - 30;
    const ax = LK.axis(w, 46);
    const vmax = Math.max(2, ...loads.flatMap((l) => LK.series(l, "replicas").map((p) => p[1]))) + 1;
    const y = (v) => top + ch - (v / vmax) * ch;
    let svg = LK.phaseBands(ax, top, ch) + LK.timeTicks(ax, h - 18, ch);
    for (let v = 0; v <= vmax; v += Math.max(1, Math.round(vmax / 5)))
      svg += `<text x="${ax.left - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text><line x1="${ax.left}" x2="${w - ax.right}" y1="${y(v)}" y2="${y(v)}" stroke="rgba(77,93,106,.18)"/>`;
    loads.forEach((l) => {
      const pts = LK.series(l, "replicas");
      let d = "";
      pts.forEach(([t, v], i) => { d += i ? `L${ax.x(t)},${y(pts[i - 1][1])}L${ax.x(t)},${y(v)}` : `M${ax.x(t)},${y(v)}`; });
      svg += `<path d="${d}" fill="none" stroke="${LK.color(l)}" stroke-width="2"/>`;
      S.diary.filter((e) => e.load === l && (e.event === "consumers_scaled" || e.event === "shop_scaled")).forEach((e) => {
        const ps = Object.entries(e.cause.per_signal || {}).map(([k, v]) => `${k} wanted ${v}`).join(", ");
        svg += `<circle cx="${ax.x(e.t)}" cy="${y(e.to)}" r="3.5" fill="${LK.color(l)}" stroke="var(--bg)"><title>${LK.fmtT(e.t)} · ${LK.esc(l)} ${e.from} → ${e.to} · ${LK.esc(ps)} → ${LK.esc(e.cause.winner)}${e.cause.reason ? "\n" + LK.esc(e.cause.reason) : ""}</title></circle>`;
      });
    });
    svg += LK.nowLine(ax, top, ch);
    el.innerHTML = `<svg width="${w}" height="${h}">${svg}</svg>` + LK.legend(loads.map((l) => [LK.color(l), `${l} (${S.types[l] === "users" ? "shop pods" : "consumers"})`, "line"]));
  },
});
