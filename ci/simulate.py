"""CI helper: run the short scenarios on the test-only simulated cluster and compare two versions of the kit.

    python ci/simulate.py run --out ci-out/head                      # this checkout
    python ci/simulate.py run --repo base --out ci-out/base          # another checkout (e.g. main)
    python ci/simulate.py compare ci-out/base ci-out/head --md ci-out/report.md

The simulated cluster (tests/fake_cluster.py) is deterministic: the same code and seed always give the same numbers,
so any difference between main and a pull request comes from the code change. It is a model, not a real machine;
real numbers come from the nightly kind run (.github/workflows/nightly-kind.yml).
"""
import argparse
import json
import pathlib
import sys
import time

MARKER = "<!-- loadkit-simulation-report -->"


def _summary(engine):
    """Per scenario: for every load, unhealthy minutes, worst batch run (x normal), peak pods; plus event counts."""
    loads = {}
    for ld in engine.sc["loads"]:
        loads[ld["name"]] = {"type": ld["type"], "unhealthy_min": 0.0, "worst_ratio": None,
                             "peak_pods": engine.peak_replicas.get(ld["name"]) or None}
    for p in engine.periods:
        loads[p["load"]]["unhealthy_min"] += (p.get("end_t", p["start_t"]) - p["start_t"]) / 60.0
    counts = {}
    for e in engine.diary.events:
        if e["event"] == "job_ended" and e.get("ratio") is not None:
            r = loads[e["load"]]
            r["worst_ratio"] = max(r["worst_ratio"] or 0, e["ratio"])
        if e["origin"] == "reactive" and e["event"] in ("pod_preempted", "pod_evicted", "oom_killed", "run_overlapped",
                                                       "run_skipped", "job_timed_out", "consumers_scaled", "shop_scaled"):
            counts[e["event"]] = counts.get(e["event"], 0) + 1
    for r in loads.values():
        r["unhealthy_min"] = round(r["unhealthy_min"], 1)
    return {"loads": loads, "events": counts}


def cmd_run(args):
    repo = pathlib.Path(args.repo).resolve()
    out = pathlib.Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(repo))
    try:
        from tests.helpers import run, setup          # the repo's own test-only fake cluster + settings
        from loadkit import scenario
    except ImportError as e:                          # e.g. an old main branch without the simulator
        (out / "summary.json").write_text(json.dumps({"error": f"cannot simulate this version: {e}"}))
        print(f"cannot simulate {repo}: {e}")
        return 0
    s, pl = setup()
    names = args.scenarios.split(",") if args.scenarios else \
        [n for n in scenario.list_allowed(pl, s) if n.endswith("-short")]
    result = {"seed": args.seed, "scenarios": {}, "errors": {}}
    for n in names:
        t0 = time.time()
        try:
            e = run(n, seed=args.seed, record=True, out_dir=out / "runs")
        except Exception as ex:  # noqa: BLE001 — a crash in a student's plugin must show up in the report, not hide it
            result["errors"][n] = f"{type(ex).__name__}: {ex}"
            print(f"{n:28} ERROR {ex}")
            continue
        result["scenarios"][n] = _summary(e)
        try:
            from loadkit import export
            name, data = export.build(e.dir)
            (out / "excel").mkdir(exist_ok=True)
            (out / "excel" / f"{n}.xlsx").write_bytes(data)
        except Exception as ex:  # noqa: BLE001
            print(f"  (no Excel export: {ex})")
        print(f"{n:28} ok in {time.time() - t0:4.1f} s")
    (out / "summary.json").write_text(json.dumps(result, indent=1))
    return 1 if result["errors"] else 0


def _fmt(v, kind):
    if v is None:
        return "–"
    return f"{v:g} min" if kind == "min" else f"{v:g}×" if kind == "ratio" else f"{v:g}"


TOLERANCE = {"unhealthy_min": 0.25, "worst_ratio": 0.05, "peak_pods": 0}   # smaller differences count as "same"


def _change(a, b, worse_if_higher=True, tol=0.0):
    if a is not None and b is not None and abs(b - a) <= tol:
        return "same"
    if a is None or b is None or a == b:
        return "same" if a == b else "new" if a is None else "gone"
    up = b > a
    return "worse" if up == worse_if_higher else "better"


def cmd_compare(args):
    base = json.loads((pathlib.Path(args.base) / "summary.json").read_text()) \
        if (pathlib.Path(args.base) / "summary.json").exists() else {"error": "no baseline"}
    head = json.loads((pathlib.Path(args.head) / "summary.json").read_text())
    rows, changed, worse = [], [], 0
    for n, hs in sorted(head.get("scenarios", {}).items()):
        bs = base.get("scenarios", {}).get(n, {"loads": {}})
        for ld, h in hs["loads"].items():
            b = bs["loads"].get(ld, {})
            cells = []
            ch = []
            for key, kind in (("unhealthy_min", "min"), ("worst_ratio", "ratio"), ("peak_pods", "pods")):
                bv, hv = b.get(key), h.get(key)
                c = _change(bv, hv, worse_if_higher=(key != "peak_pods"), tol=TOLERANCE[key]) if b else "new"
                if key == "peak_pods" and c in ("worse", "better"):
                    c = "more" if c == "worse" else "fewer"   # more pods is not automatically worse
                cells.append(f"{_fmt(bv, kind)} → {_fmt(hv, kind)}" if c not in ("same",) else _fmt(hv, kind))
                ch.append(c)
            verdict = "worse" if "worse" in ch else "better" if "better" in ch else \
                "changed" if any(c not in ("same",) for c in ch) else "same"
            worse += verdict == "worse"
            row = f"| `{n}` | {ld} | {cells[0]} | {cells[1]} | {cells[2]} | **{verdict}** |"
            rows.append(row)
            if verdict != "same":
                changed.append(row)
    head_err = head.get("errors", {})
    lines = [MARKER, "### Simulated scenario results",
             f"Short scenarios on the **test-only simulated cluster**, seed {head.get('seed', 42)}, "
             f"compared with `{args.base_label}`. The simulation is a model: use it to see the *direction* of a "
             "change; real numbers come from the nightly kind run.", ""]
    if head_err:
        lines += ["**Scenarios that crashed with this change:**", ""] + \
                 [f"- `{n}`: {msg}" for n, msg in head_err.items()] + [""]
    if "error" in base:
        lines += [f"_No comparison: {base['error']}._", ""]
    head_line = "| Scenario | Load | Unhealthy (min) | Worst batch run (× normal) | Peak pods | Result |"
    sep = "|---|---|---|---|---|---|"
    if changed:
        lines += [f"**{len(changed)} load(s) changed, {worse} got worse.**", "", head_line, sep] + changed + [""]
    else:
        lines += ["**No change** in any short scenario compared with the base.", ""]
    lines += ["<details><summary>All scenarios and loads</summary>", "", head_line, sep] + rows + ["", "</details>",
              "", "Excel files for every scenario are in the workflow run's artifacts (`simulation-…`)."]
    md = "\n".join(lines) + "\n"
    if args.md:
        pathlib.Path(args.md).write_text(md, encoding="utf-8")
    print(md)
    json.dump({"changed": len(changed), "worse": worse, "crashed": list(head_err)},
              open(pathlib.Path(args.head) / "compare.json", "w"))
    return 1 if (head_err or (args.fail_on_worse and worse)) else 0


def main():
    ap = argparse.ArgumentParser(prog="ci/simulate.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--repo", default=".")
    r.add_argument("--out", required=True)
    r.add_argument("--scenarios", help="comma-separated; default: all short scenarios")
    r.add_argument("--seed", type=int, default=42)
    c = sub.add_parser("compare")
    c.add_argument("base")
    c.add_argument("head")
    c.add_argument("--md")
    c.add_argument("--base-label", default="main")
    c.add_argument("--fail-on-worse", action="store_true", help="exit 1 if any load got worse")
    args = ap.parse_args()
    sys.exit(cmd_run(args) if args.cmd == "run" else cmd_compare(args))


if __name__ == "__main__":
    main()
