# Running all 5 groups on one shared cluster

One run of `deploy/provision-groups.sh` gives every group its own **machine**, its own
**key**, and its own **2-line config**. After that, the groups just work — in parallel,
with no further coordination from you.

## The picture, in one breath

Think of the cluster as a building with a front desk (the API server).
Each group is a tenant with:

- **their own machine** — one node, labelled `role=experiment-N`. The kit is pinned to it,
  and it *refuses to start* on a node where another group is running.
- **their own key card** — a kubeconfig whose token only opens the doors the kit needs
  (no `delete nodes`, no `create pods` anywhere, no admin).
- **a 2-line config** — `group: gN` and `kubernetes.node: role=experiment-N` in `loadkit.yaml`.

Everything the kit creates (namespaces, pods, Redis) is named and labelled with the group,
and the run's Redis gets a fresh random password each run — so even inside the cluster the
groups never touch each other's data.

## What you need first

1. A cluster with **at least 5 worker nodes** (for 5 groups). On kind, create 5 workers:

   ```yaml
   # kind.yaml — extend your local cluster to 5 workers
   kind: Cluster
   apiVersion: kind.x-k8s.io/v1alpha4
   name: loadkit
   nodes:
     - role: control-plane
     - role: worker
     - role: worker
     - role: worker
     - role: worker
     - role: worker
   ```

2. **metrics-server** installed (the kit checks for it; on kind: `make kind-metrics`).
3. Your **admin kubeconfig** active on your laptop (`kubectl get nodes` works).
4. **python3 + pyyaml** on your laptop (the script builds the key files with them).
5. The **worker image** published where the nodes can pull it. CI does this automatically:
   `image.yml` pushes `ghcr.io/<owner>/loadkit-worker:latest` from `main` whenever `worker/`
   changes. For a private repo the nodes also need an `imagePullSecret`.

## Run it

From the repo root, with your admin kubeconfig active:

```bash
NODES="loadkit-worker loadkit-worker-2 loadkit-worker-3 loadkit-worker-4 loadkit-worker-5" \
  ./deploy/provision-groups.sh
```

(Replace the node names with yours — `kubectl get nodes -o name` lists them. The *first*
name becomes `experiment-1`, the second `experiment-2`, and so on.)

What it does, in order:

| step | what happens |
|---|---|
| 1 | labels the 5 nodes `role=experiment-1` … `role=experiment-5` |
| 2 | installs one `ClusterRole loadkit-student` — the exact list of API calls the kit makes, nothing more |
| 3 | for each group: a ServiceAccount `gN` in namespace `loadkit-groups`, plus a ClusterRoleBinding |
| 4 | mints a **30-day token** for `gN` and writes `deploy/groups/gN.kubeconfig` (file mode 600) |
| 5 | writes `deploy/groups/gN.loadkit.yaml.snippet` — the group's 2-line config |
| 6 | checks every key: it can list nodes, but **cannot** delete nodes or create pods anywhere |

The script is **idempotent** — re-running it just re-labels, re-applies, and re-issues keys.
30 days is the longest token the API server will give, so **re-run the script when tokens
expire**; nothing else needs to change.

## Handing keys to the groups

Give each group **exactly one** file: `deploy/groups/gN.kubeconfig`, plus that group's
snippet. Never your admin kubeconfig, and never another group's file.
(`*kubeconfig*` is in `.gitignore`, so the keys can't be committed by accident — but they
are still secrets: send them over a private channel.)

**What a student does** (this is the whole ceremony):

1. Edit their `loadkit.yaml` with the 2 lines from the snippet:

   ```yaml
   group: g3
   kubernetes:
     node: role=experiment-3
     image: ghcr.io/dhikom/loadkit-worker:latest
   ```

2. Run the kit with their key:

   ```bash
   python -m loadkit run normal-day-short --kubeconfig /path/to/g3.kubeconfig
   python -m loadkit server    --kubeconfig /path/to/g3.kubeconfig
   python -m loadkit reset     --kubeconfig /path/to/g3.kubeconfig   # only their own group's stuff
   ```

That's it. Each group's kit lands only on its own node, and all 5 groups can run at the
same time.

## Why this is safe (and where the line is)

- **Node pinning is the main wall.** Each group's kit only matches its own label, and the
  runner's preflight refuses any node where another group's pods are running (it even names
  the group that's there).
- **RBAC is a guardrail, not a wall.** The keys allow only what the kit calls. But they are
  cluster-scoped read permissions (a student *could* `kubectl get pods` in another group's
  namespaces if they bothered) — the design assumes students use **the kit as their only
  interface**, which is exactly what the course requires.
- **Data isolation is real:** per-run Redis with a random password, NetworkPolicy that only
  lets the group's own namespaces reach its Redis, and every object named/labelled `gN-…`.
- **Cleanup is scoped:** `loadkit reset` with the group's key deletes only that group's
  namespaces and priority classes.

Honest caveat: the labels are a *soft* boundary — other (non-kit) workloads could still be
scheduled onto a group's node. If you want a hard boundary, add a taint per node and a
matching toleration in the kit's runner (a core change, so it goes through a normal PR).

## If something goes wrong

| symptom | fix |
|---|---|
| student's run says `no free node` | their `kubernetes.node` label doesn't match, or their node is labelled to another group — check `kubectl get nodes --show-labels` |
| student's key stopped working | token expired (30-day max) — re-run `provision-groups.sh` and send them the fresh `gN.kubeconfig` |
| a group left garbage behind | `python -m loadkit reset --kubeconfig deploy/groups/gN.kubeconfig` (teacher or the group, same effect) |
| you want to tear down the whole setup | `python -m loadkit reset --kubeconfig <admin> --all-groups`, then `kubectl delete clusterrolebindings 'loadkit-g*'`, `kubectl -n loadkit-groups delete sa 'g?' -l loadkit/group`, `kubectl delete clusterrole loadkit-student`, `kubectl delete ns loadkit-groups` |
