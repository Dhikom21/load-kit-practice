# Load Scenario Kit — one-command automation (LOAD_KIT_PLAN.md section 17)
PY ?= python3
SCENARIO ?= midnight-pile-up
SEED ?= 42
PORT ?= 8765
IMAGE ?= loadkit-worker:0.1.0
KIND_CLUSTER ?= loadkit
KUBECONFIG_ARG = $(if $(KUBECONFIG),--kubeconfig "$(KUBECONFIG)",)
METRICS_SERVER = https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

.PHONY: ci-local student-copy help ui run validate timeline analyze compare snapshot reset test image kind-up kind-load kind-metrics kind-down calibrate

help:            ## list commands
	@grep -E '^[a-z-]+:.*##' Makefile | sed 's/:.*##/\t/'

ui:              ## start the UI on http://127.0.0.1:$(PORT)  (KUBECONFIG=path to use a specific cluster)
	$(PY) -m loadkit serve --port $(PORT) $(KUBECONFIG_ARG)

run:             ## headless run on the cluster: make run SCENARIO=backup-snowball SEED=7
	$(PY) -m loadkit run $(SCENARIO) --seed $(SEED) $(KUBECONFIG_ARG)

validate:        ## validate all scenarios (no cluster needed)
	$(PY) -m loadkit validate

timeline:        ## print the planned timeline (no cluster needed)
	$(PY) -m loadkit timeline $(SCENARIO) --seed $(SEED)

analyze:         ## likely causes of a run; confirm with BASELINE=<same seed, one load removed>
	$(PY) -m loadkit analyze $(RUN) $(if $(BASELINE),--baseline $(BASELINE),)

compare:         ## make compare RUN_A=... RUN_B=...
	$(PY) -m loadkit compare $(RUN_A) $(RUN_B)

snapshot:        ## one self-contained HTML page of a finished run: make snapshot RUN=...
	$(PY) -m loadkit snapshot $(RUN)

reset:           ## delete everything the kit created on the cluster
	$(PY) -m loadkit reset $(KUBECONFIG_ARG)

student-copy:    ## the kit for students without the memory scenarios: make student-copy DEST=../load-kit-students
	$(PY) -m loadkit student-copy $(DEST)

ci-local:        ## run the same checks as the pipeline on your own computer (check + test + simulate vs nothing)
	$(PY) -m ruff check .
	$(PY) -m loadkit validate
	$(PY) ci/policy.py
	$(PY) -m unittest discover -s tests -t .
	$(PY) ci/simulate.py run --out ci-out/head

test:            ## unit tests (no cluster needed)
	$(PY) -m unittest discover -s tests -t .

image:           ## build the worker image
	docker build -t $(IMAGE) worker

kind-up:         ## create a local cluster (Docker + kind), install metrics-server, load the worker image
	kind create cluster --config deploy/kind.yaml
	$(MAKE) kind-metrics image kind-load

kind-metrics:    ## install metrics-server (kind needs --kubelet-insecure-tls)
	kubectl apply -f $(METRICS_SERVER)
	kubectl -n kube-system patch deployment metrics-server --type=json \
	  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'

kind-load:       ## copy the worker image into the kind nodes
	kind load docker-image $(IMAGE) --name $(KIND_CLUSTER)

kind-down:       ## delete the local cluster
	kind delete cluster --name $(KIND_CLUSTER)

calibrate:       ## how fast is the experiment node's CPU (runs one pod)
	kubectl run loadkit-calibrate --rm -i --restart=Never --image=$(IMAGE) \
	  --overrides='{"spec":{"nodeSelector":{"role":"experiment"}}}' -- calibrate --seconds 3
