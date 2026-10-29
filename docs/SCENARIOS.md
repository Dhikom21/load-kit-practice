# Scenarios: what each one shows, how to run it, what to look for

Each scenario is a small story about **loads competing for one machine**. This guide explains every scenario in the
same way:

1. **The story** in one or two sentences.
2. **The loads** and how much CPU each one needs (the maths).
3. **What happens**, minute by minute.
4. **What to look for** in the UI and in the Excel export.
5. **Expected result**, so you can tell whether your run behaved as designed.
6. **Try this**: small changes that turn the scenario into an experiment.

There are two versions of most scenarios:

| | Short (`…-short`) | Full |
|---|---|---|
| Length | 20–30 min (about 35 min with warm-up and the end) | 1–3 h |
| Machine size it is written for | 4 cores (3 for `backup-snowball-short`) | 3–8 cores |
| Use it for | learning, trying ideas, most student work | final experiments, longer patterns |

The short versions show the same effects; this guide describes the **short version first** and the full version
in one line at the end of each section.

---

## Before you start: a few words

| Word | Meaning |
|---|---|
| **helper / core** | one CPU core = one "helper" that can do 1 second of work every second |
| **CPU-second** | the work one core does in one second. A job of 360 CPU-seconds takes 6 min on 1 core, 3 min on 2 cores |
| **normal time** | how long a batch run takes when it gets all the CPU it asks for |
| **× normal** | how much longer a run really took: 1.8× normal = 80 % longer than planned |
| **unhealthy** | a load broke its limit for 2 checks in a row (checks every 15 s). Limits: batch run > 1.5× normal; stream: oldest waiting event > 30 s; shop: 95 % of answers slower than 300 ms, or > 1 % errors |
| **planned** | decided by the scenario file and the seed before the run (due times, bursts, user counts) |
| **reactive** | decided during the run (how long runs take, scaling, pods pushed out, unhealthy periods) |
| **seed** | the "day number" for the random parts (random delays and bursts). Same seed = same plan |

**Expected results** in this guide come from the kit's own test simulation. On a real cluster the numbers differ a
little from run to run (real CPUs are never perfectly even), but the pattern must be the same: if a scenario says
"all three jobs become unhealthy", that must happen in your run too.

---

## How to run any scenario

**In the UI** (`python -m loadkit serve`, then open <http://127.0.0.1:8765>):

1. Pick the scenario in the list on the left (short versions are under *Short · for learning*).
2. Leave the seed at 42 unless the guide says otherwise, press **Start**.
3. Watch it live, or come back later: open it under **Past runs**, drag the **replay** slider, press
   **Export to Excel**.

**From the command line** (WSL / Linux terminal, in the `load-kit` folder):

```bash
python -m loadkit run midnight-pile-up-short --seed 42      # runs it; the result goes to runs/<run-id>/
python -m loadkit run midnight-pile-up-short --seed 42 --without backup   # same day without one load
python -m loadkit compare <run-a> <run-b>                    # side by side
python -m loadkit analyze <run> --baseline <run-without-x>   # did the problem disappear without x?
```

**To reproduce someone else's run:** use the same scenario file, the same seed and a machine of the size the
scenario is written for (the kit adds a "ballast" pod on bigger machines so the size matches).

## Where to look

| Question | Where in the UI | Where in the Excel export |
|---|---|---|
| Is the machine full? | **The machine** (CPU bar) and **The maths** (% busy) | *Timeline*: "Machine % busy" |
| Which load is unhealthy, and when? | **Health of every load**; **Advanced → Health** strips | *Health*; *Summary* |
| What happened, in order? | **Recent events**; **Advanced → Diary** | *Events* (problems in red, warnings in yellow) |
| Who slowed down whom? | **Impact between loads** (after the run) | *Impact* |
| What did the scalers do and why? | **Advanced → Replicas and scaling decisions** | *Events*: `consumers_scaled`, `shop_scaled`, column "Why" |
| Were pods pushed out or waiting? | **The machine**: "Waiting outside the machine" | *Events*: `pod_preempted`, `pod_pending` |
| Planned vs what really happened? | **Advanced → Timeline** (outlined = planned, filled = real) | *Events*: "Planned minute" vs "Minute" |

---

## 1. `normal-day-short` — the quiet reference

**Story.** An ordinary office day: users follow office hours, a sensor stream is steady, one evening report runs.
Nothing competes. This is the run you compare everything else with.

**The loads** (machine: 4 cores)

| Load | What it does | CPU needed |
|---|---|---|
| `report` (batch) | 250 units × 0.18 CPU-s = 45 CPU-s, using 0.5 core | 0.5 core for **1.5 min**, at minute 18 |
| `telemetry` (stream) | 20 events/s × 8 ms | 20 × 0.008 = **0.16 core**, all the time |
| `shop-users` (users) | 15 users × office-day pattern, 20 ms per request, about 1 request per user per second | 0.1–0.35 core (busiest around minute 5 = 10:00) |

The 24-hour day is squeezed into 30 minutes: 1 minute = 48 day-minutes, starting at 06:00. So minute 5 is 10:00,
minute 18 is about 20:20 (the evening report).

**What happens.** Telemetry starts with 1 consumer pod, the scaler adds a second one in the first minute (each pod
booked 0.1 core but needed more). The shop's user count follows the day. At minute 18 the report runs for 1.5 min.

**What to look for**

- Every health card stays **green** the whole run.
- **The maths**: never more than about 1 of 4 cores busy (< 30 %).
- **Recent events**: "telemetry scaled 1 → 2 (or 3) consumer pods" near the start, nothing red.
- The report finishes at about **1.0× normal**.

**Expected result:** no unhealthy periods; report 1.0×; telemetry 2–3 consumers.

**Try this**

- Run it twice with seed 42 and compare the two runs: how different are two "identical" days on a real cluster?
- A forecaster or alarm you build must stay **silent** on this scenario. If it warns here, it is too nervous.

*Full version `normal-day`*: 2 h on 8 cores; report at minute 70 for 6 min; at most about 1.6 of 8 cores busy;
all green.

---

## 2. `midnight-pile-up-short` — batch jobs slow each other

**Story.** Finance, IT and the data team each scheduled "a nightly job". Nobody planned for them to run at the same
time.

**The loads** (machine: 4 cores; the clock is real time: the run covers 23:30 → 00:00)

| Load | What it does | CPU needed |
|---|---|---|
| `report` (batch, finance) | 1000 × 0.45 CPU-s = 450 CPU-s on 2 pods × 1.5 cores | **3 cores** for 2.5 min, every 10 min |
| `backup` (batch, IT) | 125 × 3.8 CPU-s = 475 CPU-s on 2 cores | **2 cores** for 4 min, every 10 min |
| `training` (batch, data, priority low) | 75 × 10 CPU-s = 750 CPU-s on 2.5 cores; `forbid`: never two runs at once | **2.5 cores** for 5 min, every 15 min |
| `telemetry` (stream) | 40 events/s × 8 ms, small bursts | 0.32 core |
| `shop-users` (users, priority high) | 40 users × night pattern (about 12 at night) | about 0.25 core |

**What happens.** At about minute 14 (23:44) all three jobs become due within half a minute of each other. They
want 3 + 2 + 2.5 = **7.5 cores** on a 4-core machine (plus the stream and the shop). Kubernetes shares the CPU in
proportion to what each pod booked, so every job gets only about half of what it wants: each run takes longer. A
second wave starts at minute 24.

**What to look for**

- **The maths** at minute 15: more than **100 % busy**, red text "more work than helpers".
- **Health of every load**: `report`, `backup` and `training` all turn red a few minutes after minute 14.
- **Recent events**: "… became unhealthy", later "… finished in … (1.7× normal)".
- **Impact between loads**: each job → the other two, "used the CPU while the machine was full".
- Excel *Timeline*: "Machine % busy" at 100 % during the pile-up.

**Expected result:** all three jobs unhealthy; each run about **1.7× normal**; shop stays healthy.

**Try this**

- `--without backup` (same seed): do report and training stay healthy? → that confirms backup as a cause.
- Change `first:` of `training` to `20m` (spread the schedule): does the pile-up disappear?
- Change `concurrency_policy` of `report` to `forbid` and compare.

*Full version `midnight-pile-up`*: 2 h on 4 cores, jobs at about 23:56; runs of 10 / 16 / 20 min become about
1.6× normal.

---

## 3. `sensor-storm-short` — scaling up hurts a neighbour

**Story.** Factory sensors send a storm of readings a few times an hour. The consumers scale up to keep up — on the
same machine as the web shop.

**The loads** (machine: 4 cores)

| Load | What it does | CPU needed |
|---|---|---|
| `telemetry` (stream) | 66.7 events/s × 8 ms; random bursts (about 4 in 30 min) of **6× more** for 1.5–3 min; 2–12 consumer pods; scaler `target-tracking` | 0.53 core normally, **3.2 cores** in a burst |
| `shop-users` (users, priority high) | 40 users, office day (starts 09:00) | about 0.8 core |
| `report` (batch) | every 7.5 min, 0.75 core for 2.5 min | 0.75 core |

**What happens.** When a burst arrives (first one at about minute 5.7 with seed 42), waiting events pile up, the
scaler adds consumer pods ("backlog wants 9 …"), and the new pods take CPU on the machine the shop also uses. The
extra pods stay for about 2 minutes after the burst (the scaler waits before removing pods).

**What to look for**

- **Advanced → Replicas and scaling decisions**: telemetry jumps up to **12** pods at a burst; hover a dot to see
  "backlog wanted …, cpu wanted …".
- **The machine**: many telemetry blocks appear during a burst.
- **Health of every load**: the shop turns red for about a minute **although its own traffic did not change**.
- **Advanced → Shop clue**: requests in flight go up while the request rate stays flat — the sign of a hidden
  neighbour.

**Expected result:** telemetry up to 12 consumers; shop unhealthy about 1 min; report about 1.0×.

**Try this**

- Change the telemetry scaler to `{name: none}` (fixed 2 pods): is the shop now fine? Does telemetry get unhealthy
  instead? That is the trade-off of autoscaling on a shared machine.
- Try the `step` scaler example: `python -m loadkit run students/examples/sensor-storm-step.yaml`.

*Full version `sensor-storm`*: 2 h on 6 cores, about 4 bursts of 3–6 min; shop unhealthy about 3 min.

---

## 4. `flash-sale-short` — two services scale up at the same time

**Story.** Marketing starts a flash sale; a few minutes later order events flood the stream.

**The loads** (machine: 4 cores)

| Load | What it does | CPU needed |
|---|---|---|
| `shop-users` (users, priority high) | 27 users (office day); **sale: 4× users** from minute 10 for 3.75 min; scaler `concurrency` (4 requests in flight per pod), 2–6 pods | 0.6 core normally, **about 2.2 cores** in the sale |
| `orders` (stream) | 40 events/s × 10 ms; **burst 5×** at minute 11 for 2 min; 1–8 consumers | 0.4 core normally, **2 cores** in the burst |
| `report` (batch) | at minutes 5 and 20, 1 core for 2.5 min | 1 core |

**What happens.** From minute 10 the shop needs about 2.2 cores, from minute 11 the orders stream needs about
2 cores more: together more than the 4-core machine. Both scalers add pods at the same time.

**What to look for**

- **Replicas**: shop goes up to **6** pods, orders up to **8** consumers, around minute 10–13.
- **Health**: the shop turns red for about 2 min during the sale.
- **Impact between loads**: which load used the CPU while the shop was slow?

**Expected result:** shop unhealthy about 2 min; shop 6 pods, orders 8 consumers; report 1.0×.

**Try this**

- `--without orders`: is the sale alone enough to make the shop slow?
- Raise the shop's `target_in_flight` from 4 to 10: fewer shop pods — better or worse?

*Full version `flash-sale`*: 2 h on 6 cores, sale at minute 40 for 15 min; shop unhealthy about 6 min.

---

## 5. `overlapping-teams-short` — sometimes they meet, sometimes not

**Story.** Three teams run regular jobs on their own schedules. Most of the time they miss each other; sometimes
they meet. Slowdowns come and go — hard to spot.

**The loads** (machine: 4 cores; the teams expect runs within **1.3×** normal)

| Load | Schedule | Each run |
|---|---|---|
| `report` | every 5 min | 2.25 cores for 2.3 min |
| `etl` | every 7.5 min | 2.25 cores for 2.9 min |
| `backup` | every 10 min | 2.25 cores for 3.5 min |
| `shop-users` | 34 users, office day | about 0.7 core |

**What happens.** One job alone fits (2.25 of 4 cores). **Two jobs at once** need 4.5 cores — the machine is full
and both slow down. With seed 42 the runs are due at: report 0.4, 5.5, 10.4, 15.4, 20.4, 25.6 · etl 2.0, 9.3, 16.8,
24.6 · backup 3.7, 13.7, 23.4 (minutes).

**What to look for**

- **Advanced → Timeline**: filled bars longer than the outlined (planned) bars only where two bars overlap.
- **Health**: red stripes that come and go, on different jobs.
- The same job is fine in one run and slow in the next — compare the "finished in …" events.

**Expected result:** 13 runs; about 7 unhealthy periods spread over the three jobs; runs up to about 1.6× normal.

**Try this**

- A detector should flag the slow runs **and** name the job that overlapped — check its answers against the
  *Impact* sheet.
- Change the seed: the overlaps move. Does your detector still work?

*Full version `overlapping-teams`*: 3 h on 3.5 cores, jobs every 30 / 45 / 60 min; a few runs reach about 1.4×.

---

## 6. `everything-short` — a mixed, realistic day

**Story.** Office traffic, a sensor stream with bursts, three teams' batch jobs, and a lunchtime peak — all at once,
mildly.

**The loads** (machine: 4 cores; preemption on)

| Load | What it does | CPU needed |
|---|---|---|
| `report` | 2 pods × 1 core, 2.5 min, at minutes 6.5, 17.6, 28.4 | 2 cores |
| `backup` | 1.25 cores, 2.2 min, at minutes 7.3 and 22.3 | 1.25 cores |
| `training` (priority low) | 1.5 cores, 2.5 min, at minute 8.3 | 1.5 cores |
| `telemetry` (stream) | 64 events/s × 8 ms, random bursts 4× | 0.5 core, about 2 cores in a burst |
| `shop-users` (priority high) | 48 users, office day, **2× peak** at minute 18 for 2.5 min | about 1 core, 2 in the peak |

**What happens.** Around minutes 6.5–9 report, backup and training run together with a telemetry burst: the machine
is overloaded. The high-priority shop scales up and its new pods push out telemetry consumer pods (normal priority)
to get room.

**What to look for**

- Several loads unhealthy at different times, for different reasons.
- **Recent events**: "telemetry pod pushed out by shop-users (priority high)" around minute 8.
- **Impact between loads**: more than one cause per unhealthy period — read the shares.

**Expected result:** backup about 2× normal, training about 1.4×; shop briefly unhealthy around minute 8; 2 telemetry
pods pushed out by the shop.

**Try this:** use the *Impact* sheet to rank the causes, then confirm the top one with `--without`.

*Full version `everything`*: 2 h on 5 cores, same pattern spread over the day.

---

## 7. `backup-snowball-short` — a job snowballs on itself

**Story.** A backup every 3.75 min that normally takes 3 min has always fitted. Today a training run made it a bit
slower — and nobody changed anything.

**The loads** (machine: **3 cores**)

| Load | What it does | CPU needed |
|---|---|---|
| `backup` | 180 × 1 CPU-s on 1 core, every 3.75 min; `allow`: a new run starts even if the last one is still working | 1 core for **3 min** |
| `training` | at minute 5: 105 × 10 CPU-s on 2 cores | 2 cores for 8.75 min |
| `telemetry` | 150 events/s × 10 ms, fixed 2 consumers | **1.5 cores**, all the time |
| `shop-users` | 25 users, 1 pod | about 0.5 core |

**The maths.** Without training: 1 (backup) + 1.5 + 0.5 = 3 cores — the machine is exactly full, the backup just
fits. When training adds 2 cores, the backup gets less CPU, a run is not finished when the next one starts, two
runs share the backup's CPU, both get slower, a third one starts …

**What to look for**

- **Recent events**: "backup run 3 started while 1 earlier run(s) were still working", later "… 2 earlier runs".
- **Advanced → Timeline**: backup bars getting longer and overlapping, **even after training has finished**.
- **Impact between loads**: `backup (own other runs)` → `backup` — the job is slowed by itself.

**Expected result:** up to **3 backup runs at once**; the slowest about 3× normal; telemetry and shop also
unhealthy.

**Try this**

- Set the backup's `concurrency_policy: forbid` (skip a run if the previous one is still working): no snowball, but
  skipped backups — which is worse for the business?
- `--without training`: does the snowball start at all?

*Full version `backup-snowball`*: 2 h, backup every 15 min (12 min normal), training at minute 20; same pile-up.

---

## 8. `idle-consumers-short` — more pods do not help

**Story.** The consumer group was given more pods, but the topic only has 2 partitions. Extra pods sit idle while
the waiting events keep piling up.

**The loads** (machine: 4 cores)

| Load | What it does | CPU needed |
|---|---|---|
| `clicks` (stream) | 20 events/s × 20 ms; **burst 8×** (160 events/s) at minute 5 for 5 min; **2 partitions**; 1–6 consumers | 0.4 core normally, **3.2 cores** in the burst |

**The maths.** Each partition is read by one consumer, each consumer handles 1 event at a time: at most
2 × (1 ÷ 0.02 s) = **100 events/s** can be read, whatever the number of pods. In the burst 160 arrive: 60 more per
second than can be read → after 5 minutes about **18,000** events wait. After the burst it takes about 4 minutes to
catch up.

**What to look for**

- **Replicas**: the scaler goes to **6** consumers ("backlog wants …").
- **Recent events**: "clicks: 4 consumer pod(s) idle — only 2 partitions".
- **Health**: clicks red (oldest event waits far more than 30 s); the machine is **not** full (**The maths**).
- **Impact between loads**: no other load is to blame — the note says "more consumers than partitions".

**Expected result:** clicks unhealthy about 7 min; up to 6 consumers, 4 idle.

**Try this:** set `partitions: 6` and run again: the same burst is handled. This is a configuration problem, not
a capacity problem — a good test for a cause finder.

*Full version `idle-consumers`*: 1 h on 8 cores, 4 partitions, up to 9 consumers.

---

## 9. `priority-squeeze-short` — someone else's priority

**Story.** The nightly ETL has run fine for months. Tonight the high-priority payment stream had a burst — and the ETL
took almost three times as long. Nothing changed in the ETL itself.

**The loads** (machine: 4 cores; **preemption on**)

| Load | What it does | CPU needed |
|---|---|---|
| `nightly-etl` (batch, priority **low**) | 450 × 4 CPU-s on 4 pods × 0.75 core, due at minute 1.2 | 3 cores for **10 min** |
| `payments` (stream, priority **high**) | 33 events/s × 10 ms; **bursts 8×** at minutes 5 and 14 for 2 min; 1–8 consumers | 0.33 core normally, about 2.7 cores in a burst |

**What happens.** At each burst the scaler adds payments consumers. There is no room left on the machine for their
booked CPU, so Kubernetes **pushes out low-priority ETL pods** to make room (preemption). The ETL pods wait outside
the machine and lose the work they had done.

**What to look for**

- **The machine**: ETL blocks move to **"Waiting outside the machine"** right after the burst.
- **Recent events**: "nightly-etl pod pushed out by payments (priority high)".
- **Health**: ETL red long after the burst is over.
- **Impact between loads**: `payments → nightly-etl`, "pushed out its pods 6×".

**Expected result:** 6 ETL pods pushed out; ETL about **2.7× normal**; payments stays healthy.

**Try this**

- Set `preemption: off`: payments pods now wait instead — who suffers now?
- Give `nightly-etl` `checkpoint: true` (keeps finished work when a pod is pushed out): how much shorter is it?

*Full version `priority-squeeze`*: 2 h on 6 cores, ETL 40 min normal, bursts at minutes 20 and 55; ETL about 2.5×.

---

## After every run: a 5-point check

1. **Did it run as planned?** *Events* sheet: every `job_due` has a `job_started`; "Planned minute" ≈ "Minute".
2. **Is "normal" normal?** Batch runs that ran alone should be about 1.0× normal. If they are clearly slower, the
   machine is smaller or busier than the scenario expects (the kit warns at the start).
3. **Did the expected effect appear?** Compare with "Expected result" above.
4. **Is the cause plausible?** *Impact* sheet — then confirm the main cause with `--without`.
5. **Write down** scenario, seed, machine size and the run folder name, so anyone can reproduce it.
