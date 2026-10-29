"""Batch jobs: real CPU work that runs until a FIXED AMOUNT OF WORK is done (never a fixed time).

On a busy machine a run gets less CPU and takes longer, so it can overlap the next run and snowball.
Only the due time and the amount of work are planned; how long a run takes is reactive.
"""
from ... import units
from ...registry import register

KINDS = ("report", "backup", "training", "etl", "generic")


@register("load_types")
class Batch:
    name = "batch"
    defaults = {
        "request": "250m",
        "limit": None,
        "memory": {"request": "256Mi", "limit": "1Gi", "base": "200Mi", "peak": "400Mi", "shape": "ramp"},
        "health": {"metric": "runtime_ratio", "threshold": 1.5},
    }

    @staticmethod
    def validate(ld, sc):
        errs, out = [], {}
        try:
            out["every"] = units.duration(ld["every"])
            out["first"] = units.duration(ld.get("first", 0))
            out["jitter"] = units.duration(ld.get("jitter", 0))
        except KeyError:
            errs.append("'every' is required (interval between runs, e.g. 30m)")
            return out, errs
        except units.UnitError as e:
            errs.append(str(e))
            return out, errs
        w = ld.get("work") or {}
        try:
            out["units"] = float(w["units"])
            out["unit"] = w.get("unit", "unit")
            out["cost_per_unit"] = units.duration(w["cost_per_unit"])
        except KeyError:
            errs.append("'work' needs units and cost_per_unit, e.g. {units: 500, unit: file, cost_per_unit: 1.2s}")
            return out, errs
        except (units.UnitError, ValueError) as e:
            errs.append(f"work: {e}")
            return out, errs
        if out["units"] <= 0 or out["cost_per_unit"] <= 0:
            errs.append("work.units and work.cost_per_unit must be > 0")
        out["work_spread"] = units.percent(ld.get("work_spread", 0))
        out["parallelism"] = int(ld.get("parallelism", 1))
        out["intensity"] = units.cpu(ld.get("intensity", 1.0))
        out["deadline_factor"] = float(ld.get("deadline_factor", 4))
        out["concurrency_policy"] = ld.get("concurrency_policy", "allow")
        if out["concurrency_policy"] not in ("allow", "forbid", "replace"):
            errs.append("concurrency_policy must be allow|forbid|replace")
        mcr = ld.get("max_concurrent_runs")
        out["max_concurrent_runs"] = int(mcr) if mcr is not None else None
        out["checkpoint"] = bool(ld.get("checkpoint", False))
        out["kind"] = ld.get("kind") or next((k for k in KINDS if k in str(ld.get("name", ""))), "generic")
        if out["parallelism"] < 1 or out["intensity"] <= 0:
            errs.append("parallelism must be >= 1 and intensity > 0")
        if not errs:
            out["normal_duration"] = out["units"] * out["cost_per_unit"] / (out["parallelism"] * out["intensity"])
        return out, errs

    @staticmethod
    def plan(rng, ld, sc):
        """Planned events: when each run is due, and how much work it gets."""
        ev, t, k = [], ld["first"], 0
        while t < sc["duration"]:
            due = t + rng.uniform(0, ld["jitter"])
            if due < sc["duration"]:
                spread = ld["work_spread"]
                n = ld["units"] * (1 + rng.uniform(-spread, spread)) if spread else ld["units"]
                k += 1
                ev.append({"t": round(due, 3), "load": ld["name"], "kind": "job_due",
                           "value": {"run": k, "units": round(n), "normal_duration": round(
                               round(n) * ld["cost_per_unit"] / (ld["parallelism"] * ld["intensity"]), 1)}})
            t += ld["every"]
        return ev
