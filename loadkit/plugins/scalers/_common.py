"""Shared safety knobs for every scaler: min/max, stabilization window, scale-up rate limit.

Mirrors the Kubernetes HPA defaults: scale up at once (at most max(+4 pods, x2) per decision), scale down only to the
highest recommendation seen during the stabilization window (default 5 min).
"""
import math

from ... import units


def setting(cfg, key, default, kind=float):
    v = cfg.get(key, default)
    if kind == "duration":
        return units.duration(v)
    return kind(v)


def finish(now, wanted, current, cfg, state, per_signal, winner, reason):
    mn, mx = int(cfg.get("min", 1)), int(cfg.get("max", 10))
    wanted = max(mn, min(mx, int(wanted)))
    window = setting(cfg, "stabilization", "5m", "duration")
    hist = state.setdefault("recommendations", [])
    hist.append((now, wanted))
    while hist and hist[0][0] < now - window:
        hist.pop(0)
    if wanted > current:
        limit = max(current + 4, current * 2)
        target = min(wanted, limit)
        if target < wanted:
            reason += f"; rate limit: at most {limit} now"
    elif wanted < current:
        target = max(r for _, r in hist)          # highest recommendation in the window
        if target > wanted:
            reason += f"; waiting for stabilization ({int(window)} s) before going below {target}"
    else:
        target = current
    return {"replicas": max(mn, min(mx, target)), "wanted": wanted, "per_signal": per_signal,
            "winner": winner, "reason": reason}


def ceil_div(a, b):
    return int(math.ceil(a / b - 1e-9)) if b > 0 else 0
