/* diary — the full record (ground truth), newest first, filter by load / origin / text. */
LK.register({
  id: "diary", title: "Diary", hint: "planned · reactive · control — the ground truth",
  setup(el) {
    el.innerHTML = `<div class="filters"><select class="fl"><option value="">all loads</option></select>
      <select class="fo"><option value="">all origins</option><option>planned</option><option>reactive</option><option>control</option></select>
      <label class="inl"><input type="checkbox" class="fq" checked> hide routine (rate/user steps, progress)</label>
      <input class="ft" placeholder="search…"></div><div class="diary-box"></div>`;
    el.querySelectorAll("select,input").forEach((i) => (i.oninput = () => LK.renderAll()));
  },
  render(el, S) {
    const fl = el.querySelector(".fl");
    if (fl.dataset.loads !== S.loads.join()) {
      const v = fl.value;
      fl.innerHTML = `<option value="">all loads</option>` + S.loads.map((l) => `<option>${LK.esc(l)}</option>`).join("");
      fl.value = v; fl.dataset.loads = S.loads.join();
    }
    const load = fl.value, origin = el.querySelector(".fo").value, q = el.querySelector(".ft").value.toLowerCase();
    const quiet = el.querySelector(".fq").checked;
    const routine = new Set(["rate_changed", "users_changed", "job_progress"]);
    const bad = new Set(["health_degraded", "pod_preempted", "oom_killed", "pod_evicted", "job_timed_out", "run_skipped"]);
    const rows = [];
    for (let i = S.diary.length - 1; i >= 0 && rows.length < 500; i--) {
      const e = S.diary[i];
      if (load && e.load !== load) continue;
      if (origin && e.origin !== origin) continue;
      if (quiet && routine.has(e.event)) continue;
      const plain = LK.plainEvent(e);
      if (q && !(plain + JSON.stringify(e)).toLowerCase().includes(q)) continue;
      const cause = e.cause ? ` <span class="cause">cause: ${LK.esc(JSON.stringify(e.cause)).slice(0, 220)}</span>` : "";
      rows.push(`<div class="row"><span class="t">${LK.fmtT(e.t).padStart(8, " ")}</span> <span class="${bad.has(e.event) ? "bad" : e.origin}">${e.origin.padEnd(8, " ")}</span> ${LK.esc(plain)}${cause}</div>`);
    }
    el.querySelector(".diary-box").innerHTML = rows.join("") || `<span class="muted">${S.mode === "preview" ? "The diary fills during a run." : "No events match."}</span>`;
  },
});
