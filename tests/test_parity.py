"""The simulated node and the real cluster must book the same CPU and memory for every pod of every scenario.

Otherwise a scenario fills the node differently on the two: e.g. high-priority payments consumers booked 1 core
on the real cluster and 0.35 on the simulated node, so only 4 of 8 fit on kind while all 8 ran in simulation.
"""
import random
import unittest

from loadkit.plugins.runners import k8s_objects as K
from loadkit.scenario import SCENARIO_DIR
from tests.fake_cluster import FakeCluster
from tests.helpers import load

ROLE = {"batch": "batch", "stream": "consumer", "users": "shop"}


def real_resources(ld):
    if ld["type"] == "batch":
        obj = K.batch_job("ns", "run-ab12", "loadkit", "img", ld, 1, 10, 60, None)
    elif ld["type"] == "stream":
        obj = K.stream_objects("ns", "run-ab12", "loadkit", "img", ld, None)[1]
    else:
        obj = K.users_objects("ns", "run-ab12", "loadkit", "img", ld, None)[0]
    return obj["spec"]["template"]["spec"]["containers"][0]["resources"]


class TestSameBookingOnBoth(unittest.TestCase):
    def test_every_pod_books_the_same_on_both(self):
        files = sorted(SCENARIO_DIR.rglob("*.yaml"))
        self.assertTrue(files)
        for f in files:
            sc = load(str(f))
            sim = FakeCluster(sc, random.Random(1))
            for ld in sc["loads"]:
                with self.subTest(scenario=f.stem, load=ld["name"]):
                    real = real_resources(ld)
                    p = sim._new_pod(ld, ROLE[ld["type"]])
                    self.assertEqual(real["requests"]["cpu"], K.cpu_str(p.req_cpu), "cpu request")
                    self.assertEqual(real["limits"].get("cpu"), K.cpu_str(p.lim_cpu) if p.lim_cpu else None, "cpu limit")
                    self.assertEqual(real["requests"]["memory"], K.mem_str(p.req_mem), "memory request")
                    self.assertEqual(real["limits"].get("memory"), K.mem_str(p.lim_mem) if p.lim_mem else None,
                                     "memory limit")


if __name__ == "__main__":
    unittest.main()
