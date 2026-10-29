"""Load and validate scenario files (YAML) into a normalised dict the generator and engine use.

All errors are collected and reported together with their location, e.g.
    loads[1] (backup): work.units must be > 0
"""
import hashlib
import json
import pathlib
import re

import yaml

from . import patterns, units
from .settings import ROOT

SCENARIO_DIR = ROOT / "scenarios"
PRIORITIES = ("high", "normal", "low")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")


class ScenarioError(ValueError):
    def __init__(self, errors, source=""):
        self.errors = errors
        super().__init__(f"{source}: " + "; ".join(errors) if source else "; ".join(errors))


def find(name_or_path):
    p = pathlib.Path(name_or_path)
    if p.suffix in (".yaml", ".yml") and p.exists():
        return p
    cand = SCENARIO_DIR / f"{name_or_path}.yaml"
    if cand.exists():
        return cand
    raise FileNotFoundError(f"scenario '{name_or_path}' not found (looked in {SCENARIO_DIR})")


def list_builtin():
    return sorted(p.stem for p in SCENARIO_DIR.glob("*.yaml"))


def _onoff(v):
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("on", "true", "yes", "1")


def normalize_memory(m, defaults, errs, where):
    m = {**defaults, **(m or {})}
    out = {}
    try:
        for k in ("request", "limit", "base", "peak", "per_buffered_event", "per_in_flight"):
            if m.get(k) is not None:
                out[k] = units.memory(m[k])
        out["shape"] = m.get("shape", "flat")
        out["leak_per_s"] = units.rate_per_minute(m["leak"]) if m.get("leak") else 0.0
    except units.UnitError as e:
        errs.append(f"{where}memory: {e}")
        return out
    if out["shape"] not in ("flat", "ramp", "step", "spiky"):
        errs.append(f"{where}memory.shape must be flat|ramp|step|spiky")
    out.setdefault("base", out.get("request", 64 * 1024 ** 2))
    out.setdefault("peak", out["base"])
    if out.get("limit") is not None and out.get("request") is not None and out["limit"] < out["request"]:
        errs.append(f"{where}memory.limit must be >= memory.request")
    return out


def normalize_common(load, plugin, errs, where):
    """Fields every load type has: name, team, priority, cpu request/limit, memory, health."""
    d = plugin.defaults
    out = {"name": load.get("name"), "type": load.get("type"), "team": load.get("team", d.get("team", "default"))}
    if not isinstance(out["name"], str) or not NAME_RE.match(out["name"]):
        errs.append(f"{where}name must be lowercase letters/digits/dashes, starting with a letter")
    pr = load.get("priority", "normal")
    if pr not in PRIORITIES:
        errs.append(f"{where}priority must be one of {PRIORITIES}")
    out["priority"] = pr
    try:
        out["request"] = units.cpu(load.get("request", d.get("request", "100m")))
        lim = load.get("limit", d.get("limit"))
        out["limit"] = None if lim in (None, "none", "None") else units.cpu(lim)
    except units.UnitError as e:
        errs.append(f"{where}{e}")
    out["memory"] = normalize_memory(load.get("memory"), d.get("memory", {}), errs, where)
    h = {**d.get("health", {}), **(load.get("health") or {})}
    try:
        if "threshold" in h and h.get("metric") in ("latency_p95", "event_age"):
            h["threshold_s"] = units.duration(h["threshold"])
        if "window" in h:
            h["window_s"] = units.duration(h["window"])
        if "errors_max" in h:
            h["errors_max_frac"] = units.percent(h["errors_max"])
    except units.UnitError as e:
        errs.append(f"{where}health: {e}")
    out["health"] = h
    return out


def load_file(name_or_path, plugins):
    path = find(name_or_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sc = normalize(raw, plugins, source=str(path.name))
    sc["_source"] = str(path)
    sc["_hash"] = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return sc


def normalize(raw, plugins, source=""):
    errs = []
    lt = plugins["load_types"]
    sc = {"name": raw.get("name"), "description": raw.get("description", ""),
          "story": raw.get("story", ""), "look_for": raw.get("look_for", [])}
    if not isinstance(sc["name"], str) or not NAME_RE.match(sc["name"] or ""):
        errs.append("name must be lowercase letters/digits/dashes")
    try:
        sc["duration"] = units.duration(raw.get("duration", "2h"))
        sc["day_length"] = units.duration(raw.get("day_length", raw.get("duration", "2h")))
    except units.UnitError as e:
        errs.append(str(e))
        sc["duration"] = sc["day_length"] = 7200.0
    sc["day_start_hour"] = float(raw.get("day_start_hour", 0))
    sc["seed"] = int(raw.get("seed", 42))
    cap = raw.get("capacity_hint", {"cpu": 8, "memory": "32Gi"})
    if not isinstance(cap, dict):
        cap = {"cpu": cap}
    try:
        sc["capacity"] = {"cpu": units.cpu(cap.get("cpu", 8)), "memory": units.memory(cap.get("memory", "32Gi"))}
    except units.UnitError as e:
        errs.append(f"capacity_hint: {e}")
        sc["capacity"] = {"cpu": 8.0, "memory": 32 * 1024 ** 3}
    sc["preemption"] = _onoff(raw.get("preemption", False))
    pl = raw.get("placement", {}) or {}
    sc["placement"] = {"mode": pl.get("mode", "together"), "node": pl.get("node")}
    if sc["placement"]["mode"] not in ("together", "spread"):
        errs.append("placement.mode must be together|spread")
    sc["phases"] = raw.get("phases", {})
    sc["flags"] = list(raw.get("flags", []) or [])
    sc["set"] = raw.get("set", "full")                 # "teaching" (20-30 min) or "full" (1-3 h)
    names = set()
    loads = []
    for i, ld in enumerate(raw.get("loads") or []):
        where = f"loads[{i}] ({ld.get('name', '?')}): "
        t = ld.get("type")
        if t not in lt:
            errs.append(f"{where}type '{t}' is not an enabled load type ({', '.join(lt)})")
            continue
        plugin = lt[t]
        common = normalize_common(ld, plugin, errs, where)
        specific, e2 = plugin.validate(ld, sc)
        errs.extend(where + e for e in e2)
        common.update(specific)
        if common["name"] in names:
            errs.append(f"{where}duplicate load name")
        names.add(common["name"])
        loads.append(common)
    if not loads:
        errs.append("a scenario needs at least one load")
    sc["loads"] = loads
    for ld in loads:
        sc_cfg = ld.get("scaler")
        if sc_cfg and sc_cfg.get("name") not in plugins["scalers"]:
            errs.append(f"{ld['name']}: scaler '{sc_cfg.get('name')}' is not an enabled scaler "
                        f"({', '.join(plugins['scalers'])})")
        pat = ld.get("daily_pattern")
        if pat and pat not in patterns.PATTERNS:
            errs.append(f"{ld['name']}: unknown daily_pattern '{pat}' ({', '.join(patterns.names())})")
    if errs:
        raise ScenarioError(errs, source)
    if "memory-pressure" not in sc["flags"] and needs_memory_flag(sc):
        sc["flags"].append("memory-pressure")      # also for scenarios students write themselves
    return sc


def planned_memory(sc):
    """Rough peak memory the scenario's pods are designed to use (bytes)."""
    total = 0.0
    for ld in sc["loads"]:
        m = ld["memory"]
        if ld["type"] == "batch":
            total += m.get("peak", m.get("base", 0)) * ld["parallelism"]
        elif ld["type"] == "stream":
            total += m.get("base", 0) * ld["consumers"][1]
        else:
            total += m.get("base", 0) * ld["shop_replicas"][1]
    return total


def needs_memory_flag(sc):
    """Memory scenarios need explicit permission: a memory leak, or pods designed to fill >= 90 % of the node."""
    if any(ld["memory"].get("leak_per_s") for ld in sc["loads"]):
        return True
    return planned_memory(sc) >= 0.9 * sc["capacity"]["memory"]


def blocked_flags(sc, settings):
    """Flags of this scenario that are NOT allowed in loadkit.yaml (allow_flags). Empty list = allowed."""
    allowed = set((settings or {}).get("allow_flags") or [])
    return [f for f in sc.get("flags", []) if f not in allowed]


def list_allowed(plugins, settings):
    out = []
    for n in list_builtin():
        try:
            sc = load_file(n, plugins)
        except ScenarioError:
            continue
        if not blocked_flags(sc, settings):
            out.append(n)
    return out
