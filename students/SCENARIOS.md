# Your three scenarios

*(The big picture is in [PROJECT.md](PROJECT.md); your four builds are in [TASK.md](TASK.md). This file is the map of the scenarios you work on — the whole experiment is these three, in this order.)*

Each scenario adds **exactly one new idea** on top of the last. No more.

**The machine.** Every scenario lives on **one machine** (4 cores, 8 GiB). All the loads in a scenario sit on it, so what one load does to the CPU shows up in the others. That is the whole game — and the reason a load can be red even when nothing about it changed.

## Step 1 — `idle-consumers-short` (one load)

**Story.** The consumer group was given more pods, but the topic only has 2 partitions. Extra pods sit idle while the backlog keeps growing.

**The one new idea:** *lanes.* Events are spread over 2 lanes; more workers than lanes does not add speed — the extra consumers sit idle.

**Look for:**
- replicas go above 2, but the backlog does not drop faster (the diary says `consumers_idle`)
- the processed rate is capped at what 2 consumers can do

**Run it:** `python -m loadkit run idle-consumers-short --seed 42`
The seed does not matter here — the script is fixed, so every run is the same run. You are learning the mechanism, not the dice.

## Step 2 — `priority-squeeze-short` (two loads)

**Story.** The nightly ETL has run fine for months. Tonight the high-priority payment stream had a burst — and the ETL took three times as long.

**The one new idea:** *one CPU, two owners.* When the machine is full, priority decides who yields: the high-priority stream scales up and **preempts** (evicts) the low-priority batch pods — and without a checkpoint, their progress is lost.

**Look for:**
- `consumers_scaled` followed by `pod_preempted` on the ETL pods
- the ETL loses progress at each preemption and its run time explodes
- nothing changed in the ETL job itself

Still a fixed script — the payment bursts come at 5:00 and 14:00 every time. Same run, every run.

## Step 3 — `sensor-storm-short` (three loads)

**Story.** The factory's sensors send a storm of readings a few times an hour. The consumers scale up to keep up — on the same machine as the shop.

**The one new idea:** *the full picture.* All three kinds of load (stream + users + batch) at once — and the indirect effect: **the shop slows during and after the bursts although its own traffic did not change.**

**Look for:**
- replicas jump at each burst (backlog wins over CPU in the scaler decision)
- the extra consumers stay for minutes after the burst (stabilization window)
- the shop slows although its traffic did not change

**This is the first scenario where the seed matters.** Same seed = same day (same burst times, same job offsets); a different seed = a different day — the sizes never change, only when things happen. In class we use **seed 42** so every group sees the same day.

## Three rules that apply to all of them

1. **Read the "look for" list before you run; check it after.** That list is the scenario's answer key.
2. **When a load goes red, go to the diary.** `runs/<run-id>/diary.jsonl` (or the Diary panel) records every real event **with its cause**. Find the cause before you guess one.
3. **The fight is over CPU.** You will see memory numbers in the scenario files — they are part of the real config, but in your version of the course the memory-pressure stories are switched off.

Every scenario also has a **full-length version** (1–3 h) — same story, a longer day. You develop on the short versions; the final test is on a full-length version (see TASK.md).

That is the whole set. (The other scenario files live in a subfolder and are not part of your experiment — the picker and the commands will only ever offer you these three.)
