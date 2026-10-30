"""Forecasters (CONNECTION 5 pattern): prove the forecaster plugin contract WITHOUT a cluster or the model.

A forecaster is a pure function: context in -> quantiles out. So we test fit_predict with made-up history. The
'timestral' forecaster is only checked for its contract here (its model call is a stub; the real call is exercised
on the student's machine once Timestral is installed).  Run:  python -m unittest tests.test_forecaster
"""
import unittest

from loadkit.plugins.forecasters.reference import ReferenceForecaster
from loadkit.plugins.forecasters.timestral import TimestralForecaster
from loadkit.registry import contract_errors


def ctx(history, horizon=12, step=15.0, planned=(), node=None):
    return {"history": history, "horizon": horizon, "step_s": step, "planned_events": list(planned), "node": node}


class TestReferenceForecaster(unittest.TestCase):
    def test_keeps_its_interface(self):
        # the same check the kit runs for every enabled plugin: does it have "name" and "fit_predict"?
        self.assertEqual(contract_errors("forecasters", ReferenceForecaster), [])

    def test_returns_quantiles_at_every_step(self):
        out = ReferenceForecaster.fit_predict(ctx({"cpu": [(i, 1.0) for i in range(0, 600, 15)]}, horizon=12))
        for q in ("p10", "p50", "p90"):
            self.assertEqual(len(out["cpu"][q]), 12)
        # a proper band: p10 <= p50 <= p90 at every step
        self.assertTrue(all(a <= b <= c for a, b, c in zip(out["cpu"]["p10"], out["cpu"]["p50"], out["cpu"]["p90"])))

    def test_is_deterministic(self):
        a = ReferenceForecaster.fit_predict(ctx({"cpu": [(0, 2.0), (15, 3.0), (30, 2.5)]}, horizon=6))
        b = ReferenceForecaster.fit_predict(ctx({"cpu": [(0, 2.0), (15, 3.0), (30, 2.5)]}, horizon=6))
        self.assertEqual(a, b)

    def test_planned_event_is_a_future_covariate(self):
        hist = {"cpu": [(0, 1.0), (15, 1.0), (30, 1.0)]}
        base = ReferenceForecaster.fit_predict(ctx(hist, horizon=8))["cpu"]["p50"]
        bumped = ReferenceForecaster.fit_predict(
            ctx(hist, horizon=8, planned=[(60, "cpu", "burst_start", {"x": 3.0})]))["cpu"]["p50"]
        self.assertGreater(max(bumped), max(base))          # a scheduled burst lifts the forecast

    def test_timestral_keeps_its_interface(self):
        self.assertEqual(contract_errors("forecasters", TimestralForecaster), [])

    def test_forecasts_a_real_run(self):
        # end-to-end on the test-only fake cluster (no Kubernetes, no Timestral): context -> quantiles.
        import pathlib
        import tempfile
        from loadkit.forecast import build_forecast_context
        from tests.helpers import run as krun
        e = krun("normal-day-short", 7, record=True, out_dir=pathlib.Path(tempfile.mkdtemp()))
        c = build_forecast_context(e.dir, horizon=12)
        if not c["history"]:
            self.skipTest("fake cluster produced no metrics for this scenario")
        out = ReferenceForecaster.fit_predict(c)
        self.assertEqual(set(out), {k for k, v in c["history"].items() if v})


if __name__ == "__main__":
    unittest.main()
