/* compare — two past runs on the same clock: health strips + summary. */
LK.register({
  id: "compare", title: "Compare two runs", hint: "e.g. same seed with another scaler, or with one load removed",
  setup(el) {
    el.innerHTML = `<div class="filters"><select class="ca"></select><select class="cb"></select><button class="go">Compare</button></div><div class="out muted">Pick two past runs.</div>`;
    el.querySelector(".go").onclick = async () => {
      const a = el.querySelector(".ca").value, b = el.querySelector(".cb").value;
      if (!a || !b) return;
      const out = el.querySelector(".out");
      out.innerHTML = "loading…";
      try {
        const r = await LK.api(`/api/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
        const mk = (x) => {
          const S = { mode: "past", m: {}, h: {}, diary: x.diary, loads: [], types: {}, prio: {}, phases: x.run.phases, now: null };
          x.health.forEach((h) => (S.h[h.load] ||= []).push(h));
          (x.scenario?.loads || []).forEach((l) => { S.types[l.name] = l.type; S.prio[l.name] = l.priority; });
          S.loads = Object.keys(S.h);
          return S;
        };
        const SA = mk(r.a), SB = mk(r.b);
        const all = [...new Set([...SA.loads, ...SB.loads])];
        SA.loads = all; SB.loads = all;
        const keep = LK.S;
        const t0 = Math.min(...Object.values(SA.phases).map((p) => p.start_t)), t1 = Math.max(r.a.run.duration_s, r.b.run.duration_s) + 600;
        [SA, SB].forEach((S) => { S.t0 = t0; S.t1 = t1; S.types = { ...SA.types, ...SB.types }; });
        const w = out.clientWidth || 900;
        LK.S = SA; const ha = LK.healthStrips(SA, w, { ticks: false });
        LK.S = SB; const hb = LK.healthStrips(SB, w);
        LK.S = keep;
        out.className = "out";
        out.innerHTML = `<div class="section-label">A · ${LK.esc(a)}</div>${ha}<div class="section-label">B · ${LK.esc(b)}</div>${hb}
          <pre class="diary-box" style="max-height:none">${LK.esc(r.text)}</pre>`;
      } catch (e) { out.innerHTML = `<span class="err">${LK.esc(e.message)}</span>`; }
    };
  },
  render(el) {
    const runs = LK.pastRuns || [];
    const opts = `<option value="">— run —</option>` + runs.map((r) => `<option value="${LK.esc(r.run_id)}">${LK.esc(r.scenario)} · s${r.seed} · ${LK.esc((r.started_utc || "").slice(5, 16).replace("T", " "))}${r.note ? " · " + LK.esc(r.note) : ""}</option>`).join("");
    ["ca", "cb"].forEach((c) => {
      const s = el.querySelector("." + c);
      if (s.dataset.n !== String(runs.length)) { const v = s.value; s.innerHTML = opts; s.value = v; s.dataset.n = runs.length; }
    });
  },
});
