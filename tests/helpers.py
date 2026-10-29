import copy
import functools

from loadkit import registry, scenario, settings
from loadkit.engine import Engine
from tests.fake_cluster import FakeCluster


@functools.lru_cache(maxsize=None)
def setup():
    """Kit settings + enabled plugins, plus the test-only fake cluster as runner 'fake'."""
    s = settings.load()
    s["allow_flags"] = ["memory-pressure"]        # tests may use memory scenarios when they exist (research copy)
    pl = registry.load_enabled(s)
    pl["runners"] = {**pl["runners"], "fake": FakeCluster}
    return s, pl


@functools.lru_cache(maxsize=None)
def _cached_run(name, seed):
    return run(name, seed)


def load(name):
    s, pl = setup()
    return scenario.load_file(name, pl)


def run(name_or_sc, seed=None, record=False, out_dir=None, **kw):
    s, pl = setup()
    sc = load(name_or_sc) if isinstance(name_or_sc, str) else name_or_sc
    e = Engine(sc, s, pl, seed=seed, runner="fake", record=record, out_dir=out_dir, **kw)
    e.run()
    return e


def cached(name, seed=None):
    """A finished run, shared between tests (read-only!)."""
    return _cached_run(name, seed)


def events(e, name=None, load=None):
    return [x for x in e.diary.events if (name is None or x["event"] == name) and (load is None or x.get("load") == load)]


def mutate(name, fn):
    sc = copy.deepcopy(load(name))
    fn(sc)
    return sc


def needs_scenario(*names):
    """Skip a test when a scenario file is not in this copy (the student copy has no memory scenarios)."""
    import unittest
    missing = [n for n in names if not (scenario.SCENARIO_DIR / f"{n}.yaml").exists()]
    return unittest.skipIf(missing, f"scenario not in this copy: {', '.join(missing)}")
