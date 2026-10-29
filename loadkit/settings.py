"""Kit settings (`loadkit.yaml` in the repo root): which plugins are enabled, naming mode, safety caps."""
import copy
import os
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent

DEFAULTS = {
    "plugins": {
        "load_types": ["batch", "stream", "users"],
        "runners": ["kubernetes"],
        "scalers": ["target-tracking", "concurrency", "none"],
        "metrics": [],
        "forecasters": [],
        "ui_panels": ["machine", "maths", "health-cards", "story", "blame",
                      "timeline", "health", "live-load", "replicas", "shop-clue", "memory", "compare", "diary"],
    },
    "naming": {"mode": "plain"},
    "group": None,              # multi-group sharing of one cluster: e.g. "g3" -> names g3-batch ..., own node
    "allow_flags": [],          # scenarios with these flags may run; e.g. [memory-pressure]. Empty = not allowed.
    "caps": {"max_total_cores": 16, "max_total_memory": "64Gi"},
    "phases": {"warmup": "2m", "baseline": "5m", "drain": "10m"},
    "engine": {"tick": 1.0, "decision_interval": 15.0, "metrics_interval": 15.0},
    "server": {"host": "127.0.0.1", "port": 8765},
    "runs_dir": "runs",
}


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path=None):
    path = pathlib.Path(path or os.environ.get("LOADKIT_SETTINGS", ROOT / "loadkit.yaml"))
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    s = _merge(DEFAULTS, data)
    s["_path"] = str(path)
    return s


def runs_dir(settings):
    p = pathlib.Path(os.environ.get("LOADKIT_RUNS_DIR") or settings.get("runs_dir", "runs"))
    return p if p.is_absolute() else ROOT / p
