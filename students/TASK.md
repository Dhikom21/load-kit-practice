# Your build — the intelligence layer

*(The big picture — what the project is, what it trains, and the hardware — is in [PROJECT.md](PROJECT.md). This file is the working contract for each build.)*

You are handed a running kit. It drives real pods on a real machine and writes down **everything that
really happened**. Your job is to add the four things an on-call engineer actually wants — and to show
them in the UI:

- a **scaler** — *how many pods do I need, right now?*
- a **forecaster** — *what will the metric look like over the next few minutes?*
- an **early-warning detector** — *is something about to go wrong, before the alarm trips?*
- a **cause-finder** — *when it did go wrong, who caused it?*

**The one rule that keeps this clear:** the kit already records the ground truth in every run folder.
You never guess whether your work is good — you **measure it** against `metrics.csv`, `diary.jsonl` and
`impact.json`. Nobody's forecaster is "the victim" in advance: the diary decides.

## First, how a plugin works here (the only pattern you need)

You have one finished example to learn from: **`loadkit/plugins/scalers/step.py`** — every line is
explained. Every plugin of every kind follows the same **five connections**:

1. **Registry** — `loadkit/registry.py`: the name is mapped to a file, and `@register("…")` puts your class on the list.
2. **Config** — `loadkit.yaml`: the name appears in an enabled list (`scalers:`, `forecasters:`, `ui_panels:`…).
3. **Scenario** — a scenario file names it (e.g. `scaler: {name: step, …}`).
4. **One call site** — the core calls you in exactly one place. For scalers it is `_control_loop()` in `loadkit/engine.py`.
5. **A test with no cluster** — `tests/test_step_scaler.py` proves it works on plain data. *Every* plugin must pass a test without a cluster — that is how this whole kit is built, and how your work is checked.

The mechanics (1–3 and 5) are copy-paste. **Your real work is the algorithm inside**, and it is judged by
the diary, not by a secret. Read `step.py` and `target_tracking.py` and the pattern is yours.

## Your answer key: the run folder

Every finished run (`runs/<run-id>/`) already contains what you are judged against:

| file | what it is | you use it to check |
|---|---|---|
| `metrics.csv` | every metric, every 15 s (CPU, memory, backlog, latency…) | does your forecast / band match reality? |
| `health.csv` | was each load healthy, and when | does your warning fire early? |
| `diary.jsonl` | **every real event + its cause** (kills, preemptions, bursts, health changes) | did you detect it before it happened? who caused it? |
| `impact.json` | the kit's own "likely causes" | does your cause-finder agree? |

Before you write any code, run your first scenario and open these:
`python -m loadkit run idle-consumers-short --runner fake` (no cluster needed; the seed does not matter
there — the script is fixed) → then read `runs/<run-id>/README.txt`.

## The four builds (same shape each time)

You choose the model and write it from scratch. Your scenarios are the three in
[SCENARIOS.md](SCENARIOS.md), in that order. Develop on the **short** versions (they are written for a
4-core machine); prove it at the end on the full-length version of one of your three.

| build | where it plugs in | interface | judged against | minimum-viable (so you never stall) |
|---|---|---|---|---|
| **Scaler (yours)** | `scalers:` + `_MODULES["scalers"]` + `@register("scalers")` | `decide(now, signals, current, cfg, state)` | fewer degraded-minutes in `health.csv` than the `none` scaler | a new scaler that beats `none` on one short scenario, with a passing test |
| **Forecaster** | a built-in `reference` forecaster + a `timestral` adapter already exist in `loadkit/plugins/forecasters/` (the real Mistral call is the one stub in `timestral.py::_model`) | `fit_predict(ctx) -> {series: {p10, p50, p90}}` | does your p10–p90 band contain the real line in `metrics.csv`? was the peak predicted? | beat the `reference` baseline; then put **Timestral** (Mistral, standard model `mistralai/Timestral-209M`) through the `_model` seam |
| **Early-warning detector** | new (your choice of kind) | your contract | lead time: the warning lands **before** the `health_degraded` event in `diary.jsonl`; few false alarms | fires early at least once on a short scenario, with a small false-alarm count |
| **Cause-finder** | a reference already lives in `loadkit/analyze.py` (`build_impact`) | your contract | names the same culprit load as `impact.json` (precision / recall) | reproduces the overlap rule; stretch: catch a case the kit misses |

**The data contract is already in `loadkit/forecast.py`.** The forecaster gets a context: `history`
(every load's metrics, 15 s steps), `horizon` (steps ahead), and `planned_events` — the *known* future
(planned bursts, job due-times, user surges) that Timestral can use as **future covariates** (its
`known_covariates` argument spans the past *and* the horizon). It returns `{series: {p10, p50, p90}}` —
**those percentiles are your forecast bands**, so the forecaster and the UI panel are one feature seen from
two ends. Timestral is probabilistic and multivariate, so it fits this contract natively; your job is to call
it inside `timestral.py::_model` and make it beat the built-in `reference` forecaster. The model's quantile
head is deterministic — same input, same band — so no seeding is needed.

**Installing Timestral (once, on the laptop):** `pip install git+https://github.com/mistralai/timestral.git` —
it is **not on PyPI**; needs Python ≥ 3.10 and pulls in torch. The standard model `mistralai/Timestral-209M`
(~837 MB) downloads from Hugging Face on first use, then runs fully offline. Smaller sizes (123M / 28M / 5M)
exist for weak machines, but the class standard is **209M** so all groups' numbers stay comparable.

## Show it: the forecast-bands panel (post-run, in the replay view)

The panel already exists and is enabled: `ui/panels/forecast.js` (listed in `ui_panels:` in `loadkit.yaml`),
fed by `/api/runs/<id>/forecast`, which calls the enabled forecaster. Read `ui/app.js` (how a panel works)
and `forecast.js` (how the band is drawn from the `p10/p50/p90` the API returns).

- It draws the real metric line, **shades between your p10 and p90**, and puts a dashed line at p50. Open a
  finished run, pick a load and a metric, and the band appears at the end of the run.
- Make it better: more metrics, a detector-alerts layer on top, or the live view (a later step).
- Minimum-viable: the band renders for one load in one finished run (it already does — the marks are for a
  *better* forecaster behind it).

## How you'll be graded (plain words)

- **Mechanical (must be present):** registered, switched on in `loadkit.yaml`, a passing test with no
  cluster, visible in the UI. This is the entry ticket, not where the marks are.
- **Algorithmic (the marks):** forecast quality (does the band contain reality? is the peak right?),
  warning lead-time (early? how many false alarms?), cause-finder accuracy (right load?).
  **Report your numbers** against the run folder — a claim without the number is not done.
- **Always:** develop on a short scenario, then prove the same thing on the full-length version of one
  of your three scenarios.

## Your first 30 minutes

1. `make ui` and open a past run (or run one).
2. Read `loadkit/plugins/scalers/step.py` top to bottom.
3. `python -m loadkit run idle-consumers-short --runner fake`; open the run folder and read `README.txt`,
   `metrics.csv` and `impact.json`.
4. Open the finished run in `make ui` and look at the **Forecast** panel — the built-in `reference` band is
   already there. Your job is to make Timestral beat it.
