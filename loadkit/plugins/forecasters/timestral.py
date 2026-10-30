"""timestral: forecaster adapter for Mistral's Timestral time-series foundation model.

Why an adapter
  Timestral is probabilistic, multivariate and covariate-aware, and runs on CPU. That fits the kit exactly:
  * probabilistic  -> it already gives the quantiles the forecast band is drawn from;
  * covariates     -> the kit's planned timeline (bursts, job due-times, rate/user changes) is a KNOWN future
                      covariate, so a forecast can reflect "a 3x burst is scheduled at t+600 s";
  * CPU, small     -> the standard model (mistralai/Timestral-209M, ~837 MB) runs fine on a laptop's CPU; it
                      downloads once from Hugging Face, then everything is offline.
  * deterministic  -> the model's 21-quantile head is a regression, not sampling: same input, same band —
                      reproducibility + CI come for free, no seed needed.

The seam
  Everything below is model-agnostic: it shapes the kit's context into model-ready data and turns the model's
  output back into the kit's {series: {p10, p50, p90: [...]}} shape. The ONE place that talks to Mistral is
  `_model` — it is a stub here. The exact Timestral call is sketched in its docstring; fill in the body
  (and load the model ONCE — it is ~837 MB), and this whole file works unchanged.

Enable it (once installed with `pip install git+https://github.com/mistralai/timestral.git`) by adding
"timestral" to `forecasters:` in loadkit.yaml.
"""
from ...registry import register

QUANTILES = ("p10", "p50", "p90")


@register("forecasters")
class TimestralForecaster:
    name = "timestral"

    @staticmethod
    def fit_predict(ctx):
        """ctx -> {series: {p10, p50, p90: [...]}}. Timestral is zero-shot: no fit, so we go straight to the model."""
        model = TimestralForecaster._model(TimestralForecaster._to_model_input(ctx))
        if model is None:                       # model not installed: say so, rather than a silent crash
            raise RuntimeError("Timestral is not installed; enable 'reference' or install the model "
                               "(then complete TimestralForecaster._model)")
        return TimestralForecaster._to_quantiles(model, ctx)

    # ---------------------------------------------------------------- model-ready input (shape, model-agnostic)
    @staticmethod
    def _to_model_input(ctx):
        """Assemble the tensors Timestral would consume: the multivariate past + the known future covariates.

        Returns a plain dict (no numpy here) so this runs even when the model is absent. The real model call may
        convert it to its own array format.
        """
        history, horizon = ctx["history"], int(ctx["horizon"])
        step = float(ctx.get("step_s", 15.0))
        names = sorted(history)
        # past: per series, the observed values on the step grid
        past = {n: [v for _, v in history[n]] for n in names if history[n]}
        # known future covariates: the planned events, one-hot over the horizon, per series
        cov = {}
        for n in names:
            cells = [0.0] * horizon
            for (t, load, kind, value) in ctx.get("planned_events") or []:
                i = int(round((t - (history[n][-1][0] if history[n] else 0.0)) / step)) - 1
                if 0 <= i < horizon:
                    cells[i] = float(value.get("x") or value.get("factor") or value.get("units") or 1.0)
            cov[n] = cells
        return {"series": names, "past": past, "horizon": horizon, "step_s": step, "future_covariates": cov}

    # ---------------------------------------------------------------- the ONE place that talks to Mistral (STUB)
    @staticmethod
    def _model(model_input):
        """Call Timestral here and return {series: {p10, p50, p90: [...]}}, or None if it is not installed.

        The real API (mistralai/timestral, confirmed 2026-10):
            import torch
            from timestral import TimestralForecaster as TimestralModel   # same name as this class — alias it
            model = TimestralModel.from_pretrained("mistralai/Timestral-209M",
                                                   device="cuda" if torch.cuda.is_available() else "cpu")
            forecast = model.forecast(
                context,            # numpy/torch: (T,) one series, or (B, V, T) — V variates forecast jointly
                prediction_length=horizon,
                known_covariates=known,   # (B, C, T + horizon): past AND future; all-0/1 rows are treated
                                          # as indicators — exactly the shape _to_model_input already builds
            )
            p10, p50, p90 = forecast.quantile(0.1), forecast.quantile(0.5), forecast.quantile(0.9)  # (B, V, H)
            return {name: {"p10": p10[0, i], "p50": p50[0, i], "p90": p90[0, i]}
                    for i, name in enumerate(model_input["series"])}
        Load the model ONCE (module level or a cached instance — ~837 MB); the quantile head is deterministic,
        so no seed is needed and a run's band is reproducible by construction.
        """
        try:
            import timestral  # noqa: F401
        except Exception:  # noqa: BLE001
            return None
        return None        # <-- complete this with the real Timestral call (see the docstring above)

    # ---------------------------------------------------------------- model output -> kit quantiles
    @staticmethod
    def _to_quantiles(model_out, ctx):
        names = model_out.get("series") or sorted((model_out.get("past") or {}))
        return {n: {q: [float(v) for v in model_out[n][q]] for q in QUANTILES if n in model_out and q in model_out[n]}
                for n in names}
