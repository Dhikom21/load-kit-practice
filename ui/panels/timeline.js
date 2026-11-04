/* timeline — planned (outlined) vs actual (filled) per load, with reactive markers. */
LK.register({
  id: "timeline", title: "Timeline", hint: "planned ▭ vs actual ▬ · markers = reactive decisions (hover for the cause)",
  teach: "Only due times, bursts and user counts are planned. How long a run takes, overlaps and scaling are decided during the run.",
  render(el, S) {
    const w = el.clientWidth || 900, rowH = 30, top = 6;
    const loads = S.loads;
    const h = top + loads.length * rowH + 22;
    const ax = LK.axis(w);
    const esc = LK.esc;
    let svg = LK.phaseBands(ax, 0, h - 20) + LK.timeTicks(ax, h - 18, h - 20);
    const tl = (S.timeline && S.timeline.events) || [];
    const runEnd = {}, runStart = {};
    S.diary.forEach((e) => {
      if (e.event === "job_started") runStart[`${e.load}#${e.run}`] = e;
      if (["job_ended", "job_timed_out", "run_replaced"].includes(e.event)) runEnd[`${e.load}#${e.run}`] = e;
    });
    const liveRuns = {};
    (S.status?.runs || []).forEach((r) => (liveRuns[`${r.load}#${r.run}`] = r));
    loads.forEach((load, i) => {
      const y = top + i * rowH, c = LK.color(load), type = S.types[load];
      svg += `<text class="lbl" x="4" y="${y + 18}">${esc(load)}</text>`;
      svg += `<line x1="${ax.left}" x2="${w - ax.right}" y1="${y + rowH - 2}" y2="${y + rowH - 2}" stroke="rgba(77,93,106,.2)"/>`;
      if (type === "batch") {
        tl.filter((e) => e.load === load && e.kind === "job_due").forEach((e) => {
          const x1 = ax.x(e.t), x2 = ax.x(e.t + e.value.normal_duration);
          svg += `<rect x="${x1}" y="${y + 4}" width="${Math.max(2, x2 - x1)}" height="${rowH - 10}" rx="3" fill="none" stroke="${c}" stroke-opacity=".8" stroke-dasharray="3 2"><title>${esc(load)} run ${e.value.run} due ${LK.fmtTs(e.t)} · ${e.value.units} units · normal ≈ ${Math.round(e.value.normal_duration / 60)} min</title></rect>`;
        });
        Object.entries(runStart).filter(([k]) => k.startsWith(load + "#")).forEach(([k, s]) => {
          const end = runEnd[k], lr = liveRuns[k];
          const t2 = end ? end.t : (S.now ?? s.t);
          const x1 = ax.x(s.t), x2 = ax.x(t2);
          const slow = end && end.ratio && end.ratio > 1.5;
          const fill = end && end.event !== "job_ended" ? "var(--danger)" : c;
          const label = end ? (end.event === "job_ended" ? `finished in ${Math.round(end.actual_duration / 60)} min (${end.ratio}× normal)` : end.event.replace(/_/g, " "))
                            : `running · ${lr ? lr.progress : ""}`;
          svg += `<rect x="${x1}" y="${y + 9}" width="${Math.max(2, x2 - x1)}" height="${rowH - 20}" rx="2" fill="${fill}" fill-opacity="${slow ? 1 : .8}" ${slow ? 'stroke="var(--danger)" stroke-width="1.5"' : ""}><title>${esc(load)} run ${s.run}: ${esc(label)}</title></rect>`;
          if (!end && lr && x2 - x1 > 40) svg += `<text x="${x2 + 3}" y="${y + 18}" font-size="10" fill="var(--mint)">${lr.progress}</text>`;
        });
      } else if (type === "stream") {
        const bs = tl.filter((e) => e.load === load && e.kind === "burst_start");
        bs.forEach((b) => {
          const e = tl.find((x) => x.load === load && x.kind === "burst_end" && x.t >= b.t);
          const x1 = ax.x(b.t), x2 = ax.x(e ? e.t : b.t + 60);
          svg += `<rect x="${x1}" y="${y + 4}" width="${Math.max(2, x2 - x1)}" height="${rowH - 10}" rx="3" fill="${c}" fill-opacity=".12" stroke="${c}" stroke-dasharray="3 2"><title>planned burst ×${b.value.x} at ${LK.fmtTs(b.t)}</title></rect>`;
        });
        const pts = LK.series(load, "arrival_rate");
        if (pts.length) {
          const vmax = Math.max(...pts.map((p) => p[1]), 1);
          const yy = (v) => y + rowH - 4 - (v / vmax) * (rowH - 9);
          svg += LK.line(ax, pts, yy, c, 1.4);
        }
      } else if (type === "users") {
        const steps = tl.filter((e) => e.load === load && e.kind === "users_change");
        const vmax = Math.max(1, ...steps.map((s) => s.value.users));
        const yy = (v) => y + rowH - 4 - (v / vmax) * (rowH - 9);
        let d = "";
        steps.forEach((s, j) => {
          const nx = steps[j + 1] ? steps[j + 1].t : (S.timeline.duration);
          d += `${j ? "L" : "M"}${ax.x(s.t)},${yy(s.value.users)}L${ax.x(nx)},${yy(s.value.users)}`;
        });
        if (d) svg += `<path d="${d}" fill="none" stroke="${c}" stroke-width="1.4" stroke-dasharray="3 2"><title>planned users</title></path>`;
        const act = LK.series(load, "request_rate");
        if (act.length) {
          const rmax = Math.max(...act.map((p) => p[1]), 1);
          svg += LK.line(ax, act, (v) => y + rowH - 4 - (v / rmax) * (rowH - 9), c, 1.4);
        }
      }
      // reactive markers
      S.diary.filter((e) => e.load === load && e.origin === "reactive").forEach((e) => {
        const m = { run_overlapped: "⧉", run_skipped: "⤫", run_queued: "…", job_timed_out: "⏱", pod_preempted: "↯",
                    consumers_scaled: e.to > e.from ? "▲" : "▼", shop_scaled: e.to > e.from ? "▲" : "▼",
                    oom_killed: "⚡", pod_evicted: "⛔", consumers_idle: "◌" }[e.event];
        if (!m) return;
        const col = ["pod_preempted", "oom_killed", "pod_evicted", "job_timed_out"].includes(e.event) ? "var(--danger)" : "var(--mint)";
        svg += `<text x="${ax.x(e.t)}" y="${y + 9}" text-anchor="middle" font-size="10" fill="${col}" style="cursor:help">${m}<title>${LK.fmtTs(e.t)} · ${esc(LK.plainEvent(e))}</title></text>`;
      });
    });
    svg += LK.nowLine(ax, 0, h - 20);
    el.innerHTML = `<svg width="${w}" height="${h}">${svg}</svg>`;
  },
});
