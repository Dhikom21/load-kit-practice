"""test_determinism, test_independence, test_schema, test_generator_rules, test_origin (timeline part)."""
import copy
import unittest

from loadkit import generator, scenario
from tests.helpers import load, setup


class TestGenerator(unittest.TestCase):
    def test_all_builtin_scenarios_valid(self):
        names = scenario.list_builtin()
        self.assertGreaterEqual(len(names), 5)        # 3 experiment pairs (+2 memory in research); the rest are in advanced/
        adv = scenario.SCENARIO_DIR / "advanced"
        for n in names + [p.stem for p in adv.glob("*.yaml")]:
            with self.subTest(n):
                load(n)

    def test_same_seed_same_timeline_bytes(self):
        _, pl = setup()
        for n in scenario.list_builtin():
            sc = load(n)
            a = generator.dumps(generator.make_timeline(sc, pl, 42))
            b = generator.dumps(generator.make_timeline(load(n), pl, 42))
            self.assertEqual(a, b, n)

    def test_different_seed_differs(self):
        _, pl = setup()
        sc = load("everything")
        self.assertNotEqual(generator.make_timeline(sc, pl, 1)["events"], generator.make_timeline(sc, pl, 2)["events"])

    def test_adding_a_load_keeps_other_timings(self):
        _, pl = setup()
        sc = load("everything")
        base = generator.make_timeline(sc, pl, 42)["events"]
        sc2 = copy.deepcopy(sc)
        sc2["loads"] = [ld for ld in sc2["loads"] if ld["name"] != "telemetry"]
        less = generator.make_timeline(sc2, pl, 42)["events"]
        self.assertEqual([e for e in base if e["load"] != "telemetry"], less)

    def test_rules(self):
        _, pl = setup()
        sc = load("everything")
        tl = generator.make_timeline(sc, pl, 9)
        lds = {ld["name"]: ld for ld in sc["loads"]}
        for e in tl["events"]:
            self.assertTrue(0 <= e["t"] <= sc["duration"])
            self.assertIn(e["kind"], ("job_due", "burst_start", "burst_end", "rate_change", "users_change"))
            if e["kind"] == "job_due":
                ld = lds[e["load"]]
                k = e["value"]["run"] - 1
                slot = ld["first"] + k * ld["every"]
                self.assertTrue(slot <= e["t"] <= slot + ld["jitter"] + 1e-6)

    def test_bad_files_rejected_with_clear_messages(self):
        _, pl = setup()
        bad = {"name": "Bad Name", "loads": [{"name": "x", "type": "batch"},
                                             {"name": "y", "type": "nosuch"},
                                             {"name": "s", "type": "stream", "base_rate": 5, "scaler": {"name": "magic"}}]}
        with self.assertRaises(scenario.ScenarioError) as cm:
            scenario.normalize(bad, pl)
        msg = " | ".join(cm.exception.errors)
        for part in ("name must be", "'every' is required", "type 'nosuch'", "scaler 'magic'"):
            self.assertIn(part, msg)


if __name__ == "__main__":
    unittest.main()
