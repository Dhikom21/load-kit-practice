#!/usr/bin/env bash
# Prepare a shared cluster for the D0020E student groups — one run, every group gets
#   * its own node            (labelled role=experiment-1 .. role=experiment-N)
#   * its own scoped key      (ServiceAccount + ClusterRoleBinding + kubeconfig, token valid 30 days)
#   * its own 2-line config   (group: gN + kubernetes.node in loadkit.yaml)
#
# Usage:
#   NODES="node-1 node-2 node-3 node-4 node-5" ./deploy/provision-groups.sh
#
# Idempotent: safe to re-run at any time (for example to re-issue keys after they expire).
#
# Environment (all optional except NODES):
#   NUM_GROUPS   number of groups                          (default 5)
#   NODES        space-separated node names, one per group (required)
#   TOKEN_HOURS  key lifetime in hours                     (default 720 = 30 days, the API server maximum)
#   IMAGE        worker image the groups will run          (default ghcr.io/dhikom/loadkit-worker:latest)
#   OUT_DIR      where keys + snippets are written         (default deploy/groups, relative to the repo root)
#
# The script does NOT build a cluster, install metrics-server, or push the worker image.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$ROOT"

# (not named GROUPS: bash already uses that variable for the user's group IDs)
NUM_GROUPS=${NUM_GROUPS:-5}
NODES=${NODES:-}
TOKEN_HOURS=${TOKEN_HOURS:-720}
IMAGE=${IMAGE:-ghcr.io/dhikom/loadkit-worker:latest}
OUT_DIR=${OUT_DIR:-deploy/groups}
MGMT_NS=loadkit-groups

# ------------------------------------------------- sanity checks
command -v kubectl >/dev/null || { echo "kubectl not found in PATH" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found (needed to build the kubeconfig files)" >&2; exit 1; }
python3 -c "import yaml" 2>/dev/null || { echo "python3 needs the yaml module:  pip install pyyaml" >&2; exit 1; }
kubectl get nodes >/dev/null 2>&1 || {
  echo "cannot reach the cluster with your current kubeconfig (kubectl get nodes failed)" >&2
  echo "log in as the cluster administrator first, then re-run" >&2
  exit 1
}
if [[ -z $NODES ]]; then
  echo "NODES is required — one node name per group, space-separated. Your nodes:" >&2
  kubectl get nodes -o name >&2
  exit 1
fi
read -r -a NODE_LIST <<<"$NODES"
if (( ${#NODE_LIST[@]} != NUM_GROUPS )); then
  echo "NODES has ${#NODE_LIST[@]} names but NUM_GROUPS=$NUM_GROUPS — they must match, one node per group" >&2
  kubectl get nodes -o name >&2
  exit 1
fi
for n in "${NODE_LIST[@]}"; do
  kubectl get node "$n" >/dev/null 2>&1 || { echo "node '$n' not found in the cluster" >&2; kubectl get nodes -o name >&2; exit 1; }
done
if (( TOKEN_HOURS > 720 )); then
  echo "TOKEN_HOURS cannot exceed 720 (30 days) — the maximum the API server will issue" >&2
  exit 1
fi
mkdir -p "$OUT_DIR"

# ------------------------------------------------- the ClusterRole (once for all groups)
echo "→ ClusterRole loadkit-student (the smallest set of permissions the kit needs)"
kubectl apply -f - <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: loadkit-student
  labels: {app.kubernetes.io/part-of: loadkit}
rules:
  # every rule below is one API call the kit actually makes (see loadkit/plugins/runners/kubernetes.py)
  - apiGroups: [""]
    resources: [namespaces]
    verbs: [get, list, watch, create, delete]
  - apiGroups: [""]
    resources: [secrets, services]
    verbs: [get, list, watch, create]
  - apiGroups: [""]
    resources: [pods]
    verbs: [get, list, watch]
  # (the API server authorises port-forwarding with the verb get, not create — verified on the live cluster)
  - apiGroups: [""]
    resources: [pods/portforward]
    verbs: [get]
  - apiGroups: [""]
    resources: [events]
    verbs: [get, list, watch]
  - apiGroups: [""]
    resources: [nodes]
    verbs: [get, list, watch]
  - apiGroups: [apps]
    resources: [deployments]
    verbs: [get, list, watch, create, delete]
  - apiGroups: [apps]
    resources: [deployments/scale]
    verbs: [get, patch, update]
  - apiGroups: [networking.k8s.io]
    resources: [networkpolicies]
    verbs: [get, list, watch, create, delete]
  - apiGroups: [batch]
    resources: [jobs]
    verbs: [get, list, watch, create, delete]
  - apiGroups: [scheduling.k8s.io]
    resources: [priorityclasses]
    verbs: [get, list, watch, create, delete]
  - apiGroups: [metrics.k8s.io]
    resources: [nodes, pods]
    verbs: [get, list]
EOF
kubectl get namespace "$MGMT_NS" >/dev/null 2>&1 || kubectl create namespace "$MGMT_NS"

# ------------------------------------------------- one helper: turn a token into a kubeconfig file
write_kubeconfig() { # $1 = SA token, $2 = output path
  local json
  json=$(kubectl config view --minify --flatten -o json 2>/dev/null) || {
    echo "   could not read your current kubeconfig (kubectl config view failed)" >&2
    return 1
  }
  # the python program comes from the heredoc, so the JSON travels via an environment variable
  KUBE_JSON="$json" python3 - "$1" "$2" <<'PY'
import base64, json, os, pathlib, sys
import yaml
cur = json.loads(os.environ["KUBE_JSON"])
token, out = sys.argv[1], sys.argv[2]
if not cur.get("clusters"):
    sys.exit("your kubeconfig has no cluster entry — run this script as the cluster administrator, not in-cluster")
c = cur["clusters"][0]["cluster"]
cluster = {"server": c["server"]}
if c.get("certificate-authority-data"):
    cluster["certificate-authority-data"] = c["certificate-authority-data"]
elif c.get("certificate-authority"):
    cluster["certificate-authority-data"] = base64.b64encode(
        pathlib.Path(c["certificate-authority"]).read_bytes()).decode()
elif c.get("insecure-skip-tls-verify"):
    cluster["insecure-skip-tls-verify"] = True
else:
    sys.exit("cannot find the cluster's CA certificate in your kubeconfig")
cfg = {
    "apiVersion": "v1",
    "kind": "Config",
    "clusters": [{"name": "loadkit", "cluster": cluster}],
    "users": [{"name": "loadkit", "user": {"token": token}}],
    "contexts": [{"name": "loadkit", "context": {"cluster": "loadkit", "user": "loadkit"}}],
    "current-context": "loadkit",
}
p = pathlib.Path(out)
p.write_text(yaml.safe_dump(cfg, sort_keys=False))
p.chmod(0o600)
PY
}

# ------------------------------------------------- per group: label, identity, key, snippet
for i in $(seq 1 "$NUM_GROUPS"); do
  g="g$i"
  node=${NODE_LIST[$((i-1))]}
  echo ""
  echo "→ group $g  (node: $node)"

  kubectl label node "$node" "role=experiment-$i" --overwrite

  kubectl apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: $g
  namespace: $MGMT_NS
  labels: {app.kubernetes.io/part-of: loadkit, loadkit/group: $g}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: loadkit-$g
  labels: {app.kubernetes.io/part-of: loadkit, loadkit/group: $g}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: loadkit-student
subjects:
  - kind: ServiceAccount
    name: $g
    namespace: $MGMT_NS
EOF

  token=$(kubectl create token "$g" -n "$MGMT_NS" --duration="${TOKEN_HOURS}h")
  write_kubeconfig "$token" "$OUT_DIR/$g.kubeconfig"
  echo "   key:   $OUT_DIR/$g.kubeconfig (valid ${TOKEN_HOURS}h)"

  cat > "$OUT_DIR/$g.loadkit.yaml.snippet" <<EOF
# group $g — the settings that change in loadkit.yaml (edit the existing lines, do not add a second kubernetes: block)
group: $g                 # was: null
kubernetes:
  node: role=experiment-$i          # was: null — this group's own node
  image: $IMAGE            # the shared worker image (built by CI on main)

# Then run the kit with YOUR key (the teacher handed you $g.kubeconfig):
#   python -m loadkit run normal-day-short --kubeconfig /path/to/$g.kubeconfig
#   python -m loadkit server               --kubeconfig /path/to/$g.kubeconfig
#   python -m loadkit reset                --kubeconfig /path/to/$g.kubeconfig
# or once per shell:  export KUBECONFIG=/path/to/$g.kubeconfig
EOF
  echo "   config: $OUT_DIR/$g.loadkit.yaml.snippet"
done

# ------------------------------------------------- verify every key works (and is really scoped)
echo ""
echo "Checking the keys against the cluster:"
fail=0
for i in $(seq 1 "$NUM_GROUPS"); do
  g="g$i"
  f="$OUT_DIR/$g.kubeconfig"
  if KUBECONFIG="$f" kubectl get nodes >/dev/null 2>&1; then nodes=ok; else nodes=FAIL; fail=1; fi
  # (kubectl auth can-i exits non-zero when the answer is "no", so we judge by the output:
  #  anything that is not a plain "no" — including an empty output — fails the check)
  can_delete=$(KUBECONFIG="$f" kubectl auth can-i delete nodes 2>/dev/null | head -n1) || true
  can_pods=$(KUBECONFIG="$f" kubectl auth can-i create pods --all-namespaces 2>/dev/null | head -n1) || true
  if [[ $can_delete != no || $can_pods != no ]]; then fail=1; fi
  printf "  %-4s  get nodes: %-4s   can delete nodes: %-3s   can create pods anywhere: %-3s\n" \
    "$g" "$nodes" "$can_delete" "$can_pods"
done

if (( fail )); then
  echo ""
  echo "PROBLEM: one or more checks failed — see above. Do not hand out the failing keys." >&2
  exit 1
fi

echo ""
echo "Done. Hand each group exactly one key file ($OUT_DIR/gN.kubeconfig) — never your admin kubeconfig."
echo "The keys are scoped to the kit's needs; students run everything through the kit (see deploy/GROUPS.md)."
