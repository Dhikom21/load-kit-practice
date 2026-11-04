"""Kubernetes runner: real pods on a real cluster (kind, minikube, Rancher …).

What it does
  * setup     : namespaces, priority classes (high/normal/low), the kit's Redis (password per run), and — if the node is
                bigger than the scenario's node size — a "ballast" pod that really uses the extra CPU/memory, so the
                scenario sees the node size it was written for.
  * batch     : one Indexed Job per run (parallelism pods; each does its share of the units).
  * streams   : producer + consumer Deployments; the engine sets the rate (Redis) and scales consumers.
  * users     : shop Deployment + Service, users Deployment; the engine sets the number of users (Redis).
  * observing : pods / events / node conditions (Kubernetes API), CPU and memory (metrics-server), backlog, event
                age, shop and users stats (Redis).
  * cleanup   : deletes the run's namespaces and priority classes.
All kit pods are placed on ONE node (placement: together) so the loads really share a machine.

Your kubeconfig is only read, never stored. Inside the cluster the in-cluster service account is used.
"""
import os
import pathlib
import re
import secrets
import time
import uuid

from ...registry import register
from ...runner_api import Runner
from ... import units as U
from ...channel import PortForwardBridge, key
from . import k8s_objects as K
from .k8s_watch import Watcher, is_ready, is_waiting, pod_load, pod_role, unscheduled_reason


def detect(kubeconfig=None):
    if pathlib.Path("/var/run/secrets/kubernetes.io/serviceaccount/token").exists():
        return "in-cluster"
    path = kubeconfig or os.environ.get("KUBECONFIG") or str(pathlib.Path.home() / ".kube" / "config")
    if path and pathlib.Path(path).exists():
        return "kubeconfig"
    return None


def _load_config(kubeconfig=None):
    from kubernetes import config
    if detect(kubeconfig) == "in-cluster":
        config.load_incluster_config()
        return "in-cluster"
    config.load_kube_config(config_file=kubeconfig or os.environ.get("KUBECONFIG"))
    try:
        _, active = config.list_kube_config_contexts(config_file=kubeconfig or os.environ.get("KUBECONFIG"))
        return active.get("name", "kubeconfig")
    except Exception:  # noqa: BLE001
        return "kubeconfig"


GROUP_RE = re.compile(r"^[a-z][a-z0-9-]{0,19}$")


def group_name(settings):
    """The group this kit belongs to (multi-group sharing of one cluster). None = single user (names start with loadkit-)."""
    g = (settings or {}).get("group")
    if g in (None, "", "null"):
        return None
    g = str(g)
    if not GROUP_RE.match(g):
        raise ValueError(f"group '{g}' must be lowercase letters, digits or '-', start with a letter, max 20 chars")
    return g


def parse_cpu(v):
    v = str(v)
    if v.endswith("n"):
        return float(v[:-1]) / 1e9
    if v.endswith("u"):
        return float(v[:-1]) / 1e6
    if v.endswith("m"):
        return float(v[:-1]) / 1e3
    return float(v)


def parse_mem(v):
    return float(U.memory(str(v)))


@register("runners")
class KubernetesRunner(Runner):
    name = "kubernetes"
    label = "Kubernetes"
    realtime = True

    # ---------------------------------------------------------------- availability
    @staticmethod
    def available(settings, kubeconfig=None):
        try:
            import kubernetes  # noqa: F401
            import redis  # noqa: F401
        except ImportError as e:
            return False, f"missing Python package: {e.name} (pip install kubernetes redis)"
        where = detect(kubeconfig)
        if not where:
            return False, "no cluster found (no in-cluster service account, no --kubeconfig / KUBECONFIG / ~/.kube/config)"
        try:
            from kubernetes import client
            ctx = _load_config(kubeconfig)
            client.VersionApi().get_code(_request_timeout=4)
            return True, f"Kubernetes · {ctx}"
        except Exception as e:  # noqa: BLE001
            return False, f"cluster not reachable ({type(e).__name__})"

    # ---------------------------------------------------------------- init
    def __init__(self, sc, rng, settings=None, kubeconfig=None, **kw):
        from kubernetes import client
        self.sc, self.rng = sc, rng
        self.cfg = (settings or {}).get("kubernetes", {}) or {}
        self.where = _load_config(kubeconfig)
        self.label = f"Kubernetes · {self.where}"
        self.core, self.apps, self.batchapi = client.CoreV1Api(), client.AppsV1Api(), client.BatchV1Api()
        self.sched, self.custom, self.netapi = client.SchedulingV1Api(), client.CustomObjectsApi(), client.NetworkingV1Api()
        self.ser = client.ApiClient().sanitize_for_serialization
        # MULTI-GROUP: every name and label carries the group, so groups sharing one cluster never touch each other
        self.group = group_name(settings)                      # e.g. "g3", or None for a single user
        self.group_label = self.group or "default"
        self.prefix = self.group or self.cfg.get("namespace_prefix", "loadkit")   # g3-batch / loadkit-batch
        self.image = self.cfg.get("image", "loadkit-worker:0.1.0")
        self.rid = f"{sc['name'][:30]}-{uuid.uuid4().hex[:6]}"
        self.ns = {k: f"{self.prefix}-{k}" for k in ("system", "batch", "stream", "users")}
        self.loads = {ld["name"]: ld for ld in sc["loads"]}
        self.node = None
        self.node_selector = None
        self.alloc_cpu = self.alloc_mem = None
        self.ballast_cpu = self.ballast_mem = 0.0
        self.redis = None
        self.bridge = None
        self.watch = Watcher()
        self.runs = {}                       # key -> dict(load, run, job, units, start, normal, state)
        self.now = 0.0
        self.pods = []
        self.pod_metrics = {}                # pod name -> (cpu cores, memory bytes)
        self.node_metrics = (0.0, 0.0)
        self.stats = {}
        self._last = {"pods": -1e9, "metrics": -1e9}
        self._written = {}
        self._produced = {}
        self._idle = {}
        self._replicas = {}
        self.info = {}

    # ---------------------------------------------------------------- preflight / setup / cleanup
    def preflight(self):
        problems = []
        try:
            nodes = [self.ser(n) for n in self.core.list_node().items]
        except Exception as e:  # noqa: BLE001
            return [f"cannot list nodes: {e}"]
        want = self.cfg.get("node")
        busy = self._nodes_used_by_other_groups()
        cands, taken = [], []
        for n in nodes:
            md, spec = n["metadata"], n.get("spec") or {}
            if spec.get("unschedulable"):
                continue
            if any(t.get("effect") == "NoSchedule" for t in spec.get("taints") or []):
                continue
            if want and "=" in want:
                k, v = want.split("=", 1)
                if (md.get("labels") or {}).get(k) != v:
                    continue
            elif want and md["name"] != want:
                continue
            if md["name"] in busy and self.sc["placement"]["mode"] == "together":
                taken.append(f"{md['name']} (in use by group {', '.join(sorted(busy[md['name']]))})")
                continue
            cands.append(n)
        if not cands:
            if taken:
                return ["no free node: " + "; ".join(taken) + ". Wait for the other run to finish, or set "
                        "kubernetes.node in loadkit.yaml to your group's own node"]
            return [f"no schedulable node{' matching ' + want if want else ''}"]
        best = max(cands, key=lambda n: parse_cpu(n["status"]["allocatable"]["cpu"]))
        self.node = best["metadata"]["name"]
        self.node_selector = {"kubernetes.io/hostname": best["metadata"]["labels"].get("kubernetes.io/hostname",
                                                                                         self.node)}
        if self.sc["placement"]["mode"] == "spread":
            self.node_selector = None
        self.alloc_cpu = parse_cpu(best["status"]["allocatable"]["cpu"])
        self.alloc_mem = parse_mem(best["status"]["allocatable"]["memory"])
        hint_cpu, hint_mem = self.sc["capacity"]["cpu"], self.sc["capacity"]["memory"]
        headroom_cpu, headroom_mem = 0.5, 512 * 1024 ** 2          # kubelet, Redis, system pods
        if self.cfg.get("size_node_to_scenario", True) and self.node_selector:
            self.ballast_cpu = max(0.0, round(self.alloc_cpu - hint_cpu - headroom_cpu, 2))
            self.ballast_mem = max(0.0, self.alloc_mem - hint_mem - headroom_mem)
            if self.ballast_cpu < 0.25:
                self.ballast_cpu = 0.0
            if self.ballast_mem < 256 * 1024 ** 2:
                self.ballast_mem = 0.0
        if self.alloc_cpu < hint_cpu:
            problems_note = (f"node {self.node} has {self.alloc_cpu:g} cores, the scenario expects {hint_cpu:g}: "
                             "contention will be stronger than designed")
            self.info["warning"] = problems_note
        try:
            self.custom.list_namespaced_custom_object("metrics.k8s.io", "v1beta1", "kube-system", "pods", limit=1)
        except Exception:  # noqa: BLE001
            problems.append("metrics-server is not installed (on kind: make kind-metrics)")
        mine = f"app.kubernetes.io/part-of=loadkit,loadkit/group={self.group_label}"
        left = {self.ser(n)["metadata"]["name"] for n in self.core.list_namespace(label_selector=mine).items}
        existing = {self.ser(n)["metadata"]["name"] for n in self.core.list_namespace().items}
        left |= set(self.ns.values()) & existing             # our names already taken (also by older kit versions)
        if left:
            problems.append("leftovers from an earlier run of this group: " + ", ".join(sorted(left)) +
                            " (run `python -m loadkit reset`)")
        self.info.update(group=self.group_label, node=self.node, node_cpu=self.alloc_cpu, node_memory=self.alloc_mem,
                         ballast_cpu=self.ballast_cpu, ballast_memory=self.ballast_mem, image=self.image,
                         connection=self.where)
        return problems

    def _nodes_used_by_other_groups(self):
        """node name -> {groups} for nodes where ANOTHER group's kit pods are running right now."""
        busy = {}
        try:
            pods = self.core.list_pod_for_all_namespaces(label_selector="app.kubernetes.io/part-of=loadkit").items
        except Exception:  # noqa: BLE001
            return busy
        for p in pods:
            p = self.ser(p)
            g = ((p.get("metadata") or {}).get("labels") or {}).get("loadkit/group", "default")
            node = (p.get("spec") or {}).get("nodeName")
            phase = (p.get("status") or {}).get("phase")
            if node and g != self.group_label and phase in ("Pending", "Running"):
                busy.setdefault(node, set()).add(g)
        return busy

    def _tag(self, obj):
        """Add loadkit/group to every label set the kit creates (namespaces, pods, jobs, priority classes ...)."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "labels" and isinstance(v, dict) and "loadkit/run" in v:
                    v["loadkit/group"] = self.group_label
                else:
                    self._tag(v)
        elif isinstance(obj, list):
            for x in obj:
                self._tag(x)
        return obj

    def _create(self, fn, *a, body):
        from kubernetes.client.rest import ApiException
        body = self._tag(body)
        try:
            return fn(*a, body=body)
        except ApiException as e:
            if e.status != 409:              # already exists is fine
                raise

    def setup(self):
        import redis
        pw = secrets.token_hex(16)
        url = f"redis://:{pw}@redis.{self.ns['system']}.svc.cluster.local:6379/0"
        for lvl in ("high", "normal", "low", "system"):
            name = f"{self.prefix}-{lvl}"
            try:
                self.sched.delete_priority_class(name)
            except Exception:  # noqa: BLE001
                pass
            body = K.priority_class(self.prefix, lvl, self.sc["preemption"] if lvl != "system" else True, self.rid)
            self._create(self.sched.create_priority_class, body=body)
        for ns in self.ns.values():
            self._create(self.core.create_namespace, body=K.namespace(ns, self.rid))
            self._create(self.core.create_namespaced_secret, ns, body=K.secret(ns, self.rid, url, pw))
        dep, svc, netpol = K.redis_objects(self.ns["system"], self.rid, self.prefix, self.node_selector)
        # only THIS group's namespaces may reach this group's Redis
        netpol["spec"]["ingress"] = [{"from": [{"namespaceSelector": {"matchLabels": {
            "app.kubernetes.io/part-of": "loadkit", "loadkit/group": self.group_label}}}]}]
        self._create(self.apps.create_namespaced_deployment, self.ns["system"], body=dep)
        self._create(self.core.create_namespaced_service, self.ns["system"], body=svc)
        self._create(self.netapi.create_namespaced_network_policy, self.ns["system"], body=netpol)
        if self.ballast_cpu or self.ballast_mem:
            self._create(self.apps.create_namespaced_deployment, self.ns["system"],
                         body=K.ballast(self.ns["system"], self.rid, self.prefix, self.image,
                                        max(self.ballast_cpu, 0.05), self.ballast_mem, self.node_selector))
        pod = self._wait_ready(self.ns["system"], "app=loadkit-redis")
        if detect() == "in-cluster":
            local = url
        else:
            self.bridge = PortForwardBridge(self.core, self.ns["system"],
                                            lambda: self._wait_ready(self.ns["system"], "app=loadkit-redis", 30))
            local = f"redis://:{pw}@127.0.0.1:{self.bridge.port}/0"
        self.redis = redis.Redis.from_url(local, decode_responses=True, socket_timeout=5,
                                          retry_on_timeout=True, health_check_interval=10)
        for attempt in range(30):
            try:
                self.redis.ping()
                break
            except redis.RedisError:
                time.sleep(1)
        else:
            raise RuntimeError(f"cannot reach the kit's Redis (pod {pod})")
        self.redis.set(key("run_state"), "running")

    def _wait_ready(self, ns, selector, timeout=None):
        end = time.monotonic() + (timeout or self.cfg.get("setup_timeout", 300))
        while time.monotonic() < end:
            for p in self.core.list_namespaced_pod(ns, label_selector=selector).items:
                if is_ready(self.ser(p)):
                    return p.metadata.name
            time.sleep(1)
        raise RuntimeError(f"timed out waiting for {selector} in {ns} (is the image available on the node?)")

    def cleanup(self):
        try:
            if self.redis is not None:
                self.redis.set(key("run_state"), "stopping")
        except Exception:  # noqa: BLE001
            pass
        for ns in self.ns.values():
            try:
                self.core.delete_namespace(ns)
            except Exception:  # noqa: BLE001
                pass
        for lvl in ("high", "normal", "low", "system"):
            try:
                self.sched.delete_priority_class(f"{self.prefix}-{lvl}")
            except Exception:  # noqa: BLE001
                pass
        if self.bridge:
            self.bridge.close()

    def calibration(self):
        return {"source": "cpu-time", "node": self.node,
                "note": "work units are measured in CPU-seconds by the worker, so cost_per_unit is exact on any node",
                "cost_per_unit_s": {n: ld["cost_per_unit"] for n, ld in self.loads.items() if ld["type"] == "batch"},
                "ballast_cpu": self.ballast_cpu, "ballast_memory": self.ballast_mem}

    # ---------------------------------------------------------------- services
    def setup_service(self, name):
        ld = self.loads[name]
        if ld["type"] == "stream":
            objs = K.stream_objects(self.ns["stream"], self.rid, self.prefix, self.image, ld, self.node_selector)
            ns = self.ns["stream"]
            self._replicas[name] = ld["consumers"][0]
        else:
            objs = K.users_objects(self.ns["users"], self.rid, self.prefix, self.image, ld, self.node_selector)
            ns = self.ns["users"]
            self._replicas[name] = ld["shop_replicas"][0]
        for o in objs:
            if o["kind"] == "Deployment":
                self._create(self.apps.create_namespaced_deployment, ns, body=o)
            else:
                self._create(self.core.create_namespaced_service, ns, body=o)

    def _set(self, k, v):
        if self._written.get(k) != v:
            try:
                self.redis.set(k, v)
                self._written[k] = v
            except Exception:  # noqa: BLE001 — retried next tick
                pass

    def set_rate(self, name, rate):
        self._set(key("rate", load=name), round(float(rate), 3))

    def set_users(self, name, users):
        self._set(key("users", load=name), int(users))

    def replicas(self, name):
        return self._replicas.get(name, 0)

    def _deploy_name(self, name):
        ld = self.loads[name]
        return (f"{name}-consumer", self.ns["stream"]) if ld["type"] == "stream" else (f"{name}-shop", self.ns["users"])

    def scale(self, name, n):
        dep, ns = self._deploy_name(name)
        self.apps.patch_namespaced_deployment_scale(dep, ns, {"spec": {"replicas": int(n)}})
        self._replicas[name] = int(n)

    # ---------------------------------------------------------------- batch runs
    def start_run(self, name, run, units):
        ld = self.loads[name]
        normal = units * ld["cost_per_unit"] / (ld["parallelism"] * ld["intensity"])
        job = K.batch_job(self.ns["batch"], self.rid, self.prefix, self.image, ld, run, units,
                          ld["deadline_factor"] * normal, self.node_selector)
        self.batchapi.create_namespaced_job(self.ns["batch"], self._tag(job))
        k = f"{name}#{run}"
        self.runs[k] = {"load": name, "run": run, "job": job["metadata"]["name"], "units": units,
                        "start": self.now, "normal": normal, "state": "running", "done": 0}
        return k

    def stop_run(self, k, reason):
        r = self.runs.get(k)
        if not r or r["state"] != "running":
            return
        r["state"] = reason
        try:
            self.batchapi.delete_namespaced_job(r["job"], self.ns["batch"], propagation_policy="Background")
        except Exception:  # noqa: BLE001
            pass

    def active_runs(self, name=None):
        return [k for k, r in self.runs.items() if r["state"] == "running" and (name is None or r["load"] == name)]

    def run_info(self, k):
        r = self.runs[k]
        return {"elapsed": self.now - r["start"], "normal": r["normal"], "done": r["done"], "units": r["units"],
                "state": r["state"]}

    # ---------------------------------------------------------------- observing
    def step(self, now, dt):
        self.now = now
        out = []
        self._read_redis()
        if now - self._last["metrics"] >= float(self.cfg.get("metrics_every", 5)):
            self._last["metrics"] = now
            self._read_metrics()
        if now - self._last["pods"] >= float(self.cfg.get("pod_poll_every", 2)):
            self._last["pods"] = now
            out += self._poll_cluster()
        return out

    def _units_done(self, r):
        """Units finished by one run right now (sum of its pods' progress counters), or None if Redis is unreachable."""
        try:
            n = self.loads[r["load"]]["parallelism"]
            vals = self.redis.mget([key("progress", load=r["load"], run=r["run"], index=i) for i in range(n)])
            return sum(int(x or 0) for x in vals)
        except Exception:  # noqa: BLE001
            return None

    def _read_redis(self):
        """Progress of runs, backlog, event age, shop/users stats — one pipeline per tick."""
        try:
            pipe = self.redis.pipeline(transaction=False)
            plan = []
            for k, r in self.runs.items():
                if r["state"] == "running":
                    ld = self.loads[r["load"]]
                    for i in range(ld["parallelism"]):
                        pipe.get(key("progress", load=r["load"], run=r["run"], index=i))
                    plan.append(("run", k, ld["parallelism"]))
            for name, ld in self.loads.items():
                if ld["type"] == "stream":
                    for p in range(ld["partitions"]):
                        pipe.llen(key("partition", load=name, k=p))
                        pipe.lindex(key("partition", load=name, k=p), 0)
                    pipe.get(key("produced", load=name))
                    pipe.get(key("processed", load=name))
                    pipe.keys(key("idle", load=name, pod="*"))
                    plan.append(("stream", name, ld["partitions"]))
                elif ld["type"] == "users":
                    pipe.hgetall(key("users_stat", load=name))
                    pipe.keys(key("shop", load=name, pod="*"))
                    plan.append(("users", name, 0))
            res = pipe.execute()
        except Exception:  # noqa: BLE001 — Redis briefly unreachable: keep the last values
            return
        i, wall = 0, time.time()
        for kind, name, n in plan:
            if kind == "run":
                self.runs[name]["done"] = sum(int(x or 0) for x in res[i:i + n])
                i += n
            elif kind == "stream":
                lens = res[i:i + 2 * n:2]
                heads = [float(x) for x in res[i + 1:i + 2 * n:2] if x]
                i += 2 * n
                produced, processed, idle = int(res[i] or 0), int(res[i + 1] or 0), res[i + 2]
                i += 3
                prev = self._produced.get(name)
                st = self.stats.setdefault(name, {})
                if prev:
                    dtw = max(wall - prev[0], 1e-3)
                    st["arrival_rate"] = (produced - prev[1]) / dtw
                    st["processed_rate"] = (processed - prev[2]) / dtw
                self._produced[name] = (wall, produced, processed)
                st["backlog"] = float(sum(lens))
                st["event_age"] = max(0.0, wall - min(heads)) if heads else 0.0
                st["idle"] = len(idle)
            else:
                us = res[i] or {}
                shops = res[i + 1] or []
                i += 2
                st = self.stats.setdefault(name, {})
                st.update(request_rate=float(us.get("request_rate", 0)), latency_p95=float(us.get("p95", 0)),
                          errors=float(us.get("errors", 0)), users=float(us.get("users", 0)))
                if shops:
                    try:
                        st["in_flight"] = float(sum(int(self.redis.hget(s, "in_flight") or 0) for s in shops))
                    except Exception:  # noqa: BLE001
                        pass

    def _read_metrics(self):
        pm = {}
        for ns in self.ns.values():
            try:
                items = self.custom.list_namespaced_custom_object(
                    "metrics.k8s.io", "v1beta1", ns, "pods", label_selector=f"loadkit/run={self.rid}")["items"]
            except Exception:  # noqa: BLE001
                continue
            for it in items:
                cpu = sum(parse_cpu(c["usage"]["cpu"]) for c in it["containers"])
                mem = sum(parse_mem(c["usage"]["memory"]) for c in it["containers"])
                pm[it["metadata"]["name"]] = (cpu, mem)
        self.pod_metrics = pm
        if self.node:
            try:
                nm = self.custom.get_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes", self.node)
                self.node_metrics = (parse_cpu(nm["usage"]["cpu"]), parse_mem(nm["usage"]["memory"]))
            except Exception:  # noqa: BLE001
                pass

    def _memory_top(self):
        above = {}
        for p in self.pods:
            ld = pod_load(p)
            if not ld or pod_role(p) == "ballast":
                continue
            used = self.pod_metrics.get(p["metadata"]["name"], (0, 0))[1]
            req = 0.0
            for c in p["spec"]["containers"]:
                req += parse_mem(((c.get("resources") or {}).get("requests") or {}).get("memory", "0"))
            above[ld] = above.get(ld, 0.0) + max(0.0, used - req)
        return [{"load": k, "above_request": round(v)} for k, v in sorted(above.items(), key=lambda kv: -kv[1])[:3]]

    def _poll_cluster(self):
        pods, events = [], []
        for ns in self.ns.values():
            try:
                pods += [self.ser(p) for p in self.core.list_namespaced_pod(
                    ns, label_selector=f"loadkit/run={self.rid}").items]
                events += [self.ser(e) for e in self.core.list_namespaced_event(ns).items
                           if e.reason in ("Preempted", "Evicted")]
            except Exception:  # noqa: BLE001
                continue
        self.pods = pods
        node = None
        if self.node:
            try:
                node = self.ser(self.core.read_node_status(self.node))
            except Exception:  # noqa: BLE001
                pass
        out = self.watch.update(pods, events, node, memory_top=self._memory_top())
        # finished / failed jobs
        try:
            for j in self.batchapi.list_namespaced_job(self.ns["batch"], label_selector=f"loadkit/run={self.rid}").items:
                k = f"{j.metadata.labels['loadkit/load']}#{j.metadata.labels['loadkit/run-no']}"
                r = self.runs.get(k)
                if not r or r["state"] != "running":
                    continue
                conds = {c.type: c for c in (j.status.conditions or []) if c.status == "True"}
                if "Complete" in conds:
                    # Kubernetes says Complete. Trust it only if the work units really are done: a pod that exits
                    # with code 0 when it is stopped early would make the Job "Complete" with its work undone.
                    done = self._units_done(r)
                    if done is not None and done < r["units"]:
                        r["state"] = "failed"
                        r["done"] = done
                        out.append({"event": "job_failed", "load": r["load"], "run": r["run"],
                                    "progress": f"{done}/{r['units']}",
                                    "cause": {"reason": "Kubernetes marked the Job Complete, but not all work units "
                                                        "were done (its pods ended early, e.g. pushed out)"}})
                        continue
                    r["state"] = "done"
                    r["done"] = r["units"]
                    el = self.now - r["start"]
                    out.append({"event": "job_ended", "load": r["load"], "run": r["run"], "actual_duration": round(el, 1),
                                "normal_duration": round(r["normal"], 1),
                                "ratio": round(el / r["normal"], 2) if r["normal"] else None})
                elif "Failed" in conds:
                    r["state"] = "failed"
                    c = conds["Failed"]
                    out.append({"event": "job_timed_out" if c.reason == "DeadlineExceeded" else "job_failed",
                                "load": r["load"], "run": r["run"], "progress": f"{r['done']}/{r['units']}",
                                "cause": {"reason": c.reason, "message": (c.message or "")[:200]}})
        except Exception:  # noqa: BLE001
            pass
        # consumers beyond the partition count
        for name, ld in self.loads.items():
            if ld["type"] == "stream":
                idle = self.stats.get(name, {}).get("idle", 0)
                if idle != self._idle.get(name, 0):
                    self._idle[name] = idle
                    if idle:
                        out.append({"event": "consumers_idle", "load": name, "idle": idle,
                                    "active": max(0, self._running(name, "consumer") - idle),
                                    "cause": {"partitions": ld["partitions"], "consumers": self._replicas.get(name)}})
        # pods that stopped reporting
        try:
            alive = set(self.redis.keys("hb:*"))
            for p in pods:
                md = p["metadata"]
                rec = self.watch.seen.get(md["uid"])
                started = (p.get("status") or {}).get("startTime")
                if rec and is_ready(p) and not rec.get("lost") and f"hb:{md['name']}" not in alive and started:
                    rec.setdefault("first_miss", self.now)
                    if self.now - rec["first_miss"] > 15:
                        rec["lost"] = True
                        out.append({"event": "pod_lost", "load": pod_load(p), "pod": md["name"],
                                    "cause": {"reason": "no heartbeat for 15 s"}})
                elif rec:
                    rec.pop("first_miss", None)
        except Exception:  # noqa: BLE001
            pass
        return out

    def _running(self, name, role=None):
        return sum(1 for p in self.pods if pod_load(p) == name and (role is None or pod_role(p) == role)
                   and is_ready(p))

    # ---------------------------------------------------------------- signals and snapshot
    def _cpu_mem(self, name, role=None):
        cpu = mem = req = 0.0
        for p in self.pods:
            if pod_load(p) != name or (role and pod_role(p) != role) or not is_ready(p):
                continue
            c, m = self.pod_metrics.get(p["metadata"]["name"], (0.0, 0.0))
            cpu += c
            mem += m
            for ct in p["spec"]["containers"]:
                req += parse_cpu(((ct.get("resources") or {}).get("requests") or {}).get("cpu", "0"))
        return cpu, mem, req

    def signals(self, name):
        ld = self.loads[name]
        role = "consumer" if ld["type"] == "stream" else "shop"
        cpu, _, req = self._cpu_mem(name, role)
        st = self.stats.get(name, {})
        out = {"current": self.replicas(name), "running": self._running(name, role),
               "cpu_util": 100.0 * cpu / req if req else 0.0}
        if ld["type"] == "stream":
            out.update(backlog=st.get("backlog", 0.0), arrival_rate=st.get("arrival_rate", 0.0),
                       event_age=st.get("event_age", 0.0))
        else:
            out.update(in_flight=st.get("in_flight", 0.0), request_rate=st.get("request_rate", 0.0),
                       p95=st.get("latency_p95", 0.0), errors=st.get("errors", 0.0))
        return out

    def event_age(self, name):
        return self.stats.get(name, {}).get("event_age", 0.0)

    def backlog(self, name):
        return self.stats.get(name, {}).get("backlog", 0.0)

    def snapshot(self):
        out = {}
        for name, ld in self.loads.items():
            cpu, mem, _ = self._cpu_mem(name)
            mine = [p for p in self.pods if pod_load(p) == name]
            row = {"cpu": cpu, "memory": mem, "pods": sum(1 for p in mine if is_ready(p)),
                   "pending": sum(1 for p in mine if is_waiting(p) and unscheduled_reason(p))}
            st = self.stats.get(name, {})
            if ld["type"] == "stream":
                row.update(replicas=self.replicas(name), backlog=st.get("backlog", 0.0),
                           event_age=st.get("event_age", 0.0), arrival_rate=st.get("arrival_rate", 0.0),
                           processed_rate=st.get("processed_rate", 0.0))
            elif ld["type"] == "users":
                row.update(replicas=self.replicas(name), users=st.get("users", 0.0),
                           request_rate=st.get("request_rate", 0.0), in_flight=st.get("in_flight", 0.0),
                           latency_p95=st.get("latency_p95", 0.0), errors=st.get("errors", 0.0))
            else:
                act = self.active_runs(name)
                row["active_runs"] = len(act)
                if act:
                    row["progress"] = sum(self.runs[k]["done"] for k in act) / max(
                        sum(self.runs[k]["units"] for k in act), 1)
            out[name] = row
        ballast_cpu = sum(c for n, (c, _) in self.pod_metrics.items() if n.startswith("ballast"))
        ballast_mem = sum(m for n, (_, m) in self.pod_metrics.items() if n.startswith("ballast"))
        cap_cpu = (self.alloc_cpu or self.sc["capacity"]["cpu"]) - self.ballast_cpu
        cap_mem = (self.alloc_mem or self.sc["capacity"]["memory"]) - self.ballast_mem
        out["_node"] = {"cpu_used": max(0.0, self.node_metrics[0] - ballast_cpu), "cpu_capacity": cap_cpu,
                        "memory_used": max(0.0, self.node_metrics[1] - ballast_mem), "memory_capacity": cap_mem}
        return out
