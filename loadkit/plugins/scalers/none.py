"""none: fixed replicas (control). Keeps `replicas` (default: the minimum)."""
from ...registry import register


@register("scalers")
class NoScaler:
    name = "none"

    @staticmethod
    def decide(now, signals, current, cfg, state):
        n = int(cfg.get("replicas", cfg.get("min", current)))
        return {"replicas": n, "wanted": n, "per_signal": {"fixed": n}, "winner": "fixed", "reason": "fixed replicas"}
