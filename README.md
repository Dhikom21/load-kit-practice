# Load Scenario Kit

Repeatable **resource-contention scenarios** on a real Kubernetes cluster — batch jobs, data streams and user traffic
sharing one machine — with a **diary** of everything that happened and an after-the-run view of the **impact between loads**.
Built for teaching (D0020E) and for testing agents and forecasters on hidden causes.

## Quick start on a laptop (kind)

Needs Docker, [kind](https://kind.sigs.k8s.io/), kubectl and Python 3.10+.

```bash
pip install pyyaml kubernetes redis openpyxl
make kind-up        # local cluster + metrics-server + worker image (first time only)
make ui             # open http://127.0.0.1:8765, pick a scenario, press Start
```

On another cluster (e.g. Rancher): `make image`, push the image to a registry the cluster can pull from, set
`kubernetes.image` (and `kubernetes.node`, e.g. `role=experiment`) in `loadkit.yaml`, then
`make ui KUBECONFIG=path/to/your/kubeconfig`. Your kubeconfig is only read, never stored.

Runs take real time: a 2-hour scenario takes 2 hours plus 7 minutes of warm-up/baseline and a short drain.

## What you see

- **The machine** — every pod as a block on the node, pods waiting outside when there is no room, CPU and memory bars.
- **Health of every load** — a light and one sentence per load. Nobody is labelled the victim in advance.
- **Recent events** — the important events in plain words.
- **Impact between loads** — after the run: likely causes, and a button to re-run the same seed without a suspect to confirm.
- **Replay** — drag the slider to any moment of a finished run.
- **Export to Excel** — next to the replay slider: the whole run as a workbook (summary, every event in plain words with the
  state of the load and the machine at that moment, a 15 s timeline, health checks, settings, impact between loads).
- **Advanced** — timeline (planned vs actual), CPU and memory per load, replicas and scaler decisions, the shop clue,
  comparing two runs, and the full diary.

## Pipeline (CI/CD)

Every push runs the checks on GitHub (style, scenario files, course rules, unit tests); every pull request also gets
a simulated comparison of all short scenarios against `main`, posted as a comment; a small real Kubernetes cluster
runs a check every night. Details, and the one-time GitHub setup for the teacher: [`docs/CI.md`](docs/CI.md).
Run the same checks locally with `make ci-local`.

## Sharing one cluster between groups

Each group sets its own name and machine in `loadkit.yaml`:

```yaml
group: g3                       # names become g3-system, g3-batch, ...; priority classes g3-high, ...
kubernetes:
  node: role=experiment-3       # this group's worker node (or null: the kit picks a node no other group uses)
```

- Everything the kit creates carries the label `loadkit/group=g3`; start-up checks, clean-up and `reset` only touch
  your own group (`reset --all-groups` is for the admin).
- A run never starts on a node where another group's run is active; it says which group is using it.
- Only your group's namespaces can reach your group's Redis.
- Without `group`, the kit behaves as a single-user kit (names start with `loadkit-`), e.g. on your own laptop.
- Recommended for the admin: give each group a kubeconfig that may only touch its own `g3-*` namespaces.

## The scenarios

**Full guide: [`docs/SCENARIOS.md`](docs/SCENARIOS.md)** — for every scenario: the story, the maths, what happens
minute by minute, how to run it, what to look for in the UI and the Excel export, the expected result, and small
experiments to try.

**Short teaching versions** (`…-short`, 20–30 min, written for a 4-core machine; about 35 min including warm-up
and drain): `normal-day-short`, `midnight-pile-up-short`, `sensor-storm-short`, `flash-sale-short`,
`overlapping-teams-short`, `everything-short`, `backup-snowball-short`, `idle-consumers-short` (2 partitions),
`priority-squeeze-short`. They show the same effects as the full versions; the UI lists them first.

**Full versions** (1–3 h):

| Scenario | What it shows |
|---|---|
| `normal-day` | baseline: little or no contention |
| `midnight-pile-up` | three teams' batch jobs overlap and slow each other |
| `sensor-storm` | stream bursts; the autoscaler adds consumers on a busy node |
| `flash-sale` | user peak + order-stream burst at the same time |
| `overlapping-teams` | jobs every 30/45/60 min drift in and out of overlap |
| `everything` | all load types together, mild |
| `backup-snowball` | a backup's runs overlap and pile up after a busy period — nobody changed anything |
| `idle-consumers` | more consumers than partitions: extra pods idle, backlog keeps growing |
| `priority-squeeze` | a high-priority stream scales up and pushes out a low-priority batch job |

Scenario files are YAML in `scenarios/`. A scenario + seed always gives the same **planned** events (due times, work
units, bursts, user counts); what happens during the run is real and varies a little between runs.

## Command line

```bash
python -m loadkit validate                         # check all scenarios (no cluster needed)
python -m loadkit timeline backup-snowball --seed 7
python -m loadkit run backup-snowball --seed 42    # headless run -> runs/<run-id>/
python -m loadkit run backup-snowball --seed 42 --without training   # same seed, one load removed
python -m loadkit analyze <run> --baseline <run-without-training>   # did the slowdown disappear?
python -m loadkit compare <run-a> <run-b>
python -m loadkit snapshot <run>                   # one HTML file of the run to share
python -m loadkit reset                            # delete everything the kit created on the cluster
make test                                          # unit tests (no cluster needed)
```

## What a run produces (`runs/<run-id>/`, format_version 1)

`run.json`, `scenario.yaml`, `timeline.json` (planned events only), `diary.jsonl` (every event with
`origin: planned | reactive | control`; reactive ones carry a `cause`), `metrics.csv` and `health.csv` (long format,
15 s steps), `impact.json` (unhealthy periods per load + likely causes), `calibration.json`, `README.txt`.
`t` = seconds since the scenario phase started (negative during warm-up/baseline).

## How it works

- **Engine** (your laptop, or inside the cluster): plays the planned timeline, applies each batch job's overlap rule
  (allow / forbid / replace), runs the scalers every 15 s, checks every load's health, writes the diary.
- **Kubernetes runner**: batch runs are Indexed Jobs; streams and the shop are Deployments; all kit pods are placed on
  one node; priority classes high / normal / low (with or without preemption, per scenario). If the node is bigger
  than the scenario's node size, a ballast pod uses the difference.
- **Worker image** (`worker/`): real CPU work measured in CPU-seconds (so a work unit is the same amount of work on
  any machine and simply takes longer when the machine is busy) and real memory.
- **Redis** inside the cluster is the meeting point: pods read control values and write stats/heartbeats; they never
  call the engine. From a laptop the engine reaches it through the Kubernetes API (no kubectl needed).

## Plugins

`loadkit.yaml` lists the enabled plugins — remove a name to switch it off: load types (`batch`, `stream`, `users`),
runner (`kubernetes`), scalers (`target-tracking`, `concurrency`, `none`), UI panels. Code: `loadkit/plugins/`.

## Status

Done: scenarios, generator, engine, health, scalers, run folder, likely causes + counterfactual, compare, UI,
worker image, Kubernetes runner. Built and unit-tested without a cluster — **the first end-to-end run on kind is the
next step**. Later: Prometheus metrics (M7), blind naming + leak check (M7b), running the kit inside the cluster
(M8), Rancher (M9), more scalers and forecasters. ColonyOS integration comes last.

## Safety

The kubeconfig is a run-time argument and is never stored. `runs/`, kubeconfigs and env files are git-ignored.
The kit only creates objects labelled `app.kubernetes.io/part-of=loadkit`, every kit pod has a memory limit, and
`make reset` removes everything. The UI server binds to 127.0.0.1.
