"""Diagnostic: does a "CPU-second" of work take one second of wall time inside a pod? (run inside the worker image)

  kubectl run cpucheck --rm -i --restart=Never --image=loadkit-worker:0.1.0 \
    --overrides='{"spec":{"nodeSelector":{"kubernetes.io/hostname":"loadkit-worker"}}}' \
    --command -- python - < tools/cpu_check.py

Each test burns 3 CPU-seconds in a different way and prints how long it took on the wall clock, plus what three
different CPU clocks say (Python's process clock, getrusage, and the pod's cgroup = what Kubernetes measures).
On a free core every column should be close to 3.0.
"""
import hashlib
import multiprocessing as mp
import os
import resource
import time

SECONDS = 3.0
BLOCK = b"loadkit" * 9


def cgroup_usage():
    try:
        for line in open("/sys/fs/cgroup/cpu.stat"):
            if line.startswith("usage_usec"):
                return int(line.split()[1]) / 1e6
    except OSError:
        pass
    try:
        return int(open("/sys/fs/cgroup/cpuacct/cpuacct.usage").read()) / 1e9
    except OSError:
        return float("nan")


def busy_clock():                       # what the earlier quick test did (mostly clock calls)
    c = time.process_time()
    while time.process_time() - c < SECONDS:
        pass


def hashing_like_worker():              # what worker.py's burn_cpu() does for every work unit
    end = time.process_time() + SECONDS
    h = hashlib.sha256()
    while time.process_time() < end:
        for _ in range(200):
            h.update(BLOCK)


def units_like_batch():                 # 0.18 s units in 0.02 s slices, like _batch_proc()
    for _ in range(int(SECONDS / 0.18)):
        t_end = time.process_time() + 0.18
        while time.process_time() < t_end:
            end = time.process_time() + min(0.02, t_end - time.process_time())
            h = hashlib.sha256()
            while time.process_time() < end:
                for _ in range(200):
                    h.update(BLOCK)


def measure(name, fn, in_child=False):
    r0, g0, w0 = resource.getrusage(resource.RUSAGE_SELF), cgroup_usage(), time.time()
    c0 = time.process_time()
    if in_child:
        p = mp.Process(target=fn)
        p.start()
        p.join()
        rc = resource.getrusage(resource.RUSAGE_CHILDREN)
        ru = rc.ru_utime + rc.ru_stime
        pc = float("nan")
    else:
        fn()
        r1 = resource.getrusage(resource.RUSAGE_SELF)
        ru = (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)
        pc = time.process_time() - c0
    wall, cg = time.time() - w0, cgroup_usage() - g0
    print(f"{name:34} wall {wall:5.2f} s | process clock {pc:5.2f} | getrusage {ru:5.2f} | pod cgroup {cg:5.2f}",
          flush=True)


if __name__ == "__main__":
    print(f"cpus visible: {os.cpu_count()}", flush=True)
    measure("1. busy loop on the clock", busy_clock)
    measure("2. hashing like the worker", hashing_like_worker)
    measure("3. 0.18 s units like the batch job", units_like_batch)
    measure("4. same, in a child process", units_like_batch, in_child=True)
