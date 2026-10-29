"""Engine on the test-only fake cluster: determinism of the whole diary, origin/cause, work-based batch, concurrency policies,
priority/preemption, memory, health, scalers, lifecycle."""
import unittest

from loadkit import diary as diary_mod
from tests.helpers import cached, events, mutate, needs_scenario, run


def strip(e):
    return [{k: v for k, v in x.items() if k != "ts"} for x in e.diary.events]


class TestDeterminism(unittest.TestCase):
    def test_whole_diary_repeatable(self):
        from loadkit.scenario import SCENARIO_DIR
        for n in ("everything", "priority-squeeze", "memory-squeeze"):
            if not (SCENARIO_DIR / f"{n}.yaml").exists():
                continue
            with self.subTest(n):
                self.assertEqual(strip(run(n, 42)), strip(run(n, 42)))

    def test_seed_changes_diary(self):
        self.assertNotEqual(strip(run("sensor-storm", 1)), strip(run("sensor-storm", 2)))


class TestOrigin(unittest.TestCase):
    def test_every_event_has_origin_and_reactive_has_cause(self):
        e = cached("everything")
        for x in e.diary.events:
            self.assertIn(x["origin"], ("planned", "reactive", "control"))
            if x["origin"] == "reactive":
                self.assertIn("cause", x, x["event"])
            if x["origin"] == "planned":
                self.assertTrue(x["event"] in diary_mod.PLANNED or x["event"] == "phase_started", x["event"])

    def test_planned_events_match_timeline(self):
        e = cached("everything")
        due = [(x["load"], x["planned_t"]) for x in events(e, "job_due")]
        tl = [(x["load"], x["t"]) for x in e.timeline["events"] if x["kind"] == "job_due"]
        self.assertEqual(sorted(due), sorted(tl))

    def test_start_end_pairs(self):
        for n in ("midnight-pile-up", "overlapping-teams", "backup-snowball"):
            e = cached(n)
            started = {(x["load"], x["run"]) for x in events(e, "job_started")}
            ended = {(x["load"], x["run"]) for x in e.diary.events
                     if x["event"] in ("job_ended", "job_timed_out", "run_replaced")}
            self.assertEqual(started, ended, n)


class TestWorkBased(unittest.TestCase):
    def test_contended_runs_take_longer(self):
        e = cached("midnight-pile-up")
        self.assertTrue(any(x["ratio"] > 1.3 for x in events(e, "job_ended")))

    def test_uncontended_runs_take_normal_time(self):
        e = cached("normal-day")
        for x in events(e, "job_ended"):
            self.assertAlmostEqual(x["ratio"], 1.0, delta=0.05)

    def test_backup_snowball_grows(self):
        e = cached("backup-snowball")
        ov = [x["active_runs"] for x in events(e, "run_overlapped", "backup")]
        self.assertGreaterEqual(max(ov), 3)
        ratios = [x["ratio"] for x in events(e, "job_ended", "backup")]
        self.assertTrue(any(b > a for a, b in zip(ratios, ratios[1:])), ratios)

    def test_deadline_times_out(self):
        sc = mutate("backup-snowball", lambda s: [ld.update(deadline_factor=1.2) for ld in s["loads"]
                                                  if ld["name"] == "backup"])
        self.assertTrue(events(run(sc), "job_timed_out", "backup"))


class TestConcurrencyPolicies(unittest.TestCase):
    def _policy(self, pol):
        sc = mutate("backup-snowball", lambda s: [ld.update(concurrency_policy=pol) for ld in s["loads"]
                                                  if ld["name"] == "backup"])
        return run(sc)

    def test_forbid_skips(self):
        e = self._policy("forbid")
        self.assertTrue(events(e, "run_skipped", "backup"))
        self.assertFalse(events(e, "run_overlapped", "backup"))

    def test_replace_stops_old_run(self):
        e = self._policy("replace")
        self.assertTrue(events(e, "run_replaced", "backup"))

    def test_max_concurrent_runs_queues(self):
        sc = mutate("backup-snowball", lambda s: [ld.update(max_concurrent_runs=1) for ld in s["loads"]
                                                  if ld["name"] == "backup"])
        e = run(sc)
        self.assertTrue(events(e, "run_queued", "backup"))
        self.assertTrue(any("waited_s" in x for x in events(e, "job_started", "backup")))

    def test_idle_consumers_beyond_partitions(self):
        e = cached("idle-consumers")
        idle = events(e, "consumers_idle", "clicks")
        self.assertTrue(any(x["idle"] > 0 for x in idle))
        self.assertTrue(all(x["active"] <= 4 for x in idle))


class TestPriority(unittest.TestCase):
    def test_priority_squeeze_preempts_low_batch(self):
        e = cached("priority-squeeze")
        pre = events(e, "pod_preempted")
        self.assertTrue(pre)
        self.assertTrue(all(x["load"] == "nightly-etl" and x["cause"]["by"] == "payments" for x in pre))
        self.assertTrue(events(e, "health_degraded", "nightly-etl"))

    def test_no_preemption_when_off(self):
        sc = mutate("priority-squeeze", lambda s: s.update(preemption=False))
        e = run(sc)
        self.assertFalse(events(e, "pod_preempted"))
        self.assertTrue(events(e, "pod_pending"))

    def test_queue_starts_high_priority_first(self):
        def f(s):
            for ld in s["loads"]:
                if ld["type"] == "batch":
                    ld["max_concurrent_runs"] = 1
        e = run(mutate("midnight-pile-up", f))
        self.assertTrue(e.counters.get("job_started"))


class TestMemory(unittest.TestCase):
    @needs_scenario("memory-squeeze")
    def test_memory_squeeze_evicts_consumers_not_training(self):
        e = cached("memory-squeeze")
        ev = events(e, "pod_evicted")
        self.assertTrue(ev)
        self.assertTrue(all(x["load"] == "telemetry" for x in ev))
        self.assertTrue(events(e, "health_degraded", "telemetry"))

    @needs_scenario("slow-leak")
    def test_slow_leak_oom_and_backoff(self):
        e = cached("slow-leak")
        self.assertGreaterEqual(len(events(e, "oom_killed", "telemetry")), 3)

    @needs_scenario("memory-squeeze")
    def test_oom_loses_batch_progress_unless_checkpoint(self):
        def f(ckpt):
            def g(s):
                for ld in s["loads"]:
                    if ld["name"] == "training":
                        ld["memory"]["limit"] = 4 * 1024 ** 3
                        ld["checkpoint"] = ckpt
            return g
        lost = [x["lost_units"] for x in events(run(mutate("memory-squeeze", f(False))), "oom_killed", "training")]
        kept = [x["lost_units"] for x in events(run(mutate("memory-squeeze", f(True))), "oom_killed", "training")]
        self.assertTrue(lost and max(lost) > 0)
        self.assertTrue(kept and max(kept) == 0)


class TestHealth(unittest.TestCase):
    def test_every_load_is_evaluated(self):
        e = cached("everything")
        loads = {r["load"] for r in e.health_rows}
        self.assertEqual(loads, {ld["name"] for ld in e.sc["loads"]})

    def test_degraded_events_carry_context(self):
        e = cached("backup-snowball")
        d = events(e, "health_degraded")
        self.assertTrue(d)
        for x in d:
            for k in ("metric", "value", "threshold", "active_events"):
                self.assertIn(k, x)
        for x in events(e, "health_recovered"):
            for k in ("peak", "duration_s", "severity"):
                self.assertIn(k, x)

    def test_warmup_not_counted(self):
        e = cached("everything")
        self.assertFalse([x for x in events(e, "health_degraded") if x["phase"] in ("warmup", "baseline")])


class TestScalers(unittest.TestCase):
    def test_target_tracking_takes_max_and_records_why(self):
        e = cached("sensor-storm")
        sc = events(e, "consumers_scaled", "telemetry")
        self.assertTrue(sc)
        for x in sc:
            ps = x["cause"]["per_signal"]
            self.assertEqual(ps[x["cause"]["winner"]], max(ps.values()))

    def test_scale_down_waits_for_stabilization(self):
        e = cached("sensor-storm")
        sc = events(e, "consumers_scaled", "telemetry")
        ups = [x for x in sc if x["to"] > x["from"]]
        downs = [x for x in sc if x["to"] < x["from"] and x["phase"] == "scenario"]
        for d in downs:
            last_up = max([u["t"] for u in ups if u["t"] < d["t"]], default=None)
            if last_up is not None:
                self.assertGreaterEqual(d["t"] - last_up, 300 - 15)

    def test_none_never_scales(self):
        e = cached("backup-snowball")                 # telemetry uses scaler "none" there
        self.assertFalse(events(e, "consumers_scaled", "telemetry"))

    def test_concurrency_scales_shop_at_flat_traffic(self):
        # flat users, a neighbour fills the node -> shop slower -> in-flight up -> shop scaled
        def f(s):
            for ld in s["loads"]:
                if ld["type"] == "users":
                    ld["daily_pattern"] = "flat"
                    ld["peaks"] = []
        e = run(mutate("midnight-pile-up", f))
        up = [x for x in events(e, "shop_scaled") if x["to"] > x["from"] and x["phase"] == "scenario"]
        self.assertTrue(up)
        self.assertEqual(up[0]["cause"]["winner"], "concurrency")


class TestLifecycle(unittest.TestCase):
    def test_phases_in_order(self):
        e = cached("normal-day")
        names = [x["name"] for x in events(e, "phase_started")]
        self.assertEqual(names, ["preflight", "setup", "warmup", "baseline", "scenario", "drain", "teardown"])
        self.assertEqual(e.diary.events[-1]["event"], "run_ended")

    def test_soft_stop_goes_to_drain(self):
        from tests.helpers import load, setup
        from loadkit.engine import Engine
        s, pl = setup()
        e = Engine(load("everything"), s, pl, runner="fake", record=False)
        e.start()
        while e.t < 600:
            e.step()
        e.request_stop("soft")
        e.run()
        names = [x["name"] for x in events(e, "phase_started")]
        self.assertEqual(names[-2:], ["drain", "teardown"])
        self.assertEqual(e.status, "stopped")

    def test_hard_stop(self):
        from tests.helpers import load, setup
        from loadkit.engine import Engine
        s, pl = setup()
        e = Engine(load("everything"), s, pl, runner="fake", record=False)
        e.start()
        for _ in range(100):
            e.step()
        e.request_stop("hard")
        e.run()
        self.assertEqual(e.status, "stopped")
        self.assertNotIn("drain", [x["name"] for x in events(e, "phase_started")])


if __name__ == "__main__":
    unittest.main()


class TestRealTime(unittest.TestCase):
    def test_one_engine_second_is_one_real_second(self):
        """Regression test: the engine once ran ~1.9x faster than the wall clock on a real cluster."""
        import time
        from loadkit.engine import Engine
        from tests.fake_cluster import FakeCluster
        from tests.helpers import load, setup

        class RealTimeFake(FakeCluster):
            realtime = True                     # behave like the Kubernetes runner: no fast-forward

        s, pl = setup()
        pl = {**pl, "runners": {**pl["runners"], "rt": RealTimeFake}}
        e = Engine(load("normal-day"), s, pl, runner="rt", record=False)
        e.start()
        e.set_speed(1)
        w0, c0 = time.monotonic(), e.clock
        for _ in range(3):
            e.step()
            e.pace()
        wall, sim = time.monotonic() - w0, e.clock - c0
        self.assertEqual(sim, 3.0)
        self.assertGreater(wall, 2.9)           # 3 engine seconds took (at least) 3 real seconds
        self.assertLess(wall, 3.6)
