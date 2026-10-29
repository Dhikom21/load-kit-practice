"""Seeded generator: scenario + seed -> timeline of PLANNED events only.

Each load gets its own random sub-stream derived from (seed, load name), so adding or removing a load never changes
the others' timings. Reactive events (how long a run takes, scaling, overlaps …) are never in the timeline.
"""
import hashlib
import json
import random


def sub_rng(seed, name):
    h = hashlib.sha256(f"{seed}:{name}".encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def make_timeline(sc, plugins, seed=None):
    seed = sc["seed"] if seed is None else int(seed)
    events = []
    for ld in sc["loads"]:
        plugin = plugins["load_types"][ld["type"]]
        events.extend(plugin.plan(sub_rng(seed, ld["name"]), ld, sc))
    order = {"burst_end": 0, "rate_change": 1, "users_change": 2, "burst_start": 3, "job_due": 4}
    events.sort(key=lambda e: (e["t"], order.get(e["kind"], 9), e["load"]))
    return {"scenario": sc["name"], "scenario_hash": sc.get("_hash"), "seed": seed,
            "duration": sc["duration"], "events": events}


def dumps(timeline):
    return json.dumps(timeline, indent=1, sort_keys=True)
