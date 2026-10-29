"""Static snapshot: one self-contained HTML file showing a finished run in the kit's UI (no server needed).
Handy for sharing a run with students or supervisors, and for screenshots. Fonts fall back to system fonts offline."""
import json

from . import scenario
from .server import UI_DIR, State


def build(settings, plugins, run_id, out_path):
    st = State(settings, plugins)
    run = st.past_run(run_id)
    responses = {
        "/api/info": {"version": "snapshot", "runners": [{"name": "snapshot", "label": "Snapshot of a finished run",
                                                        "available": True, "reason": ""}],
                      "panels": [p for p in settings["plugins"]["ui_panels"] if p != "compare"],
                      "naming": settings["naming"]["mode"]},
        "/api/scenarios": [st.scenario_view(n) for n in scenario.list_allowed(plugins, settings)],
        "/api/runs": [r for r in st.past_runs() if r["run_id"] == run_id],
        f"/api/runs/{run_id}": run,
        "/api/runs/current": {"status": "idle"},
    }
    shim = ("window.__SNAP = " + json.dumps({"open": run_id, "r": responses}, default=str).replace("</", "<\\/") + ";\n"
            "window.fetch = async (path) => { const u = path.split('?')[0]; const d = window.__SNAP.r[decodeURIComponent(u)];"
            " return { ok: d !== undefined, statusText: 'not in snapshot',"
            " json: async () => d ?? {error: 'not in snapshot'} }; };\n")
    css = (UI_DIR / "theme.css").read_text(encoding="utf-8")
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    panels = "\n".join((UI_DIR / "panels" / f"{p}.js").read_text(encoding="utf-8")
                       for p in responses["/api/info"]["panels"])
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace('<link rel="stylesheet" href="/theme.css">', f"<style>{css}</style>")
    html = html.replace('<script src="/app.js"></script>',
                        f"<script>{shim}</script>\n<script>{js}</script>\n<script>{panels}</script>")
    out_path.write_text(html, encoding="utf-8")
    return out_path
