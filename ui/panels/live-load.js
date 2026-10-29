/* live-load — stacked CPU per load vs node capacity; red where the loads wanted more than the node has. */
LK.register({
  id: "live-load", title: "CPU per load", hint: "stacked · dashed = capacity · red = wanted but not available",
  teach: "When the loads want more than the node has, CPU is shared in proportion to requests: everyone runs slower.",
  render(el, S) {
    if (S.mode === "preview") { el.innerHTML = `<div class="muted">Starts with the run.</div>`; return; }
    const w = el.clientWidth || 600, h = 210, top = 8, ch = h - 30;
    const ax = LK.axis(w, 46);
    const cap = S.node.cpu;
    const demand = new Map();
    S.loads.forEach((l) => LK.series(l, "cpu_demand").forEach(([t, v]) => demand.set(t, (demand.get(t) || 0) + v)));
    const dmax = Math.max(cap, ...demand.values());
    const vmax = Math.max(cap * 1.15, dmax * 1.05);
    const st = LK.stacked(ax, S.loads, "cpu", top, ch, vmax);
    const y = (v) => top + ch - (v / vmax) * ch;
    let svg = LK.phaseBands(ax, top, ch) + LK.timeTicks(ax, h - 18, ch);
    const over = [...demand.entries()].sort((a, b) => a[0] - b[0]);
    if (over.length) {
      let d = over.map(([t, v], i) => `${i ? "L" : "M"}${ax.x(t)},${y(Math.max(v, cap))}`).join("");
      for (let i = over.length - 1; i >= 0; i--) d += `L${ax.x(over[i][0])},${y(cap)}`;
      svg += `<path d="${d}Z" fill="var(--danger)" fill-opacity=".45"><title>CPU wanted above capacity</title></path>`;
    }
    svg += st.svg;
    svg += `<line x1="${ax.left}" x2="${w - ax.right}" y1="${y(cap)}" y2="${y(cap)}" stroke="var(--text-2)" stroke-dasharray="5 4"/>`;
    svg += `<text x="${ax.left - 6}" y="${y(cap) + 4}" text-anchor="end">${cap}</text><text x="${ax.left - 6}" y="${top + 8}" text-anchor="end">cores</text>`;
    svg += LK.nowLine(ax, top, ch);
    el.innerHTML = `<svg width="${w}" height="${h}">${svg}</svg>` + LK.legend(S.loads.map((l) => [LK.color(l), l]).concat([["var(--danger)", "wanted, not available"]]));
  },
});
