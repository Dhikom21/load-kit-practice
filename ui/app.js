/* Load Scenario Kit — UI shell.
   Main view (machine, maths, health cards, recent events, impact between loads) + "Advanced" (detailed charts).
   Loads only the panels enabled in loadkit.yaml; each panel is ui/panels/<name>.js. */
"use strict";

const LK = {
  panels: {},
  main: ["machine", "maths", "health-cards", "story", "blame"],
  advanced: ["timeline", "health", "live-load", "replicas", "shop-clue", "memory", "compare", "diary"],
  wide: new Set(["machine", "maths", "health-cards", "timeline", "health", "compare", "diary"]),
  S: null,            // what is shown now (live run, past run or planned preview) — same shape for all
  info: null,
  scenarios: [],
  selected: null,
  advOpen: false,
  register(p) { LK.panels[p.id] = p; },
};

const PALETTE = {
  batch: ["#5B9A9A", "#3E7777", "#7FB5B5", "#2F5F5F"],
  stream: ["#4A90D9", "#2C66A8", "#7FB2EA"],
  users: ["#7C6B9E", "#5A4B7A", "#A796C9"],
};
const ICON = {   // tiny inline icons (no icon font needed offline)
  batch: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><ellipse cx="8" cy="3.5" rx="5" ry="2"/><path d="M3 3.5v9c0 1.1 2.2 2 5 2s5-.9 5-2v-9M3 8c0 1.1 2.2 2 5 2s5-.9 5-2"/></svg>',
  stream: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M2 5h10M9 2l3 3-3 3M14 11H4M7 8l-3 3 3 3"/></svg>',
  users: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="8" cy="5" r="3"/><path d="M2.5 14.5c.8-3 3-4.5 5.5-4.5s4.7 1.5 5.5 4.5"/></svg>',
  wait: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="8" cy="8" r="6"/><path d="M8 4.5V8l2.5 1.5"/></svg>',
};

// ------------------------------------------------------------------ helpers
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
LK.esc = esc;
LK.icon = ICON;

LK.api = async (path, body) => {
  const r = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};

LK.fmtT = (t) => {
  const s = Math.round(t), sign = s < 0 ? "−" : "", a = Math.abs(s);
  const h = Math.floor(a / 3600), m = Math.floor((a % 3600) / 60), sec = a % 60;
  return `${sign}${h}:${String(m).padStart(2, "0")}${h === 0 && a < 600 ? ":" + String(sec).padStart(2, "0") : ""}`;
};
LK.fmtMem = (b) => b >= 1024 ** 3 ? (b / 1024 ** 3).toFixed(1) + " GiB" : Math.round(b / 1024 ** 2) + " MiB";
LK.num = (v, d = 1) => (Math.abs(v) >= 100 ? Math.round(v) : Number(v).toFixed(d));
LK.mins = (s) => (s < 90 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`);

LK.color = (load) => {
  const S = LK.S;
  const type = S.types[load] || "batch";
  const same = S.loads.filter((l) => (S.types[l] || "batch") === type);
  const pal = PALETTE[type] || PALETTE.batch;
  return pal[Math.max(0, same.indexOf(load)) % pal.length];
};

/* value of a metric at time t (latest sample at or before t) */
LK.at = (load, metric, t) => {
  const s = LK.series(load, metric);
  let v = null;
  for (const [tt, vv] of s) { if (tt <= t + 1e-9) v = vv; else break; }
  return v;
};
LK.healthAt = (load, t) => {
  const rows = LK.S.h[load] || [];
  let r = null;
  for (const x of rows) { if (x.t <= t + 1e-9) r = x; else break; }
  return r;
};
LK.cursor = () => {
  const S = LK.S;
  if (!S) return 0;
  if (S.mode === "live") return S.now ?? 0;
  if (S.mode === "past") return S.cursor ?? S.t1;
  return 0;
};

/* Time axis shared by all charts. */
LK.axis = (w, left = 92, right = 12) => {
  const S = LK.S;
  const t0 = S.t0, t1 = S.t1;
  const x = (t) => left + ((t - t0) / (t1 - t0 || 1)) * (w - left - right);
  return { x, t0, t1, left, right, w };
};

LK.phaseBands = (ax, y0, h, labels = true) => {
  const S = LK.S, ph = S.phases || {};
  const names = ["warmup", "baseline", "scenario", "drain"];
  let out = "";
  names.forEach((n, i) => {
    if (!ph[n]) return;
    const a = ph[n].start_t;
    const nxt = names.slice(i + 1).find((m) => ph[m]);
    const b = nxt ? ph[nxt].start_t : (ph.teardown ? ph.teardown.start_t : S.now ?? S.t1);
    const fill = n === "scenario" ? "rgba(0,0,0,0)" : n === "drain" ? "rgba(74,144,217,.07)" : "rgba(100,116,139,.08)";
    out += `<rect x="${ax.x(a)}" y="${y0}" width="${Math.max(0, ax.x(b) - ax.x(a))}" height="${h}" fill="${fill}"/>`;
    if (labels && n !== "scenario" && ax.x(b) - ax.x(a) > n.length * 5.5 + 4)
      out += `<text x="${ax.x(a) + 3}" y="${y0 + 10}" font-size="9">${n}</text>`;
  });
  return out;
};

LK.timeTicks = (ax, y, h) => {
  const pps = (ax.w - ax.left - ax.right) / ((ax.t1 - ax.t0) || 1);
  const step = [300, 600, 900, 1200, 1800, 3600, 7200].find((s) => s * pps >= 56) || 7200;
  let out = "";
  for (let t = Math.ceil(ax.t0 / step) * step; t <= ax.t1; t += step) {
    const x = ax.x(t);
    out += `<line x1="${x}" x2="${x}" y1="${y - h}" y2="${y}" stroke="rgba(100,116,139,.18)"/>`;
    out += `<text x="${x}" y="${y + 13}" text-anchor="middle">${LK.fmtT(t)}</text>`;
  }
  return out;
};

LK.nowLine = (ax, y0, h) => {
  const S = LK.S;
  if (S.mode === "preview") return "";
  const t = LK.cursor();
  if (S.mode === "past" && t >= S.t1) return "";
  const x = ax.x(t);
  return `<line x1="${x}" x2="${x}" y1="${y0}" y2="${y0 + h}" stroke="var(--mint)" stroke-width="1.5" stroke-dasharray="3 3"/>`;
};

LK.series = (load, metric) => (LK.S.m[load] && LK.S.m[load][metric]) || [];

/* stacked areas: loads -> [[t,v]], all sampled on the same t grid */
LK.stacked = (ax, loads, metric, y0, h, vmax, colorOf = LK.color) => {
  const grid = new Map();
  loads.forEach((l) => LK.series(l, metric).forEach(([t, v]) => {
    if (!grid.has(t)) grid.set(t, {});
    grid.get(t)[l] = v;
  }));
  const ts = [...grid.keys()].sort((a, b) => a - b);
  if (!ts.length) return { svg: "", totals: [] };
  const y = (v) => y0 + h - (Math.min(v, vmax * 1.15) / vmax) * h;
  let base = ts.map(() => 0), svg = "";
  loads.forEach((l) => {
    const top = ts.map((t, i) => base[i] + (grid.get(t)[l] || 0));
    let d = ts.map((t, i) => `${i ? "L" : "M"}${ax.x(t).toFixed(1)},${y(top[i]).toFixed(1)}`).join("");
    for (let i = ts.length - 1; i >= 0; i--) d += `L${ax.x(ts[i]).toFixed(1)},${y(base[i]).toFixed(1)}`;
    svg += `<path d="${d}Z" fill="${colorOf(l)}" fill-opacity=".75" stroke="${colorOf(l)}" stroke-width=".6"><title>${esc(l)}</title></path>`;
    base = top;
  });
  return { svg, totals: ts.map((t, i) => [t, base[i]]), y };
};

LK.line = (ax, pts, y, color, width = 1.6, dash = "") => {
  if (!pts.length) return "";
  const d = pts.map(([t, v], i) => `${i ? "L" : "M"}${ax.x(t).toFixed(1)},${y(v).toFixed(1)}`).join("");
  return `<path d="${d}" fill="none" stroke="${color}" stroke-width="${width}" ${dash ? `stroke-dasharray="${dash}"` : ""}/>`;
};

LK.legend = (items) => `<div class="legend">${items.map(([c, n, shape]) =>
  `<span><i class="chip" style="background:${c};${shape === "line" ? "height:3px;" : ""}"></i>${esc(n)}</span>`).join("")}</div>`;

LK.plainEvent = (e) => {
  const c = e.cause || {};
  switch (e.event) {
    case "job_started": return `${e.load} run ${e.run} started (${e.units} units, normally ${Math.round(e.normal_duration / 60)} min)`;
    case "job_ended": return `${e.load} run ${e.run} finished in ${Math.round(e.actual_duration / 60)} min (${e.ratio}× normal)`;
    case "run_overlapped": return `${e.load} run ${e.run} started while ${e.active_runs - 1} earlier run(s) were still working`;
    case "run_skipped": return `${e.load} run ${e.run} skipped: the previous run was still working`;
    case "run_replaced": return `${e.load} run ${e.run} stopped and replaced by run ${e.by_run}`;
    case "run_queued": return `${e.load} run ${e.run} waits in the queue (too many runs at once)`;
    case "consumers_scaled": return `${e.load} scaled ${e.from} → ${e.to} consumer pods`;
    case "shop_scaled": return `${e.load} scaled ${e.from} → ${e.to} shop pods`;
    case "consumers_idle": return `${e.load}: ${e.idle} consumer pod(s) idle — only ${c.partitions} partitions`;
    case "pod_preempted": return `${e.load} pod pushed out by ${c.by} (priority ${c.by_priority})`;
    case "pod_evicted": return `${e.load} pod evicted: the machine ran out of memory`;
    case "oom_killed": return `${e.load} pod killed: used more memory than its limit`;
    case "pod_pending": return `${e.load} pod waiting: ${c.reason}`;
    case "pod_restarted": return `${e.load} pod running again`;
    case "pod_lost": return `${e.load} pod stopped reporting`;
    case "node_memory_pressure": return e.state === "on" ? "the machine is low on memory" : "the machine has enough memory again";
    case "health_degraded": return `${e.load} became unhealthy`;
    case "health_recovered": return `${e.load} is healthy again (after ${LK.mins(e.duration_s)})`;
    case "job_timed_out": return `${e.load} run ${e.run} stopped: took too long (${e.progress} done)`;
    case "job_due": return `${e.load} run ${e.run} is due (${e.units} units)`;
    case "burst_started": return `${e.load} burst: ${e.x}× more events`;
    case "burst_ended": return `${e.load} burst over`;
    case "users_changed": return `${e.load}: ${e.users} users`;
    case "rate_changed": return `${e.load}: ${e.rate} events/s`;
    case "phase_started": return { scenario: "the scenario starts", drain: "the scenario is over: letting running work finish",
      warmup: "warming up", baseline: "measuring what normal looks like" }[e.name] || `phase: ${e.name}`;
    case "run_ended": return "run ended, everything cleaned up";
    default: return `${e.load ? e.load + ": " : ""}${e.event.replace(/_/g, " ")}`;
  }
};

// ------------------------------------------------------------------ state building
function emptyState(mode) {
  return { mode, m: {}, h: {}, diary: [], loads: [], types: {}, prio: {}, teams: {}, phases: {}, t0: -420, t1: 7200,
           now: null, cursor: null, impact: null, node: { cpu: 8, memory: 32 * 1024 ** 3 }, timeline: null, scenario: null };
}
function addMetrics(S, rows) { rows.forEach((r) => ((S.m[r.load] ||= {})[r.metric] ||= []).push([r.t, r.value])); }
function addHealth(S, rows) { rows.forEach((r) => (S.h[r.load] ||= []).push(r)); }
function setLoads(S, scenario, loadNames) {
  S.scenario = scenario;
  const sl = scenario ? scenario.loads : [];
  S.loads = loadNames || sl.map((l) => l.name);
  sl.forEach((l) => { S.types[l.name] = l.type; S.prio[l.name] = l.priority; S.teams[l.name] = l.team; });
}
function domain(S, duration, drain) {
  const st = Object.values(S.phases).map((p) => p.start_t);
  S.t0 = st.length ? Math.min(...st) : -420;
  S.t1 = Math.max(duration + (drain || 0), S.phases.teardown ? S.phases.teardown.start_t : 0);
}

// ------------------------------------------------------------------ live polling
let live = { runId: null, diaryNext: 0, lastT: -1e18, timer: null };

async function poll() {
  let cur;
  try { cur = await LK.api("/api/runs/current"); } catch (e) { return; }
  setStatus(cur);
  if (cur.status === "idle") return;
  if (LK.S?.mode === "past" && LK.S.viewing !== cur.run_id) return;   // looking at an old run
  if (live.runId !== cur.run_id) {
    live = { ...live, runId: cur.run_id, diaryNext: 0, lastT: -1e18 };
    LK.S = emptyState("live");
    LK.S.viewing = cur.run_id;
  }
  const S = LK.S;
  setLoads(S, cur.scenario, cur.loads);
  Object.assign(S, { timeline: cur.timeline, node: cur.node, phases: cur.phases, status: cur, now: cur.t });
  domain(S, cur.duration, cur.drain);
  const d = await LK.api(`/api/runs/current/diary?since=${live.diaryNext}`);
  S.diary.push(...d.events);
  live.diaryNext = d.next;
  const m = await LK.api(`/api/runs/current/metrics?since=${live.lastT}`);
  addMetrics(S, m.metrics);
  addHealth(S, m.health);
  const ts = m.metrics.map((r) => r.t).concat(m.health.map((r) => r.t));
  if (ts.length) live.lastT = Math.max(live.lastT, ...ts);
  if (cur.status !== "running" && cur.status !== "created") {
    clearInterval(live.timer); live.timer = null;
    if (cur.status === "failed") { showBanner(`<div class="err">${esc(cur.error || "the run failed")}</div>`, true); renderAll(); return; }
    await openPast(cur.run_id, true);
    return;
  }
  $("replay").style.display = "none";
  renderAll();
}

function setStatus(cur) {
  LK.cur = cur;
  const running = cur.status === "running" || cur.status === "created";
  $("statusDot").className = "dot" + (running ? " run" : cur.status === "failed" ? " bad" : "");
  $("statusText").textContent = cur.status === "idle" ? "idle" :
    cur.status === "created" ? "starting · setting up" : `${cur.status} · ${cur.phase} · ${LK.fmtT(cur.t)}`;
  syncStartBtn();
  $("stopSoft").disabled = !running;
  $("stopHard").disabled = !running;
}

function syncStartBtn() {
  const running = LK.cur && (LK.cur.status === "running" || LK.cur.status === "created");
  $("startBtn").disabled = running || !LK.clusterOk;
}

function showBanner(html, bad = false) {
  const b = $("banner");
  b.style.display = html ? "" : "none";
  b.className = "banner" + (bad ? " bad" : "");
  b.innerHTML = html || "";
}

// ------------------------------------------------------------------ past runs + replay
async function openPast(runId, justFinished = false) {
  const r = await LK.api(`/api/runs/${encodeURIComponent(runId)}`);
  const S = emptyState("past");
  S.viewing = runId;
  S.run = r.run;
  setLoads(S, r.scenario, null);
  if (!S.loads.length) S.loads = r.run.loads || [...new Set(r.metrics.map((x) => x.load).filter((l) => l !== "_node"))];
  Object.assign(S, { phases: r.run.phases, diary: r.diary, timeline: r.timeline, impact: r.impact,
                     node: { cpu: r.run.node.cpu, memory: r.run.node.memory }, folder: r.folder });
  addMetrics(S, r.metrics);
  addHealth(S, r.health);
  domain(S, r.run.duration_s, 0);
  S.cursor = S.t1;
  LK.S = S;
  if (r.scenario) showScenario(r.scenario, false);
  showBanner(`${justFinished ? "Run finished. " : "Past run: "}<b>${esc(runId)}</b> · seed ${r.run.seed}` +
    `${r.run.note ? ` · ${esc(r.run.note)}` : ""}. Drag the slider to replay it.`);
  const rt = $("replayT");
  rt.min = Math.floor(S.t0); rt.max = Math.ceil(S.t1); rt.value = rt.max;
  $("replay").style.display = "";
  $("replayClock").textContent = "end of run";
  await loadPastList();
  $("pastRuns").value = runId;
  renderAll();
  const m = /[#&]t=(-?\d+)/.exec(window.location.hash);          // link to a moment: ...#t=1300
  if (m) setCursor(Number(m[1]));
}

let replayTimer = null;
function setCursor(t) {
  if (!LK.S || LK.S.mode !== "past") return;
  LK.S.cursor = Math.min(t, LK.S.t1);
  $("replayT").value = LK.S.cursor;
  $("replayClock").textContent = LK.S.cursor >= LK.S.t1 ? "end of run" : LK.fmtT(LK.S.cursor);
  renderAll(true);
}

async function loadPastList() {
  const runs = await LK.api("/api/runs");
  LK.pastRuns = runs;
  const sel = $("pastRuns"), v = sel.value;
  sel.innerHTML = `<option value="">Open a past run</option>` + runs.map((r) =>
    `<option value="${esc(r.run_id)}">${esc(r.scenario)} · s${r.seed} · ${esc((r.started_utc || "").slice(5, 16).replace("T", " "))}${r.note ? " · " + esc(r.note) : ""}</option>`).join("");
  sel.value = v;
}

// ------------------------------------------------------------------ scenarios
function showScenario(sc, setSeed = true) {
  LK.selected = sc.name;
  $("scTitle").textContent = sc.name;
  $("scDesc").textContent = sc.description || "";
  $("scStory").textContent = sc.story || "";
  $("scLook").innerHTML = (sc.look_for || []).map((x) => `<li>${esc(x)}</li>`).join("");
  if (setSeed) $("seed").value = sc.seed;
  const scSel = $("scenarios");
  if (scSel && scSel.value !== sc.name) scSel.value = sc.name;
  const keep = LK.S;
  LK.S = { types: {}, loads: sc.loads.map((l) => l.name) };
  sc.loads.forEach((l) => (LK.S.types[l.name] = l.type));
  $("loads").innerHTML = sc.loads.map((l) => `<div class="load-row"><i class="chip" style="background:${LK.color(l.name)}"></i>
     <span>${esc(l.name)}</span><span class="muted" style="font-size:11px">${l.type}</span>
     <span class="spacer"></span><span class="badge ${l.priority}">${l.priority}</span></div>`).join("") +
    `<div class="muted" style="font-size:12px;margin-top:6px">needs a node of ${sc.capacity.cpu} cores · ${LK.fmtMem(sc.capacity.memory)}` +
    `${sc.preemption ? " · preemption on" : ""}</div>`;
  LK.S = keep;
}

async function loadScenarios() {
  LK.scenarios = await LK.api("/api/scenarios");
  const opt = (s) => `<option value="${esc(s.name)}">${esc(s.name)} · ${Math.round(s.duration / 60)} min</option>`;
  const teach = LK.scenarios.filter((s) => s.set === "teaching"), full = LK.scenarios.filter((s) => s.set !== "teaching");
  const sel = $("scenarios");
  sel.innerHTML =
    (teach.length ? `<optgroup label="Short · for learning">${teach.map(opt).join("")}</optgroup>` : "") +
    (full.length ? `<optgroup label="Full length">${full.map(opt).join("")}</optgroup>` : "");
  sel.onchange = () => {
    if (window.__SNAP) return;
    const sc = LK.scenarios.find((s) => s.name === sel.value);
    showScenario(sc);
    if (!(LK.S?.mode === "live" && LK.S?.status?.status === "running")) previewTimeline(sc.name);
  };
}

async function previewTimeline(name) {
  const sc = LK.scenarios.find((s) => s.name === name);
  const tl = await LK.api("/api/timeline", { scenario: name, seed: Number($("seed").value) });
  const S = emptyState("preview");
  setLoads(S, sc, null);
  Object.assign(S, { timeline: tl, node: sc.capacity, t0: 0, t1: sc.duration });
  LK.S = S;
  $("replay").style.display = "none";
  const k = (LK.info.runners || []).find((r) => r.name === LK.runnerName);
  showBanner(LK.clusterOk ? `Nothing is running yet. Press <b>Start</b> to run this scenario ` +
    (k && !k.realtime ? `on the <b>simulated node</b> — it finishes in seconds.` :
      `on the cluster (it takes ${Math.round((sc.duration + 420) / 60)} min plus a short drain).`) : "");
  renderAll();
}

// ------------------------------------------------------------------ panels
function makePanel(id, box) {
  const p = LK.panels[id];
  const el = document.createElement("section");
  el.className = "panel" + (LK.wide.has(id) ? " wide" : "");
  el.innerHTML = `<h2>${esc(p.title)}${p.hint ? ` <span class="hint">${esc(p.hint)}</span>` : ""}</h2><div class="body"></div>` +
    (p.teach ? `<div class="teach">${esc(p.teach)}</div>` : "");
  el.dataset.id = id;
  box.appendChild(el);
  if (p.setup) p.setup(el.querySelector(".body"));
}

function buildPanels() {
  const enabled = LK.info.panels;
  $("mainPanels").innerHTML = "";
  $("advPanels").innerHTML = "";
  LK.main.filter((id) => enabled.includes(id) && LK.panels[id]).forEach((id) => makePanel(id, $("mainPanels")));
  const adv = LK.advanced.filter((id) => enabled.includes(id) && LK.panels[id]);
  adv.forEach((id) => makePanel(id, $("advPanels")));
  $("advanced").style.display = adv.length ? "" : "none";
}

function renderAll(fast = false) {
  if (!LK.S) return;
  const sel = LK.advOpen ? ".panel" : "#mainPanels .panel";
  document.querySelectorAll(sel).forEach((el) => {
    const p = LK.panels[el.dataset.id];
    if (fast && p.slow) return;
    try { p.render(el.querySelector(".body"), LK.S); }
    catch (e) { el.querySelector(".body").innerHTML = `<div class="err">${esc(e.stack || e)}</div>`; }
  });
}
LK.renderAll = renderAll;
LK.openPast = openPast;

LK.startRun = async (extra = {}) => {
  if (!LK.selected) return showBanner("Choose a scenario first.");
  try {
    showBanner("");
    LK.S = null;
    live.runId = null;
    await LK.api("/api/runs", { scenario: LK.selected, seed: Number($("seed").value), runner: LK.runnerName, ...extra });
    $("pastRuns").value = "";
    if (!live.timer) live.timer = setInterval(poll, 1000);
    poll();
  } catch (e) { showBanner(`<span class="err">${esc(e.message)}</span>`, true); }
};

function loadScript(src) {
  return new Promise((ok) => { const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = ok; document.head.appendChild(s); });
}

// ------------------------------------------------------------------ boot
async function boot() {
  LK.info = await LK.api("/api/info");
  const runners = LK.info.runners || [];
  const firstOk = runners.find((r) => r.available);
  LK.runnerName = (firstOk || runners[0] || { name: "kubernetes" }).name;
  const sel = $("runnerSel");
  if (runners.length > 1) {
    sel.style.display = "";
    sel.innerHTML = runners.map((r) =>
      `<option value="${esc(r.name)}">${esc(r.label)}${r.available ? "" : " — not available"}</option>`).join("");
    sel.value = LK.runnerName;
    sel.onchange = () => { LK.runnerName = sel.value; LK.updatePill(); syncStartBtn(); };
  }
  LK.updatePill = () => {
    const k = runners.find((r) => r.name === LK.runnerName) ||
      { available: false, reason: "no runner enabled in loadkit.yaml" };
    LK.clusterOk = !!k.available;
    const pill = $("cluster");
    pill.className = "pill" + (k.available ? "" : " bad");
    pill.innerHTML = `<span class="dot ${k.available ? "run" : "bad"}"></span>${esc(k.available ? k.label : "No cluster")}` +
      (LK.info.group ? ` · group ${esc(LK.info.group)}` : "");
    pill.title = k.reason || "";
  };
  LK.updatePill();
  if (!window.__SNAP) {
    await Promise.all(LK.info.panels.map((p) => loadScript(`/panels/${p}.js`)));
  }
  buildPanels();
  await loadScenarios();
  await loadPastList();
  window.addEventListener("resize", () => renderAll());

  $("advHead").onclick = () => {
    LK.advOpen = !LK.advOpen;
    $("advanced").classList.toggle("open", LK.advOpen);
    renderAll();
  };
  $("startBtn").onclick = () => LK.startRun();
  $("stopSoft").onclick = () => LK.api("/api/runs/current/stop", { mode: "soft" });
  $("stopHard").onclick = () => LK.api("/api/runs/current/stop", { mode: "hard" });
  $("pastRuns").onchange = (e) => { if (e.target.value) openPast(e.target.value); };
  $("replayT").oninput = (e) => setCursor(Number(e.target.value));
  $("exportBtn").onclick = () => {
    if (LK.S?.mode === "past" && LK.S.viewing) window.location = `/api/runs/${encodeURIComponent(LK.S.viewing)}/export.xlsx`;
  };
  if (window.__SNAP) $("exportBtn").style.display = "none";
  $("replayPlay").onclick = () => {
    if (replayTimer) { clearInterval(replayTimer); replayTimer = null; $("replayPlay").textContent = "▶ Replay"; return; }
    const S = LK.S;
    if (!S || S.mode !== "past") return;
    if (S.cursor >= S.t1) setCursor(S.t0);
    $("replayPlay").textContent = "❚❚ Pause";
    const step = (S.t1 - S.t0) / 240;               // whole run in about 24 s
    replayTimer = setInterval(() => {
      setCursor(LK.S.cursor + step);
      if (LK.S.cursor >= LK.S.t1) { clearInterval(replayTimer); replayTimer = null; $("replayPlay").textContent = "▶ Replay"; renderAll(); }
    }, 100);
  };

  if (window.__SNAP) {                       // static snapshot (python -m loadkit snapshot): no server
    ["startBtn", "stopSoft", "stopHard", "seed"].forEach((id) => ($(id).disabled = true));
    $("scenarios").disabled = true;
    $("runnerSel").style.display = "none";
    return openPast(window.__SNAP.open);
  }
  if (!LK.clusterOk) {
    const k = (LK.info.runners || []).find((r) => r.name === LK.runnerName) || {};
    showBanner(`<b>No machine available for live runs.</b> ${esc(k.reason || "")} Start the kit with ` +
      `<code>make ui KUBECONFIG=path/to/kubeconfig</code>, or create a local cluster with <code>make kind-up</code>. ` +
      `Past runs can still be opened.`, true);
  }
  const cur = await LK.api("/api/runs/current");
  setStatus(cur);
  if (cur.status === "running" || cur.status === "created") {
    live.timer = setInterval(poll, 1000); poll();
    if (cur.scenario) showScenario(cur.scenario, false);
  } else if (LK.scenarios.length) {
    const first = LK.scenarios.find((s) => s.name === "normal-day-short") || LK.scenarios[0];
    showScenario(first);
    previewTimeline(first.name);
  }
}
const go = () => boot().catch((e) => showBanner(`<div class="err">${esc(e.stack || e)}</div>`, true));
if (window.__SNAP) window.addEventListener("load", go); else go();   // snapshot: wait for the inlined panels
