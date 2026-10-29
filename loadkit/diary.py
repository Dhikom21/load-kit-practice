"""Diary: the ground truth of a run. One JSON object per line; every event says where it came from.

origin = "planned"  (from the timeline; has planned_t)
       | "reactive" (decided during the run; has a cause)
       | "control"  (run phases and run control: phase_started, run_interrupted …)
"""
import json

PLANNED = {"run_started", "job_due", "job_started", "burst_started", "burst_ended", "rate_changed", "users_changed",
           "run_ended"}
CONTROL = {"phase_started", "run_interrupted", "run_resumed", "run_aborted"}


class Diary:
    def __init__(self, path=None, run_id=""):
        self.events = []
        self.run_id = run_id
        self._fh = open(path, "w", encoding="utf-8") if path else None

    def write(self, t, event, origin, load=None, **fields):
        rec = {"t": round(t, 3), "event": event, "origin": origin}
        if load is not None:
            rec["load"] = load
        rec.update(fields)
        self.events.append(rec)
        if self._fh:
            self._fh.write(json.dumps(rec, sort_keys=True) + "\n")
        return rec

    def since(self, index):
        return self.events[index:]

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None
