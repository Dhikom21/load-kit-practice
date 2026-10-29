# loadkit-worker

The pods of the Load Scenario Kit. One image; the role is the first argument:

| Role | What it does |
|---|---|
| `batch` | does `--units` work units, each costing `--cost` CPU-seconds, using up to `--intensity` cores; reports progress; exits when done |
| `produce` | sends events to the stream's partitions at the rate the engine sets (`ctl:<load>:rate`) |
| `consume` | joins the consumer group, gets its share of the partitions (idle if there are more consumers than partitions) and spends `--work-ms` CPU per event |
| `shop` | small HTTP service; each request costs `--cost-ms` CPU; reports requests in flight |
| `users` | closed-loop users calling the shop; reports request rate, p95 response time and errors |
| `calibrate` | prints how fast this node's CPU is |

Work is measured in CPU time, so a unit is the same amount of work on any machine; on a busy machine it simply
takes longer. Memory options (`--mem-base`, `--mem-peak`, `--mem-shape`, `--leak`) allocate real memory.

Pods only talk to the kit's Redis (`REDIS_URL`). Build: `make image` (from the repo root).
