"""Multi-group sharing of one cluster: names, labels, node choice, leftovers. Kubernetes API is mocked (no cluster)."""
import copy
import random
import types
import unittest
from unittest import mock

from loadkit.plugins.runners import kubernetes as KR
from loadkit.plugins.runners import k8s_objects as K
from tests.helpers import load, setup


def node(name, cpu="8", mem="16Gi"):
    return {"metadata": {"name": name, "labels": {"kubernetes.io/hostname": name}}, "spec": {},
            "status": {"allocatable": {"cpu": cpu, "memory": mem}}}


def pod(group, node_name, phase="Running"):
    return {"metadata": {"labels": {"app.kubernetes.io/part-of": "loadkit", "loadkit/group": group}},
            "spec": {"nodeName": node_name}, "status": {"phase": phase}}


def ns(name):
    return {"metadata": {"name": name}}


def runner(group=None, node_setting=None, nodes=(), pods=(), namespaces=(), own_namespaces=()):
    s = copy.deepcopy(setup()[0])
    s["group"] = group
    s.setdefault("kubernetes", {})["node"] = node_setting
    with mock.patch.object(KR, "_load_config", return_value="test"), mock.patch("kubernetes.client.CoreV1Api"), \
            mock.patch("kubernetes.client.AppsV1Api"), mock.patch("kubernetes.client.BatchV1Api"), \
            mock.patch("kubernetes.client.SchedulingV1Api"), mock.patch("kubernetes.client.CustomObjectsApi"), \
            mock.patch("kubernetes.client.NetworkingV1Api"):
        r = KR.KubernetesRunner(load("midnight-pile-up"), random.Random(1), settings=s)
    r.ser = lambda x: x
    r.core.list_node.return_value = types.SimpleNamespace(items=list(nodes))
    r.core.list_pod_for_all_namespaces.return_value = types.SimpleNamespace(items=list(pods))
    r.core.list_namespace.side_effect = lambda label_selector=None: types.SimpleNamespace(
        items=list(own_namespaces) if label_selector else list(namespaces))
    return r


class TestGroups(unittest.TestCase):
    def test_group_names_are_checked(self):
        self.assertEqual(KR.group_name({"group": "g3"}), "g3")
        self.assertIsNone(KR.group_name({"group": None}))
        with self.assertRaises(ValueError):
            KR.group_name({"group": "Group 3!"})

    def test_names_and_labels_carry_the_group(self):
        r = runner(group="g3")
        self.assertEqual(r.ns["batch"], "g3-batch")
        self.assertEqual(r.prefix, "g3")                         # priority classes g3-high, g3-normal, g3-low
        ld = next(l for l in r.sc["loads"] if l["type"] == "batch")
        job = r._tag(K.batch_job(r.ns["batch"], r.rid, r.prefix, "img", ld, 1, 100, 600, None))
        self.assertEqual(job["metadata"]["labels"]["loadkit/group"], "g3")
        self.assertEqual(job["spec"]["template"]["metadata"]["labels"]["loadkit/group"], "g3")
        self.assertEqual(job["spec"]["template"]["spec"]["priorityClassName"], f"g3-{ld['priority']}")

    def test_single_user_keeps_the_old_names(self):
        r = runner(group=None)
        self.assertEqual(r.ns["batch"], "loadkit-batch")
        self.assertEqual(r.group_label, "default")

    def test_picks_a_node_no_other_group_uses(self):
        r = runner(group="g3", nodes=[node("n1"), node("n2", cpu="6")], pods=[pod("g1", "n1")])
        problems = r.preflight()
        self.assertEqual(problems, [])
        self.assertEqual(r.node, "n2")                           # n1 is busy with group g1

    def test_refuses_a_node_used_by_another_group(self):
        r = runner(group="g3", node_setting="n1", nodes=[node("n1"), node("n2")], pods=[pod("g1", "n1")])
        problems = r.preflight()
        self.assertTrue(problems and "in use by group g1" in problems[0])

    def test_finished_pods_of_others_do_not_block(self):
        r = runner(group="g3", node_setting="n1", nodes=[node("n1")], pods=[pod("g1", "n1", phase="Succeeded")])
        self.assertEqual(r.preflight(), [])

    def test_other_groups_namespaces_are_not_our_leftovers(self):
        r = runner(group="g3", nodes=[node("n1")], namespaces=[ns("g1-batch"), ns("g1-system")])
        self.assertEqual(r.preflight(), [])

    def test_our_own_leftovers_are_found(self):
        r = runner(group="g3", nodes=[node("n1")], namespaces=[ns("g3-batch")])
        problems = r.preflight()
        self.assertTrue(any("g3-batch" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
