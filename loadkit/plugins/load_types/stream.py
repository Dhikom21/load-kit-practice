"""Data streams: a producer sends events continuously (daily pattern + random bursts); consumers process them.

Concurrency = active consumers x threads, where active consumers = min(consumer pods, partitions).
Consumers are scaled by a scaler plugin (default: target-tracking on CPU + backlog).
"""
import math

from ... import units
from ...registry import register


@register("load_types")
class Stream:
    name = "stream"
    defaults = {
        "request": "250m",
        "limit": None,
        "memory": {"request": "128Mi", "limit": "512Mi", "base": "100Mi", "per_buffered_event": "2Ki"},
        "health": {"metric": "event_age", "threshold": "30s"},
    }

    @staticmethod
    def validate(ld, sc):
        errs, out = [], {}
        try:
            out["base_rate"] = float(ld["base_rate"])
        except (KeyError, ValueError):
            errs.append("'base_rate' (events per second) is required")
            return out, errs
        out["daily_pattern"] = ld.get("daily_pattern", "flat")
        b = ld.get("bursts")
        if b:
            try:
                ln = b.get("length", ["2m", "4m"])
                out["bursts"] = {"per_hour": float(b.get("per_hour", 1)), "x": float(b.get("x", 3)),
                                 "length": [units.duration(ln[0]), units.duration(ln[-1])],
                                 "at": [units.duration(a) for a in b.get("at", [])]}
            except (units.UnitError, ValueError, TypeError) as e:
                errs.append(f"bursts: {e}")
        else:
            out["bursts"] = None
        try:
            out["work_per_event"] = units.duration(ld.get("work_per_event", "8ms")) \
                if isinstance(ld.get("work_per_event", "8ms"), str) else float(ld["work_per_event"]) / 1000.0
        except units.UnitError as e:
            errs.append(str(e))
        c = ld.get("consumers", [1, 4])
        out["consumers"] = [int(c[0]), int(c[-1])] if isinstance(c, list) else [int(c), int(c)]
        out["threads_per_consumer"] = int(ld.get("threads_per_consumer", 1))
        out["partitions"] = int(ld.get("partitions", out["consumers"][1]))
        out["prefetch"] = int(ld.get("prefetch", 50))
        out["scaler"] = dict(ld.get("scaler") or {"name": "target-tracking"})
        if out["consumers"][0] < 1 or out["consumers"][1] < out["consumers"][0]:
            errs.append("consumers must be [min, max] with 1 <= min <= max")
        return out, errs

    @staticmethod
    def plan(rng, ld, sc):
        """Planned events: burst start/end (random Poisson process or fixed times) + pattern steps for display."""
        ev = []
        b = ld.get("bursts")
        if b:
            starts = list(b["at"])
            if b["per_hour"] > 0 and not b["at"]:
                t = 0.0
                rate = b["per_hour"] / 3600.0
                while True:
                    t += rng.expovariate(rate)
                    if t >= sc["duration"]:
                        break
                    starts.append(t)
            for s in sorted(starts):
                length = rng.uniform(*b["length"])
                ev.append({"t": round(s, 3), "load": ld["name"], "kind": "burst_start", "value": {"x": b["x"]}})
                ev.append({"t": round(min(s + length, sc["duration"]), 3), "load": ld["name"], "kind": "burst_end",
                           "value": {}})
        if ld["daily_pattern"] != "flat":
            step = sc["day_length"] / 48.0
            n = int(math.ceil(sc["duration"] / step))
            from ...patterns import factor
            for i in range(n):
                t = i * step
                ev.append({"t": round(t, 3), "load": ld["name"], "kind": "rate_change",
                           "value": {"factor": round(factor(ld["daily_pattern"], t, sc["day_length"],
                                                            sc["day_start_hour"]), 3)}})
        return ev
