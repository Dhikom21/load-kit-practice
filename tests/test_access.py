"""Memory scenarios need permission (allow_flags in loadkit.yaml); the student copy does not allow them."""
import copy
import unittest

from loadkit import scenario
from loadkit.engine import Engine
from tests.helpers import load, needs_scenario, setup


class TestAccess(unittest.TestCase):
    def settings(self, allow):
        s = copy.deepcopy(setup()[0])
        s["allow_flags"] = allow
        return s

    @needs_scenario("memory-squeeze", "slow-leak")
    def test_memory_scenarios_are_flagged(self):
        for n in ("memory-squeeze", "slow-leak"):
            self.assertIn("memory-pressure", load(n)["flags"], n)

    def test_cpu_scenarios_are_not_flagged(self):
        for n in ("normal-day", "midnight-pile-up", "sensor-storm", "priority-squeeze", "backup-snowball"):
            self.assertNotIn("memory-pressure", load(n)["flags"], n)

    def test_a_leak_written_by_a_student_is_flagged_automatically(self):
        sc = copy.deepcopy(load("normal-day"))
        raw = {"name": "my-scenario", "loads": [{"name": "s", "type": "stream", "base_rate": 10,
                                                  "memory": {"leak": "5Mi/min"}}]}
        self.assertIn("memory-pressure", scenario.normalize(raw, setup()[1])["flags"])
        self.assertTrue(sc)

    def test_not_allowed_means_hidden_and_refused(self):
        s, pl = self.settings([]), setup()[1]
        sc = copy.deepcopy(load("normal-day"))
        sc["flags"] = ["memory-pressure"]
        self.assertEqual(scenario.blocked_flags(sc, s), ["memory-pressure"])
        with self.assertRaises(PermissionError):
            Engine(sc, s, pl, runner="fake", record=False)
        if (scenario.SCENARIO_DIR / "memory-squeeze.yaml").exists():
            self.assertNotIn("memory-squeeze", scenario.list_allowed(pl, s))
            self.assertIn("memory-squeeze", scenario.list_allowed(pl, self.settings(["memory-pressure"])))


if __name__ == "__main__":
    unittest.main()
