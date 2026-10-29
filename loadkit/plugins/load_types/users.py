"""User traffic: simulated users calling a small user-facing service (`kit-shop`).

Closed loop: each user sends a request, waits for the answer, thinks, sends the next. The shop reports response time
(p95), error rate, request rate and in-flight requests (concurrency). It is a load like any other — it can be slowed
by others and it can slow others.
"""
import math

from ... import units
from ...registry import register


@register("load_types")
class Users:
    name = "users"
    defaults = {
        "request": "500m",
        "limit": None,
        "memory": {"request": "128Mi", "limit": "256Mi", "base": "90Mi", "per_in_flight": "1Mi"},
        "health": {"metric": "latency_p95", "threshold": "300ms", "window": "60s", "errors_max": "1%"},
    }

    @staticmethod
    def validate(ld, sc):
        errs, out = [], {}
        try:
            out["users"] = float(ld["users"])
        except (KeyError, ValueError):
            errs.append("'users' (concurrent users at quiet time) is required")
            return out, errs
        out["daily_pattern"] = ld.get("daily_pattern", "flat")
        out["peaks"] = []
        for p in ld.get("peaks") or []:
            try:
                out["peaks"].append({"at": units.duration(p["at"]), "x": float(p.get("x", 3)),
                                     "length": units.duration(p.get("length", "10m"))})
            except (KeyError, units.UnitError, ValueError) as e:
                errs.append(f"peaks: {e}")
        try:
            out["think_time"] = units.duration(ld.get("think_time", "1s"))
            out["cost_per_request"] = units.duration(ld.get("cost_per_request", "20ms"))
            out["timeout"] = units.duration(ld.get("timeout", "5s"))
        except units.UnitError as e:
            errs.append(str(e))
        r = ld.get("shop_replicas", [2, 6])
        out["shop_replicas"] = [int(r[0]), int(r[-1])] if isinstance(r, list) else [int(r), int(r)]
        out["scaler"] = dict(ld.get("scaler") or {"name": "concurrency", "target_in_flight": 10})
        return out, errs

    @staticmethod
    def plan(rng, ld, sc):
        """Planned events: user-count changes (daily pattern steps + peaks)."""
        from ...patterns import factor
        ev = []
        step = sc["day_length"] / 48.0
        times = {round(i * step, 3) for i in range(int(math.ceil(sc["duration"] / step)))}
        for p in ld["peaks"]:
            times.add(round(p["at"], 3))
            times.add(round(min(p["at"] + p["length"], sc["duration"]), 3))
        last = None
        for t in sorted(times):
            f = factor(ld["daily_pattern"], t, sc["day_length"], sc["day_start_hour"])
            for p in ld["peaks"]:
                if p["at"] <= t < p["at"] + p["length"]:
                    f *= p["x"]
            n = max(1, round(ld["users"] * f))
            if n != last:
                ev.append({"t": t, "load": ld["name"], "kind": "users_change", "value": {"users": n}})
                last = n
        return ev
