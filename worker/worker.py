"""loadkit-worker: the pods of the Load Scenario Kit. One image, the role is chosen by the first argument.

  worker batch     --load backup --run 3 --units 250 --cost 1.9 --intensity 1.5 [--checkpoint] [memory options]
  worker produce   --load telemetry --partitions 4
  worker consume   --load telemetry --partitions 4 --threads 2 --work-ms 8 --prefetch 50 [memory options]
  worker shop      --load shop-users --cost-ms 20 [--port 8080] [memory options]
  worker users     --load shop-users --url http://shop-users:8080/ --think 1.0 --timeout 5
  worker calibrate [--seconds 3]

Work is REAL and work-based:
  * batch: a work unit costs `--cost` CPU-seconds (measured with the process CPU clock, so it is the same amount of
    work on any machine). On a busy machine the pod gets less CPU, so the same units take longer wall-clock time.
  * consume: each event costs `--work-ms` CPU-milliseconds.  shop: each request costs `--cost-ms` CPU-milliseconds.
Memory is REAL too: buffers are allocated and touched so the pages count against the pod's memory limit.

Pods only talk to the kit's Redis (REDIS_URL): they read control keys, write stats/heartbeats/progress and append
facts to the `diary` stream. They never call the engine. Key names: see KEYS below (mirrored in loadkit/channel.py).
"""
import argparse
import hashlib
import http.server
import json
import math
import multiprocessing as mp
import os
import random
import signal
import socket
import sys
import threading
import time
import urllib.request

import redis

# ------------------------------------------------------------------ Redis keys (keep in sync with loadkit/channel.py)
KEYS = {
    "rate": "ctl:{load}:rate",              # stream: events per second (engine -> producer)
    "users": "ctl:{load}:users",            # users: number of simulated users (engine -> users pod)
    "run_state": "ctl:run_state",           # running | stopping
    "hb": "hb:{pod}",                       # heartbeat, expires after 5 s
    "progress": "progress:{load}:{run}:{index}",   # batch: units done by one pod index
    "partition": "stream:{load}:p{k}",      # stream events (list of send timestamps)
    "members": "members:{load}",           # consumer group: live consumer pods (sorted set, score = last seen)
    "idle": "idle:{load}:{pod}",            # consumer pod without a partition (expires after 3 s)
    "produced": "stat:{load}:produced",
    "processed": "stat:{load}:processed",
    "shop": "shop:{load}:{pod}",            # hash: in_flight, served (expires after 5 s)
    "users_stat": "usersstat:{load}",       # hash: request_rate, p95, errors, users (written by the users pod)
    "diary": "diary",                       # stream of facts from pods
}
MiB = 1024 * 1024
UNITS = {"Ki": 1024, "Mi": MiB, "Gi": 1024 * MiB, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3}


def key(name, **kw):
    return KEYS[name].format(**kw)


def mem_bytes(v):
    v = str(v or "0").strip()
    for suf, mul in sorted(UNITS.items(), key=lambda kv: -len(kv[0])):
        if v.endswith(suf):
            return int(float(v[: -len(suf)]) * mul)
    return int(float(v))


def connect():
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    for attempt in range(60):
        try:
            r = redis.Redis.from_url(url, decode_responses=True, socket_timeout=10)
            r.ping()
            return r
        except redis.RedisError:
            time.sleep(min(5, 0.5 * (attempt + 1)))
    sys.exit("cannot reach Redis at REDIS_URL")


POD = os.environ.get("POD_NAME") or socket.gethostname()


def diary(r, event, **fields):
    try:
        r.xadd(key("diary"), {"json": json.dumps({"ts": time.time(), "pod": POD, "event": event, **fields})},
               maxlen=200000, approximate=True)
    except redis.RedisError:
        pass


def heartbeat_loop(r, role, stop):
    while not stop.is_set():
        try:
            r.set(key("hb", pod=POD), role, ex=5)
        except redis.RedisError:
            pass
        stop.wait(1.0)


# ------------------------------------------------------------------ CPU and memory primitives
_BLOCK = b"loadkit" * 9


def burn_cpu(seconds):
    """Do `seconds` of CPU work, measured on this process's CPU clock (independent of how busy the machine is)."""
    end = time.process_time() + seconds
    h = hashlib.sha256()
    while time.process_time() < end:
        for _ in range(200):
            h.update(_BLOCK)
    return h.digest()


class Throttle:
    """Keep a process at no more than `cores` of CPU on average (duty cycle), like a CPU limit inside the app."""

    def __init__(self, cores):
        self.cores = max(0.01, float(cores))
        self.w0, self.c0 = time.monotonic(), time.process_time()

    def wait(self):
        if self.cores >= 0.999:
            return
        cpu = time.process_time() - self.c0
        wall = time.monotonic() - self.w0
        ahead = cpu / self.cores - wall
        if ahead > 0:
            time.sleep(min(ahead, 0.2))
        if wall > 10:                    # forget the far past so bursts after waits stay smooth
            self.w0, self.c0 = time.monotonic(), time.process_time()


class Memory:
    """Real, resident memory: 1 MiB chunks, every page written."""

    def __init__(self):
        self.chunks = []

    def set(self, nbytes):
        want = max(0, int(nbytes) // MiB)
        while len(self.chunks) < want:
            b = bytearray(MiB)
            for i in range(0, MiB, 4096):
                b[i] = 1
            self.chunks.append(b)
        del self.chunks[want:]

    @property
    def nbytes(self):
        return len(self.chunks) * MiB


def mem_target(args, progress, age):
    base, peak = mem_bytes(args.mem_base), mem_bytes(args.mem_peak or args.mem_base)
    shape = args.mem_shape
    if shape == "ramp":
        v = base + (peak - base) * progress
    elif shape == "step":
        v = peak if progress >= 0.15 else base
    elif shape == "spiky":
        v = base + (peak - base) * (0.5 + 0.5 * math.sin(age / 7.0))
    else:
        v = base
    return v + mem_bytes(args.leak) * age / 60.0          # leak is given per minute


def add_memory_args(p):
    p.add_argument("--mem-base", default="0")
    p.add_argument("--mem-peak", default=None)
    p.add_argument("--mem-shape", default="flat", choices=["flat", "ramp", "step", "spiky"])
    p.add_argument("--leak", default="0", help="per minute, e.g. 5Mi")


# ------------------------------------------------------------------ batch
def _batch_proc(units_total, done, lock, cost, cores, progress_key, url, stats):  # noqa: PLR0913
    """One process of a batch pod. stats = shared [cpu_s, redis_s, slices] for the timing log."""
    os.environ["REDIS_URL"] = url
    r = connect()
    th = Throttle(cores)
    c_start = time.process_time()
    while True:
        with lock:
            if done.value >= units_total:
                return
            done.value += 1
        t_end = time.process_time() + cost
        while time.process_time() < t_end:
            burn_cpu(min(0.02, t_end - time.process_time()))
            th.wait()
            stats[2] += 1
        w = time.monotonic()
        try:
            r.incr(progress_key)
        except redis.RedisError:
            pass
        stats[1] += time.monotonic() - w
        stats[0] = time.process_time() - c_start


def cgroup_cpu():
    """CPU-seconds used by this whole pod, as Kubernetes measures it (cgroup v2, else v1)."""
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


def role_batch(args):
    r = connect()
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(r, "batch", stop), daemon=True).start()
    index = int(os.environ.get("JOB_COMPLETION_INDEX", args.index))
    if args.parallel > 1:                           # Indexed Job: --units is the run's total; take this pod's share
        args.units = args.units // args.parallel + (1 if index < args.units % args.parallel else 0)
    pkey = key("progress", load=args.load, run=args.run, index=index)
    start_at = 0
    if args.checkpoint:
        start_at = int(r.get(pkey) or 0)
    else:
        r.set(pkey, 0)                              # a restarted pod without checkpoint starts again (lost work)
    nproc = max(1, math.ceil(args.intensity))
    cores_each = args.intensity / nproc
    done = mp.Value("i", start_at)
    lock = mp.Lock()
    stats = [mp.Array("d", 3) for _ in range(nproc)]
    diary(r, "worker_started", role="batch", load=args.load, run=args.run, index=index, units=args.units,
          resumed_from=start_at, processes=nproc)
    procs = [mp.Process(target=_batch_proc, args=(args.units, done, lock, args.cost, cores_each, pkey,
                                                   os.environ.get("REDIS_URL", ""), st), daemon=True)
             for st in stats]
    t0, c0, g0 = time.monotonic(), time.process_time(), cgroup_cpu()
    for p in procs:
        p.start()
    mem = Memory()
    next_log = 0.0

    def log(final=False):
        wall = time.monotonic() - t0
        units = min(done.value, args.units) - start_at
        child = sum(st[0] for st in stats)
        print(f"[batch {args.load} run {args.run} pod {index}] {'DONE ' if final else ''}wall {wall:6.1f} s | "
              f"units {units}/{args.units - start_at} ({units / wall if wall else 0:.2f}/s, planned "
              f"{nproc * cores_each / args.cost:.2f}/s) | work CPU {child:6.1f} s | parent CPU "
              f"{time.process_time() - c0:5.1f} s | pod CPU {cgroup_cpu() - g0:6.1f} s | waiting on Redis "
              f"{sum(st[1] for st in stats):5.1f} s | slices {int(sum(st[2] for st in stats))}", flush=True)

    while any(p.is_alive() for p in procs):
        prog = done.value / max(args.units, 1)
        mem.set(mem_target(args, prog, time.monotonic() - t0))
        if time.monotonic() - t0 >= next_log:       # timing log every 10 s: `kubectl logs -f <pod>`
            log()
            next_log += 10.0
        time.sleep(0.5)
    log(final=True)
    diary(r, "worker_finished", role="batch", load=args.load, run=args.run, index=index,
          seconds=round(time.monotonic() - t0, 1))
    stop.set()


# ------------------------------------------------------------------ stream: producer
def role_produce(args):
    r = connect()
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(r, "producer", stop), daemon=True).start()
    diary(r, "worker_started", role="producer", load=args.load)
    carry, k, tick = 0.0, 0, 0.1
    rng = random.Random(POD)
    nxt = time.monotonic()
    while True:
        try:
            rate = float(r.get(key("rate", load=args.load)) or 0)
            carry += rate * tick
            n = int(carry)
            carry -= n
            if n:
                now = f"{time.time():.3f}"
                pipe = r.pipeline(transaction=False)
                per = {}
                for _ in range(n):
                    per[k] = per.get(k, 0) + 1
                    k = (k + 1) % args.partitions
                for part, cnt in per.items():
                    pipe.rpush(key("partition", load=args.load, k=part), *([now] * cnt))
                pipe.incrby(key("produced", load=args.load), n)
                pipe.execute()
        except redis.RedisError:
            time.sleep(1)
        nxt += tick * (1 + 0.02 * rng.uniform(-1, 1))
        time.sleep(max(0.0, nxt - time.monotonic()))


# ------------------------------------------------------------------ stream: consumer
MAX_PARTITIONS = 256


def assignment(members, pod, partitions):
    """Consumer-group rule (like Kafka's range/round-robin assignors): live members sorted by name; member i gets the
    partitions k with k % n == i. Fewer consumers than partitions -> some get several; more -> the extra ones are idle."""
    members = sorted(members)
    if pod not in members:
        return []
    i, n = members.index(pod), len(members)
    return [k for k in range(partitions) if k % n == i]


def _consume_proc(load, mine, n_mine, j, work_ms, prefetch, pad, url, buffered):
    """One consumer thread (a process, so several threads can really use several cores). Thread j starts at its own
    offset in the pod's partition list so the threads spread over the partitions."""
    os.environ["REDIS_URL"] = url
    r = connect()
    buf, turn = [], j
    while True:
        n = n_mine.value
        if n <= 0:
            time.sleep(0.2)
            continue
        if not buf:
            got = None
            for _ in range(n):                         # try each assigned partition once
                k = mine[turn % n]
                turn += 1
                try:
                    got = r.lpop(key("partition", load=load, k=k), prefetch)
                except redis.RedisError:
                    time.sleep(1)
                    break
                if got:
                    break
            if not got:
                time.sleep(0.05)
                continue
            buf = [(e, bytearray(pad)) for e in got]     # buffered events hold real memory
            buffered.value = len(buf)
        buf.pop(0)
        buffered.value = len(buf)
        burn_cpu(work_ms / 1000.0)
        try:
            r.incr(key("processed", load=load))
        except redis.RedisError:
            pass


def role_consume(args):
    r = connect()
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(r, "consumer", stop), daemon=True).start()
    mine = mp.Array("i", MAX_PARTITIONS)
    n_mine = mp.Value("i", 0)
    url = os.environ.get("REDIS_URL", "")
    pad = mem_bytes(args.per_buffered_event)
    buffered = [mp.Value("i", 0) for _ in range(args.threads)]
    procs = [mp.Process(target=_consume_proc, args=(args.load, mine, n_mine, j, args.work_ms, args.prefetch, pad,
                                                     url, b), daemon=True) for j, b in enumerate(buffered)]
    for p in procs:
        p.start()
    diary(r, "worker_started", role="consumer", load=args.load, threads=args.threads)
    mem, leak, t0, last = Memory(), [], time.monotonic(), None
    gkey = key("members", load=args.load)
    while True:
        # consumer-group membership: say "I'm alive", forget members silent for 5 s, take my share of partitions
        try:
            now = time.time()
            pipe = r.pipeline(transaction=False)
            pipe.zadd(gkey, {POD: now})
            pipe.zremrangebyscore(gkey, "-inf", now - 5)
            pipe.zrange(gkey, 0, -1)
            members = pipe.execute()[2]
            parts = assignment(members, POD, args.partitions)
            for i, k in enumerate(parts[:MAX_PARTITIONS]):
                mine[i] = k
            n_mine.value = len(parts)
            if parts != last:
                diary(r, "partitions_assigned", load=args.load, partitions=parts, members=len(members))
                last = parts
            if not parts:
                r.set(key("idle", load=args.load, pod=POD), 1, ex=3)
        except redis.RedisError:
            pass
        age = time.monotonic() - t0
        mem.set(mem_bytes(args.mem_base))
        want_leak = int(mem_bytes(args.leak) * age / 60.0) // MiB
        while len(leak) < want_leak:                 # the "bug": references kept forever
            b = bytearray(MiB)
            for i in range(0, MiB, 4096):
                b[i] = 1
            leak.append(b)
        time.sleep(1.0)


# ------------------------------------------------------------------ shop (user-facing service)
class Shop:
    in_flight = 0
    served = 0
    lock = threading.Lock()


def role_shop(args):
    r = connect()
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(r, "shop", stop), daemon=True).start()
    per_req = mem_bytes(args.per_in_flight)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/healthz"):
                return self._send(200, b"ok")
            if self.path.startswith("/metrics"):
                body = (f"http_requests_in_flight {Shop.in_flight}\nhttp_requests_total {Shop.served}\n").encode()
                return self._send(200, body)
            with Shop.lock:
                Shop.in_flight += 1
            try:
                hold = bytearray(per_req)               # memory grows with requests in flight
                burn_cpu(args.cost_ms / 1000.0)
                del hold
                self._send(200, b"ok")
            finally:
                with Shop.lock:
                    Shop.in_flight -= 1
                    Shop.served += 1

        def _send(self, code, body):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", args.port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    diary(r, "worker_started", role="shop", load=args.load)
    mem = Memory()
    while True:
        mem.set(mem_bytes(args.mem_base))
        try:
            r.hset(key("shop", load=args.load, pod=POD), mapping={"in_flight": Shop.in_flight, "served": Shop.served})
            r.expire(key("shop", load=args.load, pod=POD), 5)
        except redis.RedisError:
            pass
        time.sleep(1.0)


# ------------------------------------------------------------------ users (closed loop)
def role_users(args):
    r = connect()
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(r, "users", stop), daemon=True).start()
    samples, lock = [], threading.Lock()
    users = []

    def user(my_stop, seed):
        rng = random.Random(seed)
        while not my_stop.is_set():
            t0 = time.monotonic()
            ok = True
            try:
                with urllib.request.urlopen(args.url, timeout=args.timeout) as resp:
                    resp.read()
                    ok = resp.status == 200
            except Exception:  # noqa: BLE001 — timeouts and refused connections are errors for the user
                ok = False
            with lock:
                samples.append((time.monotonic(), time.monotonic() - t0, ok))
            my_stop.wait(rng.expovariate(1.0 / args.think) if args.think > 0 else 0)

    diary(r, "worker_started", role="users", load=args.load)
    k = 0
    while True:
        try:
            want = int(float(r.get(key("users", load=args.load)) or 0))
        except redis.RedisError:
            want = len(users)
        while len(users) < want:
            ev = threading.Event()
            k += 1
            threading.Thread(target=user, args=(ev, f"{POD}-{k}"), daemon=True).start()
            users.append(ev)
        while len(users) > want:
            users.pop().set()
        now = time.monotonic()
        with lock:
            samples[:] = [s for s in samples if s[0] > now - args.window]
            lat = sorted(s[1] for s in samples)
            errs = sum(1 for s in samples if not s[2])
            n = len(samples)
        p95 = lat[min(n - 1, int(0.95 * n))] if n else 0.0
        try:
            r.hset(key("users_stat", load=args.load), mapping={
                "request_rate": round(n / args.window, 3), "p95": round(p95, 4),
                "errors": round(errs / n, 4) if n else 0.0, "users": len(users), "ts": time.time()})
        except redis.RedisError:
            pass
        time.sleep(1.0)


# ------------------------------------------------------------------ calibrate
def role_calibrate(args):
    """How much hashing one CPU-second does on this machine, and how steady it is (single core)."""
    rates = []
    for _ in range(max(1, int(args.seconds))):
        c0, n = time.process_time(), 0
        h = hashlib.sha256()
        while time.process_time() - c0 < 1.0:
            for _ in range(200):
                h.update(_BLOCK)
            n += 200
        rates.append(n / (time.process_time() - c0))
    mean = sum(rates) / len(rates)
    sd = (sum((x - mean) ** 2 for x in rates) / len(rates)) ** 0.5
    print(json.dumps({"node": os.environ.get("NODE_NAME", socket.gethostname()), "hash_blocks_per_cpu_second": round(mean),
                      "variation": round(sd / mean, 4) if mean else None,
                      "note": "work units are measured in CPU-seconds, so they cost the same CPU time on any node; "
                              "this number tells how fast the node is compared with others"}))


# ------------------------------------------------------------------ main
def build_parser():
    ap = argparse.ArgumentParser(prog="worker")
    sub = ap.add_subparsers(dest="role", required=True)
    b = sub.add_parser("batch")
    b.add_argument("--load", required=True)
    b.add_argument("--run", type=int, required=True)
    b.add_argument("--units", type=int, required=True, help="units of the run (split over --parallel pods)")
    b.add_argument("--parallel", type=int, default=1)
    b.add_argument("--cost", type=float, required=True, help="CPU-seconds per unit")
    b.add_argument("--intensity", type=float, default=1.0, help="cores this pod tries to use")
    b.add_argument("--index", type=int, default=0)
    b.add_argument("--checkpoint", action="store_true")
    add_memory_args(b)
    p = sub.add_parser("produce")
    p.add_argument("--load", required=True)
    p.add_argument("--partitions", type=int, default=4)
    c = sub.add_parser("consume")
    c.add_argument("--load", required=True)
    c.add_argument("--partitions", type=int, default=4)
    c.add_argument("--threads", type=int, default=1)
    c.add_argument("--work-ms", type=float, default=8.0)
    c.add_argument("--prefetch", type=int, default=50)
    c.add_argument("--per-buffered-event", default="0")
    add_memory_args(c)
    s = sub.add_parser("shop")
    s.add_argument("--load", required=True)
    s.add_argument("--cost-ms", type=float, default=20.0)
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--per-in-flight", default="0")
    add_memory_args(s)
    u = sub.add_parser("users")
    u.add_argument("--load", required=True)
    u.add_argument("--url", required=True)
    u.add_argument("--think", type=float, default=1.0)
    u.add_argument("--timeout", type=float, default=5.0)
    u.add_argument("--window", type=float, default=15.0)
    k = sub.add_parser("calibrate")
    k.add_argument("--seconds", type=float, default=3)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    {"batch": role_batch, "produce": role_produce, "consume": role_consume, "shop": role_shop,
     "users": role_users, "calibrate": role_calibrate}[args.role](args)


if __name__ == "__main__":
    main()
