/* shop-clue — request rate and in-flight requests together: in-flight up while request rate stays flat = hidden slowdown. */
LK.register({
  id: "shop-clue", title: "Shop clue: request rate vs in-flight", hint: "in-flight = request rate × response time",
  teach: "If in-flight requests rise while the request rate stays flat, each request got slower — something else is slowing the shop.",
  render(el, S) {
    if (S.mode === "preview") { el.innerHTML = `<div class="muted">Starts with the run.</div>`; return; }
    const shops = S.loads.filter((l) => S.types[l] === "users");
    if (!shops.length) { el.innerHTML = `<div class="muted">No user-facing service in this scenario.</div>`; return; }
    const l = shops[0];
    const w = el.clientWidth || 600, h = 210, top = 8, ch = h - 30;
    const ax = LK.axis(w, 46, 46);
    const rr = LK.series(l, "request_rate"), inf = LK.series(l, "in_flight"), p95 = LK.series(l, "latency_p95");
    const rmax = Math.max(1, ...rr.map((p) => p[1])) * 1.15, imax = Math.max(1, ...inf.map((p) => p[1])) * 1.15;
    const yr = (v) => top + ch - (v / rmax) * ch, yi = (v) => top + ch - (v / imax) * ch;
    let svg = LK.phaseBands(ax, top, ch) + LK.timeTicks(ax, h - 18, ch);
    svg += `<text x="${ax.left - 6}" y="${top + 8}" text-anchor="end" fill="var(--blue)">${LK.num(rmax / 1.15, 0)}/s</text>`;
    svg += `<text x="${w - ax.right + 6}" y="${top + 8}" fill="var(--mint)">${LK.num(imax / 1.15, 1)}</text>`;
    svg += LK.line(ax, rr, yr, "var(--blue)", 2) + LK.line(ax, inf, yi, "var(--mint)", 2);
    const thr = S.h[l]?.[0]?.threshold;
    if (p95.length && thr) {
      const pmax = Math.max(thr * 2, ...p95.map((p) => p[1]));
      svg += LK.line(ax, p95, (v) => top + ch - (Math.min(v, pmax) / pmax) * ch, "var(--purple)", 1.2, "4 3");
    }
    svg += LK.nowLine(ax, top, ch);
    el.innerHTML = `<svg width="${w}" height="${h}">${svg}</svg>` +
      LK.legend([["var(--blue)", "request rate (left)", "line"], ["var(--mint)", "in-flight requests (right)", "line"], ["var(--purple)", "p95 response time (shape only)", "line"]]);
  },
});
