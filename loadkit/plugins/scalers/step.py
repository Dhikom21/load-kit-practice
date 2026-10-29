"""step: add or remove a FIXED number of pods when a signal crosses a band (like "step scaling" in AWS).

WORKED EXAMPLE of a student plugin. Every line is explained. Read it together with:
  * loadkit/registry.py            -> where the name "step" is mapped to this file   (connection 1)
  * loadkit.yaml                   -> where "step" is switched on                     (connection 2)
  * students/examples/sensor-storm-step.yaml -> a scenario that uses it               (connection 3)
  * loadkit/engine.py, _control_loop()       -> the ONLY place that calls decide()    (connection 4)
  * tests/test_step_scaler.py      -> how we prove it works without a cluster         (connection 5)

How it is used in a scenario file:
  scaler: {name: step, signal: backlog, cooldown: 60s,
           steps: [{above: 2000, change: 4}, {above: 500, change: 2}, {below: 100, change: -1}]}
  -> backlog above 2000: +4 pods, above 500: +2 pods, below 100: -1 pod, otherwise: no change.
"""
# --- imports -------------------------------------------------------------------------------------------------------
from ... import units                  # the kit's unit helper: turns "60s" or "2m" into seconds (60.0, 120.0)
from ...registry import register       # the decorator that puts this class on the list of scalers (see below)

# The bands used when the scenario does not give its own. They are checked from top to bottom and the FIRST band
# that matches wins, so the biggest "above" must come first (otherwise "above 500" would also catch 2500).
DEFAULT_STEPS = [
    {"above": 2000, "change": +4},     # a very big pile of waiting events: add 4 pods
    {"above": 500, "change": +2},      # a big pile: add 2 pods
    {"below": 100, "change": -1},      # almost nothing waiting: remove 1 pod
]


# --- the plugin ----------------------------------------------------------------------------------------------------
@register("scalers")                   # CONNECTION 1: "add the class below to the list of scalers under its name".
                                       # The registry (loadkit/registry.py) imports this file when "step" is enabled
                                       # in loadkit.yaml; importing runs this line, and the class is on the list.
class StepScaler:
    name = "step"                      # the name the scenario file uses: scaler: {name: step, ...}

    # THE INTERFACE (the agreement every scaler keeps). The engine calls exactly this, every 15 s, for every stream
    # and user-traffic load that uses this scaler:
    #   now      = engine clock in seconds (0 = start of warm-up)
    #   signals  = what is happening now, from the runner: {"backlog": 820, "cpu_util": 95.0, "current": 3, ...}
    #   current  = how many pods the load has now
    #   cfg      = the scaler settings from the scenario file, plus "min" and "max" (added by the engine)
    #   state    = a dict that belongs to this load and survives between calls: our memory (here: last change time)
    # It must return a dict with: replicas (how many pods there should be), per_signal, winner, reason.
    @staticmethod                      # no "self": the engine calls StepScaler.decide(...) directly on the class
    def decide(now, signals, current, cfg, state):
        signal = cfg.get("signal", "backlog")            # which signal to watch (default: waiting events)
        value = float(signals.get(signal, 0.0))          # its value right now, e.g. backlog = 820 events
        steps = cfg.get("steps") or DEFAULT_STEPS        # the bands from the scenario file, or the defaults above

        change, why = 0, f"{signal} {value:.0f} is inside the quiet band"   # if no band matches: keep the pod count
        for band in steps:                               # check the bands from top to bottom ...
            if "above" in band and value > band["above"]:            # ... e.g. 820 > 500?
                change, why = band["change"], f"{signal} {value:.0f} > {band['above']}"
                break                                    # ... first match wins: stop looking
            if "below" in band and value < band["below"]:            # ... e.g. 40 < 100?
                change, why = band["change"], f"{signal} {value:.0f} < {band['below']}"
                break

        # Cooldown: after a change, wait before the next one. New pods need time to start and to eat into the
        # backlog; without a cooldown the scaler would add pods every 15 s while the first ones are still starting.
        cooldown = units.duration(cfg.get("cooldown", "60s"))               # "60s" -> 60.0
        since = now - state.get("last_change", -1e9)                        # seconds since our last change
        if change != 0 and since < cooldown:
            change, why = 0, f"{why}, but waiting {cooldown - since:.0f} s more (cooldown {cooldown:.0f} s)"

        # Never go below the minimum or above the maximum pods from the scenario ("consumers: [2, 12]").
        lo, hi = int(cfg.get("min", 1)), int(cfg.get("max", 10))
        wanted = max(lo, min(hi, current + change))      # e.g. current 3, change +2 -> 5 (if 5 <= max)

        if wanted != current:                            # we really change something:
            state["last_change"] = now                   # remember when, for the cooldown next time

        # The answer, in the agreed shape. The engine scales the load to "replicas" and writes the rest into the
        # diary (event consumers_scaled / shop_scaled), so the UI and the Excel export can show WHY.
        return {
            "replicas": wanted,
            "wanted": wanted,
            "per_signal": {signal: round(value)},        # what we looked at (shown as "backlog wanted ...")
            "winner": signal,                            # the signal that decided (only one here)
            "reason": f"{why} -> {current} {change:+d} = {wanted}",
        }
