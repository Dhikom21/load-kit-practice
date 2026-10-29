# The pipeline (CI/CD): what runs, when, and how to read it

Every time you push code, GitHub runs the kit's checks on a fresh computer and reports ✓ or ✗. A change can only be
merged into `main` through a pull request whose checks are green and which someone has approved.

## What runs when

| Workflow (file) | Job | When | What it does | Time |
|---|---|---|---|---|
| CI (`.github/workflows/ci.yml`) | `check` | every push and pull request | code style (`ruff`), all scenario files valid, course rules (`ci/policy.py`: no secrets, no memory scenarios), UI JavaScript parses | < 1 min |
| | `test` | after `check` | all unit tests (no cluster), coverage table in the run summary | 1–2 min |
| | `simulate` | pull requests, "Run workflow" button | runs every short scenario on the **simulated cluster** for this change **and** for `main`, compares them, posts the result as a comment on the pull request, keeps the Excel files | about 1 min |
| Worker image (`image.yml`) | `build` | pushes that change `worker/` | builds the image, checks it starts; on `main` publishes it to `ghcr.io/<org>/loadkit-worker` | 2–3 min |
| Nightly real run (`nightly-kind.yml`) | `kind-run` | every night 02:00 UTC, or "Run workflow" | creates a small **real** Kubernetes cluster (kind) on the runner, runs `tools/check-batch.yaml` (or the scenario you choose), keeps the run folder | about 5 min (short scenario: about 40 min) |

## Reading the results

- **On a pull request:** the checks list at the bottom shows `check`, `test`, `simulate` with ✓/✗. Click
  **Details** to see the log of a step.
- **The simulation comment** ("Simulated scenario results") lists every load whose result changed compared with
  `main`: unhealthy minutes, worst batch run (× normal), peak pods, and **worse / better / changed**. It is updated
  on every new push to the pull request. The simulation is a model — it shows the *direction* of a change; the
  nightly kind run and your own runs give real numbers.
- **Files:** at the bottom of a workflow run (Actions tab → the run) under **Artifacts**: `simulation-…` (Excel per
  scenario, numbers as JSON) and `kind-run-…` (the full run folder of the nightly real run).
- **Run summary:** each run's page shows the coverage table, the simulation report and the real run's README.

## Run the same checks on your computer first

```bash
make ci-local            # ruff + scenario check + course rules + unit tests + simulation of all short scenarios
python ci/simulate.py run --out ci-out/mine                          # just the simulation
python ci/simulate.py compare ci-out/main ci-out/mine --md report.md # compare with an earlier simulation
```

## Rules the pipeline enforces

- Changes to the core, the worker, the pipeline and `loadkit.yaml` need the **teacher's** approval
  (`.github/CODEOWNERS`). Plugins, scenarios, tests and docs only need a group member.
- No kubeconfig files, private keys or cluster tokens in the repository (`ci/policy.py`). Kubeconfigs are passed at
  run time and, for the pipeline, stored as GitHub **secrets**.
- In the students' copy, memory scenarios stay switched off.

---

## For the teacher: one-time setup on GitHub

1. **Organization.** Apply for GitHub Education as a teacher (or ask the course responsible) and create an
   organization for the course, e.g. `ltu-d0020e-2026`. In **Settings → Billing**, keep the spending limit for
   Actions at **$0** (jobs stop instead of costing money if the free minutes run out).
2. **Template repository.** Make the students' copy and push it as a new private repository in the organization:
   ```bash
   python -m loadkit student-copy ../load-kit-students --owner @YOUR-GITHUB-NAME
   cd ../load-kit-students && git init && git add . && git commit -m "Load Scenario Kit (student copy)"
   git branch -M main && git remote add origin git@github.com:ORG/load-kit-template.git && git push -u origin main
   ```
   In the repository's **Settings → General**, tick **Template repository**.
3. **Group repositories.** In GitHub Classroom, create a **group assignment** from `load-kit-template`. Each group
   gets its own private repository when its members accept the assignment.
4. **Protect `main` in every group repository** — easiest once for all, at organization level:
   **Organization settings → Repository → Rulesets → New branch ruleset**, target all repositories, branch `main`:
   - Require a pull request before merging, with **1 approval** and **review from Code Owners**;
   - Require status checks to pass: `check`, `test`, `simulate`;
   - Block force pushes.
5. **Actions settings** (organization → Actions → General): allow GitHub Actions and the actions used here
   (`actions/*`, `helm/kind-action`); workflow permissions: **read** by default (the workflows ask for more only where
   needed).
6. **Your overview:** `is:pr is:open org:ORG` in GitHub's search, review requests at github.com/pulls/review-requested,
   and later the course dashboard.

The real-cluster stage on Rancher (later) needs a self-hosted runner and each group's kubeconfig stored as a secret —
it is not part of this setup yet.
