"""target-tracking: the Kubernetes HPA with several metrics (as KEDA sets it up). Each signal computes the replicas it
wants; the largest wins. Streams: CPU + backlog. kit-shop: CPU + in-flight requests (if target_in_flight is set)."""
from ...registry import register
from ._common import ceil_div, finish


@register("scalers")
class TargetTracking:
    name = "target-tracking"

    @staticmethod
    def decide(now, signals, current, cfg, state):
        cpu_target = float(cfg.get("cpu_target", 70))
        per = {}
        running = max(signals.get("running", current), 1)
        ratio = signals.get("cpu_util", 0.0) / cpu_target
        tol = float(cfg.get("tolerance", 0.1))        # HPA default: ignore changes within 10 %
        per["cpu"] = current if abs(ratio - 1) <= tol else ceil_div(running * ratio, 1.0)
        if "backlog" in signals:
            bpc = float(cfg.get("backlog_per_consumer", 100))
            per["backlog"] = ceil_div(signals["backlog"], bpc)
        if "in_flight" in signals and cfg.get("target_in_flight"):
            per["concurrency"] = ceil_div(signals["in_flight"], float(cfg["target_in_flight"]))
        winner = max(per, key=lambda k: per[k])
        reason = ", ".join(f"{k} wants {v}" for k, v in per.items()) + f" -> {winner}"
        return finish(now, per[winner], current, cfg, state, per, winner, reason)
