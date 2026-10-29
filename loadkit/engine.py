"""Engine: drives the selected runner along the planned timeline.

Two loops on one simulated clock:
  * schedule loop (every tick)          — plays planned events (job_due, bursts, rate/user changes), applies each batch
                                          job's concurrency policy, starts queued runs, enforces deadlines.
  * control loop (every decision step)  — scalers decide replicas, health of EVERY load is checked (with hysteresis),
                                          metrics are recorded.

Phases: preflight -> setup -> warmup -> baseline -> scenario -> drain -> teardown (all logged in the diary).
Shared clock: t = seconds since the `scenario` phase started (negative during warm-up and baseline).
"""
import collections
import datetime as dt
import json
import shutil
import threading
import time

import yaml

from . import FORMAT_VERSION, __version__, units
from .diary import Diary
from .generator import make_timeline, sub_rng
from .patterns import factor
from .settings import runs_dir

PRIO = {"high": 3, "normal": 2, "low": 1}
KILL_EVENTS = ("oom_killed", "pod_evicted", "pod_preempted")
QUIET_FOR_CONTEXT = ("health_degraded", "health_recovered", "job_progress", "rate_changed", "phase_started")
DEFAULT_CAUSE = {"job_ended": "all work units done", "pod_restarted": "pod recreated after a kill",
                 "node_memory_pressure": "node memory above allocatable"}


def _fmt_progress(info):
    return f"{int(info['done'])}/{int(info['units'])}"


class Engine:
    def __init__(self, sc, settings, plugins, seed=None, runner="kubernetes", record=True, out_dir=None, note="",
                 runner_options=None):
        self.sc, self.settings, self.plugins = sc, settings, plugins
        from .scenario import blocked_flags
        blocked = blocked_flags(sc, settings)          # e.g. memory scenarios on a student installation
        if blocked:
            raise PermissionError(f"scenario '{sc['name']}' needs {', '.join(blocked)}, which is not allowed in "
                                  f"loadkit.yaml (allow_flags)")
        self.seed = sc["seed"] if seed is None else int(seed)
        self.timeline = make_timeline(sc, plugins, self.seed)
        self.runner_name = runner
        self.runner = plugins["runners"][runner](sc, sub_rng(self.seed, "_runner"), settings=settings,
                                                 **(runner_options or {}))
        eng = settings["engine"]
        self.tick = float(eng["tick"])
        self.decision = float(eng["decision_interval"])
        self.metrics_every = float(eng["metrics_interval"])
        ph = {**settings["phases"], **(sc.get("phases") or {})}
        self.len = {k: units.duration(ph[k]) for k in ("warmup", "baseline", "drain")}
        self.scenario_start = self.len["warmup"] + self.len["baseline"]
        self.started_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        stamp = self.started_utc.strftime("%Y%m%dT%H%M%S")
        self.run_id = f"{sc['name']}-s{self.seed}-{runner}-{stamp}"
        self.note = note
        self.loads = {ld["name"]: ld for ld in sc["loads"]}

        # run state
        self.clock = 0.0
        self.phase = "preflight"
        self.phase_times = {}
        self.status = "created"
        self.stop_mode = None
        self._ev_i = 0
        self.runs = {}            # runner key -> record
        self.queue = []           # waiting batch runs
        self.pattern = {}         # stream load -> current pattern factor
        self.burst = {}           # stream load -> burst multiplier
        self.scaler_state = {n: {} for n in self.loads}
        self.health = {n: {"bad": 0, "good": 0, "open": None, "last": None} for n in self.loads}
        self.periods = []         # closed and open degraded periods
        self.kills_since_eval = collections.Counter()
        self.ended_since_eval = collections.defaultdict(list)
        self.flags_since_eval = collections.defaultdict(list)
        self.user_samples = {n: collections.deque() for n, ld in self.loads.items() if ld["type"] == "users"}
        self.metric_rows = []
        self.health_rows = []
        self._acc = collections.defaultdict(lambda: collections.defaultdict(list))
        self._next_decision = 0.0
        self._next_metrics = self.metrics_every
        self.counters = collections.Counter()
        self.peak_replicas = collections.Counter()
        self.lock = threading.RLock()

        # run folder
        self.dir = None
        if record:
            base = out_dir or runs_dir(settings)
            rid, k = self.run_id, 1
            while (base / rid).exists():          # never overwrite a run folder
                k += 1
                rid = f"{self.run_id}-{k}"
            self.run_id = rid
            self.dir = base / rid
            self.dir.mkdir(parents=True)
        self.diary = Diary(self.dir / "diary.jsonl" if self.dir else None, self.run_id)
        self._mfh = self._hfh = None

    # ------------------------------------------------------------------ helpers
    @property
    def t(self):
        return self.clock - self.scenario_start

    def ts(self, clock=None):
        c = self.clock if clock is None else clock
        return (self.started_utc + dt.timedelta(seconds=c)).isoformat().replace("+00:00", "Z")

    def write(self, event, origin, load=None, **fields):
        if origin == "reactive" and "cause" not in fields:
            fields["cause"] = {"reason": DEFAULT_CAUSE.get(event, event.replace("_", " "))}
        fields["ts"] = self.ts()
        fields["phase"] = self.phase
        rec = self.diary.write(self.t, event, origin, load, **fields)
        self.counters[event] += 1
        return rec

    def _set_phase(self, name):
        self.phase = name
        self.phase_times[name] = {"start_t": round(self.t, 3), "start_ts": self.ts()}
        self.write("phase_started", "control" if name in ("preflight", "teardown") else "planned", name=name)

    # ------------------------------------------------------------------ start / stop
    def start(self):
        self.status = "running"
        if self.dir:
            self._write_static_files()
            self._mfh = open(self.dir / "metrics.csv", "w", encoding="utf-8", newline="")
            self._mfh.write("t,ts,phase,load,metric,value\n")
            self._hfh = open(self.dir / "health.csv", "w", encoding="utf-8", newline="")
            self._hfh.write("t,ts,phase,load,metric,value,threshold,healthy\n")
        self.write("run_started", "planned", scenario=self.sc["name"], seed=self.seed, runner=self.runner_name,
                   scenario_hash=self.sc.get("_hash"))
        self._set_phase("preflight")
        self._preflight()
        self._set_phase("setup")
        self.runner.setup()
        for name, ld in self.loads.items():
            if ld["type"] == "stream":
                self.pattern[name] = factor(ld["daily_pattern"], 0.0, self.sc["day_length"], self.sc["day_start_hour"])
                self.burst[name] = 1.0
            if ld["type"] in ("stream", "users"):
                self.runner.setup_service(name)
        self._apply_service_levels()
        self._set_phase("warmup" if self.len["warmup"] > 0 else "baseline")

    def _preflight(self):
        caps = self.settings["caps"]
        need = sum(ld["intensity"] * ld["parallelism"] for ld in self.sc["loads"] if ld["type"] == "batch")
        problems = list(self.runner.preflight())
        if self.sc["capacity"]["cpu"] > float(caps["max_total_cores"]):
            problems.append(f"capacity_hint.cpu {self.sc['capacity']['cpu']} > max_total_cores {caps['max_total_cores']}")
        if self.sc["capacity"]["memory"] > units.memory(caps["max_total_memory"]):
            problems.append("capacity_hint.memory above max_total_memory")
        self.write("preflight_checked", "control", ok=not problems, problems=problems,
                   runner_info=getattr(self.runner, "info", {}),
                   batch_cores_if_all_overlap=round(need, 2), node_cpu=self.sc["capacity"]["cpu"])
        if problems:
            raise RuntimeError("preflight failed: " + "; ".join(problems))

    def request_stop(self, mode="soft"):
        """soft: jump to drain (running batch runs may finish); hard: teardown now."""
        with self.lock:
            self.stop_mode = mode
            if mode == "soft" and self.phase in ("warmup", "baseline", "scenario"):
                self.write("stop_requested", "control", mode="soft")
                self._set_phase("drain")
                self._drain_started = self.clock
                for n in self.burst:
                    self.burst[n] = 1.0
                self.queue = []

    # ------------------------------------------------------------------ main loop
    def set_speed(self, speed):
        # a real cluster cannot be fast-forwarded: real-time runners always run at 1x
        self.speed = 1.0 if getattr(self.runner, "realtime", True) else float(speed or 0.0)
        self._wall0, self._sim0 = time.monotonic(), self.clock

    def run(self, speed=0.0):
        """speed 0 = as fast as possible; otherwise simulated seconds per real second (e.g. 20). Can be changed live
        with set_speed()."""
        if self.status == "created":
            self.start()
        self.set_speed(speed)
        while self.status == "running":
            with self.lock:
                self.step()
            self.pace()
        return self

    def pace(self):
        """Wait until the wall clock has caught up with the engine clock (real-time runners: 1 engine second = 1 real
        second). Sleeps in short pieces so a stop request is seen quickly, but ALWAYS until it has caught up.
        (Bug fixed 29 Oct 2026: it used to sleep at most 0.5 s per 1 s tick, so the engine ran about 1.9x too fast.)"""
        if self.speed <= 0:
            return
        while self.status == "running" and self.stop_mode != "hard":
            lag = (self.clock - self._sim0) / self.speed - (time.monotonic() - self._wall0)
            if lag <= 0:
                return
            time.sleep(min(lag, 0.5))

    def step(self):
        if self.stop_mode == "hard":
            return self.finish("stopped")
        self._advance_phase()
        if self.status != "running":
            return
        if self.phase == "scenario":
            self._play_planned()
        self._start_queued()
        self._apply_service_levels()
        for ev in self.runner.step(self.clock, self.tick):
            self._on_runner_event(ev)
        self._watch_runs()
        self._sample()
        if self.clock >= self._next_decision:
            self._control_loop()
            self._next_decision += self.decision
        if self.clock >= self._next_metrics:
            self._flush_metrics()
            self._next_metrics += self.metrics_every
        self.clock += self.tick

    def _advance_phase(self):
        w, b, d = self.len["warmup"], self.len["baseline"], self.sc["duration"]
        if self.phase == "warmup" and self.clock >= w:
            self._set_phase("baseline")
        if self.phase == "baseline" and self.clock >= w + b:
            self._set_phase("scenario")
        if self.phase == "scenario" and self.t >= d:
            self._set_phase("drain")
            self._drain_started = self.clock
            for n in self.burst:
                self.burst[n] = 1.0
            if self.queue:
                for q in self.queue:
                    self.write("run_skipped", "reactive", q["load"], run=q["run"],
                               cause={"reason": "scenario ended while the run was still queued"})
                self.queue = []
        if self.phase == "drain":
            elapsed = self.clock - self._drain_started
            busy = bool(self.runner.active_runs()) or any(
                self.runner.backlog(n) > 1 for n, ld in self.loads.items() if ld["type"] == "stream")
            if elapsed >= self.len["drain"] or (not busy and elapsed >= min(60.0, self.len["drain"])):
                for key in self.runner.active_runs():
                    info = self.runner.run_info(key)
                    self.runner.stop_run(key, "drain_limit")
                    self.write("job_timed_out", "reactive", self.runs[key]["load"], run=self.runs[key]["run"],
                               progress=_fmt_progress(info),
                               cause={"reason": "drain time limit reached", "drain_s": self.len["drain"]})
                self.finish("completed" if self.stop_mode is None else "stopped")

    # ------------------------------------------------------------------ schedule loop
    def _play_planned(self):
        evs = self.timeline["events"]
        while self._ev_i < len(evs) and evs[self._ev_i]["t"] <= self.t + 1e-9:
            ev = evs[self._ev_i]
            self._ev_i += 1
            k, name, v = ev["kind"], ev["load"], ev["value"]
            if k == "job_due":
                self._job_due(name, v, ev["t"])
            elif k == "burst_start":
                self.burst[name] = v["x"]
                self.write("burst_started", "planned", name, planned_t=ev["t"], x=v["x"])
            elif k == "burst_end":
                self.burst[name] = 1.0
                self.write("burst_ended", "planned", name, planned_t=ev["t"])
            elif k == "rate_change":
                self.pattern[name] = v["factor"]
                self.write("rate_changed", "planned", name, planned_t=ev["t"], factor=v["factor"],
                           rate=round(self._stream_rate(name), 2))
            elif k == "users_change":
                self.runner.set_users(name, v["users"])
                self.write("users_changed", "planned", name, planned_t=ev["t"], users=v["users"])

    def _stream_rate(self, name):
        return self.loads[name]["base_rate"] * self.pattern.get(name, 1.0) * self.burst.get(name, 1.0)

    def _apply_service_levels(self):
        for name, ld in self.loads.items():
            if ld["type"] == "stream":
                self.runner.set_rate(name, self._stream_rate(name))
            elif ld["type"] == "users" and self.phase in ("setup", "warmup", "baseline"):
                f = factor(ld["daily_pattern"], 0.0, self.sc["day_length"], self.sc["day_start_hour"])
                self.runner.set_users(name, max(1, round(ld["users"] * f)))

    def _job_due(self, name, v, planned_t):
        ld = self.loads[name]
        self.write("job_due", "planned", name, planned_t=planned_t, run=v["run"], units=v["units"],
                   normal_duration=v["normal_duration"])
        active = self.runner.active_runs(name)
        pol = ld["concurrency_policy"]
        if active:
            prev = [_fmt_progress(self.runner.run_info(k)) for k in active]
            if pol == "forbid":
                self.write("run_skipped", "reactive", name, run=v["run"],
                           cause={"policy": "forbid", "previous_run_progress": prev})
                return
            if pol == "replace":
                for k in active:
                    self.runner.stop_run(k, "replaced")
                    self.write("run_replaced", "reactive", name, run=self.runs[k]["run"], by_run=v["run"],
                               cause={"policy": "replace", "progress_lost": _fmt_progress(self.runner.run_info(k))})
        q = {"load": name, "run": v["run"], "units": v["units"], "planned_t": planned_t, "due_t": self.t,
             "prio": PRIO[ld["priority"]]}
        mcr = ld.get("max_concurrent_runs")
        if mcr and len(self.runner.active_runs(name)) >= mcr:
            self.queue.append(q)
            self.write("run_queued", "reactive", name, run=v["run"],
                       cause={"max_concurrent_runs": mcr, "active_runs": len(self.runner.active_runs(name))})
            return
        self._start_run(q)

    def _start_run(self, q):
        name = q["load"]
        ld = self.loads[name]
        active_before = self.runner.active_runs(name)
        key = self.runner.start_run(name, q["run"], q["units"])
        info = self.runner.run_info(key)
        self.runs[key] = {"load": name, "run": q["run"], "planned_t": q["planned_t"], "start_t": self.t,
                          "normal": info["normal"], "deadline": ld["deadline_factor"] * info["normal"],
                          "milestone": 0}
        extra = {"waited_s": round(self.t - q["due_t"], 1)} if self.t - q["due_t"] > 0.5 else {}
        self.write("job_started", "planned", name, planned_t=q["planned_t"], run=q["run"], units=q["units"],
                   pods=ld["parallelism"], team=ld["team"], priority=ld["priority"],
                   normal_duration=round(info["normal"], 1), **extra)
        if active_before:
            self.write("run_overlapped", "reactive", name, run=q["run"], active_runs=len(active_before) + 1,
                       cause={"policy": ld["concurrency_policy"],
                              "previous_run_progress": [_fmt_progress(self.runner.run_info(k)) for k in active_before]})

    def _start_queued(self):
        if not self.queue:
            return
        self.queue.sort(key=lambda q: (-q["prio"], q["due_t"], q["load"]))
        left = []
        for q in self.queue:
            mcr = self.loads[q["load"]].get("max_concurrent_runs")
            if not mcr or len(self.runner.active_runs(q["load"])) < mcr:
                self._start_run(q)
            else:
                left.append(q)
        self.queue = left

    def _watch_runs(self):
        for key in self.runner.active_runs():
            r = self.runs.get(key)
            if not r:
                continue
            info = self.runner.run_info(key)
            frac = info["done"] / max(info["units"], 1)
            m = int(frac * 4)
            if 0 < m < 4 and m > r["milestone"]:
                r["milestone"] = m
                self.write("job_progress", "reactive", r["load"], run=r["run"], progress=_fmt_progress(info),
                           elapsed_s=round(info["elapsed"], 1), cause={"reason": f"sampled at {m * 25}%"})
            if info["elapsed"] > r["deadline"]:
                self.runner.stop_run(key, "timed_out")
                self.flags_since_eval[r["load"]].append("timed_out")
                self.write("job_timed_out", "reactive", r["load"], run=r["run"], progress=_fmt_progress(info),
                           cause={"deadline_s": round(r["deadline"], 1),
                                  "reason": "activeDeadlineSeconds = deadline_factor x normal duration"})

    def _on_runner_event(self, ev):
        ev = dict(ev)
        event, load = ev.pop("event"), ev.pop("load")
        if event == "job_ended":
            key = f"{load}#{ev['run']}"
            if key in self.runs:
                ev["planned_t"] = self.runs[key]["planned_t"]
            if ev.get("ratio") is not None:
                self.ended_since_eval[load].append(ev["ratio"])
        if event in KILL_EVENTS and load:
            self.kills_since_eval[load] += 1
            if ev.get("run"):
                ev["run"] = int(str(ev["run"]).split("#")[-1])
        self.write(event, "reactive", load, **ev)

    # ------------------------------------------------------------------ control loop
    def _control_loop(self):
        for name, ld in self.loads.items():
            if ld["type"] not in ("stream", "users"):
                continue
            cfg = dict(ld["scaler"])
            mn, mx = ld["consumers"] if ld["type"] == "stream" else ld["shop_replicas"]
            cfg.setdefault("min", mn)
            cfg.setdefault("max", mx)
            # CONNECTION 4 (plugins): the engine finds the scaler named in the scenario file on the list and calls it
            # through the agreed interface. It never knows WHICH scaler it is (target-tracking, step, a student's ...).
            plugin = self.plugins["scalers"][cfg["name"]]                 # e.g. StepScaler (from plugins/scalers/step.py)
            sig = self.runner.signals(name)                               # what is happening now (backlog, CPU, ...)
            cur = self.runner.replicas(name)                              # how many pods now
            dec = plugin.decide(self.clock, sig, cur, cfg, self.scaler_state[name])   # -> {"replicas": ..., "reason": ...}
            if dec["replicas"] != cur:
                self.runner.scale(name, dec["replicas"])
                ev = "consumers_scaled" if ld["type"] == "stream" else "shop_scaled"
                extra = {"in_flight": round(sig.get("in_flight", 0), 1),
                         "request_rate": round(sig.get("request_rate", 0), 1)} if ld["type"] == "users" else \
                    {"backlog": round(sig.get("backlog", 0))}
                self.write(ev, "reactive", name, **{"from": cur, "to": dec["replicas"]}, scaler=cfg["name"],
                           **extra, cause={"scaler": cfg["name"], "per_signal": dec["per_signal"],
                                           "winner": dec["winner"], "reason": dec["reason"]})
            self.peak_replicas[name] = max(self.peak_replicas[name], dec["replicas"])
        self._evaluate_health()

    def _health_value(self, name, ld):
        h = ld["health"]
        if ld["type"] == "users":
            win = h.get("window_s", 60.0)
            dq = self.user_samples[name]
            while dq and dq[0][0] < self.clock - win:
                dq.popleft()
            if not dq:
                return "latency_p95", 0.0, h.get("threshold_s", 0.3), True, {}
            p95 = sum(s[1] for s in dq) / len(dq)
            err = sum(s[2] for s in dq) / len(dq)
            thr = h.get("threshold_s", 0.3)
            emax = h.get("errors_max_frac", 0.01)
            ok = p95 <= thr and err <= emax
            return "latency_p95", p95, thr, ok, {"errors": round(err, 4)}
        if ld["type"] == "stream":
            age = self.runner.event_age(name)
            thr = h.get("threshold_s", 30.0)
            return "event_age", age, thr, age <= thr, {}
        # batch
        thr = float(h.get("threshold", 1.5))
        vals = list(self.ended_since_eval.pop(name, []))
        active = self.runner.active_runs(name)
        for k in active:
            info = self.runner.run_info(k)
            if info["normal"]:
                vals.append(info["elapsed"] / info["normal"])
        v = max(vals) if vals else 0.0
        flags = self.flags_since_eval.pop(name, [])
        if len(active) > 1:
            flags.append("overlapped")
        if active and (self.runner.snapshot().get(name, {}).get("pending") or 0) > 0:
            flags.append("pods waiting")
        return "runtime_ratio", v, thr, v <= thr and not flags, {"flags": flags} if flags else {}

    def _evaluate_health(self):
        counted = self.phase in ("scenario", "drain")
        for name, ld in self.loads.items():
            metric, val, thr, ok, extra = self._health_value(name, ld)
            kills = self.kills_since_eval.pop(name, 0)
            if kills:
                ok = False
                extra["kills"] = kills
            self._health_row(name, metric, val, thr, ok)
            st = self.health[name]
            st["last"] = {"metric": metric, "value": val, "threshold": thr, "healthy": ok, **extra}
            if not counted:
                st["bad"] = st["good"] = 0
                continue
            sev = max(0.0, val / thr - 1.0) if thr else 0.0
            if not ok:
                st["bad"] += 1
                st["good"] = 0
                if st["open"] is None and st["bad"] >= 2:
                    st["open"] = {"load": name, "metric": metric, "start_t": round(self.t - self.decision, 1),
                                  "peak": val, "threshold": thr, "severity": 0.0, "reasons": set()}
                    self.periods.append(st["open"])
                    self.write("health_degraded", "reactive", name, metric=metric, value=round(val, 3),
                               threshold=thr, **{k: v for k, v in extra.items()},
                               active_events=self._context(),
                               cause={"reason": "2 unhealthy checks in a row (facts only; causes are computed "
                                                "after the run)"})
                if st["open"] is not None:
                    o = st["open"]
                    o["peak"] = max(o["peak"], val)
                    o["severity"] += (sev if sev > 0 else 0.5) * self.decision
                    o["reasons"].update(extra.get("flags", []))
                    if kills:
                        o["reasons"].add("pod killed")
            else:
                st["good"] += 1
                st["bad"] = 0
                if st["open"] is not None and st["good"] >= 2:
                    o = st["open"]
                    o["end_t"] = round(self.t - self.decision, 1)
                    self.write("health_recovered", "reactive", name, metric=metric,
                               peak=round(o["peak"], 3), threshold=thr,
                               duration_s=round(o["end_t"] - o["start_t"], 1), severity=round(o["severity"], 1),
                               cause={"reason": "2 healthy checks in a row"})
                    st["open"] = None

    def _context(self):
        """What else was going on: events of the last 5 minutes + what is active now (facts, not a verdict)."""
        out = []
        for e in reversed(self.diary.events):
            if e["t"] < self.t - 300:
                break
            if e["event"] in QUIET_FOR_CONTEXT or e["origin"] == "control":
                continue
            item = {"t": e["t"], "load": e.get("load"), "event": e["event"]}
            if isinstance(e.get("cause"), dict) and "by" in e["cause"]:
                item["by"] = e["cause"]["by"]
            out.append(item)
            if len(out) >= 15:
                break
        active = [{"load": n, "event": "burst_active"} for n, x in self.burst.items() if x > 1]
        active += [{"load": self.runs[k]["load"], "event": "run_active", "run": self.runs[k]["run"]}
                   for k in self.runner.active_runs() if k in self.runs]
        return out + active

    # ------------------------------------------------------------------ metrics
    def _sample(self):
        snap = self.runner.snapshot()
        for name, row in snap.items():
            for m, v in row.items():
                if v is not None:
                    self._acc[name][m].append(v)
        for name in self.user_samples:
            s = snap[name]
            self.user_samples[name].append((self.clock, s.get("latency_p95", 0.0), s.get("errors", 0.0)))

    LAST = ("pods", "pending", "replicas", "users", "active_runs", "cpu_capacity", "memory_capacity", "progress")
    MAX = ("event_age", "latency_p95", "backlog")

    def _flush_metrics(self):
        t, ts = round(self.t, 1), self.ts()
        for name, ms in self._acc.items():
            for m, vals in ms.items():
                if not vals:
                    continue
                v = vals[-1] if m in self.LAST else max(vals) if m in self.MAX else sum(vals) / len(vals)
                row = {"t": t, "phase": self.phase, "load": name, "metric": m, "value": round(float(v), 4)}
                self.metric_rows.append(row)
                if self._mfh:
                    self._mfh.write(f"{t},{ts},{self.phase},{name},{m},{row['value']}\n")
        self._acc.clear()

    def _health_row(self, name, metric, val, thr, ok):
        row = {"t": round(self.t, 1), "phase": self.phase, "load": name, "metric": metric,
               "value": round(float(val), 4), "threshold": thr, "healthy": ok}
        self.health_rows.append(row)
        if self._hfh:
            self._hfh.write(f"{row['t']},{self.ts()},{self.phase},{name},{metric},{row['value']},{thr},"
                            f"{int(ok)}\n")

    # ------------------------------------------------------------------ finish + export
    def finish(self, status="completed"):
        if self.status != "running":
            return
        self._flush_metrics()
        for st in self.health.values():
            if st["open"] is not None:
                st["open"]["end_t"] = round(self.t, 1)
                st["open"]["open_at_end"] = True
                st["open"] = None
        self._set_phase("teardown")
        self.runner.cleanup()
        self.write("run_ended", "planned", status=status)
        self.status = status
        self.diary.close()
        for fh in (self._mfh, self._hfh):
            if fh:
                fh.close()
        if self.dir:
            from . import analyze
            impact = analyze.impact_of(self)
            (self.dir / "impact.json").write_text(json.dumps(impact, indent=1), encoding="utf-8")
            self._write_run_json()
            (self.dir / "README.txt").write_text(analyze.readme(self, impact), encoding="utf-8")

    def _write_static_files(self):
        src = self.sc.get("_source")
        if src:
            shutil.copyfile(src, self.dir / "scenario.yaml")
        else:
            (self.dir / "scenario.yaml").write_text(yaml.safe_dump(self.sc), encoding="utf-8")
        (self.dir / "timeline.json").write_text(json.dumps(self.timeline, indent=1, sort_keys=True), encoding="utf-8")
        cal = getattr(self.runner, "calibration", None)
        cal = cal() if callable(cal) else None
        (self.dir / "calibration.json").write_text(json.dumps(cal or {
            "source": "scenario",
            "note": "not calibrated: cost_per_unit is taken from the scenario file as is (run `make calibrate`)",
            "cost_per_unit_s": {n: ld["cost_per_unit"] for n, ld in self.loads.items() if ld["type"] == "batch"}},
            indent=1), encoding="utf-8")
        self._write_run_json()

    def _write_run_json(self):
        info = {
            "format_version": FORMAT_VERSION, "kit_version": __version__, "run_id": self.run_id,
            "scenario": self.sc["name"], "scenario_hash": self.sc.get("_hash"), "seed": self.seed,
            "runner": self.runner_name, "naming": self.settings["naming"]["mode"], "status": self.status,
            "group": self.settings.get("group") or "default",
            "started_utc": self.ts(0), "clock_note": "t = seconds since the scenario phase started; ts = UTC time",
            "node": {"cpu": self.sc["capacity"]["cpu"], "memory": self.sc["capacity"]["memory"]},
            "phases": self.phase_times, "duration_s": self.sc["duration"], "preemption": self.sc["preemption"],
            "loads": [ld["name"] for ld in self.sc["loads"]],
            "placement": self.sc["placement"], "note": self.note,
            "counters": dict(self.counters), "peak_replicas": dict(self.peak_replicas),
        }
        (self.dir / "run.json").write_text(json.dumps(info, indent=1), encoding="utf-8")

    # ------------------------------------------------------------------ live status for the UI
    def status_view(self):
        with self.lock:
            runs = []
            for key, r in self.runs.items():
                try:
                    info = self.runner.run_info(key)
                except KeyError:
                    continue
                runs.append({"load": r["load"], "run": r["run"], "planned_t": r["planned_t"],
                             "start_t": r["start_t"], "normal": round(r["normal"], 1), "state": info["state"],
                             "progress": _fmt_progress(info), "elapsed": round(info["elapsed"], 1)})
            return {"run_id": self.run_id, "status": self.status, "phase": self.phase, "t": round(self.t, 1),
                    "duration": self.sc["duration"], "scenario_start": self.scenario_start,
                    "phases": self.phase_times, "runs": runs,
                    "health": {n: st["last"] for n, st in self.health.items()},
                    "queue": [{"load": q["load"], "run": q["run"]} for q in self.queue]}
