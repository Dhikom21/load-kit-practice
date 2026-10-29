/* health-cards — one card per load: a light, a word, and one plain sentence. No load is "the victim". */
(function () {
  const ST = { ok: ["var(--ok)", "Healthy"], warn: ["var(--amber)", "Under pressure"], bad: ["var(--danger)", "Unhealthy"],
               idle: ["var(--border)", "Not started"], warm: ["var(--muted)", "Warming up"] };

  function batchSentence(S, l, t) {
    const started = {}, ended = {};
    S.diary.forEach((e) => {
      if (e.load !== l || e.t > t) return;
      if (e.event === "job_started") started[e.run] = e;
      if (["job_ended", "job_timed_out", "run_replaced"].includes(e.event)) ended[e.run] = e;
    });
    const active = Object.values(started).filter((s) => !ended[s.run]);
    if (active.length) {
      const s = active[active.length - 1];
      const ratio = (t - s.t) / s.normal_duration;
      return `${active.length > 1 ? `${active.length} runs at once · ` : ""}run ${s.run}: ${LK.mins(t - s.t)} so far (normally ${LK.mins(s.normal_duration)})` +
        (ratio > 1 ? ` · already ${ratio.toFixed(1)}× normal` : "");
    }
    const last = Object.values(ended).pop();
    if (last && last.event === "job_ended") return `last run took ${LK.mins(last.actual_duration)} (${last.ratio}× normal)`;
    if (last) return `last run was stopped (${last.event.replace(/_/g, " ")})`;
    return "no run yet";
  }

  LK.register({
    id: "health-cards", title: "Health of every load",
    render(el, S) {
      const t = LK.cursor();
      el.innerHTML = `<div class="hcards">${S.loads.map((l) => {
        const type = S.types[l] || "batch";
        const h = S.mode === "preview" ? null : LK.healthAt(l, t);
        let st = "idle", tx = "";
        const atEnd = S.mode === "past" && t >= S.t1 && S.impact;
        if (atEnd) {
          const ps = S.impact.periods.filter((p) => p.load === l);
          const mins = ps.reduce((a, p) => a + ((p.end_t ?? p.start_t) - p.start_t), 0);
          st = ps.length ? "bad" : "ok";
          tx = ps.length ? `was unhealthy ${ps.length}× for ${LK.mins(mins)} in total` : "stayed healthy the whole run";
          if (type === "batch") tx += " · " + batchSentence(S, l, t);
        } else if (h) {
          st = h.phase === "warmup" ? "warm" : !h.healthy ? "bad" : (h.threshold && h.value > 0.8 * h.threshold ? "warn" : "ok");
          if (type === "users") {
            const err = LK.at(l, "errors", t) || 0;
            tx = `pages load in ${Math.round(h.value * 1000)} ms (limit ${Math.round(h.threshold * 1000)} ms)` + (err > 0.005 ? ` · ${(err * 100).toFixed(1)} % errors` : "");
          } else if (type === "stream") {
            const bl = LK.at(l, "backlog", t) || 0;
            tx = `oldest event waits ${LK.mins(h.value)} (limit ${LK.mins(h.threshold)}) · ${Math.round(bl)} waiting`;
          } else {
            tx = batchSentence(S, l, t);
            const w = Math.round(LK.at(l, "pending", t) || 0);
            if (w) tx += ` · ${w} pod${w > 1 ? "s" : ""} waiting outside`;
          }
        } else tx = type === "batch" ? "runs on a schedule" : type === "stream" ? "receives events all the time" : "users visit the shop";
        const [col, w0] = ST[st];
        const word = atEnd ? (st === "bad" ? "Was unhealthy" : "Stayed healthy") : w0;
        return `<div class="hcard"><div class="nm"><span class="light" style="background:${col}"></span>${LK.esc(l)}</div>
          <div class="pr">${LK.esc(type)} · priority ${LK.esc(S.prio[l] || "normal")}</div>
          <div class="st" style="color:${col}">${word}</div><div class="tx">${LK.esc(tx)}</div></div>`;
      }).join("")}</div>`;
    },
  });
})();
