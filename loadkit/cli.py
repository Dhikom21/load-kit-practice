"""Command line:  python -m loadkit <command>

  list                              built-in scenarios
  validate [NAME ...]               validate scenarios (all if none given)
  timeline NAME [--seed N]          print the planned timeline (no run)
  run NAME [--seed N] [--kubeconfig PATH] [--without LOAD]   headless run on the cluster -> runs/<run-id>/
  analyze RUN [--baseline RUN]      (re)compute impact; counterfactual when a baseline run is given
  compare RUN_A RUN_B               side-by-side summary
  runners [--kubeconfig PATH]       which runners are available here
  student-copy DEST                 the kit for students, without the memory scenarios
  reset [--kubeconfig PATH]         delete what THIS group's kit created on the cluster (--all-groups: everything)
  snapshot RUN [--out FILE]         one self-contained HTML page showing a finished run
  serve [--port N]                  start the web UI + API
"""
import argparse
import json
import pathlib
import sys

from . import analyze, generator, registry, scenario, settings as settings_mod


def _setup():
    s = settings_mod.load()
    return s, registry.load_enabled(s)


def _run_path(s, name):
    p = pathlib.Path(name)
    if p.exists():
        return p
    p = settings_mod.runs_dir(s) / name
    if p.exists():
        return p
    sys.exit(f"run '{name}' not found")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="loadkit", description="Load Scenario Kit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    v = sub.add_parser("validate")
    v.add_argument("names", nargs="*")
    t = sub.add_parser("timeline")
    t.add_argument("name")
    t.add_argument("--seed", type=int)
    r = sub.add_parser("run")
    r.add_argument("name")
    r.add_argument("--seed", type=int)
    r.add_argument("--runner", default="kubernetes")
    r.add_argument("--kubeconfig", help="path to your kubeconfig (never stored)")
    r.add_argument("--without", action="append", default=[], help="remove a load (counterfactual run)")
    r.add_argument("--quiet", action="store_true")
    a = sub.add_parser("analyze")
    a.add_argument("run")
    a.add_argument("--baseline")
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    rn = sub.add_parser("runners")
    rn.add_argument("--kubeconfig")
    sc_ = sub.add_parser("student-copy")
    sc_.add_argument("dest")
    sc_.add_argument("--owner", help="your GitHub username, written into .github/CODEOWNERS (e.g. @dhikom)")
    rs = sub.add_parser("reset")
    rs.add_argument("--kubeconfig")
    rs.add_argument("--all-groups", action="store_true", help="admin: delete the kit objects of EVERY group")
    sn = sub.add_parser("snapshot")
    sn.add_argument("run")
    sn.add_argument("--out")
    sv = sub.add_parser("serve")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    sv.add_argument("--kubeconfig")
    args = ap.parse_args(argv)
    s, plugins = _setup()

    if args.cmd == "list":
        for n in scenario.list_allowed(plugins, s):
            sc = scenario.load_file(n, plugins)
            print(f"{n:20} {sc['description'].strip()[:90]}")
    elif args.cmd == "validate":
        bad = 0
        for n in args.names or scenario.list_builtin():
            try:
                sc = scenario.load_file(n, plugins)
                print(f"ok    {n:20} {len(sc['loads'])} loads, {sc['duration'] / 60:.0f} min")
            except scenario.ScenarioError as e:
                bad += 1
                print(f"FAIL  {n}")
                for err in e.errors:
                    print(f"        {err}")
        sys.exit(1 if bad else 0)
    elif args.cmd == "timeline":
        sc = scenario.load_file(args.name, plugins)
        print(generator.dumps(generator.make_timeline(sc, plugins, args.seed)))
    elif args.cmd == "run":
        from .engine import Engine
        if args.runner not in plugins["runners"]:
            sys.exit(f"runner '{args.runner}' is not enabled in loadkit.yaml")
        ok, why = plugins["runners"][args.runner].available(s, args.kubeconfig)
        if not ok:
            sys.exit(f"cannot run: {why}")
        sc = scenario.load_file(args.name, plugins)
        blocked = scenario.blocked_flags(sc, s)
        if blocked:
            sys.exit(f"cannot run: scenario '{sc['name']}' needs {', '.join(blocked)}, which is not allowed in "
                     f"loadkit.yaml (allow_flags)")
        if args.without:
            sc["loads"] = [ld for ld in sc["loads"] if ld["name"] not in args.without]
            sc["_hash"] = f"{sc['_hash']}-without-{'-'.join(args.without)}"
        eng = Engine(sc, s, plugins, seed=args.seed, runner=args.runner,
                     note=f"without {', '.join(args.without)}" if args.without else "",
                     runner_options={"kubeconfig": args.kubeconfig})
        eng.run()
        if not args.quiet:
            print((eng.dir / "README.txt").read_text(encoding="utf-8"))
        print(eng.dir)
    elif args.cmd == "analyze":
        run = _run_path(s, args.run)
        if args.baseline:
            res = analyze.counterfactual(run, _run_path(s, args.baseline))
            (run / "impact_counterfactual.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
            print(json.dumps(res, indent=1))
        else:
            imp = analyze.analyze_dir(run)
            for p in imp["periods"]:
                causes = ", ".join(f"{c['load']} ({c['kind']})" for c in p["causes"]) or "-"
                print(f"{p['load']:16} {p['metric']:14} {p['start_t']:>8.0f} -> {p.get('end_t', 0):>8.0f}  {causes}")
            if not imp["periods"]:
                print("no load became unhealthy")
    elif args.cmd == "compare":
        print(analyze.compare(_run_path(s, args.a), _run_path(s, args.b))["text"])
    elif args.cmd == "runners":
        for name, cls in plugins["runners"].items():
            ok, why = cls.available(s) if name != "kubernetes" else cls.available(s, args.kubeconfig)
            print(f"{name:12} {'yes' if ok else 'no ':4} {why}")
    elif args.cmd == "student-copy":
        from . import student_copy
        res = student_copy.make(args.dest, plugins, owner=args.owner)
        print(f"student copy written to {res['dest']} ({res['files']} files)")
        print(f"left out: {', '.join(res['left_out']) or 'nothing'}; allow_flags: [] in its loadkit.yaml")
    elif args.cmd == "reset":
        from .plugins.runners.kubernetes import _load_config
        from kubernetes import client
        _load_config(args.kubeconfig)
        from .plugins.runners.kubernetes import group_name
        core, sched = client.CoreV1Api(), client.SchedulingV1Api()
        sel = "app.kubernetes.io/part-of=loadkit"
        if not args.all_groups:                       # only this group's objects, never another group's run
            sel += f",loadkit/group={group_name(s) or 'default'}"
        print(f"deleting kit objects with labels {sel}")
        for ns in core.list_namespace(label_selector=sel).items:
            core.delete_namespace(ns.metadata.name)
            print("deleted namespace", ns.metadata.name)
        for pc in sched.list_priority_class(label_selector=sel).items:
            sched.delete_priority_class(pc.metadata.name)
            print("deleted priority class", pc.metadata.name)
    elif args.cmd == "snapshot":
        from . import snapshot
        run = _run_path(s, args.run)
        out = pathlib.Path(args.out) if args.out else run / "snapshot.html"
        print(snapshot.build(s, plugins, run.name, out))
    elif args.cmd == "serve":
        from . import server
        server.serve(s, plugins, host=args.host, port=args.port, kubeconfig=args.kubeconfig)


if __name__ == "__main__":
    main()
