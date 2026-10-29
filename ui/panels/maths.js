/* maths — how busy is the machine, worked out from the scenario's own numbers at the time shown.
   1 helper = 1 CPU core = 60 s of work per minute. */
(function () {
  const fmt = (v, d = 0) => Number(v).toLocaleString("en", { minimumFractionDigits: d, maximumFractionDigits: d });
  const sec = (v) => (v < 0.1 ? `${fmt(v * 1000)} ms` : `${fmt(v, v < 1 ? 2 : 1)} s`);

  function latest(tl, load, kind, t) {
    let v = null, first = null;
    for (const e of tl) {
      if (e.load !== load || e.kind !== kind) continue;
      if (first === null) first = e;
      if (e.t <= t + 1e-9) v = e; else break;
    }
    return v || first;
  }

  function batchRuns(S, l, t) {
    if (S.mode === "preview") {
      return (S.timeline?.events || []).filter((e) => e.load === l && e.kind === "job_due" &&
        e.t <= t && t < e.t + e.value.normal_duration).length;
    }
    const started = {}, ended = {};
    S.diary.forEach((e) => {
      if (e.load !== l || e.t > t + 1e-9) return;
      if (e.event === "job_started") started[e.run] = 1;
      if (["job_ended", "job_timed_out", "run_replaced", "job_failed"].includes(e.event)) ended[e.run] = 1;
    });
    return Object.keys(started).filter((r) => !ended[r]).length;
  }

  function rows(S, t) {
    const tl = S.timeline?.events || [];
    return (S.scenario?.loads || []).map((ld) => {
      const l = ld.name;
      if (ld.type === "batch") {
        const n = batchRuns(S, l, t), cores = ld.parallelism * ld.intensity;
        const helpers = n * cores;
        return { l, set: `${sec(ld.cost_per_unit)} per unit · ${ld.parallelism} pod × ${fmt(ld.intensity, 1)} core`,
                 inm: n ? `${n} run${n > 1 ? "s" : ""} working non-stop` : "no run right now",
                 work: n ? `${n} × ${fmt(cores, 1)} × 60 = ${fmt(helpers * 60, 1)} s` : "0 s", helpers };
      }
      if (ld.type === "stream") {
        const rc = latest(tl, l, "rate_change", t);
        const factor = rc ? rc.value.factor : 1;
        const bs = tl.filter((e) => e.load === l && e.kind === "burst_start" && e.t <= t);
        const last = bs[bs.length - 1];
        const be = last && tl.find((e) => e.load === l && e.kind === "burst_end" && e.t >= last.t);
        const x = last && be && t < be.t ? last.value.x : 1;
        const rate = ld.base_rate * factor * x, ev = rate * 60, work = ev * ld.work_per_event;
        const why = [factor !== 1 ? `× ${fmt(factor, 2)} time of day` : "", x !== 1 ? `× ${fmt(x, 1)} burst` : ""].filter(Boolean).join(" ");
        return { l, set: `${sec(ld.work_per_event)} per event · ${fmt(ld.base_rate)} events/s ${why}`,
                 inm: `${fmt(rate, rate < 10 ? 1 : 0)} × 60 = ${fmt(ev)} events`,
                 work: `${fmt(ev)} × ${fmt(ld.work_per_event, 3)} = ${fmt(work, 1)} s`, helpers: work / 60 };
      }
      const uc = latest(tl, l, "users_change", t);
      const users = uc ? uc.value.users : ld.users;
      const per = 60 / (ld.think_time + ld.cost_per_request), req = users * per, work = req * ld.cost_per_request;
      return { l, set: `${sec(ld.cost_per_request)} per request · each user ~${fmt(per / 60, 1)} request/s`,
               inm: `${fmt(users)} users × ${fmt(per)} per min ≈ ${fmt(req)} requests`,
               work: `${fmt(req)} × ${fmt(ld.cost_per_request, 3)} = ${fmt(work, 1)} s`, helpers: work / 60 };
    });
  }

  LK.register({
    id: "maths", title: "The maths · how busy is the machine?", hint: "1 helper = 1 CPU core = 60 s of work per minute",
    render(el, S) {
      if (!S.scenario?.loads?.length || S.scenario.loads[0].type === undefined) {
        el.innerHTML = `<div class="muted">Open a scenario to see its numbers.</div>`;
        return;
      }
      let t = LK.cursor(), when = "";
      if (S.mode === "preview") {                       // nothing running: show the busiest planned minute
        let best = 0, bt = 0;
        for (let x = 0; x <= S.t1; x += 60) {
          const s = rows(S, x).reduce((a, r) => a + r.helpers, 0);
          if (s > best + 1e-9) { best = s; bt = x; }
        }
        t = bt;
        when = `Busiest planned minute: ${LK.fmtT(t)}`;
      } else when = t >= S.t1 ? "End of run — drag the replay slider to any moment" : `At ${LK.fmtT(t)}`;
      const rs = rows(S, Math.max(0, t));
      const total = rs.reduce((a, r) => a + r.helpers, 0);
      const cap = (S.mode !== "preview" && LK.at("_node", "cpu_capacity", t)) || S.node.cpu;
      const pct = Math.round((100 * total) / cap);
      const verdict = pct < 70 ? ["var(--ok)", "nobody waits"] : pct <= 100 ? ["var(--amber)", "busy: short waits start"]
        : ["var(--danger)", "more work than helpers: work piles up and everything slows down"];
      const measured = S.mode !== "preview" ? LK.at("_node", "cpu_used", t) : null;
      el.innerHTML = `<div class="muted" style="font-size:11px;margin-bottom:4px">${LK.esc(when)}</div>
        <table class="maths"><thead><tr><th>Load</th><th>Setting in the file</th><th>In 1 minute</th>
        <th style="text-align:right">Work</th><th style="text-align:right">Helpers</th></tr></thead><tbody>
        ${rs.map((r) => `<tr><td style="white-space:nowrap"><i class="chip" style="background:${LK.color(r.l)};margin-right:6px"></i>${LK.esc(r.l)}</td>
          <td class="f">${LK.esc(r.set)}</td><td>${LK.esc(r.inm)}</td><td class="n">${LK.esc(r.work)}</td>
          <td class="n"><b>${fmt(r.helpers, 2)}</b></td></tr>`).join("")}
        <tr class="total"><td colspan="3">Total</td><td class="n">${fmt(total * 60, 1)} s</td><td class="n">${fmt(total, 2)}</td></tr>
        </tbody></table>
        <div class="maths-sum">${fmt(cap, 1)} helpers can do <b>${fmt(cap, 1)} × 60 = ${fmt(cap * 60)} s</b> of work per minute.
          Work arriving: <b>${fmt(total * 60, 1)} s</b>.<br>
          Busy: ${fmt(total * 60, 1)} ÷ 60 = <b>${fmt(total, 2)}</b> · free: ${fmt(cap, 1)} − ${fmt(total, 2)} = <b>${fmt(Math.max(0, cap - total), 2)}</b> ·
          <span style="color:${verdict[0]};font-weight:600">${pct} % busy → ${verdict[1]}</span>
          ${measured != null ? `<br><span class="muted">Measured on the cluster: ${fmt(measured, 2)} cores (includes Redis, the producer, the users pod and Kubernetes itself).</span>` : ""}
        </div>`;
    },
  });
})();
