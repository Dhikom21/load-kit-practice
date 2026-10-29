/* blame — "Impact between loads": worked out after the run from what was recorded. A button re-runs without a suspect. */
LK.register({
  id: "blame", title: "Impact between loads",
  render(el, S) {
    if (S.mode !== "past" || !S.impact) {
      el.innerHTML = `<div class="muted">Shown when the run has finished. Nobody is labelled as the victim in advance — the kit
        works out afterwards which loads became unhealthy and which other loads were involved at that moment.</div>`;
      return;
    }
    const agg = {};
    S.impact.periods.forEach((p) => {
      const mins = ((p.end_t ?? p.start_t) - p.start_t) / 60;
      const seen = new Set();
      p.causes.forEach((c) => {
        const self = c.self || c.load === p.load;
        const cause = self && c.kind === "cpu" ? `${p.load} (own other runs)` : c.load;
        const k = `${cause}|${p.load}`;
        const a = (agg[k] ||= { cause, victim: p.load, self, kinds: {}, minutes: 0 });
        const kk = (a.kinds[c.kind] ||= { count: 0, share: [] });
        kk.count += c.count || 0;
        if (c.share != null) kk.share.push(c.share);
        if (!seen.has(k)) { a.minutes += mins; seen.add(k); }
      });
    });
    const avg = (xs) => Math.round(100 * xs.reduce((x, y) => x + y, 0) / xs.length);
    const word = (kind, v) => ({
      preemption: `pushed out its pods ${v.count}×`,
      cpu: `used the CPU while the machine was full${v.share.length ? ` (${avg(v.share)} %)` : ""}`,
      memory: "held the machine's memory",
      oom: `hit its own memory limit ${v.count}×`,
    })[kind];
    const order = ["preemption", "memory", "oom", "cpu"];
    const rows = Object.values(agg).sort((a, b) => b.minutes - a.minutes).slice(0, 6);
    const tag = (n) => `<span class="tag" style="background:${LK.color(n.split(" ")[0])}">${LK.esc(n)}</span>`;
    const unhealthy = S.impact.periods.length;
    let html = rows.map((a) => `<div class="blame-row">${tag(a.cause)}<span class="arrow">→</span>${tag(a.victim)}
        <span style="font-size:13px;color:var(--text-2)">${order.filter((k) => a.kinds[k]).map((k) => LK.esc(word(k, a.kinds[k]))).join(" · ")}
        <span class="muted">· ${a.victim} unhealthy ${Math.max(1, Math.round(a.minutes))} min</span></span></div>`).join("");
    if (!unhealthy) html = `<div class="muted">No load became unhealthy in this run.</div>`;
    else if (!rows.length) html = `<div class="muted">${unhealthy} unhealthy period(s), but no other load was involved: ${LK.esc(S.impact.periods[0].notes[0] || "")}</div>`;
    const suspects = [...new Set(rows.filter((a) => !a.self).map((a) => a.cause))];
    if (suspects.length && !window.__SNAP)
      html += `<div style="margin-top:10px;font-size:12px" class="muted">These are likely causes. To be sure, run the same seed without a suspect:
        ${suspects.map((s) => `<button class="small" data-without="${LK.esc(s)}" style="margin:4px 4px 0 0">Re-run without ${LK.esc(s)}</button>`).join("")}</div>`;
    el.innerHTML = html;
    el.querySelectorAll("button[data-without]").forEach((b) => (b.onclick = () => {
      if (S.run) document.getElementById("seed").value = S.run.seed;
      LK.startRun({ without: [b.dataset.without] });
    }));
  },
});
