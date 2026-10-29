"""concurrency: in-flight requests per pod (Knative-style). replicas = ceil(in_flight / target_in_flight).
In-flight = request rate x response time (Little's law), so it also rises when requests get SLOWER — e.g. because a
neighbour fills the machine — and the scaler then adds pods although traffic did not change."""
from ...registry import register
from ._common import ceil_div, finish


@register("scalers")
class Concurrency:
    name = "concurrency"

    @staticmethod
    def decide(now, signals, current, cfg, state):
        target = float(cfg.get("target_in_flight", 10))
        per = {"concurrency": ceil_div(signals.get("in_flight", signals.get("backlog", 0.0)), target)}
        if cfg.get("cpu_target"):
            running = max(signals.get("running", current), 1)
            ratio = signals.get("cpu_util", 0.0) / float(cfg["cpu_target"])
            per["cpu"] = current if abs(ratio - 1) <= 0.1 else ceil_div(running * ratio, 1.0)
        winner = max(per, key=lambda k: per[k])
        reason = (f"in-flight {signals.get('in_flight', 0):.1f} / target {target:g} per pod"
                  + (f", request rate {signals['request_rate']:.1f}/s" if "request_rate" in signals else ""))
        return finish(now, per[winner], current, cfg, state, per, winner, reason)
