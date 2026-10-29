"""Kubernetes runner pieces that can be tested without a cluster: object builders, event detection, channel keys."""
import importlib.util
import pathlib
import unittest

from loadkit import channel
from loadkit.scenario import SCENARIO_DIR
from loadkit.plugins.runners import k8s_objects as K
from loadkit.plugins.runners.k8s_watch import Watcher
from loadkit.plugins.runners.kubernetes import KubernetesRunner, parse_cpu, parse_mem
from tests.helpers import load

ROOT = pathlib.Path(__file__).resolve().parent.parent


def worker_module():
    spec = importlib.util.spec_from_file_location("worker", ROOT / "worker" / "worker.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def pod(name, uid, load, role="batch", phase="Running", ready=True, **status):
    st = {"phase": phase, "containerStatuses": [{"ready": ready, "restartCount": status.pop("restarts", 0),
                                                 **status.pop("cs", {})}], **status}
    return {"metadata": {"name": name, "uid": uid, "labels": {"loadkit/load": load, "loadkit/role": role}},
            "spec": {"containers": [{"resources": {"requests": {"cpu": "500m", "memory": "128Mi"}}}]}, "status": st}


class TestObjects(unittest.TestCase):
    def test_every_pod_has_memory_limit_and_priority(self):
        for n in ("everything", "memory-squeeze", "priority-squeeze", "sensor-storm"):
            if not (SCENARIO_DIR / f"{n}.yaml").exists():
                continue
            sc = load(n)
            for ld in sc["loads"]:
                if ld["type"] == "batch":
                    objs = [K.batch_job("ns", "r", "loadkit", "img", ld, 1, ld["units"], 600, {"x": "y"})]
                elif ld["type"] == "stream":
                    objs = K.stream_objects("ns", "r", "loadkit", "img", ld, None)
                else:
                    objs = [o for o in K.users_objects("ns", "r", "loadkit", "img", ld, None) if o["kind"] != "Service"]
                for o in objs:
                    spec = o["spec"]["template"]["spec"]
                    self.assertTrue(spec["priorityClassName"].startswith("loadkit-"))
                    for c in spec["containers"]:
                        self.assertIn("memory", c["resources"]["limits"], (n, ld["name"]))

    def test_high_priority_is_guaranteed(self):
        ld = next(l for l in load("priority-squeeze")["loads"] if l["name"] == "payments")
        cons = K.stream_objects("ns", "r", "loadkit", "img", ld, None)[1]
        res = cons["spec"]["template"]["spec"]["containers"][0]["resources"]
        self.assertEqual(res["requests"], res["limits"])

    def test_batch_job_is_indexed_and_work_based(self):
        ld = next(l for l in load("midnight-pile-up")["loads"] if l["name"] == "report")
        j = K.batch_job("ns", "r", "loadkit", "img", ld, 3, 4000, 900, {"kubernetes.io/hostname": "n1"})
        self.assertEqual(j["spec"]["completionMode"], "Indexed")
        self.assertEqual(j["spec"]["parallelism"], ld["parallelism"])
        args = j["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertEqual(args[0], "batch")
        self.assertIn("--cost", args)
        self.assertEqual(j["spec"]["template"]["spec"]["nodeSelector"], {"kubernetes.io/hostname": "n1"})

    def test_generated_args_parse_with_the_worker(self):
        parser = worker_module().build_parser()
        for n in ("everything", "slow-leak", "memory-squeeze"):
            if not (SCENARIO_DIR / f"{n}.yaml").exists():
                continue
            for ld in load(n)["loads"]:
                if ld["type"] == "batch":
                    objs = [K.batch_job("ns", "r", "loadkit", "img", ld, 1, ld["units"], 600, None)]
                elif ld["type"] == "stream":
                    objs = K.stream_objects("ns", "r", "loadkit", "img", ld, None)
                else:
                    objs = [o for o in K.users_objects("ns", "r", "loadkit", "img", ld, None) if o["kind"] != "Service"]
                for o in objs:
                    for c in o["spec"]["template"]["spec"]["containers"]:
                        parser.parse_args(c["args"])          # raises SystemExit if the worker would reject them
        b = K.ballast("ns", "r", "loadkit", "img", 2.0, 1024 ** 3, None)
        parser.parse_args(b["spec"]["template"]["spec"]["containers"][0]["args"])

    def test_priority_class_follows_preemption_setting(self):
        self.assertEqual(K.priority_class("loadkit", "high", True, "r")["preemptionPolicy"], "PreemptLowerPriority")
        self.assertEqual(K.priority_class("loadkit", "high", False, "r")["preemptionPolicy"], "Never")


class TestLabels(unittest.TestCase):
    def test_every_label_value_is_valid_for_kubernetes(self):
        """Regression: the ballast once had the label value '_ballast', which Kubernetes rejects (HTTP 422)."""
        import re
        from loadkit import scenario
        ok = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")
        objs = [K.namespace("ns", "run-ab12"), K.priority_class("loadkit", "high", True, "run-ab12"),
                *K.redis_objects("ns", "run-ab12", "loadkit", {"kubernetes.io/hostname": "n1"}),
                K.ballast("ns", "run-ab12", "loadkit", "img", 2.0, 1024 ** 3, None)]
        for n in scenario.list_builtin():
            for ld in load(n)["loads"]:
                if ld["type"] == "batch":
                    objs.append(K.batch_job("ns", "run-ab12", "loadkit", "img", ld, 1, 10, 60, None))
                elif ld["type"] == "stream":
                    objs += K.stream_objects("ns", "run-ab12", "loadkit", "img", ld, None)
                else:
                    objs += K.users_objects("ns", "run-ab12", "loadkit", "img", ld, None)

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ("labels", "matchLabels", "nodeSelector") and isinstance(v, dict):
                        for lk, lv in v.items():
                            self.assertRegex(str(lv), ok, f"label {lk}={lv!r}")
                            self.assertLessEqual(len(str(lv)), 63, f"label {lk}={lv!r} too long")
                    else:
                        walk(v)
            elif isinstance(o, list):
                for x in o:
                    walk(x)
        for o in objs:
            walk(o)


class TestWatcher(unittest.TestCase):
    def test_pending_reported_once(self):
        w = Watcher()
        p = pod("etl-1", "u1", "etl", phase="Pending", ready=False,
                conditions=[{"type": "PodScheduled", "status": "False", "message": "0/1 nodes: Insufficient cpu"}])
        ev = w.update([p]) + w.update([p])
        self.assertEqual([e["event"] for e in ev], ["pod_pending"])
        self.assertIn("Insufficient cpu", ev[0]["cause"]["reason"])

    def test_preemption_names_the_preemptor_from_the_event(self):
        w = Watcher()
        victim = pod("etl-1", "u1", "etl")
        winner = pod("pay-consumer-x", "u9", "payments", role="consumer")
        w.update([victim])
        victim2 = {**victim, "status": {**victim["status"], "conditions": [
            {"type": "DisruptionTarget", "status": "True", "reason": "PreemptionByScheduler", "message": "x"}]}}
        ev = w.update([victim2, winner], events=[{"metadata": {"uid": "e1"}, "reason": "Preempted",
                                                  "involvedObject": {"uid": "u1"},
                                                  "message": "Preempted by pod u9 on node kind-worker"}])
        pre = [e for e in ev if e["event"] == "pod_preempted"]
        self.assertEqual(len(pre), 1)
        self.assertEqual(pre[0]["cause"]["by"], "payments")

    def test_oom_and_restart(self):
        w = Watcher()
        w.update([pod("c-1", "u1", "telemetry", role="consumer")])
        oom = pod("c-1", "u1", "telemetry", role="consumer", ready=False, restarts=1,
                  cs={"lastState": {"terminated": {"reason": "OOMKilled"}}})
        ev = w.update([oom])
        self.assertEqual(ev[0]["event"], "oom_killed")
        w.seen["u1"]["running"] = False
        ev = w.update([pod("c-1", "u1", "telemetry", role="consumer", restarts=1,
                           cs={"lastState": {"terminated": {"reason": "OOMKilled"}}})])
        self.assertEqual([e["event"] for e in ev], ["pod_restarted"])

    def test_eviction_and_memory_pressure(self):
        w = Watcher()
        node = {"status": {"conditions": [{"type": "MemoryPressure", "status": "False"}]}}
        w.update([pod("c-1", "u1", "telemetry")], node=node)
        ev = w.update([pod("c-1", "u1", "telemetry", phase="Failed", ready=False, reason="Evicted",
                           message="The node was low on resource: memory.")],
                      node={"status": {"conditions": [{"type": "MemoryPressure", "status": "True"}]}},
                      memory_top=[{"load": "training", "above_request": 4e9}])
        names = sorted(e["event"] for e in ev)
        self.assertEqual(names, ["node_memory_pressure", "pod_evicted"])
        ev_e = next(e for e in ev if e["event"] == "pod_evicted")
        self.assertEqual(ev_e["cause"]["top_users"][0]["load"], "training")


class TestChannel(unittest.TestCase):
    def test_keys_match_worker(self):
        self.assertEqual(channel.KEYS, worker_module().KEYS)

    def test_parse_units(self):
        self.assertAlmostEqual(parse_cpu("250m"), 0.25)
        self.assertAlmostEqual(parse_cpu("1500000000n"), 1.5)
        self.assertEqual(parse_mem("128Mi"), 128 * 1024 ** 2)

    def test_available_without_cluster_is_explained(self):
        ok, why = KubernetesRunner.available({}, kubeconfig="/nonexistent")
        if not ok:
            self.assertTrue(why)


if __name__ == "__main__":
    unittest.main()


class TestConsumerGroup(unittest.TestCase):
    def test_every_partition_has_exactly_one_owner(self):
        a = worker_module().assignment
        for n in range(1, 10):
            members = [f"pod-{i}" for i in range(n)]
            owned = sorted(k for m in members for k in a(members, m, 4))
            self.assertEqual(owned, [0, 1, 2, 3], n)               # all 4 partitions read, none twice
            idle = sum(1 for m in members if not a(members, m, 4))
            self.assertEqual(idle, max(0, n - 4), n)               # extra consumers are idle

    def test_unknown_pod_gets_nothing(self):
        self.assertEqual(worker_module().assignment(["a", "b"], "c", 4), [])
