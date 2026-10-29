"""Kubernetes objects the kit creates, as plain dicts (pure functions, unit-tested without a cluster).

Labels on everything: app.kubernetes.io/part-of=loadkit, loadkit/run=<run id>, loadkit/load=<load>, loadkit/role=<role>.
Priority mapping (plan 5.6): high -> Guaranteed (request = limit), normal/low -> Burstable; every pod has a memory limit.
"""
PART_OF = {"app.kubernetes.io/part-of": "loadkit"}
PRIORITY_VALUE = {"high": 100000, "normal": 1000, "low": 10}
SYSTEM_PRIORITY = 200000            # Redis and ballast must never be preempted by kit loads


def cpu_str(cores):
    return f"{max(1, int(round(float(cores) * 1000)))}m"


def mem_str(nbytes):
    return f"{max(1, int(nbytes // (1024 * 1024)))}Mi"


def labels(run, load=None, role=None, extra=None):
    out = {**PART_OF, "loadkit/run": run}
    if load:
        out["loadkit/load"] = load
    if role:
        out["loadkit/role"] = role
    out.update(extra or {})
    return out


def namespace(name, run):
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": labels(run)}}


def priority_class(prefix, level, preemption, run):
    return {"apiVersion": "scheduling.k8s.io/v1", "kind": "PriorityClass",
            "metadata": {"name": f"{prefix}-{level}", "labels": labels(run)},
            "value": PRIORITY_VALUE[level] if level in PRIORITY_VALUE else SYSTEM_PRIORITY,
            "preemptionPolicy": "PreemptLowerPriority" if preemption else "Never",
            "globalDefault": False, "description": f"Load Scenario Kit priority '{level}'"}


def secret(ns, run, redis_url, password):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "loadkit-redis", "namespace": ns, "labels": labels(run)},
            "stringData": {"url": redis_url, "password": password}}


def node_placement(node_selector):
    return {"nodeSelector": node_selector} if node_selector else {}


def redis_objects(ns, run, prefix, node_selector):
    lab = labels(run, role="redis", extra={"app": "loadkit-redis"})
    dep = {"apiVersion": "apps/v1", "kind": "Deployment",
           "metadata": {"name": "redis", "namespace": ns, "labels": lab},
           "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "loadkit-redis"}},
                    "template": {"metadata": {"labels": lab},
                                 "spec": {"priorityClassName": f"{prefix}-system",
                                          **node_placement(node_selector),
                                          "containers": [{
                                              "name": "redis", "image": "redis:7-alpine",
                                              "args": ["--requirepass", "$(REDIS_PASSWORD)", "--appendonly", "yes",
                                                       "--save", ""],
                                              "env": [{"name": "REDIS_PASSWORD", "valueFrom": {"secretKeyRef": {
                                                  "name": "loadkit-redis", "key": "password"}}}],
                                              "ports": [{"containerPort": 6379}],
                                              "readinessProbe": {"tcpSocket": {"port": 6379}, "periodSeconds": 2},
                                              "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                                            "limits": {"cpu": "500m", "memory": "512Mi"}}}]}}}}
    svc = {"apiVersion": "v1", "kind": "Service",
           "metadata": {"name": "redis", "namespace": ns, "labels": lab},
           "spec": {"selector": {"app": "loadkit-redis"}, "ports": [{"port": 6379, "targetPort": 6379}]}}
    netpol = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
              "metadata": {"name": "redis-only-loadkit", "namespace": ns, "labels": labels(run)},
              "spec": {"podSelector": {"matchLabels": {"app": "loadkit-redis"}},
                       "policyTypes": ["Ingress"],
                       "ingress": [{"from": [{"namespaceSelector": {"matchLabels": PART_OF}}]}]}}
    return dep, svc, netpol


def resources(ld, demand_cores, mem_request, mem_limit, high):
    """Requests/limits from the scenario. High priority -> Guaranteed (cpu and memory request = limit)."""
    if high:
        cpu = max(ld["request"], demand_cores)
        return {"requests": {"cpu": cpu_str(cpu), "memory": mem_str(mem_limit)},
                "limits": {"cpu": cpu_str(cpu), "memory": mem_str(mem_limit)}}
    res = {"requests": {"cpu": cpu_str(ld["request"]), "memory": mem_str(mem_request)},
           "limits": {"memory": mem_str(mem_limit)}}
    if ld.get("limit"):
        res["limits"]["cpu"] = cpu_str(ld["limit"])
    return res


def mem_limits(ld):
    m = ld["memory"]
    req = m.get("request", 128 * 1024 ** 2)
    lim = m.get("limit") or 4 * req                       # safety: every kit pod has a memory limit
    return req, lim


def worker_container(image, role, args, res, env_extra=None, ports=None):
    env = [{"name": "REDIS_URL", "valueFrom": {"secretKeyRef": {"name": "loadkit-redis", "key": "url"}}},
           {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
           {"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}}]
    c = {"name": role, "image": image, "imagePullPolicy": "IfNotPresent", "args": [role, *args],
         "env": env + (env_extra or []), "resources": res}
    if ports:
        c["ports"] = [{"containerPort": p} for p in ports]
    return c


def pod_spec(prefix, ld, containers, node_selector, restart="Always"):
    return {"priorityClassName": f"{prefix}-{ld['priority']}", "restartPolicy": restart,
            "terminationGracePeriodSeconds": 2, **node_placement(node_selector), "containers": containers}


def memory_args(ld, kind):
    m = ld["memory"]
    a = ["--mem-base", str(int(m.get("base", 0)))]
    if kind == "batch":
        a += ["--mem-peak", str(int(m.get("peak", m.get("base", 0)))), "--mem-shape", m.get("shape", "flat")]
    if m.get("leak_per_s"):
        a += ["--leak", str(int(m["leak_per_s"] * 60))]
    return a


def batch_job(ns, run, prefix, image, ld, run_no, units, deadline_s, node_selector):
    """One batch run = one Indexed Job with `parallelism` pods; each pod does its share of the units."""
    p = ld["parallelism"]
    req, lim = mem_limits(ld)
    res = resources(ld, ld["intensity"], req, lim, ld["priority"] == "high")
    args = ["--load", ld["name"], "--run", str(run_no), "--units", str(int(units)), "--parallel", str(p),
            "--cost", str(ld["cost_per_unit"]), "--intensity", str(ld["intensity"]), *memory_args(ld, "batch")]
    if ld.get("checkpoint"):
        args.append("--checkpoint")
    lab = labels(run, ld["name"], "batch", {"loadkit/run-no": str(run_no), "team": ld["team"]})
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": f"{ld['name']}-r{run_no}", "namespace": ns, "labels": lab},
            "spec": {"parallelism": p, "completions": p, "completionMode": "Indexed", "backoffLimit": 50,
                     "activeDeadlineSeconds": int(deadline_s) + 60,
                     "template": {"metadata": {"labels": lab},
                                  "spec": pod_spec(prefix, ld, [worker_container(image, "batch", args, res)],
                                                   node_selector, restart="Never")}}}


def deployment(ns, run, name, role, ld, replicas, container, node_selector, prefix):
    lab = labels(run, ld["name"], role, {"team": ld["team"]})
    sel = {"loadkit/run": run, "loadkit/load": ld["name"], "loadkit/role": role}
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": ns, "labels": lab},
            "spec": {"replicas": replicas, "selector": {"matchLabels": sel},
                     "template": {"metadata": {"labels": lab},
                                  "spec": pod_spec(prefix, ld, [container], node_selector)}}}


SMALL = {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}}


def stream_objects(ns, run, prefix, image, ld, node_selector):
    prod = worker_container(image, "produce", ["--load", ld["name"], "--partitions", str(ld["partitions"])], SMALL)
    req, lim = mem_limits(ld)
    res = resources(ld, ld["threads_per_consumer"], req, lim, ld["priority"] == "high")
    cons = worker_container(image, "consume", [
        "--load", ld["name"], "--partitions", str(ld["partitions"]), "--threads", str(ld["threads_per_consumer"]),
        "--work-ms", str(round(ld["work_per_event"] * 1000, 3)), "--prefetch", str(ld["prefetch"]),
        "--per-buffered-event", str(int(ld["memory"].get("per_buffered_event", 0))), *memory_args(ld, "stream")], res)
    small_ld = {**ld, "priority": ld["priority"]}
    return [deployment(ns, run, f"{ld['name']}-producer", "producer", small_ld, 1, prod, node_selector, prefix),
            deployment(ns, run, f"{ld['name']}-consumer", "consumer", ld, ld["consumers"][0], cons, node_selector,
                       prefix)]


def users_objects(ns, run, prefix, image, ld, node_selector):
    req, lim = mem_limits(ld)
    res = resources(ld, 1.0, req, lim, ld["priority"] == "high")
    shop = worker_container(image, "shop", [
        "--load", ld["name"], "--cost-ms", str(round(ld["cost_per_request"] * 1000, 3)), "--port", "8080",
        "--per-in-flight", str(int(ld["memory"].get("per_in_flight", 0))), *memory_args(ld, "users")], res,
        ports=[8080])
    shop["readinessProbe"] = {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 2}
    url = f"http://{ld['name']}-shop.{ns}.svc.cluster.local:8080/"
    users = worker_container(image, "users", ["--load", ld["name"], "--url", url, "--think",
                                              str(ld["think_time"]), "--timeout", str(ld["timeout"])], SMALL)
    svc = {"apiVersion": "v1", "kind": "Service",
           "metadata": {"name": f"{ld['name']}-shop", "namespace": ns, "labels": labels(run, ld["name"], "shop")},
           "spec": {"selector": {"loadkit/run": run, "loadkit/load": ld["name"], "loadkit/role": "shop"},
                    "ports": [{"port": 8080, "targetPort": 8080}]}}
    return [deployment(ns, run, f"{ld['name']}-shop", "shop", ld, ld["shop_replicas"][0], shop, node_selector,
                       prefix),
            deployment(ns, run, f"{ld['name']}-users", "users", ld, 1, users, node_selector, prefix), svc]


def ballast(ns, run, prefix, image, cores, mem_bytes, node_selector):
    """Makes a big node behave like the node size the scenario was written for: a Guaranteed pod that really uses
    `cores` CPU and holds `mem_bytes` memory for the whole run."""
    args = ["--load", "_ballast", "--run", "0", "--units", "1000000000", "--cost", "1", "--intensity", str(cores),
            "--mem-base", str(int(mem_bytes))]
    res = {"requests": {"cpu": cpu_str(cores), "memory": mem_str(mem_bytes + 64 * 1024 ** 2)},
           "limits": {"cpu": cpu_str(cores), "memory": mem_str(mem_bytes + 64 * 1024 ** 2)}}
    lab = labels(run, "kit-ballast", "ballast")          # label values must start with a letter or digit
    c = worker_container(image, "batch", args, res)
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "ballast", "namespace": ns, "labels": lab},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"loadkit/run": run, "loadkit/role": "ballast"}},
                     "template": {"metadata": {"labels": lab},
                                  "spec": {"priorityClassName": f"{prefix}-system", "terminationGracePeriodSeconds": 2,
                                           **node_placement(node_selector), "containers": [c]}}}}
