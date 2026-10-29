"""Control and diary channel: the kit's own Redis inside the cluster (namespace loadkit-system).

Pods never connect to the engine. The engine reaches Redis:
  * from a laptop  -> through a port-forward over the Kubernetes API (same path kubectl uses; no kubectl needed)
  * inside the cluster -> directly via the service DNS name
Key names must match worker/worker.py (KEYS); tests/test_channel.py checks that.
"""
import select
import socket
import threading

KEYS = {
    "rate": "ctl:{load}:rate",
    "users": "ctl:{load}:users",
    "run_state": "ctl:run_state",
    "hb": "hb:{pod}",
    "progress": "progress:{load}:{run}:{index}",
    "partition": "stream:{load}:p{k}",
    "members": "members:{load}",           # consumer group: live consumer pods (sorted set, score = last seen)
    "idle": "idle:{load}:{pod}",
    "produced": "stat:{load}:produced",
    "processed": "stat:{load}:processed",
    "shop": "shop:{load}:{pod}",
    "users_stat": "usersstat:{load}",
    "diary": "diary",
}


def key(name, **kw):
    return KEYS[name].format(**kw)


class PortForwardBridge:
    """Local TCP port -> Redis pod port, tunnelled through the Kubernetes API (websocket port-forward).

    Every new local connection opens a fresh port-forward to the CURRENT Redis pod, so a restarted Redis pod or a
    dropped tunnel is picked up automatically when redis-py reconnects.
    """

    def __init__(self, core_api, namespace, pod_lookup, remote_port=6379):
        self.core, self.ns, self.pod_lookup, self.remote_port = core_api, namespace, pod_lookup, remote_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.stopped = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._pipe, args=(conn,), daemon=True).start()

    def _pipe(self, conn):
        from kubernetes.stream import portforward
        pf = None
        try:
            pod = self.pod_lookup()
            pf = portforward(self.core.connect_get_namespaced_pod_portforward, pod, self.ns,
                             ports=str(self.remote_port))
            remote = pf.socket(self.remote_port)
            remote.setblocking(True)
            socks = [conn, remote]
            while not self.stopped.is_set():
                r, _, x = select.select(socks, [], socks, 1.0)
                if x:
                    break
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (remote if s is conn else conn).sendall(data)
        except Exception:  # noqa: BLE001 — a broken tunnel just closes this connection; redis-py reconnects
            return
        finally:
            for c in (conn,):
                try:
                    c.close()
                except OSError:
                    pass
            if pf is not None:
                try:
                    pf.close()
                except Exception:  # noqa: BLE001
                    pass

    def close(self):
        self.stopped.set()
        try:
            self.sock.close()
        except OSError:
            pass
