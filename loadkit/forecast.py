"""Forecast support: build the context a forecaster consumes, from a finished run's metrics + planned timeline.

A forecaster (registry kind "forecasters", interface fit_predict(ctx)) gets a model-agnostic context:
  history        {series: [(t, value)]}  the observed past, one entry per series
  horizon        how many steps to predict ahead
  step_s         one step in seconds (the kit's metrics interval, 15 s)
  planned_events [(t, load, kind, value)]  planned events AFTER the end of the history (known future covariates)
  node           {cpu, memory} the node capacity

"series" keys: "<load>:<metric>" for every load metric, and "node:<metric>" for the machine (cpu_used, ...).
The same naming the UI uses to pick what to forecast.
"""


def _metrics_to_series(rows):
    out = {}
    for r in rows:
        load = "node" if r["load"] == "_node" else r["load"]
        out.setdefault(f"{load}:{r['metric']}", []).append((float(r["t"]), float(r["value"])))
    for key in out:
        out[key].sort()
    return out


def build_forecast_context(run_dir, horizon=48, step_s=15.0, max_points=400):
    """ctx dict for the enabled forecaster, from a run folder (metrics.csv + timeline.json + run.json)."""
    import json

    from .analyze import load_run, read_metrics
    r = load_run(run_dir)
    history = _metrics_to_series(read_metrics(run_dir))
    t_end = max((pts[-1][0] for pts in history.values() if pts), default=0.0)
    tl = json.loads((run_dir / "timeline.json").read_text(encoding="utf-8"))
    events = [(e["t"], e.get("load"), e["kind"], e.get("value")) for e in tl.get("events", []) if e["t"] > t_end]
    return {"history": {k: v[-max_points:] for k, v in history.items()}, "horizon": int(horizon), "step_s": step_s,
            "planned_events": events, "node": r["info"].get("node", {})}
