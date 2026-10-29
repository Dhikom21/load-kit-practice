/* story — "Recent events": the important diary events in plain words, newest first. */
(function () {
  const BAD = new Set(["pod_preempted", "pod_evicted", "oom_killed", "job_timed_out", "run_skipped", "health_degraded", "pod_lost"]);
  const GOOD = new Set(["health_recovered", "job_ended", "burst_ended"]);
  const SKIP = new Set(["rate_changed", "users_changed", "job_progress", "job_due", "preflight_checked", "run_started",
                        "pod_pending", "pod_restarted"]);
  const mark = (e) => BAD.has(e.event) ? ["!", "var(--danger)"] : GOOD.has(e.event) ? ["✓", "var(--ok)"]
    : /scaled/.test(e.event) ? [e.to > e.from ? "▲" : "▼", "var(--brand)"] : e.event === "burst_started" ? ["⚡", "var(--amber)"] : ["•", "var(--muted)"];

  LK.register({
    id: "story", title: "Recent events",
    render(el, S) {
      if (S.mode === "preview") {
        const tl = (S.timeline?.events || []).filter((e) => e.kind === "job_due" || e.kind === "burst_start").slice(0, 8);
        el.innerHTML = `<div class="muted" style="font-size:12px;margin-bottom:4px">Planned for this seed (only these are planned — everything else happens during the run):</div>
          <div class="feed">${tl.map((e) => `<div class="ev"><span class="tm">${LK.fmtT(e.t)}</span><span class="ic" style="color:var(--muted)">◷</span>
          <span>${LK.esc(e.kind === "job_due" ? `${e.load} run ${e.value.run} is due (${e.value.units} units)` : `${e.load} burst: ${e.value.x}× more events`)}</span></div>`).join("") || '<span class="muted">nothing planned</span>'}</div>`;
        return;
      }
      const t = LK.cursor();
      const evs = [];
      for (let i = S.diary.length - 1; i >= 0 && evs.length < 7; i--) {
        const e = S.diary[i];
        if (e.t > t + 1e-9 || SKIP.has(e.event) || e.origin === "control") continue;
        if (e.event === "phase_started" && !["scenario", "drain"].includes(e.name)) continue;
        if (e.event === "consumers_idle" && !e.idle) continue;
        evs.push(e);
      }
      el.innerHTML = `<div class="feed">${evs.map((e) => {
        const [m, c] = mark(e);
        return `<div class="ev"><span class="tm">${LK.fmtT(e.t)}</span><span class="ic" style="color:${c};font-weight:700">${m}</span><span>${LK.esc(LK.plainEvent(e))}</span></div>`;
      }).join("") || '<span class="muted">Waiting for the first events…</span>'}</div>`;
    },
  });
})();
