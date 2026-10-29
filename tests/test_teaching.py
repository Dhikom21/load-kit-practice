"""Short teaching scenarios: short, small, and they still show what they are meant to show (on the fake cluster)."""
import unittest

from loadkit import scenario
from tests.helpers import cached, events, load

TEACHING = [n for n in scenario.list_builtin() if n.endswith("-short")]


class TestTeaching(unittest.TestCase):
    def test_short_small_and_marked(self):
        self.assertGreaterEqual(len(TEACHING), 9)
        for n in TEACHING:
            sc = load(n)
            self.assertEqual(sc["set"], "teaching", n)
            self.assertLessEqual(sc["duration"], 30 * 60, n)
            self.assertLessEqual(sc["capacity"]["cpu"], 4, n)
            self.assertNotIn("memory-pressure", sc["flags"], n)

    def test_normal_day_short_stays_healthy(self):
        self.assertFalse(cached("normal-day-short").periods)

    def test_pile_up_short_slows_all_three_jobs(self):
        e = cached("midnight-pile-up-short")
        self.assertEqual({p["load"] for p in e.periods} & {"report", "backup", "training"},
                         {"report", "backup", "training"})

    def test_snowball_short_grows(self):
        self.assertGreaterEqual(max(x["active_runs"] for x in events(cached("backup-snowball-short"), "run_overlapped")), 3)

    def test_idle_consumers_short(self):
        e = cached("idle-consumers-short")
        self.assertTrue(any(x["idle"] > 0 for x in events(e, "consumers_idle")))
        self.assertTrue(any(p["load"] == "clicks" for p in e.periods))

    def test_priority_squeeze_short_preempts(self):
        e = cached("priority-squeeze-short")
        self.assertTrue(events(e, "pod_preempted", "nightly-etl"))


if __name__ == "__main__":
    unittest.main()
