"""Web UI + API (standard library only). One run at a time. Binds to 127.0.0.1 by default.

See LOAD_KIT_PLAN.md section 13 for the endpoints.
"""
import csv
import json
import mimetypes
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import FORMAT_VERSION, __version__, analyze, generator, scenario
from .engine import Engine
from .settings import ROOT, runs_dir

UI_DIR = ROOT / "ui"


def _name(n):
    """API callers pick scenarios by name only (never by path)."""
    if not isinstance(n, str) or not scenario.NAME_RE.match(n):
        raise ValueError(f"bad scenario name: {n!r}")
    return n


MATHS_KEYS = {"batch": ("units", "cost_per_unit", "intensity", "parallelism", "normal_duration"),
              "stream": ("base_rate", "work_per_event", "daily_pattern"),
              "users": ("users", "think_time", "cost_per_request", "daily_pattern")}


def _maths_params(ld):
    """The few settings the UI needs to show how busy each load keeps the machine."""
    return {k: ld[k] for k in MATHS_KEYS.get(ld["type"], ()) if k in ld}


class State:
    def __init__(self, settings, plugins, kubeconfig=None):
        self.settings, self.plugins, self.kubeconfig = settings, plugins, kubeconfig
        self.engine = None
        self.thread = None
        self.error = None
        self.lock = threading.Lock()

    # -------------------------------------------------------------- helpers
    def allowed(self, name):
        """A scenario name that exists AND is allowed here (allow_flags); otherwise 'not found' (not even listed)."""
        name = _name(name)
        sc = scenario.load_file(name, self.plugins)
        if scenario.blocked_flags(sc, self.settings):
            raise FileNotFoundError(f"scenario '{name}' not found")
        return name

    def runners(self):
        out = []
        for name, cls in self.plugins["runners"].items():
            ok, why = cls.available(self.settings, self.kubeconfig)
            out.append({"name": name, "label": getattr(cls, "label", name), "available": ok, "reason": why})
        return out

    def scenario_view(self, name, full=False):
        sc = scenario.load_file(name, self.plugins)
        loads = [{k: v for k, v in ld.items()} for ld in sc["loads"]] if full else \
            [{"name": ld["name"], "type": ld["type"], "team": ld["team"], "priority": ld["priority"],
              **_maths_params(ld)} for ld in sc["loads"]]
        return {"name": sc["name"], "description": sc["description"], "story": sc.get("story", ""),
                "look_for": sc.get("look_for", []), "duration": sc["duration"], "day_length": sc["day_length"],
                "day_start_hour": sc["day_start_hour"], "seed": sc["seed"], "capacity": sc["capacity"],
                "preemption": sc["preemption"], "flags": sc["flags"], "set": sc.get("set", "full"), "loads": loads}

    def start(self, body):
        with self.lock:
            if self.engine and self.engine.status == "running":
                raise ValueError("a run is already in progress (one run at a time)")
            sc = scenario.load_file(self.allowed(body["scenario"]), self.plugins)
            without = body.get("without") or []
            if without:
                sc["loads"] = [ld for ld in sc["loads"] if ld["name"] not in without]
                sc["_hash"] = f"{sc['_hash']}-without-{'-'.join(without)}"
            runner = body.get("runner") or next(iter(self.plugins["runners"]), "kubernetes")
            ok = next((r["available"] for r in self.runners() if r["name"] == runner), False)
            if not ok:
                raise ValueError(f"runner '{runner}' is not available here")
            eng = Engine(sc, self.settings, self.plugins, seed=body.get("seed"), runner=runner,
                         note=f"without {', '.join(without)}" if without else "",
                         runner_options={"kubeconfig": self.kubeconfig})
            self.engine, self.error = eng, None
            speed = float(body.get("speed", 1))

            def go():
                try:
                    eng.run(speed=speed)          # start() (preflight + setup on the cluster) happens in here
                except Exception:  # noqa: BLE001 — show the error in the UI instead of a silent thread death
                    self.error = traceback.format_exc()
                    with eng.lock:
                        if eng.status == "created":
                            eng.status = "running"
                        eng.finish("failed")
            self.thread = threading.Thread(target=go, daemon=True)
            self.thread.start()
            return eng

    def current(self):
        e = self.engine
        if not e:
            return {"status": "idle"}
        v = e.status_view()
        v.update(scenario=self.scenario_view(e.sc["_source"]) if e.sc.get("_source") else None,
                 seed=e.seed, runner=e.runner_name, speed=getattr(e, "speed", 0), error=self.error,
                 drain=e.len["drain"], loads=[ld["name"] for ld in e.sc["loads"]],
                 timeline=e.timeline, node=e.sc["capacity"])
        return v

    def past_runs(self):
        out = []
        d = runs_dir(self.settings)
        if not d.exists():
            return out
        for p in d.iterdir():
            f = p / "run.json"
            if f.exists():
                try:
                    info = json.loads(f.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                out.append({k: info.get(k) for k in ("run_id", "scenario", "seed", "runner", "status", "started_utc",
                                                     "note")})
        return sorted(out, key=lambda r: r.get("started_utc") or "", reverse=True)

    def past_run(self, run_id):
        d = runs_dir(self.settings) / run_id
        if not (d / "run.json").exists() or "/" in run_id or ".." in run_id:
            raise FileNotFoundError(run_id)
        r = analyze.load_run(d)
        metrics = []
        with open(d / "metrics.csv", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                metrics.append({"t": float(row["t"]), "phase": row["phase"], "load": row["load"],
                                "metric": row["metric"], "value": float(row["value"])})
        health = [{"t": float(h["t"]), "phase": h["phase"], "load": h["load"], "metric": h["metric"],
                   "value": float(h["value"]), "threshold": float(h["threshold"]), "healthy": h["healthy"] == "1"}
                  for h in r["health"]]
        timeline = json.loads((d / "timeline.json").read_text(encoding="utf-8"))
        info = r["info"]
        sc_name = info["scenario"]
        try:
            scv = self.scenario_view(sc_name)
        except FileNotFoundError:
            scv = None
        return {"run": info, "scenario": scv, "diary": r["diary"], "metrics": metrics, "health": health,
                "impact": r["impact"], "timeline": timeline, "folder": str(d)}


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        server_version = f"loadkit/{__version__}"

        def log_message(self, *a):  # quiet
            pass

        def _json(self, obj, code=200):
            data = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _static(self, path):
            rel = path.lstrip("/") or "index.html"
            f = (UI_DIR / rel).resolve()
            if not str(f).startswith(str(UI_DIR.resolve())) or not f.is_file():
                return self._json({"error": "not found"}, 404)
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", (mimetypes.guess_type(str(f))[0] or "application/octet-stream")
                             + ("; charset=utf-8" if f.suffix in (".html", ".js", ".css") else ""))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            p = u.path.rstrip("/") or "/"
            try:
                if p == "/api/info":
                    return self._json({"version": __version__, "format_version": FORMAT_VERSION,
                                       "runners": state.runners(), "panels": state.settings["plugins"]["ui_panels"],
                                       "naming": state.settings["naming"]["mode"],
                                       "group": state.settings.get("group")})
                if p == "/api/scenarios":
                    return self._json([state.scenario_view(n)
                                       for n in scenario.list_allowed(state.plugins, state.settings)])
                if p.startswith("/api/scenarios/"):
                    return self._json(state.scenario_view(state.allowed(p.split("/")[-1]), full=True))
                if p == "/api/runs/current":
                    return self._json(state.current())
                if p == "/api/runs/current/diary":
                    e = state.engine
                    since = int(q.get("since", 0))
                    return self._json({"next": len(e.diary.events) if e else 0,
                                       "events": e.diary.events[since:] if e else []})
                if p == "/api/runs/current/metrics":
                    e = state.engine
                    if not e:
                        return self._json({"metrics": [], "health": []})
                    since = float(q.get("since", "-1e18"))
                    with e.lock:
                        return self._json({"metrics": [r for r in e.metric_rows if r["t"] > since],
                                           "health": [r for r in e.health_rows if r["t"] > since]})
                if p == "/api/runs":
                    return self._json(state.past_runs())
                if p.startswith("/api/runs/") and p.endswith("/export.xlsx"):
                    from . import export
                    run_id = p.split("/")[-2]
                    d = runs_dir(state.settings) / run_id
                    if "/" in run_id or ".." in run_id or not (d / "run.json").exists():
                        raise FileNotFoundError(run_id)
                    name, data = export.build(d)
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return None
                if p.startswith("/api/runs/"):
                    return self._json(state.past_run(p.split("/")[-1]))
                if p == "/api/compare":
                    d = runs_dir(state.settings)
                    res = analyze.compare(d / q["a"], d / q["b"])
                    return self._json({"a": state.past_run(q["a"]), "b": state.past_run(q["b"]),
                                       "summary": {"a": res["a"], "b": res["b"]}, "text": res["text"]})
                return self._static(p)
            except FileNotFoundError as e:
                return self._json({"error": f"not found: {e}"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e), "trace": traceback.format_exc()}, 500)

        def do_POST(self):  # noqa: N802
            p = urlparse(self.path).path.rstrip("/")
            try:
                body = self._body()
                if p == "/api/timeline":
                    sc = scenario.load_file(state.allowed(body["scenario"]), state.plugins)
                    return self._json(generator.make_timeline(sc, state.plugins, body.get("seed")))
                if p == "/api/runs":
                    eng = state.start(body)
                    return self._json({"run_id": eng.run_id})
                if p == "/api/runs/current/stop":
                    if state.engine:
                        state.engine.request_stop(body.get("mode", "soft"))
                    return self._json({"ok": True})
                if p == "/api/runs/current/speed":
                    if state.engine:
                        state.engine.set_speed(body.get("speed", 20))
                    return self._json({"ok": True})
                if p.endswith("/abort") or p.endswith("/resume"):
                    return self._json({"error": "resume/abort after a crash is not built yet"}, 400)
                return self._json({"error": "not found"}, 404)
            except (ValueError, KeyError, scenario.ScenarioError) as e:
                return self._json({"error": str(e)}, 400)
            except (FileNotFoundError, PermissionError) as e:
                return self._json({"error": str(e)}, 404)
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e), "trace": traceback.format_exc()}, 500)
    return H


def serve(settings, plugins, host=None, port=None, kubeconfig=None):
    host = host or settings["server"]["host"]
    port = int(port or settings["server"]["port"])
    state = State(settings, plugins, kubeconfig)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    print(f"Load Scenario Kit {__version__}  ->  http://{host}:{port}/   (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if state.engine and state.engine.status == "running":
            state.engine.request_stop("hard")
        httpd.server_close()
