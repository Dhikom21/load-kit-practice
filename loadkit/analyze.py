"""After the run: impact between loads (unhealthy periods + likely causes), the plain-words README, and run comparison.

Nothing here is declared in the scenario. Causes are worked out from what was recorded:
  * overlap (every run, automatic): while a load was unhealthy and the node's CPU was nearly full, which other loads
    were using the CPU (from metrics.csv), and which events point at another load: preemptions (by whom), evictions
    (who held the memory), OOM kills (own limit). These are LIKELY causes.
  * counterfactual (confirmation): re-run the same scenario + seed with one load removed (`run --without X`); if the
    unhealthy period disappears, X was a cause.  `analyze RUN --baseline RUN_WITHOUT_X`
"""
import csv
import json
import pathlib

from . import FORMAT_VERSION

KIND_LABEL = {"cpu": "used the CPU while the node was full", "preemption": "pushed out its pods (priority)",
              "memory": "held the node's memory", "oom": "hit its own memory limit"}
BUSY = 0.9          # node counts as "full" at >= 90 % of its CPU


class RunFormatError(ValueError):
    pass


def impact_of(engine):
    periods = [_period_view(p) for p in engine.periods]
    return build_impact(periods, engine.metric_rows, engine.diary.events, engine.decision, engine.run_id)


def _period_view(p):
    out = {k: v for k, v in p.items() if k != "reasons"}
    out["reasons"] = sorted(p.get("reasons", []))
    out["peak"] = round(out["peak"], 3)
    out["severity"] = round(out["severity"], 1)
    return out


def _cpu_overlap(metrics, victim, start, end):
    """CPU-seconds other loads used while the node was full during [start, end]. Same-load extra batch runs count as
    'self' (a run slowed by the job's own earlier runs)."""
    by_t = {}
    for r in metrics:
        if start - 15 <= r["t"] <= end:
            by_t.setdefault(r["t"], {}).setdefault(r["load"], {})[r["metric"]] = r["value"]
    used = {}
    for t, rows in by_t.items():
        node = rows.get("_node", {})
        cap = node.get("cpu_capacity") or 0
        if not cap or node.get("cpu_used", 0) < BUSY * cap:
            continue
        for load, m in rows.items():
            if load == "_node":
                continue
            cpu = m.get("cpu", 0.0)
            if load == victim:
                n = m.get("active_runs", 0) or 0
                if n > 1:
                    used[load] = used.get(load, 0.0) + cpu * (n - 1) / n
            elif cpu > 0:
                used[load] = used.get(load, 0.0) + cpu
    return used


def build_impact(periods, metrics, diary, decision, run_id):
    result = {"format_version": FORMAT_VERSION, "run_id": run_id, "method": "overlap", "periods": []}
    for p in periods:
        start = p["start_t"] - decision
        end = p.get("end_t", p["start_t"])
        if p["metric"] == "runtime_ratio":
            # a run can be slowed long before it looks unhealthy: look back to the start of the runs active then
            start = min([start] + _run_starts(diary, p["load"], start, end))
        used = _cpu_overlap(metrics, p["load"], start, end)
        total = sum(used.values())
        causes = []
        for c, sec in sorted(used.items(), key=lambda kv: -kv[1]):
            if total and sec / total >= 0.02:
                causes.append({"load": c, "kind": "cpu", "share": round(sec / total, 3), "self": c == p["load"]})
        events = [e for e in diary if e.get("load") == p["load"] and start - 30 <= e["t"] <= end]
        pre, mem, oom = {}, {}, 0
        for e in events:
            cause = e.get("cause") or {}
            if e["event"] == "pod_preempted":
                pre[cause.get("by", "?")] = pre.get(cause.get("by", "?"), 0) + 1
            elif e["event"] == "pod_evicted":
                tops = cause.get("top_users") or []
                tot = sum(u["above_request"] for u in tops) or 1
                for u in tops:
                    mem[u["load"]] = mem.get(u["load"], 0.0) + u["above_request"] / tot
            elif e["event"] == "oom_killed":
                oom += 1
        for by, n in sorted(pre.items(), key=lambda kv: -kv[1]):
            causes.append({"load": by, "kind": "preemption", "count": n})
        msum = sum(mem.values()) or 1
        for ld, w in sorted(mem.items(), key=lambda kv: -kv[1]):
            if w / msum >= 0.02:
                causes.append({"load": ld, "kind": "memory", "share": round(w / msum, 3)})
        if oom:
            causes.append({"load": p["load"], "kind": "oom", "count": oom, "self": True})
        notes = []
        idle = [e for e in diary if e.get("load") == p["load"] and e["event"] == "consumers_idle" and e["t"] <= end]
        if idle and idle[-1].get("idle", 0) > 0:
            notes.append("more consumers than partitions: extra consumers were idle")
        if not causes and not notes:
            notes.append("the node was not full and no pod was killed: the load's own demand was above its own "
                         "capacity (e.g. max replicas, partitions, limits)")
        others = sorted({c["load"] for c in causes if not c.get("self") and c["load"] != p["load"]})
        if others:
            notes.append("to confirm: re-run the same seed without " + " / ".join(others))
        result["periods"].append({**p, "causes": causes, "notes": notes})
    return result


def periods_from_diary(diary):
    """Rebuild degraded periods from health_degraded / health_recovered events (for re-analysis of a run folder)."""
    open_, out = {}, []
    for e in diary:
        if e["event"] == "health_degraded":
            open_[e["load"]] = {"load": e["load"], "metric": e["metric"], "start_t": e["t"], "peak": e["value"],
                                "threshold": e["threshold"], "severity": 0.0, "reasons": []}
        elif e["event"] == "health_recovered" and e["load"] in open_:
            p = open_.pop(e["load"])
            p.update(end_t=e["t"], peak=e["peak"], severity=e["severity"])
            out.append(p)
    for p in open_.values():
        p.update(end_t=diary[-1]["t"], open_at_end=True)
        out.append(p)
    return out


def _run_starts(diary, load, start, end):
    started, finished = {}, {}
    for e in diary:
        if e.get("load") != load or "run" not in e:
            continue
        if e["event"] == "job_started":
            started[e["run"]] = e["t"]
        elif e["event"] in ("job_ended", "job_timed_out", "run_replaced"):
            finished[e["run"]] = e["t"]
    return [t0 for r, t0 in started.items() if t0 <= end and finished.get(r, float("inf")) >= start]


# ---------------------------------------------------------------- reading run folders
def load_run(path):
    d = pathlib.Path(path)
    info = json.loads((d / "run.json").read_text(encoding="utf-8"))
    if info.get("format_version") != FORMAT_VERSION:
        raise RunFormatError(f"{d.name}: format_version {info.get('format_version')} is not supported "
                             f"(this kit reads version {FORMAT_VERSION})")
    diary = [json.loads(line) for line in (d / "diary.jsonl").read_text(encoding="utf-8").splitlines() if line]
    with open(d / "health.csv", encoding="utf-8") as fh:
        health = list(csv.DictReader(fh))
    impact = json.loads((d / "impact.json").read_text(encoding="utf-8")) if (d / "impact.json").exists() else None
    return {"dir": d, "info": info, "diary": diary, "health": health, "impact": impact}


def read_metrics(d):
    with open(pathlib.Path(d) / "metrics.csv", encoding="utf-8") as fh:
        return [{"t": float(r["t"]), "phase": r["phase"], "load": r["load"], "metric": r["metric"],
                 "value": float(r["value"])} for r in csv.DictReader(fh)]


def analyze_dir(run_dir, decision=15.0):
    """Recompute impact.json from a run folder (diary + metrics)."""
    r = load_run(run_dir)
    imp = build_impact(periods_from_diary(r["diary"]), read_metrics(run_dir), r["diary"], decision,
                       r["info"]["run_id"])
    (pathlib.Path(run_dir) / "impact.json").write_text(json.dumps(imp, indent=1), encoding="utf-8")
    return imp


def counterfactual(run_dir, baseline_dir):
    """Which degraded periods of run A disappear in baseline B (same scenario + seed, one load removed)?"""
    a, b = load_run(run_dir), load_run(baseline_dir)
    loads_a = set(a["info"].get("loads") or [e["load"] for e in a["diary"] if e.get("load")])
    loads_b = set(b["info"].get("loads") or [e["load"] for e in b["diary"] if e.get("load")])
    removed = sorted(loads_a - loads_b)
    out = []
    for p in (a["impact"] or {}).get("periods", []):
        rows = [r for r in b["health"] if r["load"] == p["load"]
                and p["start_t"] <= float(r["t"]) <= p.get("end_t", p["start_t"])]
        bad = sum(1 for r in rows if r["healthy"] == "0")
        frac = bad / len(rows) if rows else None
        out.append({"load": p["load"], "start_t": p["start_t"], "end_t": p.get("end_t"),
                    "unhealthy_fraction_in_baseline": None if frac is None else round(frac, 2),
                    "disappears": frac is not None and frac < 0.25,
                    "likely_cause": removed if (frac is not None and frac < 0.25) else []})
    return {"run": a["info"]["run_id"], "baseline": b["info"]["run_id"], "removed_loads": removed, "periods": out}


def summary(run):
    d, info = run["diary"], run["info"]
    loads = sorted({e["load"] for e in d if e.get("load") and e.get("event") != "phase_started"})
    per = {}
    for ld in loads:
        periods = [p for p in (run["impact"] or {}).get("periods", []) if p["load"] == ld]
        per[ld] = {"degraded_min": round(sum(p.get("end_t", p["start_t"]) - p["start_t"] for p in periods) / 60, 1),
                   "periods": len(periods),
                   "peak_replicas": info.get("peak_replicas", {}).get(ld)}
    count = lambda ev: sum(1 for e in d if e["event"] == ev)  # noqa: E731
    return {"run_id": info["run_id"], "scenario": info["scenario"], "seed": info["seed"], "status": info["status"],
            "loads": per,
            "totals": {k: count(k) for k in ("run_overlapped", "run_skipped", "job_timed_out", "pod_preempted",
                                             "oom_killed", "pod_evicted", "consumers_scaled", "shop_scaled")}}


def compare(dir_a, dir_b):
    a, b = summary(load_run(dir_a)), summary(load_run(dir_b))
    loads = sorted(set(a["loads"]) | set(b["loads"]))
    lines = [f"A = {a['run_id']}", f"B = {b['run_id']}", "", f"{'':28}{'A':>10}{'B':>10}"]
    for ld in loads:
        va, vb = a["loads"].get(ld, {}), b["loads"].get(ld, {})
        lines.append(f"{ld + ' degraded min':28}{va.get('degraded_min', '-'):>10}{vb.get('degraded_min', '-'):>10}")
        if va.get("peak_replicas") or vb.get("peak_replicas"):
            lines.append(f"{ld + ' peak replicas':28}{str(va.get('peak_replicas') or '-'):>10}"
                         f"{str(vb.get('peak_replicas') or '-'):>10}")
    for k in a["totals"]:
        lines.append(f"{k:28}{a['totals'][k]:>10}{b['totals'][k]:>10}")
    return {"a": a, "b": b, "text": "\n".join(lines)}


# ---------------------------------------------------------------- README.txt
def _mmss(t):
    sign = "-" if t < 0 else ""
    t = abs(int(t))
    return f"{sign}{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def readme(engine, impact):
    sc = engine.sc
    c = engine.counters
    lines = [f"Run {engine.run_id}", "=" * (4 + len(engine.run_id)), "",
             f"Scenario: {sc['name']} — {sc.get('description', '').strip()}",
             f"Seed {engine.seed}, runner {engine.runner_name}, status {engine.status}, "
             f"node {sc['capacity']['cpu']:g} cores / {sc['capacity']['memory'] / 1024 ** 3:.0f} GiB.", "",
             "What ran:"]
    for ld in sc["loads"]:
        lines.append(f"  - {ld['name']} ({ld['type']}, team {ld['team']}, priority {ld['priority']})")
    lines += ["", "What happened (counts):"]
    for k in ("job_started", "job_ended", "run_overlapped", "run_skipped", "run_queued", "job_timed_out",
              "consumers_scaled", "consumers_idle", "shop_scaled", "pod_pending", "pod_preempted", "oom_killed",
              "pod_evicted", "pod_restarted", "health_degraded"):
        if c.get(k):
            lines.append(f"  {k:18} {c[k]}")
    lines += ["", "Degraded periods and likely causes (computed after the run):"]
    if not impact["periods"]:
        lines.append("  none — every load stayed healthy.")
    for p in impact["periods"]:
        end = p.get("end_t", p["start_t"])
        lines.append(f"  * {p['load']}: {p['metric']} up to {p['peak']:g} (limit {p['threshold']:g}) "
                     f"from {_mmss(p['start_t'])} to {_mmss(end)}"
                     f"{' (still degraded at the end)' if p.get('open_at_end') else ''}")
        for cause in p["causes"][:4]:
            who = "itself (its own overlapping runs)" if cause.get("self") and cause["kind"] == "cpu" else cause["load"]
            amount = f"{cause['share'] * 100:.0f} %" if "share" in cause else f"{cause.get('count')}x"
            lines.append(f"      - {who} {KIND_LABEL[cause['kind']]} ({amount})")
        for n in p["notes"]:
            lines.append(f"      note: {n}")
    lines += ["", "Files: run.json, scenario.yaml, timeline.json, diary.jsonl, metrics.csv, health.csv, impact.json,",
              "calibration.json. t = seconds since the scenario phase started (negative = warm-up/baseline).", ""]
    return "\n".join(lines)
