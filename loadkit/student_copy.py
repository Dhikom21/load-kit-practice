"""Make the students' copy of the kit: the same kit WITHOUT the restricted scenarios (default: memory scenarios).

    python -m loadkit student-copy ../load-kit-students

What is different in the copy:
  * scenarios with a restricted flag (memory-pressure) are not copied at all
  * loadkit.yaml has allow_flags: []  -> scenarios students write themselves that leak memory or fill the machine's
    memory are hidden and refused too (the kit flags them automatically)
  * LOAD_KIT_PLAN.md (the research plan) and run results are not copied; secrets never are
  * the README lists only the scenarios in the copy
Tests that need a missing scenario are skipped automatically, so `make test` stays green in the copy.
"""
import pathlib
import re
import shutil

from . import scenario
from .settings import ROOT

SKIP_DIRS = {".git", "runs", "__pycache__", ".venv", "venv", ".pytest_cache", ".ruff_cache", "ci-out", "base"}
SKIP_FILES = {"LOAD_KIT_PLAN.md", "SCENARIOS_MEMORY.md", "env.sh", ".env"}
SECRET = re.compile(r"kubeconfig|token|secret", re.I)


def make(dest, plugins, restricted=("memory-pressure",), owner=None):
    dest = pathlib.Path(dest).resolve()
    if dest == ROOT or ROOT in dest.parents:
        raise ValueError("choose a folder outside the kit, e.g. ../load-kit-students")
    if dest.exists() and any(dest.iterdir()):
        raise ValueError(f"{dest} is not empty")
    left_out = []
    for n in scenario.list_builtin():
        sc = scenario.load_file(n, plugins)
        if set(sc["flags"]) & set(restricted):
            left_out.append(n)
    copied = 0
    for src in ROOT.rglob("*"):
        rel = src.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts) or src.is_dir():
            continue
        if rel.name in SKIP_FILES or src.suffix == ".pyc" or SECRET.search(rel.name):
            continue
        if rel.parts[0] == "scenarios" and rel.stem in left_out:
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        copied += 1
    # marker: the pipeline (ci/policy.py) enforces the student rules only in a copy that has this file
    (dest / ".student-copy").write_text("This is the students' copy of the Load Scenario Kit (made by loadkit "
                                        "student-copy). ci/policy.py enforces the course rules here.\n")
    co = dest / ".github" / "CODEOWNERS"
    if owner and co.exists():
        owner = owner if owner.startswith("@") else "@" + owner
        co.write_text(co.read_text(encoding="utf-8").replace("@TEACHER", owner), encoding="utf-8")
    # settings: nothing restricted is allowed
    y = dest / "loadkit.yaml"
    text = re.sub(r"^allow_flags:.*$", "allow_flags: []                         # student copy: memory scenarios are not "
                  "allowed", y.read_text(encoding="utf-8"), flags=re.M)
    y.write_text(text, encoding="utf-8")
    # README: only the scenarios that are in the copy
    r = dest / "README.md"
    lines = [ln for ln in r.read_text(encoding="utf-8").splitlines() if not any(f"`{n}`" in ln for n in left_out)]
    text = "\n".join(lines).replace(" Full design: `LOAD_KIT_PLAN.md`.", "")
    r.write_text(text + "\n", encoding="utf-8")
    return {"dest": str(dest), "files": copied, "left_out": left_out}
