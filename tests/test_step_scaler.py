"""CONNECTION 5 (step example): prove the step scaler works WITHOUT a cluster.

A scaler is a pure function: signals in -> decision out. So we can test it by calling decide() with made-up signals
and checking the answer. No Kubernetes, no Redis, runs in milliseconds.   Run:  python -m unittest tests.test_step_scaler
"""
import unittest                                     # Python's built-in test framework (no extra package needed)

from loadkit.plugins.scalers.step import StepScaler  # the plugin under test, imported directly
from loadkit.registry import contract_errors         # the check "does a plugin keep its interface?"
from tests.helpers import load, run, events   # small helpers shared by all tests (scenario loading, runs)

# The settings a scenario would give, plus min/max (the engine adds these from "consumers: [2, 12]").
CFG = {"name": "step", "signal": "backlog", "cooldown": "60s", "min": 2, "max": 12,
       "steps": [{"above": 2000, "change": 4}, {"above": 500, "change": 2}, {"below": 100, "change": -1}]}


def decide(backlog, current=3, now=1000.0, state=None):
    """Call the scaler the same way the engine does (engine.py, _control_loop)."""
    return StepScaler.decide(now, {"backlog": backlog}, current, CFG, {} if state is None else state)


class TestStepScaler(unittest.TestCase):
    def test_keeps_its_interface(self):
        # The same check the kit runs for every enabled plugin: does it have "name" and "decide"?
        self.assertEqual(contract_errors("scalers", StepScaler), [])

    def test_big_backlog_adds_two(self):
        d = decide(backlog=820)                      # 820 > 500 -> +2
        self.assertEqual(d["replicas"], 5)           # 3 + 2
        self.assertIn("820 > 500", d["reason"])      # the reason the UI and the diary will show

    def test_first_matching_band_wins(self):
        self.assertEqual(decide(backlog=2500)["replicas"], 7)   # 2500 > 2000 -> +4 (not +2: first match wins)

    def test_small_backlog_removes_one(self):
        self.assertEqual(decide(backlog=40)["replicas"], 2)     # 40 < 100 -> -1

    def test_quiet_band_changes_nothing(self):
        self.assertEqual(decide(backlog=300)["replicas"], 3)    # between 100 and 500 -> no band -> stay at 3

    def test_never_outside_min_and_max(self):
        self.assertEqual(decide(backlog=40, current=2)["replicas"], 2)      # min is 2
        self.assertEqual(decide(backlog=9999, current=11)["replicas"], 12)  # max is 12 (11 + 4 would be 15)

    def test_cooldown_blocks_a_second_change(self):
        state = {}                                                   # the scaler's memory for one load
        first = decide(backlog=820, current=3, now=1000.0, state=state)
        self.assertEqual(first["replicas"], 5)                       # change at t = 1000 s
        second = decide(backlog=820, current=5, now=1030.0, state=state)
        self.assertEqual(second["replicas"], 5)                      # 30 s later: still cooling down
        self.assertIn("cooldown", second["reason"])
        third = decide(backlog=820, current=5, now=1061.0, state=state)
        self.assertEqual(third["replicas"], 7)                       # 61 s later: allowed again

    def test_example_scenario_is_valid_and_uses_step(self):
        sc = load("students/examples/sensor-storm-step.yaml")       # the kit's own validation runs here
        tel = next(ld for ld in sc["loads"] if ld["name"] == "telemetry")
        self.assertEqual(tel["scaler"]["name"], "step")

    def test_engine_calls_it_and_writes_the_reason(self):
        # End-to-end on the test-only fake cluster: the engine must call the step scaler and log its decisions.
        e = run("students/examples/sensor-storm-step.yaml")
        scaled = events(e, "consumers_scaled", "telemetry")
        self.assertTrue(scaled)
        self.assertTrue(all(x["cause"]["scaler"] == "step" for x in scaled))
        self.assertTrue(all(abs(x["to"] - x["from"]) in (1, 2, 4) for x in scaled))   # only the band sizes


if __name__ == "__main__":
    unittest.main()
