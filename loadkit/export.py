"""Excel export of a finished run: one workbook a person can open and filter without the kit.

Sheets
  Summary   what ran, how it ended, unhealthy periods and likely causes
  Events    the diary in plain words, one row per event, PLUS the state of that load and of the machine at that moment
  Timeline  every 15 s: the machine and every load side by side
  Health    every health check of every load
  Settings  every setting of every load, as written in the scenario file
  Impact    unhealthy periods per load with their likely causes
"""
import io
import json
import pathlib

import yaml

from . import analyze, units as U

ROUTINE = {"rate_changed", "users_changed", "job_progress"}
PROBLEM = {"pod_preempted", "pod_evicted", "oom_killed", "job_timed_out", "job_failed", "run_skipped",
           "health_degraded", "pod_lost"}
WARNING = {"run_overlapped", "run_queued", "pod_pending", "consumers_idle", "node_memory_pressure", "burst_started",
           "run_replaced"}
LOAD_METRICS = {  # extra per-type columns in the Timeline sheet
    "batch": ["active_runs", "progress"],
    "stream": ["replicas", "backlog", "event_age", "arrival_rate", "processed_rate"],
    "users": ["replicas", "users", "request_rate", "in_flight", "latency_p95", "errors"],
}


def describe(e):
    """The same plain-words sentence the UI shows."""
    c, ld = e.get("cause") or {}, e.get("load") or ""
    ev = e["event"]
    m = {
        "job_due": lambda: f"{ld} run {e.get('run')} is due ({e.get('units')} units)",
        "job_started": lambda: f"{ld} run {e.get('run')} started ({e.get('units')} units, normally "
                               f"{round(e.get('normal_duration', 0) / 60)} min)",
        "job_ended": lambda: f"{ld} run {e.get('run')} finished in {round(e.get('actual_duration', 0) / 60)} min "
                             f"({e.get('ratio')}× normal)",
        "job_timed_out": lambda: f"{ld} run {e.get('run')} stopped: took too long ({e.get('progress')} done)",
        "run_overlapped": lambda: f"{ld} run {e.get('run')} started while {e.get('active_runs', 1) - 1} earlier "
                                  f"run(s) were still working",
        "run_skipped": lambda: f"{ld} run {e.get('run')} skipped: the previous run was still working",
        "run_replaced": lambda: f"{ld} run {e.get('run')} stopped and replaced by run {e.get('by_run')}",
        "run_queued": lambda: f"{ld} run {e.get('run')} waits in the queue (too many runs at once)",
        "consumers_scaled": lambda: f"{ld} scaled {e.get('from')} → {e.get('to')} consumer pods",
        "shop_scaled": lambda: f"{ld} scaled {e.get('from')} → {e.get('to')} shop pods",
        "consumers_idle": lambda: f"{ld}: {e.get('idle')} consumer pod(s) idle (only {c.get('partitions')} partitions)",
        "pod_preempted": lambda: f"{ld} pod pushed out by {c.get('by')} (priority {c.get('by_priority', 'higher')})",
        "pod_evicted": lambda: f"{ld} pod evicted: the machine ran out of memory",
        "oom_killed": lambda: f"{ld} pod killed: used more memory than its limit",
        "pod_pending": lambda: f"{ld} pod waiting: {c.get('reason')}",
        "pod_restarted": lambda: f"{ld} pod running again",
        "pod_lost": lambda: f"{ld} pod stopped reporting",
        "node_memory_pressure": lambda: "the machine is low on memory" if e.get("state") == "on"
        else "the machine has enough memory again",
        "health_degraded": lambda: f"{ld} became unhealthy ({e.get('metric')} {e.get('value')} > {e.get('threshold')})",
        "health_recovered": lambda: f"{ld} is healthy again after {round(e.get('duration_s', 0) / 60, 1)} min",
        "burst_started": lambda: f"{ld} burst: {e.get('x')}× more events",
        "burst_ended": lambda: f"{ld} burst over",
        "users_changed": lambda: f"{ld}: {e.get('users')} users",
        "rate_changed": lambda: f"{ld}: {e.get('rate')} events/s",
        "phase_started": lambda: {"scenario": "the scenario starts", "drain": "the scenario is over: letting running "
                                  "work finish", "warmup": "warming up", "baseline": "measuring what normal looks like"
                                  }.get(e.get("name"), f"phase: {e.get('name')}"),
        "run_started": lambda: "run started",
        "run_ended": lambda: f"run ended ({e.get('status')})",
    }.get(ev)
    try:
        return m() if m else (f"{ld}: " if ld else "") + ev.replace("_", " ")
    except Exception:  # noqa: BLE001 — a missing field must not break the export
        return ev.replace("_", " ")


def severity(e):
    if e["event"] in PROBLEM:
        return "problem"
    if e["event"] in WARNING and not (e["event"] == "node_memory_pressure" and e.get("state") == "off"):
        return "warning"
    return "info"


def _day_time(t, sc):
    dl = U.duration(sc.get("day_length", sc.get("duration", "2h")))
    h = (float(sc.get("day_start_hour", 0)) + 24.0 * t / dl) % 24
    return f"{int(h):02d}:{int(round((h - int(h)) * 60)) % 60:02d}"


def _minute(t):
    return round(t / 60.0, 2)


class _State:
    """Latest metric/health value at or before a time (metrics are on a 15 s grid)."""

    def __init__(self, metrics, health):
        self.m, self.h = {}, {}
        for r in metrics:
            self.m.setdefault((r["load"], r["metric"]), []).append((r["t"], r["value"]))
        for r in health:
            self.h.setdefault(r["load"], []).append((float(r["t"]), r))

    def at(self, load, metric, t):
        v = None
        for tt, val in self.m.get((load, metric), []):
            if tt <= t + 1e-9:
                v = val
            else:
                break
        return v

    def health(self, load, t):
        v = None
        for tt, r in self.h.get(load, []):
            if tt <= t + 1e-9:
                v = r
            else:
                break
        return v


def _cause_text(c):
    if not isinstance(c, dict):
        return ""
    parts = []
    for k, v in c.items():
        if k == "per_signal":
            parts.append("wanted: " + ", ".join(f"{a} {b}" for a, b in v.items()))
        elif k == "top_users":
            parts.append("memory above request: " + ", ".join(
                f"{u['load']} {round(u['above_request'] / 1024 ** 2)} MiB" for u in v))
        elif isinstance(v, (dict, list)):
            parts.append(f"{k}: {json.dumps(v)}")
        else:
            parts.append(f"{k}: {v}")
    return "; ".join(parts)


def build(run_dir):
    """-> (filename, xlsx bytes)"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    d = pathlib.Path(run_dir)
    run = analyze.load_run(d)
    info, diary, health = run["info"], run["diary"], run["health"]
    metrics = analyze.read_metrics(d)
    impact = run["impact"] or {"periods": []}
    raw = yaml.safe_load((d / "scenario.yaml").read_text(encoding="utf-8")) or {}
    loads = {ld["name"]: ld for ld in raw.get("loads", [])}
    st = _State(metrics, health)

    wb = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="5B9A9A")
    fills = {"problem": PatternFill("solid", fgColor="FDE2E2"), "warning": PatternFill("solid", fgColor="FEF3C7")}

    def sheet(ws, header, rows, widths=None):
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = head_font, head_fill
            c.alignment = Alignment(vertical="center", wrap_text=True)
        for r in rows:
            ws.append(r)
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
        for i, h in enumerate(header, 1):
            w = (widths or {}).get(h) or min(45, max(10, len(str(h)) + 2))
            ws.column_dimensions[get_column_letter(i)].width = w

    # ---------------------------------------------------------------- Summary
    ws = wb.active
    ws.title = "Summary"
    counts = {}
    for e in diary:
        counts[e["event"]] = counts.get(e["event"], 0) + 1
    rows = [["Run", info.get("run_id")], ["Scenario", info.get("scenario")],
            ["Description", raw.get("description", "")], ["Story", raw.get("story", "")],
            ["Seed", info.get("seed")], ["Status", info.get("status")], ["Started (UTC)", info.get("started_utc")],
            ["Runner", info.get("runner")], ["Machine size (scenario)", f"{info['node']['cpu']} cores, "
                                             f"{round(info['node']['memory'] / 1024 ** 3, 1)} GiB"],
            ["Scenario length", f"{round(info.get('duration_s', 0) / 60)} min"], ["Note", info.get("note") or ""],
            ["How to read", "Minute = minutes since the scenario started (negative = warm-up/baseline). In 'Events', "
                            "the load and machine columns are the latest 15 s measurement at or just before the event."],
            [],
            ["Load", "Type", "Team", "Priority", "Unhealthy periods", "Unhealthy minutes", "Likely causes"]]
    for name, ld in loads.items():
        ps = [p for p in impact["periods"] if p["load"] == name]
        mins = round(sum((p.get("end_t", p["start_t"]) - p["start_t"]) for p in ps) / 60, 1)
        causes = sorted({c["load"] + f" ({c['kind']})" for p in ps for c in p["causes"]})
        rows.append([name, ld.get("type"), ld.get("team"), ld.get("priority", "normal"), len(ps), mins,
                     ", ".join(causes)])
    rows += [[], ["Event", "Count"]] + [[k, v] for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    for r in rows:
        ws.append(r)
    for r in range(1, 13):
        ws.cell(r, 1).font = Font(bold=True)
    for row in ws.iter_rows():
        for c in row:
            if c.value in ("Load", "Event"):
                for cc in row:
                    if cc.value is not None:
                        cc.font, cc.fill = head_font, head_fill
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 60
    for col in "CDEFG":
        ws.column_dimensions[col].width = 18

    # ---------------------------------------------------------------- Events
    header = ["Minute", "Day time", "Real time (UTC)", "Phase", "Event", "What happened", "Kind", "Severity",
              "Load", "Load type", "Team", "Priority", "Why (cause)", "Planned minute", "Run", "Run progress",
              "Load CPU (cores)", "Load memory (MiB)", "Load pods running", "Load pods waiting", "Load replicas",
              "Load health metric", "Load health value", "Load health limit", "Load healthy",
              "Machine CPU used", "Machine CPU capacity", "Machine % busy", "Machine memory used (MiB)",
              "Machine memory capacity (MiB)", "All fields (JSON)"]
    rows, sev = [], []
    for e in diary:
        if e["event"] in ROUTINE:
            continue
        t, ld = e["t"], e.get("load")
        lconf = loads.get(ld, {})
        h = st.health(ld, t) if ld else None
        cpu_u, cpu_c = st.at("_node", "cpu_used", t), st.at("_node", "cpu_capacity", t)
        mem_u, mem_c = st.at("_node", "memory_used", t), st.at("_node", "memory_capacity", t)
        mib = (lambda v: round(v / 1024 ** 2) if v is not None else None)
        prog = e.get("progress") or ""
        rows.append([
            _minute(t), _day_time(t, raw), e.get("ts"), e.get("phase"), e["event"], describe(e), e.get("origin"),
            severity(e), ld, lconf.get("type"), lconf.get("team"), lconf.get("priority", "normal" if ld else None),
            _cause_text(e.get("cause")), _minute(e["planned_t"]) if "planned_t" in e else None, e.get("run"), prog,
            round(st.at(ld, "cpu", t), 3) if ld and st.at(ld, "cpu", t) is not None else None,
            mib(st.at(ld, "memory", t)) if ld else None,
            st.at(ld, "pods", t) if ld else None, st.at(ld, "pending", t) if ld else None,
            st.at(ld, "replicas", t) if ld else None,
            h["metric"] if h else None, float(h["value"]) if h else None, float(h["threshold"]) if h else None,
            ("yes" if h["healthy"] == "1" else "no") if h else None,
            round(cpu_u, 2) if cpu_u is not None else None, round(cpu_c, 2) if cpu_c is not None else None,
            round(100 * cpu_u / cpu_c) if cpu_u is not None and cpu_c else None, mib(mem_u), mib(mem_c),
            json.dumps({k: v for k, v in e.items() if k not in ("ts",)}, default=str)])
        sev.append(severity(e))
    ws = wb.create_sheet("Events")
    sheet(ws, header, rows, {"What happened": 60, "Why (cause)": 50, "Real time (UTC)": 21, "All fields (JSON)": 60,
                             "Event": 20})
    for i, s in enumerate(sev, start=2):
        if s in fills:
            for c in ws[i]:
                c.fill = fills[s]

    # ---------------------------------------------------------------- Timeline (15 s grid)
    by_t = {}
    for r in metrics:
        by_t.setdefault(r["t"], {}).setdefault(r["load"], {})[r["metric"]] = r["value"]
    cols = [("_node", "cpu_used"), ("_node", "cpu_capacity"), ("_node", "memory_used"), ("_node", "memory_capacity")]
    for name, ld in loads.items():
        for m in ["cpu", "memory", "pods", "pending"] + LOAD_METRICS.get(ld.get("type"), []):
            cols.append((name, m))
    header = ["Minute", "Day time", "Phase", "Machine % busy"] + [
        ("machine" if l == "_node" else l) + " · " + m.replace("_", " ") + (" (MiB)" if "memory" in m else "")
        for l, m in cols] + [f"{name} · healthy" for name in loads]
    phase_at = {}
    for r in metrics:
        phase_at[r["t"]] = r["phase"]
    rows = []
    for t in sorted(by_t):
        row = by_t[t]
        n = row.get("_node", {})
        busy = round(100 * n["cpu_used"] / n["cpu_capacity"]) if n.get("cpu_capacity") else None
        vals = []
        for l, m in cols:
            v = row.get(l, {}).get(m)
            if v is not None and "memory" in m:
                v = round(v / 1024 ** 2)
            elif isinstance(v, float):
                v = round(v, 3)
            vals.append(v)
        hv = []
        for name in loads:
            h = st.health(name, t)
            hv.append(("yes" if h["healthy"] == "1" else "no") if h else None)
        rows.append([_minute(t), _day_time(t, raw), phase_at.get(t), busy] + vals + hv)
    sheet(wb.create_sheet("Timeline"), header, rows, {h: 14 for h in header})

    # ---------------------------------------------------------------- Health
    rows = [[_minute(float(h["t"])), _day_time(float(h["t"]), raw), h["phase"], h["load"], h["metric"],
             float(h["value"]), float(h["threshold"]), "yes" if h["healthy"] == "1" else "no"] for h in health]
    sheet(wb.create_sheet("Health"), ["Minute", "Day time", "Phase", "Load", "Metric", "Value", "Limit", "Healthy"],
          rows)

    # ---------------------------------------------------------------- Settings (as written in the scenario file)
    rows = [["(scenario)", k, json.dumps(v) if isinstance(v, (dict, list)) else v]
            for k, v in raw.items() if k not in ("loads", "look_for", "description", "story")]
    for name, ld in loads.items():
        for k, v in ld.items():
            rows.append([name, k, json.dumps(v) if isinstance(v, (dict, list)) else
                         ("null" if v is None else v)])
    sheet(wb.create_sheet("Settings"), ["Load", "Setting", "Value"], rows, {"Value": 70, "Setting": 24, "Load": 18})

    # ---------------------------------------------------------------- Impact
    rows = []
    for p in impact["periods"]:
        causes = "; ".join(
            f"{c['load']} {c['kind']}" + (f" {round(100 * c['share'])} %" if c.get("share") is not None else "") +
            (f" {c['count']}×" if c.get("count") else "") for c in p["causes"])
        rows.append([p["load"], p["metric"], _minute(p["start_t"]), _minute(p.get("end_t", p["start_t"])),
                     round((p.get("end_t", p["start_t"]) - p["start_t"]) / 60, 1), p["peak"], p["threshold"],
                     causes, " ".join(p.get("notes", []))])
    sheet(wb.create_sheet("Impact"), ["Load", "Metric", "From minute", "To minute", "Minutes", "Worst value", "Limit",
                                      "Likely causes", "Notes"], rows, {"Likely causes": 50, "Notes": 60})

    buf = io.BytesIO()
    wb.save(buf)
    return f"{info.get('run_id', d.name)}.xlsx", buf.getvalue()
