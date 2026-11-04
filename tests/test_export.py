"""Run folder format v1, attribution, counterfactual, compare, snapshot, plugin contracts."""
import copy
import csv
import json
import pathlib
import tempfile
import unittest

from loadkit import FORMAT_VERSION, analyze, registry, settings
from tests.helpers import cached, load, needs_scenario, run, setup

FILES = ("run.json", "scenario.yaml", "timeline.json", "diary.jsonl", "metrics.csv", "health.csv", "impact.json",
         "calibration.json", "README.txt")


class TestPlainWords(unittest.TestCase):
    def test_durations_keep_seconds_under_10_min(self):
        from loadkit.export import mins
        self.assertEqual([mins(45), mins(240), mins(270), mins(260), mins(1123)],
                         ["45 s", "4 min", "4 min 30 s", "4 min 20 s", "19 min"])


class TestExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = pathlib.Path(cls.tmp.name)
        cls.e = run("backup-snowball", record=True, out_dir=cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_all_files_and_version(self):
        for f in FILES:
            self.assertTrue((self.e.dir / f).exists(), f)
        info = json.loads((self.e.dir / "run.json").read_text())
        self.assertEqual(info["format_version"], FORMAT_VERSION)
        self.assertEqual(info["status"], "completed")

    def test_shared_clock(self):
        rows = list(csv.DictReader((self.e.dir / "metrics.csv").read_text().splitlines()))
        self.assertEqual(list(rows[0].keys()), ["t", "ts", "phase", "load", "metric", "value"])
        self.assertLess(float(rows[0]["t"]), 0)                 # warm-up is negative
        h = list(csv.DictReader((self.e.dir / "health.csv").read_text().splitlines()))
        self.assertEqual(list(h[0].keys()), ["t", "ts", "phase", "load", "metric", "value", "threshold", "healthy"])
        d = [json.loads(x) for x in (self.e.dir / "diary.jsonl").read_text().splitlines()]
        scen = next(x for x in d if x["event"] == "phase_started" and x["name"] == "scenario")
        self.assertEqual(scen["t"], 0)

    def test_unknown_version_refused(self):
        bad = self.out / "bad"
        bad.mkdir()
        (bad / "run.json").write_text(json.dumps({"format_version": 99}))
        with self.assertRaises(analyze.RunFormatError):
            analyze.load_run(bad)

    def test_compare(self):
        e2 = run("backup-snowball", seed=7, record=True, out_dir=self.out)
        res = analyze.compare(self.e.dir, e2.dir)
        self.assertIn("backup degraded min", res["text"])

    def test_counterfactual(self):
        sc = copy.deepcopy(load("backup-snowball"))
        sc["loads"] = [ld for ld in sc["loads"] if ld["name"] != "training"]
        b = run(sc, record=True, out_dir=self.out)
        res = analyze.counterfactual(self.e.dir, b.dir)
        self.assertEqual(res["removed_loads"], ["training"])
        self.assertTrue(any(p["disappears"] for p in res["periods"]))

    def test_snapshot(self):
        from loadkit import snapshot
        s, pl = setup()
        old = settings.runs_dir
        try:
            settings.runs_dir = lambda _s: self.out
            import loadkit.server as srv
            srv.runs_dir = settings.runs_dir
            out = snapshot.build(s, pl, self.e.run_id, self.out / "snap.html")
        finally:
            settings.runs_dir = old
            srv.runs_dir = old
        html = out.read_text()
        self.assertIn("window.__SNAP", html)
        self.assertNotIn('src="/app.js"', html)


    def test_excel_export(self):
        import io
        import openpyxl
        from loadkit import export
        name, data = export.build(self.e.dir)
        self.assertTrue(name.endswith(".xlsx"))
        wb = openpyxl.load_workbook(io.BytesIO(data))
        self.assertEqual(wb.sheetnames, ["Summary", "Events", "Timeline", "Health", "Settings", "Impact"])
        hdr = [c.value for c in wb["Events"][1]]
        for col in ("What happened", "Why (cause)", "Load healthy", "Machine % busy", "Day time"):
            self.assertIn(col, hdr)
        rows = list(wb["Events"].iter_rows(min_row=2, values_only=True))
        self.assertTrue(any(r[hdr.index("Event")] == "run_overlapped" for r in rows))
        self.assertGreater(wb["Timeline"].max_row, 100)


class TestAttribution(unittest.TestCase):
    def test_cpu_shares_sum_to_one(self):
        e = cached("midnight-pile-up")
        imp = analyze.impact_of(e)
        self.assertTrue(imp["periods"])
        for p in imp["periods"]:
            cpu = [c["share"] for c in p["causes"] if c["kind"] == "cpu"]
            if cpu:
                self.assertAlmostEqual(sum(cpu), 1.0, delta=0.05)

    def test_snowball_names_itself(self):
        imp = analyze.impact_of(cached("backup-snowball"))
        self.assertTrue(any(c.get("self") for p in imp["periods"] if p["load"] == "backup" for c in p["causes"]))

    @needs_scenario("memory-squeeze")
    def test_memory_cause_is_training(self):
        imp = analyze.impact_of(cached("memory-squeeze"))
        p = next(p for p in imp["periods"] if p["load"] == "telemetry")
        mem = [c for c in p["causes"] if c["kind"] == "memory"]
        self.assertEqual(mem[0]["load"], "training")


class TestPlugins(unittest.TestCase):
    def test_contracts(self):
        _, pl = setup()
        for kind, items in pl.items():
            for name, cls in items.items():
                self.assertEqual(registry.contract_errors(kind, cls), [], name)

    def test_disabling_a_scaler_keeps_the_rest_working(self):
        from loadkit import scenario
        s = settings.load()
        s["plugins"]["scalers"] = ["none"]
        pl = registry.load_enabled(s)
        with self.assertRaises(scenario.ScenarioError) as cm:     # uses target-tracking -> clear error
            scenario.load_file("sensor-storm", pl)
        self.assertIn("not an enabled scaler", str(cm.exception))
        scenario.load_file("overlapping-teams", {**pl, "scalers": {**pl["scalers"],
                                                                  "concurrency": setup()[1]["scalers"]["concurrency"]}})

    def test_disabling_a_load_type(self):
        from loadkit import scenario
        s = settings.load()
        s["plugins"]["load_types"] = ["batch"]
        pl = registry.load_enabled(s)
        with self.assertRaises(scenario.ScenarioError):
            scenario.load_file("everything", pl)

if __name__ == "__main__":
    unittest.main()
