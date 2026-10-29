/* memory — stacked memory per load vs node memory, with OOM kill / eviction / restart markers. */
LK.register({
  id: "memory", title: "Memory per load", hint: "⚡ OOM kill · ⛔ eviction · ↻ restart",
  teach: "Memory cannot be shared more thinly: over the pod limit ⇒ OOM kill; node full ⇒ evictions, often of a pod that did not use the memory.",
  render(el, S) {
    if (S.mode === "preview") { el.innerHTML = `<div class="muted">Starts with the run.</div>`; return; }
    const w = el.clientWidth || 600, h = 210, top = 8, ch = h - 44;
    const ax = LK.axis(w, 46);
    const cap = S.node.memory, alloc = cap * 0.95;
    const used = LK.series("_node", "memory_used");
    const vmax = Math.max(cap * 1.05, ...used.map((p) => p[1]));
    const st = LK.stacked(ax, S.loads, "memory", top, ch, vmax);
    const y = (v) => top + ch - (v / vmax) * ch;
    let svg = LK.phaseBands(ax, top, ch) + LK.timeTicks(ax, h - 18, ch + 14) + st.svg;
    svg += `<line x1="${ax.left}" x2="${w - ax.right}" y1="${y(cap)}" y2="${y(cap)}" stroke="var(--text-2)" stroke-dasharray="5 4"/>`;
    svg += `<line x1="${ax.left}" x2="${w - ax.right}" y1="${y(alloc)}" y2="${y(alloc)}" stroke="var(--danger)" stroke-dasharray="2 3" stroke-opacity=".7"/>`;
    svg += `<text x="${ax.left - 6}" y="${y(cap) + 4}" text-anchor="end">${LK.fmtMem(cap)}</text>`;
    S.diary.forEach((e) => {
      const m = { oom_killed: "⚡", pod_evicted: "⛔", pod_restarted: "↻" }[e.event];
      if (!m) return;
      svg += `<text x="${ax.x(e.t)}" y="${top + ch + 12}" text-anchor="middle" font-size="11" fill="${e.event === "pod_restarted" ? "var(--muted)" : "var(--danger)"}">${m}<title>${LK.fmtT(e.t)} · ${LK.esc(LK.plainEvent(e))}</title></text>`;
    });
    svg += LK.nowLine(ax, top, ch);
    el.innerHTML = `<svg width="${w}" height="${h}">${svg}</svg>` + LK.legend(S.loads.map((l) => [LK.color(l), l]).concat([["var(--danger)", "eviction threshold (95 %)", "line"]]));
  },
});
