"""The runner interface: everything the engine needs from a runner. The engine never looks inside a runner.

The kit ships one runner, `kubernetes` (real pods on a real cluster). A runner plugin implements this class.

Times: `now` is seconds on the engine clock (0 = start of warm-up); `dt` is the tick length (default 1 s).
Run keys: `start_run` returns a key (e.g. "backup#3") the engine uses in later calls.
"""


class Runner:
    name = "base"
    label = "Runner"
    realtime = True          # True: the engine runs at 1x wall-clock time (a real cluster cannot be fast-forwarded)

    # ---------------------------------------------------------------- availability and lifecycle
    @staticmethod
    def available(settings, kubeconfig=None):
        """-> (bool, reason). Cheap check, used by the UI to show what can run here."""
        raise NotImplementedError

    def __init__(self, sc, rng, settings=None, **kw):
        """sc = validated scenario, rng = random.Random for this run (seeded)."""
        raise NotImplementedError

    def preflight(self):
        """-> list of problems (empty = ok): cluster reachable, node big enough, no leftovers from an earlier run."""
        return []

    def setup(self):
        """Create shared things (namespaces, Redis, priority classes) and wait until ready."""

    def cleanup(self):
        """Delete everything this run created."""

    # ---------------------------------------------------------------- services (streams and user traffic)
    def setup_service(self, name):
        """Create the always-on pods of a stream (producer + min consumers) or users load (users + min shop)."""
        raise NotImplementedError

    def set_rate(self, name, rate):
        """Stream: target events per second for the producer."""

    def set_users(self, name, users):
        """User traffic: number of simulated users."""

    def replicas(self, name):
        """Current consumer / shop pod count (desired, not only ready)."""
        raise NotImplementedError

    def scale(self, name, n):
        """Set consumer / shop replicas."""
        raise NotImplementedError

    # ---------------------------------------------------------------- batch runs
    def start_run(self, name, run, units):
        """Start batch run number `run` of load `name` with `units` work units. -> key."""
        raise NotImplementedError

    def stop_run(self, key, reason):
        """Stop a run (replaced, timed out, drain limit)."""
        raise NotImplementedError

    def active_runs(self, name=None):
        """Keys of runs still working (optionally for one load)."""
        raise NotImplementedError

    def run_info(self, key):
        """-> {elapsed, normal, done, units, state}  (state: running | done | replaced | timed_out | drain_limit)."""
        raise NotImplementedError

    # ---------------------------------------------------------------- observing
    def step(self, now, dt):
        """Advance / poll. -> list of reactive events, each {event, load, cause, ...}: job_ended, oom_killed,
        pod_evicted, pod_preempted, pod_pending, pod_restarted, pod_lost, node_memory_pressure, consumers_idle."""
        raise NotImplementedError

    def signals(self, name):
        """Scaler inputs. stream: current, running, cpu_util, backlog, arrival_rate, event_age.
        users: current, running, cpu_util, in_flight, request_rate, p95, errors."""
        raise NotImplementedError

    def event_age(self, name):
        """Stream: age in seconds of the oldest waiting event."""
        raise NotImplementedError

    def backlog(self, name):
        """Stream: events waiting."""
        raise NotImplementedError

    def snapshot(self):
        """Values for metrics.csv, per load and for the node ("_node").
        every load: cpu, memory, pods (running), pending (waiting / restarting)
        stream: replicas, backlog, event_age, arrival_rate, processed_rate
        users: replicas, users, request_rate, in_flight, latency_p95, errors
        batch: active_runs, progress
        _node: cpu_used, cpu_capacity, memory_used, memory_capacity"""
        raise NotImplementedError
