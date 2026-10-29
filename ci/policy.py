"""Course rules checked on every push (python ci/policy.py). Fails with a clear message if a rule is broken.

1. No secrets in the repository: kubeconfig files, private keys, cluster tokens.
2. In the students' copy (marked by the file .student-copy): memory scenarios stay switched off
   (allow_flags: [] in loadkit.yaml) and no scenario needs the memory-pressure flag.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SKIP = {".git", "runs", "__pycache__", ".venv", "venv", "base", "ci-out", "node_modules"}
SECRET_TEXT = [
    (re.compile(r"client-key-data:\s*\S{20,}"), "a kubeconfig with a client key"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"), "a private key"),
    (re.compile(r"^\s*token:\s*[A-Za-z0-9_\-\.]{30,}", re.M), "a cluster token"),
    (re.compile(r"kubeconfig-u-[a-z0-9]{5,}"), "a Rancher kubeconfig user"),
]


def files():
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if p.is_file() and not any(part in SKIP for part in rel.parts) and p.stat().st_size < 2_000_000:
            yield p, rel


def main():
    problems = []
    for p, rel in files():
        if re.search(r"kubeconfig", rel.name, re.I) and p.suffix in (".yaml", ".yml", ""):
            problems.append(f"{rel}: kubeconfig files must never be in the repository (pass the path at run time)")
            continue
        if p.suffix not in (".py", ".yaml", ".yml", ".md", ".txt", ".json", ".sh", ".cfg", ".toml", ""):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rx, what in SECRET_TEXT:
            if rx.search(text) and rel.as_posix() != "ci/policy.py":
                problems.append(f"{rel}: looks like {what} — remove it and rotate the secret")
    if (ROOT / ".student-copy").exists():
        from loadkit import registry, scenario, settings
        s = settings.load()
        if s.get("allow_flags"):
            problems.append(f"loadkit.yaml: allow_flags must be [] in the student copy (is {s['allow_flags']})")
        s["allow_flags"] = []
        pl = registry.load_enabled(s)
        for n in scenario.list_builtin():
            try:
                sc = scenario.load_file(n, pl)
            except Exception:  # noqa: BLE001 — invalid files are reported by `loadkit validate`
                continue
            if "memory-pressure" in sc["flags"]:
                problems.append(f"scenarios/{n}.yaml: memory scenarios are not part of this course")
    if problems:
        print("Course rules broken:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    print("Course rules: ok")


if __name__ == "__main__":
    main()
