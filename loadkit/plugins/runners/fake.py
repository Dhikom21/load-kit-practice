"""Simulated node: a deterministic model of one cluster node, as a first-class runner.

It lets the unit tests and CI drive the engine without a Kubernetes cluster, and it powers the UI's Start
button when no real cluster is around (laptop development). It simulates one node (CPU + memory) with
Kubernetes-like rules and implements the same runner interface as the Kubernetes runner (loadkit/runner_api.py)
— the engine cannot tell them apart.

What it simulates, every tick (default 1 simulated second):
  * placement   — a pod runs only if its requests fit the node; otherwise it waits (pending) or, with preemption on,
                  evicts lower-priority pods.
  * CPU         — when total demand > capacity, CPU is shared in proportion to requests (like Linux CFS), each pod
                  capped by its own limit (throttling).
  * memory      — usage follows each load's profile; over the pod's limit => OOM kill + restart; node over
                  allocatable => evictions in kubelet order.
  * batch       — work-based runs: progress = allocated cores x dt / cost_per_unit; less CPU => longer runs.
  * streams     — FIFO backlog; active consumers = min(pods, partitions); event age = age of the oldest waiting event.
  * shop        — closed-loop users; response time, p95, errors, request rate, in-flight requests (Little's law).
  * attribution — exact bookkeeping of who took CPU from whom (used by `analyze`).

Everything random is drawn from the run's seed, so the whole diary is repeatable.
"""
import math
from collections import deque

from ...registry import register
from ...runner_api import Runner

PRIO = {"high": 3, "normal": 2, "low": 1}
SHOP_THREADS = 2.0         # max cores one kit-shop pod can use
NOISE = 0.05               # seeded noise on stream arrivals (relative std)
RESTART_DELAY = 10.0       # s until a killed pod runs again; doubles per restart (back-off), max 5 min
EVICTION_THRESHOLD = 0.95  # node memory allocatable fraction
PRESSURE_TRANSITION = 300.0  # kubelet keeps MemoryPressure (and its NoSchedule taint) for 5 min after it ends
SMALL = {"producer": (0.05, 48), "users": (0.05, 48)}   # role -> (cpu demand, memory MiB)


class Pod:
    __slots__ = ("id", "load", "role", "run", "prio", "req_cpu", "lim_cpu", "req_mem", "lim_mem", "state",
                 "ready_at", "started", "units_total", "units_left", "demand", "alloc", "mem", "restarts", "created",
                 "want", "potential", "pend_reported", "pend_since")

    def __init__(self, pid, load, role, prio, req_cpu, lim_cpu, req_mem, lim_mem, now, run=None, units=0.0):
        self.id, self.load, self.role, self.run, self.prio = pid, load, role, run, prio
        self.req_cpu, self.lim_cpu, self.req_mem, self.lim_mem = req_cpu, lim_cpu, req_mem, lim_mem
        self.state = "pending"
        self.ready_at = now
        self.started = None
        self.units_total = units
        self.units_left = units
        self.demand = self.alloc = 0.0
        self.want = self.potential = None
        self.pend_reported = False
        self.pend_since = None
        self.mem = 0.0
        self.restarts = 0
        self.created = now

    def qos(self):
        if self.lim_cpu is not None and self.lim_mem is not None and \
                abs(self.lim_cpu - self.req_cpu) < 1e-9 and abs(self.lim_mem - self.req_mem) < 1:
            return "Guaranteed"
        return "Burstable"


def water_fill(capacity, pods):
    """Share `capacity` among pods by request weight, never more than each pod's (capped) demand."""
    for p in pods:
        p.alloc = 0.0
    active = [p for p in pods if p.demand > 1e-12]
    remaining = capacity
    while active and remaining > 1e-12:
        wsum = sum(max(p.req_cpu, 0.001) for p in active)
        done = []
        for p in active:
            fair = remaining * max(p.req_cpu, 0.001) / wsum
            if p.demand - p.alloc <= fair + 1e-12:
                done.append(p)
        if done:
            for p in done:
                remaining -= p.demand - p.alloc
                p.alloc = p.demand
                active.remove(p)
        else:
            for p in active:
                p.alloc += remaining * max(p.req_cpu, 0.001) / wsum
            remaining = 0.0


@register("runners")
class FakeCluster(Runner):
    name = "fake"
    label = "Simulated node (no cluster)"
    realtime = False

    @staticmethod
    def available(settings, kubeconfig=None):
        return True, "simulated node — no cluster needed"

    def __init__(self, sc, rng, settings=None, **kw):
        self.sc = sc
        self.rng = rng
        self.cap_cpu = sc["capacity"]["cpu"]
        self.cap_mem = sc["capacity"]["memory"]
        self.preemption = sc["preemption"]
        self.loads = {ld["name"]: ld for ld in sc["loads"]}
        self.pods = []
        self._pid = 0
        self.runs = {}             # run_key -> dict(load, run, units, started, pods, state)
        self.st = {}               # per-load live state
        self.events = []           # reactive events produced during step()
        self.contrib = {}          # bucket -> {victim: {cause: cpu_seconds_lost}}
        self.node_pressure = False
        self.pressure_until = -1.0
        self.now = 0.0
        for ld in sc["loads"]:
            self.st[ld["name"]] = {"rate": 0.0, "users": 0, "fifo": deque(), "backlog": 0.0, "processed": 0.0,
                                   "idle_reported": 0, "R": 0.0, "X": 0.0, "inflight": 0.0, "errors": 0.0,
                                   "throttled": 0.0, "arrived": 0.0, "desired": 0}

    # ------------------------------------------------------------------ pods
    def _new_pod(self, ld, role, run=None, units=0.0):
        self._pid += 1
        if role in SMALL:
            c, m = SMALL[role]
            req_cpu, lim_cpu, req_mem, lim_mem = 0.05, None, m * 1024 ** 2, 4 * m * 1024 ** 2
        else:
            req_cpu, lim_cpu = ld["request"], ld["limit"]
            req_mem, lim_mem = ld["memory"].get("request", 64 * 1024 ** 2), ld["memory"].get("limit")
        p = Pod(f"{ld['name']}-{role}-{self._pid}", ld["name"], role, PRIO[ld["priority"]],
                req_cpu, lim_cpu, req_mem, lim_mem, self.now, run, units)
        self.pods.append(p)
        return p

    def _pods(self, load=None, role=None, states=("running",)):
        return [p for p in self.pods if (load is None or p.load == load) and (role is None or p.role == role)
                and (states is None or p.state in states)]

    def _emit(self, event, load, **fields):
        self.events.append(dict(event=event, load=load, **fields))

    # ------------------------------------------------------------------ engine-facing API
    def setup_service(self, name):
        """Create the always-on pods of a stream (producer + min consumers) or users load (traffic + min shop)."""
        ld = self.loads[name]
        if ld["type"] == "stream":
            self._new_pod(ld, "producer")
            for _ in range(ld["consumers"][0]):
                self._new_pod(ld, "consumer")
            self.st[name]["desired"] = ld["consumers"][0]
        elif ld["type"] == "users":
            self._new_pod(ld, "users")
            for _ in range(ld["shop_replicas"][0]):
                self._new_pod(ld, "shop")
            self.st[name]["desired"] = ld["shop_replicas"][0]

    def set_rate(self, name, rate):
        self.st[name]["rate"] = rate

    def set_users(self, name, users):
        self.st[name]["users"] = users

    def replicas(self, name):
        role = "consumer" if self.loads[name]["type"] == "stream" else "shop"
        return len([p for p in self.pods if p.load == name and p.role == role and p.state != "gone"])

    def scale(self, name, n):
        ld = self.loads[name]
        role = "consumer" if ld["type"] == "stream" else "shop"
        cur = [p for p in self.pods if p.load == name and p.role == role and p.state != "gone"]
        self.st[name]["desired"] = n
        if n > len(cur):
            for _ in range(n - len(cur)):
                self._new_pod(ld, role)
        elif n < len(cur):
            # remove pending first, then newest
            cur.sort(key=lambda p: (p.state == "running", p.created), reverse=False)
            for p in cur[: len(cur) - n]:
                p.state = "gone"

    def start_run(self, name, run, units):
        ld = self.loads[name]
        key = f"{name}#{run}"
        pods = [self._new_pod(ld, "batch", run=key, units=units / ld["parallelism"]) for _ in range(ld["parallelism"])]
        self.runs[key] = {"load": name, "run": run, "units": units, "started": self.now, "pods": pods,
                          "state": "running", "normal": units * ld["cost_per_unit"] / (ld["parallelism"] * ld["intensity"])}
        return key

    def stop_run(self, key, reason):
        r = self.runs.get(key)
        if not r or r["state"] != "running":
            return
        r["state"] = reason
        for p in r["pods"]:
            p.state = "gone"

    def active_runs(self, name=None):
        return [k for k, r in self.runs.items() if r["state"] == "running" and (name is None or r["load"] == name)]

    def run_info(self, key):
        r = self.runs[key]
        done = sum(p.units_total - p.units_left for p in r["pods"])
        return {"elapsed": self.now - r["started"], "normal": r["normal"], "done": done, "units": r["units"],
                "state": r["state"]}

    # ------------------------------------------------------------------ simulation step
    def step(self, now, dt):
        self.now = now
        self.events = []
        self._restart_and_place(now)
        running = self._pods()
        self._demands(running, dt)
        water_fill(self.cap_cpu, running)          # pass 1: shop pods ask for all their threads (potential share)
        self._shop_model(running)                  # response time from the potential share; shop uses only X*c
        water_fill(self.cap_cpu, running)          # pass 2: what the shop leaves goes to the others
        self._attribute(running, now, dt)
        self._progress(running, now, dt)
        self._memory(now, dt)
        self.pods = [p for p in self.pods if p.state != "gone"]
        return self.events

    def _restart_and_place(self, now):
        for p in self.pods:
            if p.state == "restarting" and now >= p.ready_at:
                p.state = "pending"
        used_cpu = sum(p.req_cpu for p in self._pods())
        used_mem = sum(p.req_mem for p in self._pods())
        pending = sorted(self._pods(states=("pending",)), key=lambda p: (-p.prio, p.created))
        if self.node_pressure:     # node.kubernetes.io/memory-pressure:NoSchedule taint
            for p in pending:
                if not p.pend_reported:
                    self._emit("pod_pending", p.load, pod=p.id,
                               cause={"reason": "node has the memory-pressure taint (NoSchedule)"})
                    p.pend_reported = True
                    p.pend_since = now
            return
        for p in pending:
            if used_cpu + p.req_cpu <= self.cap_cpu + 1e-9 and used_mem + p.req_mem <= self.cap_mem:
                self._place(p, now)
                used_cpu += p.req_cpu
                used_mem += p.req_mem
                continue
            if self.preemption:
                victims = sorted([q for q in self._pods() if q.prio < p.prio], key=lambda q: (q.prio, -q.created))
                freed_c, freed_m, chosen = 0.0, 0.0, []
                for q in victims:
                    if used_cpu - freed_c + p.req_cpu <= self.cap_cpu + 1e-9 and \
                            used_mem - freed_m + p.req_mem <= self.cap_mem:
                        break
                    chosen.append(q)
                    freed_c += q.req_cpu
                    freed_m += q.req_mem
                if chosen and used_cpu - freed_c + p.req_cpu <= self.cap_cpu + 1e-9 and \
                        used_mem - freed_m + p.req_mem <= self.cap_mem:
                    for q in chosen:
                        self._kill(q, now, "pod_preempted", by=p.load, by_priority=self._prio_name(p.prio))
                    used_cpu -= freed_c
                    used_mem -= freed_m
                    self._place(p, now)
                    used_cpu += p.req_cpu
                    used_mem += p.req_mem
                    continue
            if not p.pend_reported:      # once per pending episode
                self._emit("pod_pending", p.load, pod=p.id, cause={"reason": "not enough room on the node",
                                                                      "requests_cpu": round(p.req_cpu, 3)})
                p.pend_reported = True
                p.pend_since = now

    @staticmethod
    def _prio_name(v):
        return {3: "high", 2: "normal", 1: "low"}[v]

    def _place(self, p, now):
        p.state = "running"
        if p.pend_reported:          # it had to wait: say for how long
            self._emit("pod_scheduled", p.load, pod=p.id, waited_s=round(now - p.pend_since, 1),
                       cause={"reason": "room on the node became free"})
        p.pend_reported = False
        p.started = now
        if p.restarts:
            self._emit("pod_restarted", p.load, pod=p.id, restarts=p.restarts)

    def _kill(self, p, now, event, **cause):
        """OOM kill, eviction or preemption: the pod disappears and is recreated (Deployment / Job controller)."""
        ld = self.loads[p.load]
        lost = 0.0
        if p.role == "batch" and not ld.get("checkpoint"):
            lost = p.units_total - p.units_left
            p.units_left = p.units_total
        p.state = "restarting"
        p.restarts += 1
        p.ready_at = now + min(RESTART_DELAY * 2 ** (p.restarts - 1), 300.0)   # CrashLoopBackOff-like
        p.alloc = p.demand = 0.0
        p.started = None
        self._emit(event, p.load, pod=p.id, run=p.run, lost_units=round(lost, 1), cause=cause)

    def _demands(self, pods, dt):
        by_load = {}
        for p in pods:
            by_load.setdefault(p.load, []).append(p)
        for name, ps in by_load.items():
            ld = self.loads[name]
            s = self.st[name]
            if ld["type"] == "batch":
                for p in ps:
                    p.demand = ld["intensity"] if p.units_left > 0 else 0.0
            elif ld["type"] == "stream":
                cons = sorted([p for p in ps if p.role == "consumer"], key=lambda p: p.created)
                active = cons[: ld["partitions"]]
                idle = cons[ld["partitions"]:]
                n = max(len(active), 1)
                cost = ld["work_per_event"]
                for p in active:
                    want = (s["backlog"] / n + s["rate"] * dt / n) * cost / dt
                    p.demand = min(ld["threads_per_consumer"], want + 0.01)
                for p in idle:
                    p.demand = 0.02
                if len(idle) != s["idle_reported"]:
                    if idle:
                        self._emit("consumers_idle", name, active=len(active), idle=len(idle),
                                   threads=ld["threads_per_consumer"],
                                   cause={"partitions": ld["partitions"], "consumers": len(cons)})
                    s["idle_reported"] = len(idle)
                for p in ps:
                    if p.role == "producer":
                        p.demand = SMALL["producer"][0]
            elif ld["type"] == "users":
                shops = [p for p in ps if p.role == "shop"]
                users_pod = any(p.role == "users" for p in ps)
                U = s["users"] if users_pod else 0
                Z, c = ld["think_time"], ld["cost_per_request"]
                need = (U / (Z + c)) * c / len(shops) if shops else 0.0
                for p in shops:
                    p.demand = SHOP_THREADS                                  # potential (pass 1)
                    p.want = min(SHOP_THREADS, 2.0 * need + 0.02)            # needs ~50 % headroom to stay fast
                for p in ps:
                    if p.role == "users":
                        p.demand = SMALL["users"][0]
        for p in pods:
            if p.lim_cpu is not None:
                self.st[p.load]["throttled"] = max(0.0, p.demand - p.lim_cpu)
                p.demand = min(p.demand, p.lim_cpu)

    def _attribute(self, pods, now, dt):
        """Exact bookkeeping: CPU an entity wanted but did not get, split over the other entities by what they used.

        Entity = one batch run (so a run slowed by an earlier run of the same job counts as 'self'), or one service.
        Stored per 15 s bucket as contrib[bucket][victim_load][cause_load] = CPU-seconds lost.
        """
        total_alloc = sum(p.alloc for p in pods)
        if total_alloc < self.cap_cpu * 0.98:
            return
        bucket = int(now // 15) * 15
        ent = {}
        for p in pods:
            key = p.run or p.load
            d = ent.setdefault(key, [p.load, 0.0, 0.0])
            if p.want is not None:     # shop: lost = headroom it needed but could not get
                d[1] += max(p.alloc, p.alloc + p.want - p.potential)
            else:
                d[1] += p.demand
            d[2] += p.alloc
        b = self.contrib.setdefault(bucket, {})
        for key, (victim, dem, al) in ent.items():
            lost = dem - al
            if lost <= 1e-6:
                continue
            others = [(v[0], v[2]) for k, v in ent.items() if k != key and v[2] > 0]
            osum = sum(a for _, a in others)
            if osum <= 0:
                continue
            row = b.setdefault(victim, {})
            for cause, a in others:
                row[cause] = row.get(cause, 0.0) + lost * dt * a / osum

    def _progress(self, pods, now, dt):
        for p in pods:
            ld = self.loads[p.load]
            if p.role == "batch" and p.units_left > 0:
                p.units_left = max(0.0, p.units_left - p.alloc * dt / ld["cost_per_unit"])
        # batch runs finished?
        for key, r in self.runs.items():
            if r["state"] == "running" and all(p.units_left <= 0 for p in r["pods"]):
                r["state"] = "done"
                for p in r["pods"]:
                    p.state = "gone"
                el = now - r["started"]
                self._emit("job_ended", r["load"], run=r["run"], actual_duration=round(el, 1),
                           normal_duration=round(r["normal"], 1), ratio=round(el / r["normal"], 2) if r["normal"] else None)
        # streams
        for name, ld in self.loads.items():
            s = self.st[name]
            if ld["type"] == "stream":
                producing = any(p.role == "producer" for p in pods if p.load == name)
                arrivals = s["rate"] * dt * max(0.0, 1 + self.rng.gauss(0, NOISE)) if producing else 0.0
                s["arrived"] = arrivals / dt
                if arrivals > 0:
                    s["fifo"].append([now, arrivals])
                    s["backlog"] += arrivals
                cap = sum(p.alloc for p in pods if p.load == name and p.role == "consumer") * dt / ld["work_per_event"]
                todo = min(cap, s["backlog"])
                s["processed"] = todo / dt
                s["backlog"] -= todo
                while todo > 1e-9 and s["fifo"]:
                    head = s["fifo"][0]
                    take = min(head[1], todo)
                    head[1] -= take
                    todo -= take
                    if head[1] <= 1e-9:
                        s["fifo"].popleft()

    def _shop_model(self, pods):
        """kit-shop as a processor-sharing server. Its capacity is the CPU it COULD get now (pass-1 share, up to its
        threads); users are a closed loop (think time Z). Little's law: in-flight = X x R."""
        for name, ld in self.loads.items():
            if ld["type"] != "users":
                continue
            s = self.st[name]
            shops = [p for p in pods if p.load == name and p.role == "shop"]
            users_pod = any(p.role == "users" for p in pods if p.load == name)
            U = s["users"] if users_pod else 0
            Z, c = ld["think_time"], ld["cost_per_request"]
            if not shops or U <= 0:
                s.update(R=0.0 if U <= 0 else ld["timeout"], X=0.0, inflight=float(U if not shops else 0),
                         errors=1.0 if (U > 0 and not shops) else 0.0)
                for p in shops:
                    p.demand = 0.02
                continue
            for p in shops:
                p.potential = p.alloc
            A = max(sum(p.alloc for p in shops) - 0.02 * len(shops), 1e-6)
            per_pod = A / len(shops)
            c_eff = c / min(1.0, per_pod)                 # a request cannot run faster than its pod's share
            X = min(U / (c_eff + Z), 0.98 * A / c)        # asymptotic bounds of a closed system
            rho = min(X * c / A, 0.97)
            R = max(U / X - Z, c_eff / (1 - rho))
            X = U / (R + Z)
            s["R"], s["X"] = R, X
            s["inflight"] = X * R
            s["errors"] = math.exp(-ld["timeout"] / R) if R > 0 else 0.0
            for p in shops:
                p.demand = X * c / len(shops) + 0.02       # what it actually uses (pass 2)

    def _profile_mem(self, p, ld, now):
        m = ld["memory"]
        if p.role in SMALL:
            return SMALL[p.role][1] * 1024 ** 2
        base, peak = m.get("base", 0.0), m.get("peak", m.get("base", 0.0))
        age = now - (p.started if p.started is not None else now)
        leak = m.get("leak_per_s", 0.0) * age
        if p.role == "batch":
            prog = 1 - (p.units_left / p.units_total) if p.units_total else 0.0
            shape = m.get("shape", "flat")
            if shape == "ramp":
                v = base + (peak - base) * prog
            elif shape == "step":
                v = peak if prog >= 0.15 else base
            elif shape == "spiky":
                v = base + (peak - base) * (0.5 + 0.5 * math.sin(age / 7.0))
            else:
                v = base
            return v + leak
        s = self.st[p.load]
        if p.role == "consumer":
            ncons = max(1, len([q for q in self.pods if q.load == p.load and q.role == "consumer" and q.state == "running"]))
            buffered = min(s["backlog"] / ncons, ld["prefetch"] * ld["threads_per_consumer"] * 20)
            return base + m.get("per_buffered_event", 0.0) * buffered + leak
        if p.role == "shop":
            nshop = max(1, len([q for q in self.pods if q.load == p.load and q.role == "shop" and q.state == "running"]))
            return base + m.get("per_in_flight", 0.0) * s["inflight"] / nshop + leak
        return base + leak

    def _memory(self, now, dt):
        running = self._pods()
        for p in running:
            p.mem = self._profile_mem(p, self.loads[p.load], now)
            if p.lim_mem is not None and p.mem > p.lim_mem:
                self._kill(p, now, "oom_killed", usage=round(p.mem), limit=round(p.lim_mem),
                           leak=bool(self.loads[p.load]["memory"].get("leak_per_s")))
        running = self._pods()
        used = sum(p.mem for p in running)
        limit = self.cap_mem * EVICTION_THRESHOLD
        over = used > limit
        if over:
            self.pressure_until = now + PRESSURE_TRANSITION
        pressure = now < self.pressure_until
        if pressure != self.node_pressure:
            self.node_pressure = pressure
            self._emit("node_memory_pressure", None, state="on" if pressure else "off", used=round(used),
                       allocatable=round(limit),
                       cause={"reason": "node memory above allocatable" if pressure else
                              f"below allocatable for {int(PRESSURE_TRANSITION)} s (pressure transition period)"})
        pressure = over
        if pressure:
            def order(q):
                over = q.mem - q.req_mem
                return (q.qos() == "Guaranteed", q.prio, -over)
            top = sorted({(q.load) for q in running}, key=lambda n: -sum(q.mem - q.req_mem for q in running if q.load == n))
            victim = sorted(running, key=order)[0]
            self._kill(victim, now, "pod_evicted", reason="node memory pressure", node_memory_used=round(used),
                       top_users=[{"load": n, "above_request": round(sum(max(0.0, q.mem - q.req_mem)
                                                                          for q in running if q.load == n))}
                                  for n in top[:3]])

    # ------------------------------------------------------------------ signals & snapshots
    def signals(self, name):
        ld = self.loads[name]
        s = self.st[name]
        role = "consumer" if ld["type"] == "stream" else "shop"
        ps = self._pods(name, role)
        req = sum(p.req_cpu for p in ps) or 1e-9
        util = 100.0 * sum(p.alloc for p in ps) / req if ps else 0.0
        out = {"current": self.replicas(name), "running": len(ps), "cpu_util": util}
        if ld["type"] == "stream":
            out.update(backlog=s["backlog"], arrival_rate=s["arrived"], event_age=self.event_age(name))
        else:
            out.update(in_flight=s["inflight"], request_rate=s["X"], p95=3.0 * s["R"], errors=s["errors"])
        return out

    def backlog(self, name):
        return self.st[name]["backlog"]

    def run_state(self, key):
        r = self.runs.get(key)
        return r["state"] if r else None

    def event_age(self, name):
        s = self.st[name]
        return (self.now - s["fifo"][0][0]) if s["fifo"] else 0.0

    def snapshot(self):
        """Per-load and node values for the metrics table."""
        out = {}
        running = self._pods()
        for name, ld in self.loads.items():
            ps = [p for p in running if p.load == name]
            row = {"cpu": sum(p.alloc for p in ps), "cpu_demand": sum(p.demand for p in ps),
                   "memory": sum(p.mem for p in ps), "pods": len(ps),
                   "pending": len([p for p in self.pods if p.load == name and p.state in ("pending", "restarting")])}
            s = self.st[name]
            if ld["type"] == "stream":
                row.update(replicas=self.replicas(name), backlog=s["backlog"], event_age=self.event_age(name),
                           arrival_rate=s["arrived"], processed_rate=s["processed"])
            elif ld["type"] == "users":
                row.update(replicas=self.replicas(name), users=s["users"], request_rate=s["X"],
                           in_flight=s["inflight"], latency_p95=3.0 * s["R"], errors=s["errors"])
            else:
                act = self.active_runs(name)
                row.update(active_runs=len(act))
                if act:
                    infos = [self.run_info(k) for k in act]
                    row["progress"] = sum(i["done"] for i in infos) / max(sum(i["units"] for i in infos), 1)
            out[name] = row
        out["_node"] = {"cpu_used": sum(p.alloc for p in running), "cpu_capacity": self.cap_cpu,
                        "memory_used": sum(p.mem for p in running), "memory_capacity": self.cap_mem}
        return out

    def cleanup(self):
        self.pods = []
        self.runs = {}
