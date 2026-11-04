"""Turn what the Kubernetes API shows into the kit's reactive events (pure logic, unit-tested with pod fixtures).

Input: pods as plain dicts (camelCase, like `kubectl get pod -o json`), events (same), node conditions.
Output: events {event, load, cause, ...}: pod_pending, pod_scheduled, oom_killed, pod_evicted, pod_preempted, pod_restarted,
pod_lost, node_memory_pressure.
"""
import datetime
import re

PREEMPT_RE = re.compile(r"[Pp]reempted by (?:pod )?(?:(?P<ns>[\w.-]+)/)?(?P<who>[\w.-]+)")


def pod_load(p):
    return (p.get("metadata", {}).get("labels") or {}).get("loadkit/load")


def pod_role(p):
    return (p.get("metadata", {}).get("labels") or {}).get("loadkit/role")


def pod_priority(p):
    """high | normal | low | system, from the pod's priority class (e.g. loadkit-high, g3-low), or None."""
    pc = (p.get("spec") or {}).get("priorityClassName") or ""
    return pc.rsplit("-", 1)[-1] if "-" in pc else (pc or None)


def pod_run_no(p):
    v = (p.get("metadata", {}).get("labels") or {}).get("loadkit/run-no")
    return int(v) if v else None


def is_ready(p):
    st = p.get("status") or {}
    return st.get("phase") == "Running" and all(c.get("ready") for c in st.get("containerStatuses") or [{}])


def is_waiting(p):
    """Pending (not scheduled, image pulling, starting) — counts as 'waiting outside the machine' when unscheduled."""
    st = p.get("status") or {}
    return st.get("phase") == "Pending" and not (p.get("metadata") or {}).get("deletionTimestamp")


def unscheduled_reason(p):
    for c in (p.get("status") or {}).get("conditions") or []:
        if c.get("type") == "PodScheduled" and c.get("status") == "False":
            return c.get("message") or c.get("reason") or "not scheduled"
    return None


def short_reason(msg):
    """Kubernetes' scheduler message in a few plain words; the full message is kept next to it."""
    m = msg or ""
    need = [w for w, k in (("CPU", "Insufficient cpu"), ("memory", "Insufficient memory")) if k in m]
    if need:
        out = f"not enough free {' and '.join(need)} on the node"
    elif "taint" in m:
        out = "no node accepts the pod (taints)"
    elif "affinity" in m or "selector" in m:
        out = "no node matches the pod's node selector"
    else:
        out = m or "not scheduled"
    if "Preemption is not helpful" in m:
        out += "; pushing out lower-priority pods would not help"
    return out


def _ts(v):
    """Kubernetes timestamp (ISO string or datetime) -> datetime, or None."""
    if not v:
        return None
    if isinstance(v, datetime.datetime):
        return v
    try:
        return datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def waited_s(p):
    """Seconds between the pod's creation and the moment the scheduler placed it, or None if unknown."""
    created = _ts((p.get("metadata") or {}).get("creationTimestamp"))
    for c in (p.get("status") or {}).get("conditions") or []:
        if c.get("type") == "PodScheduled" and c.get("status") == "True":
            placed = _ts(c.get("lastTransitionTime"))
            if created and placed:
                return round(max(0.0, (placed - created).total_seconds()), 1)
    return None


def is_scheduled(p):
    return any(c.get("type") == "PodScheduled" and c.get("status") == "True"
               for c in (p.get("status") or {}).get("conditions") or [])


def terminated_reason(p):
    for cs in (p.get("status") or {}).get("containerStatuses") or []:
        for st in ((cs.get("state") or {}).get("terminated"), (cs.get("lastState") or {}).get("terminated")):
            if st and st.get("reason"):
                return st["reason"], cs.get("restartCount", 0)
    return None, 0


def disruption(p):
    for c in (p.get("status") or {}).get("conditions") or []:
        if c.get("type") == "DisruptionTarget" and c.get("status") == "True":
            return c.get("reason"), c.get("message") or ""
    return None, ""


class Watcher:
    def __init__(self):
        self.seen = {}                # uid -> dict(load, role, name, pending_reported, oom_restarts, killed, running)
        self.name_to_load = {}        # pod name -> load (to resolve "preempted by <pod>")
        self.uid_to_load = {}
        self.prio = {}                # pod name or uid -> priority (high | normal | low), to name the preemptor's
        self.kill_debt = {}           # load -> pods killed and not yet replaced
        self.done_events = set()      # event uids already handled
        self.mem_pressure = None

    def _kill(self, rec, event, out, **fields):
        if rec.get("killed"):
            return
        rec["killed"] = True
        self.kill_debt[rec["load"]] = self.kill_debt.get(rec["load"], 0) + 1
        out.append({"event": event, "load": rec["load"], "pod": rec["name"], "run": rec.get("run"), **fields})

    def update(self, pods, events=(), node=None, memory_top=None):
        out = []
        for p in pods:                                 # know every pod's load before reading events
            md = p.get("metadata") or {}
            if pod_load(p):
                self.name_to_load[md.get("name")] = pod_load(p)
                self.uid_to_load[md.get("uid")] = pod_load(p)
                self.prio[md.get("name")] = self.prio[md.get("uid")] = pod_priority(p)
        # events first: the "Preempted" event names the preemptor, the pod condition does not
        for e in events:
            md = e.get("metadata") or {}
            if md.get("uid") in self.done_events:
                continue
            self.done_events.add(md.get("uid"))
            obj = e.get("involvedObject") or {}
            if e.get("reason") == "Preempted" and obj.get("uid") in self.uid_to_load:
                rec = self.seen.get(obj["uid"])
                if rec:
                    self._kill(rec, "pod_preempted", out, cause=self._preemptor(e.get("message", "")))
        for p in pods:
            md = p.get("metadata") or {}
            uid, load = md.get("uid"), pod_load(p)
            if not uid or not load or pod_role(p) == "ballast":      # the ballast is not a load
                continue
            self.name_to_load[md.get("name")] = load
            self.uid_to_load[uid] = load
            rec = self.seen.setdefault(uid, {"load": load, "role": pod_role(p), "name": md.get("name"),
                                             "run": pod_run_no(p), "oom_restarts": 0})
            # waiting because there is no room
            why = unscheduled_reason(p)
            if why and not rec.get("pending_reported"):
                rec["pending_reported"] = True
                out.append({"event": "pod_pending", "load": load, "pod": rec["name"],
                            "cause": {"reason": short_reason(why), "message": why[:300]}})
            # placed after waiting: say how long it waited
            elif rec.get("pending_reported") and not rec.get("scheduled_reported") and is_scheduled(p):
                rec["scheduled_reported"] = True
                out.append({"event": "pod_scheduled", "load": load, "pod": rec["name"], "waited_s": waited_s(p),
                            "cause": {"reason": "room on the node became free"}})
            # OOM kill (restartPolicy Always: restartCount grows; Never: pod Failed with OOMKilled)
            reason, restarts = terminated_reason(p)
            if reason == "OOMKilled" and restarts > rec["oom_restarts"]:
                rec["oom_restarts"] = restarts
                out.append({"event": "oom_killed", "load": load, "pod": rec["name"], "run": rec.get("run"),
                            "cause": {"reason": "used more memory than its limit", "restarts": restarts}})
                self.kill_debt[load] = self.kill_debt.get(load, 0) + 1
            elif reason == "OOMKilled" and (p.get("status") or {}).get("phase") == "Failed" and not rec.get("killed"):
                self._kill(rec, "oom_killed", out, cause={"reason": "used more memory than its limit"})
            # evicted by the kubelet (node memory pressure)
            st = p.get("status") or {}
            if st.get("reason") == "Evicted":
                self._kill(rec, "pod_evicted", out, cause={"reason": "node memory pressure",
                                                           "message": st.get("message", "")[:200],
                                                           "top_users": memory_top or []})
            # preempted by the scheduler for a higher-priority pod
            dr, msg = disruption(p)
            if dr and "Preempt" in dr:
                self._kill(rec, "pod_preempted", out, cause=self._preemptor(msg))
            # running again after a kill
            if is_ready(p) and not rec.get("running"):
                rec["running"] = True
                if self.kill_debt.get(load, 0) > 0:
                    self.kill_debt[load] -= 1
                    out.append({"event": "pod_restarted", "load": load, "pod": rec["name"],
                                "cause": {"reason": "replacement pod running after a kill"}})
        if node is not None:
            pressure = any(c.get("type") == "MemoryPressure" and c.get("status") == "True"
                           for c in (node.get("status") or {}).get("conditions") or [])
            if self.mem_pressure is not None and pressure != self.mem_pressure:
                out.append({"event": "node_memory_pressure", "load": None, "state": "on" if pressure else "off",
                            "cause": {"reason": "node condition MemoryPressure"}})
            self.mem_pressure = pressure
        return out

    def _preemptor(self, msg):
        m = PREEMPT_RE.search(msg or "")
        who = m.group("who") if m else None
        by = self.name_to_load.get(who) or self.uid_to_load.get(who) or (who or "unknown")
        out = {"by": by, "message": (msg or "")[:200]}
        if self.prio.get(who):
            out["by_priority"] = self.prio[who]
        return out
