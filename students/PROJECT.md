# Project brief — the Load Scenario Kit

*Course D0020E · 5 groups · the full contract for each build is in [TASK.md](TASK.md)*

## What this project is

One machine. Three kinds of real work fighting over it:

- **batch jobs** — a backup that must finish by 2 am, a report due every 30 minutes;
- **a data stream** — events arriving, buffered, processed;
- **user traffic** — real users waiting for a response.

The **Load Scenario Kit** puts all three on a single Kubernetes node and scripts a repeatable
storm: a burst at minute 40, a big job at minute 60, a user surge at minute 150. It runs the storm
on a real cluster, measures the machine and every load every 15 seconds, and writes down **every
real event — with its cause** — in a diary. After the run it analyses who hurt whom.

The kit you are handed is complete and working: the engine, the scenarios, the UI, the pipeline.
What it is missing is the **intelligence layer** — the four things an on-call engineer actually
wants, and what you will build:

1. a **scaler** — *how many pods do I need, right now?*
2. a **forecaster** — *what will the metric look like over the next few minutes?*
3. an **early-warning detector** — *is something about to go wrong, before the alarm trips?*
4. a **cause-finder** — *when it did go wrong, who caused it?*

## Why this is a real project, not a toy

- **The problem is real.** Every company running containers hits this: load spikes, OOM kills,
  latency storms. SREs solve it with autoscaling, forecasting, anomaly detection and root-cause
  analysis. You are building a miniature of that exact toolbox.
- **The stack is real.** Kubernetes, real worker pods, a real Redis, real CPU and memory pressure,
  a real UI with replay.
- **The grading is honest.** The kit records the ground truth in every run folder. Nobody's
  forecaster is "the victim" in advance — you **measure**: does your p10–p90 band contain the real
  line? Did your warning fire before the alarm, and how many false alarms? Does your cause-finder
  name the same culprit as the diary? No secret tests, no vibes.

## What you will build

| build | your job | measured against |
|---|---|---|
| **Scaler** (from scratch) | a `decide()` that reads live signals (backlog, CPU, event age) and returns the right pod count | fewer degraded minutes in `health.csv` than doing nothing |
| **Forecaster** (around **Timestral**) | make Mistral's Timestral model (standard model `mistralai/Timestral-209M`) work through the existing `_model` seam (probabilistic, multivariate, CPU, zero-shot) and beat the built-in `reference` baseline | your p10–p90 band contains the real line in `metrics.csv`; the peak is predicted |
| **Early-warning detector** (from scratch) | fire before the `health_degraded` event lands in the diary, with few false alarms | lead time (seconds before the alarm) and false-alarm count |
| **Cause-finder** (from scratch) | when a run goes wrong, name the load that caused it | precision / recall against `impact.json` and the diary |

The forecaster and the **forecast-bands UI panel** are one feature seen from two ends: the panel
(shipped and enabled) draws the p10/p50/p90 your forecaster returns, so a better forecaster
automatically makes the panel better. Full interfaces and the minimum-viable bar for each build:
[TASK.md](TASK.md).

## The software engineering you will actually be doing

This is the heart of the course — each build trains a named engineering skill:

| you build | the engineering skill it trains |
|---|---|
| plugins in a **frozen core** | **Open/closed architecture** — the core (engine, runner, worker, pipeline, `loadkit.yaml`) is protected: it only changes with the instructor's approval. You add capability by following the kit's five-connection plugin pattern, never by editing the engine. Your scaler, forecaster and detectors must work with **zero changes to the core**. |
| the `decide()` / `fit_predict()` contracts | **Contract-first design** — you code against a fixed data contract (live signals in, a decision out; history + known future in, quantiles out). The same contract your code follows is the one the tests and the UI rely on. |
| the model behind the `_model` seam | **Models as swappable components** — the kit never knows which forecaster model you use; the Mistral call lives in one function and the contract around it is yours to keep. That is how production ML teams keep models replaceable. |
| tests that pass with **no cluster** | **Testability by design** — every plugin must be provable on plain data against the kit's simulated cluster. If your algorithm cannot be tested without Kubernetes, it is not done. This is why the whole kit's test suite (97 tests) runs in seconds on a laptop. |
| the push → PR → CI → review → merge loop | **CI/CD in practice** — every push runs style + unit tests on GitHub's machines; every pull request also gets a deterministic simulation of all short scenarios compared against `main`, posted as a comment you have to read and defend. |
| numbers against the diary | **Evaluation-driven development** — "does the band contain reality?" "how many seconds of lead time?" "what is my precision?" You report the number; a claim without the number is not done. |
| one repo per group, protected `main` | **Team version control** — branches, pull requests, peer review, and CODEOWNERS rules that decide who may approve what. |
| the forecast-bands panel (and any detector alerts you add) | **Observability for humans** — making a machine's behaviour legible: bands, lights, plain words. |
| Timestral on a laptop-sized machine | **Time-series ML in practice** — probabilistic output as bands, the scenario's planned events as *future covariates*, and a deterministic quantile head so a band is reproducible. |

## How you will work

- **5 groups**, each with its own private repository forked from this template.
- `main` is **protected**: you push a branch, open a pull request, CI runs, a group member reviews,
  then it merges. Students never push to `main` directly.
- **CODEOWNERS**: core files need the instructor's approval; plugins, scenarios, tests and docs
  the group can self-approve.
- **The pipeline needs no cluster** — the heavy lifting happens on GitHub, not on your laptop.
- **Finals**: the groups run their scenarios on one shared instructor-managed cluster, each on its
  own labelled node with its own scoped key, so all five groups run in parallel without touching
  each other.

## Hardware requirements

### Your laptop (day-to-day development)

Everything you do day-to-day — write plugins, run the tests, run scenarios on the built-in
simulated cluster, open the UI on a simulated run — is **in-process**. No Kubernetes, no Docker,
no GPU.

| item | minimum |
|---|---|
| OS | any modern Linux / macOS / Windows |
| CPU | 2 cores |
| RAM | 8 GiB |
| disk | ~10 GiB free |
| software | Python 3.12, Git |
| network | access to github.com (repository + Actions) |

Example day-to-day loop, all on the laptop: `make ci-local` (the same checks as the pipeline),
`python ci/simulate.py run --out ci-out` (recorded runs on the simulator), then
`LOADKIT_RUNS_DIR=ci-out/runs make ui` to open one in the UI.

Once, when the forecaster starts: `pip install git+https://github.com/mistralai/timestral.git` (not on PyPI;
pulls in torch), and the standard model `mistralai/Timestral-209M` (~837 MB) downloads from Hugging Face on
first use — after that, everything runs offline.

### The shared class cluster (final validation only, instructor-managed)

| item | requirement |
|---|---|
| worker nodes | 5 — one per group, so all five finals run in parallel |
| per node | 4 cores / 8 GiB RAM (short scenarios) — up to 32 GiB for the full 2-hour scenarios |
| total | ≈ 20 cores / 40 GiB, plus a control plane and metrics-server (the kit checks for it at start-up) |
| image registry | the worker image is published by CI from `main`; the nodes must be able to pull it |

Your laptop needs **nothing** from this cluster except network access to its API server — the kit
drives everything through the API, and the group's scoped key only opens the doors the kit needs
(no kubectl, no node access).

## Where to start

[TASK.md](TASK.md) — the *first 30 minutes* section at the bottom is your onboarding: read the
example plugin, run one scenario, open the run folder, and see the built-in forecast band that
your Timestral forecaster has to beat.
