/* forecast — the forecast band: the enabled forecaster's p10–p90 (shaded) and p50 (dashed), against the actual line.
   Post-run: it predicts ahead from the end of the run. Read the context (loadkit/forecast.py) and the plugins
   (loadkit/plugins/forecasters/); this panel just draws the quantiles the API returns. */
LK.register({
  id: "forecast", title: "Forecast", hint: "shaded p10–p90 · dashed p50 · solid actual",
  teach: "The forecaster predicts each metric ahead in time. The shaded band is its uncertainty (p10 to p90); the dashed line is its best guess (p50). Pick a load and a metric; the band is drawn at the end of the run.",
  slow: true,
  async render(el, S) {
    const loads = (S && S.loads) || [];
    const withNode = loads.concat(S.m && S.m._node ? ["node"] : []);
    if (!withNode.length) { el.innerHTML = '<div class="muted">open a finished run to see the forecast band.</div>'; return; }
    if (!withNode.includes(el.dataset.fcLoad)) el.dataset.fcLoad = withNode[0];
    const uiLoad = el.dataset.fcLoad, srcKey = uiLoad === "node" ? "_node" : uiLoad;
    const avail = Object.keys((S.m && S.m[srcKey]) || {});
    if (!avail.length) { el.innerHTML = '<div class="muted">no metrics recorded for this load.</div>'; return; }
    if (!avail.includes(el.dataset.fcMetric))
      el.dataset.fcMetric = avail.find((m) => ["backlog", "latency_p95", "cpu", "cpu_used"].includes(m)) || avail[0];
    const uiMetric = el.dataset.fcMetric;
    const w = el.clientWidth || 900, H = 210;
    const head = `<div style="display:flex;gap:8px;align-items:center;margin-bottom:4px">
      <select id="fcLoad" class="sel">${withNode.map((l) => `<option value="${LK.esc(l)}" ${l === uiLoad ? "selected" : ""}>${LK.esc(l)}</option>`).join("")}</select>
      <select id="fcMetric" class="sel">${avail.map((m) => `<option value="${m}" ${m === uiMetric ? "selected" : ""}>${m}</option>`).join("")}</select>
      <span class="muted" style="font-size:11px">shaded p10–p90 · dashed p50 · solid actual</span></div>`;
    const wire = () => {
      const a = $("fcLoad"), b = $("fcMetric");
      if (a) a.onchange = (e) => { el.dataset.fcLoad = e.target.value; delete el.dataset.fcMetric; delete S._fcq; LK.renderAll(); };
      if (b) b.onchange = (e) => { el.dataset.fcMetric = e.target.value; delete S._fcq; LK.renderAll(); };
    };
    const cacheKey = `${S.viewing}|${uiLoad}|${uiMetric}`;
    if (!S._fcq || S._fcq.__k !== cacheKey) {
      el.innerHTML = head + `<div class="muted" style="height:${H}px;display:flex;align-items:center">computing the forecast…</div>`;
      wire();
      try {
        const f = await LK.api(`/api/runs/${encodeURIComponent(S.viewing)}/forecast?load=${encodeURIComponent(uiLoad)}&metric=${encodeURIComponent(uiMetric)}&horizon=48`);
        S._fcq = { __k: cacheKey, q: f.quantiles };
        LK.renderAll();
      } catch (err) {
        el.innerHTML = head + `<div class="err" style="height:${H}px;display:flex;align-items:center">${LK.esc(err.message)}</div>`;
      }
      return;
    }
    const q = S._fcq, hist = LK.series(srcKey, uiMetric).slice(0, 160);
    if (!hist.length || !q.q || !q.q.p50) { el.innerHTML = head + '<div class="muted">nothing to forecast here.</div>'; return; }
    const ax = LK.axis(w, 40, 12);
    const tStart = hist[hist.length - 1][0];
    const qPts = (k) => q.q[k].map((v, i) => [tStart + (i + 1) * 15, v]);
    const vmax = Math.max(1, ...hist.map((p) => p[1]), ...qPts("p90").map((p) => p[1])) * 1.1;
    const y = (v) => 8 + (H - 26) * (1 - Math.min(Math.max(v, 0), vmax) / vmax);
    let dBand = qPts("p90").map((p, i) => `${i ? "L" : "M"}${ax.x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
    qPts("p10").slice().reverse().forEach((p) => (dBand += `L${ax.x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`));
    dBand += "Z";
    const c = LK.color(uiLoad === "node" ? "_node" : uiLoad);
    const chart = `<path d="${dBand}" fill="${c}" fill-opacity=".16"/>`
      + LK.line(ax, qPts("p50"), y, c, 1.6, "5 3")
      + (hist.length ? LK.line(ax, hist, y, c, 1.8) : "");
    el.innerHTML = head + `<svg width="${w}" height="${H}">${LK.phaseBands(ax, 0, H - 18)}${chart}${LK.timeTicks(ax, H - 16, H - 18)}</svg>`;
    wire();
  },
});
