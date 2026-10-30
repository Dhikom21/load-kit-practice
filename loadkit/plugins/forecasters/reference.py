"""reference: the kit's built-in forecaster — a deliberately simple, stdlib-only baseline you can beat.

What it does
  * center line:  flat at the last observed value (a "seasonal naive" forecast);
  * band:         widens with the square root of the horizon (the further out, the wider the band);
  * future covariates: planned load events (bursts, batch jobs, user surges) push the center line UP and decay
                       to zero over ~30 min — a small demo of using the planned timeline as a known future covariate.

It is stdlib-only and deterministic: it runs with no model installed and no network, so the kit's tests and CI
stay green, and a run's forecast is reproducible. Your job: make a model that beats it (see students/TASK.md) —
the seam for Timestral is plugins/forecasters/timestral.py.
"""
from ...registry import register

STEP = 15.0           # the kit records one metric row per 15 s (metrics_interval)
BUMP = 0.5            # a scheduled event lifts the center line by this fraction of the recent level
DECAY_S = 1800.0      # and the lift decays to zero over about 30 min


def _quantiles(center, mean, horizon, bump):
    """p50 = center + bump; the p10/p90 band widens as a base + sqrt(steps-ahead) around the recent noise."""
    q = {}
    for qk in ("p10", "p50", "p90"):
        row = []
        for h in range(1, horizon + 1):
            c = center + bump[h - 1]
            half = 0.1 * mean + 0.18 * mean * (h ** 0.5)
            row.append(max(0.0, c + (1.65 * half if qk == "p90" else -1.65 * half if qk == "p10" else 0.0)))
        q[qk] = row
    return q


def _bump(mean, horizon, step, t_start, planned):
    """For each planned load event after the end of the history, add a lift that decays linearly over DECAY_S."""
    bump = [0.0] * horizon
    for (t, load, kind, value) in planned:
        if t <= t_start or kind not in ("burst_start", "job_due", "users_change", "rate_change"):
            continue
        strength = 1.0
        if kind == "burst_start":
            strength = max(0.0, float(value.get("x", 1.0)) - 1.0)
        elif kind == "rate_change":
            strength = max(0.0, float(value.get("factor", 1.0)) - 1.0)
        elif kind == "job_due":
            strength = float(value.get("units", 1.0)) / 500.0          # bigger job -> bigger expected lift
        if strength <= 0:
            continue
        for i in range(horizon):
            tau = t - (t_start + (i + 1) * step)        # < 0 once the event has started
            if tau <= 0:
                bump[i] += BUMP * strength * mean * max(0.0, 1.0 + tau / DECAY_S)
    return bump


@register("forecasters")
class ReferenceForecaster:
    name = "reference"

    @staticmethod
    def fit_predict(ctx):
        """ctx: {history: {series: [(t, v)]}, horizon, step_s, planned_events: [(t, load, kind, value)]}
        Returns {series: {p10, p50, p90: [value at each step 1..horizon]}} (one step = the metrics interval)."""
        history, horizon = ctx["history"], int(ctx["horizon"])
        step = float(ctx.get("step_s", STEP))
        planned = ctx.get("planned_events") or []
        out = {}
        for name, pts in history.items():
            if not pts:
                continue
            vals = [v for _, v in pts]
            mean = sum(vals) / len(vals)
            out[name] = _quantiles(vals[-1], mean or 1.0, horizon, _bump(mean, horizon, step, pts[-1][0], planned))
        return out
