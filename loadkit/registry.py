"""Plugin registry.

Plugins register themselves by kind and name; the core never imports a plugin directly. Only plugins listed as
enabled in `loadkit.yaml` are loaded. Removing a name from the list turns that feature off.

Plugin kinds and their interfaces (duck-typed):

  load_type  — class with   name, defaults (dict), validate(load) -> [errors], plan(rng, load, ctx) -> [events]
  runner     — subclass of loadkit.runner_api.Runner (see that file for every method)
  scaler     — class with   name, decide(now, signals, current, cfg, state) -> dict(replicas, reason, per_signal, winner)
  metrics    — class with   name, series(run, since) -> {series_name: [(t, value)]}
  forecaster — class with   name, fit_predict(history, horizon) -> quantiles   (none enabled in v1)
"""
import importlib

KINDS = ("load_types", "runners", "scalers", "metrics", "forecasters")
_REGISTRY = {k: {} for k in KINDS}

# plugin name -> module path (so a name in loadkit.yaml maps to a file)
_MODULES = {
    "load_types": {"batch": "loadkit.plugins.load_types.batch",
                   "stream": "loadkit.plugins.load_types.stream",
                   "users": "loadkit.plugins.load_types.users"},
    "runners": {"kubernetes": "loadkit.plugins.runners.kubernetes",
                "fake": "loadkit.plugins.runners.fake"},
    "scalers": {"target-tracking": "loadkit.plugins.scalers.target_tracking",
                "concurrency": "loadkit.plugins.scalers.concurrency",
                "step": "loadkit.plugins.scalers.step",        # CONNECTION 1 (step example): name -> file
                "none": "loadkit.plugins.scalers.none"},
    "metrics": {},                       # prometheus arrives in milestone M7
    "forecasters": {"reference": "loadkit.plugins.forecasters.reference",
                    "timestral": "loadkit.plugins.forecasters.timestral"},
}

REQUIRED = {
    "load_types": ("name", "defaults", "validate", "plan"),
    "runners": ("name", "available"),
    "scalers": ("name", "decide"),
    "metrics": ("name", "series"),
    "forecasters": ("name", "fit_predict"),
}


class PluginError(RuntimeError):
    pass


def register(kind):
    def deco(cls):
        _REGISTRY[kind][cls.name] = cls
        return cls
    return deco


def load_enabled(settings):
    """Import every enabled plugin. Returns {kind: {name: cls}} for enabled plugins only."""
    enabled = settings["plugins"]
    out = {}
    for kind in KINDS:
        out[kind] = {}
        for name in enabled.get(kind, []):
            mod = _MODULES[kind].get(name)
            if not mod:
                raise PluginError(f"unknown {kind[:-1]} plugin '{name}' in {settings.get('_path')}")
            importlib.import_module(mod)
            cls = _REGISTRY[kind].get(name)
            if cls is None:
                raise PluginError(f"plugin module {mod} did not register '{name}'")
            out[kind][name] = cls
    return out


def contract_errors(kind, cls):
    """Plugin contract check: does the plugin implement its interface?"""
    return [f"{kind}:{getattr(cls, 'name', cls)} lacks '{attr}'" for attr in REQUIRED[kind] if not hasattr(cls, attr)]
