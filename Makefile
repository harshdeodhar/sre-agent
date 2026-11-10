# Entry points for the whole project. Run `make help` for the list.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

CLUSTER := sre-testbed
IMAGE   := sre-testbed/services:dev
# The netem helper used by network incidents (deploy/chaos).
NETEM_IMAGE := sre-testbed/netem:dev

# Every kubectl call in this project uses a kubeconfig that contains only the
# k3d cluster. ~/.kube/config is never read or written, so a mistake here
# cannot touch any other cluster the user has access to.
export KUBECONFIG := $(CURDIR)/sre-testbed.kubeconfig

# Tools installed into the repo (protoc plugins) come first on PATH.
export PATH := $(CURDIR)/bin:$(PATH)

GO_MODULE := github.com/harshdeodhar/sre-agent/testbed

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-16s %s\n", $$1, $$2}'

# ---- Cluster lifecycle -------------------------------------------------------

.PHONY: up
up: cluster image secrets deploy wait ## Create the cluster and deploy testbed, observability and load generator
	@echo
	@echo "Testbed is up."
	@echo "  API gateway  http://localhost:8080/api/products/SKU-0001"
	@echo "  Grafana      http://localhost:3000"
	@echo "  Prometheus   http://localhost:9090"
	@echo "Run 'make verify' to check that telemetry is flowing."

.PHONY: down
down: ## Delete the cluster and everything in it
	k3d cluster delete $(CLUSTER)
	rm -f $(KUBECONFIG)

.PHONY: cluster
cluster: ## Create the k3d cluster if it does not exist, and write its kubeconfig
	@if k3d cluster list $(CLUSTER) >/dev/null 2>&1; then \
		echo "cluster $(CLUSTER) already exists"; \
	else \
		k3d cluster create --config deploy/cluster/k3d.yaml; \
	fi
	@k3d kubeconfig get $(CLUSTER) > $(KUBECONFIG)
	@chmod 600 $(KUBECONFIG)

.PHONY: image
image: ## Build the services and netem-helper images and load them into the cluster
	docker build -t $(IMAGE) testbed
	docker build -t $(NETEM_IMAGE) deploy/chaos/netem
	k3d image import $(IMAGE) $(NETEM_IMAGE) --cluster $(CLUSTER)

.PHONY: secrets
secrets: ## Generate the Postgres password as a Kubernetes Secret (once per cluster)
	@kubectl apply -f deploy/testbed/namespace.yaml
	@# The password is random per cluster and exists only inside it. It is
	@# piped in on stdin so it never appears in a process list, in shell
	@# history, or in this repository.
	@if kubectl -n testbed get secret postgres-credentials >/dev/null 2>&1; then \
		echo "secret postgres-credentials already exists"; \
	else \
		openssl rand -hex 16 | tr -d '\n' | \
			kubectl -n testbed create secret generic postgres-credentials --from-file=password=/dev/stdin; \
	fi

.PHONY: deploy
deploy: ## Apply all manifests (safe to re-run after editing deploy/)
	@# Data stores first, and wait for them: see deploy/testbed/stores/kustomization.yaml.
	kubectl apply -k deploy/testbed/stores
	kubectl -n testbed wait --for=condition=Available deployment postgres redis nats --timeout=600s
	kubectl apply -k deploy

.PHONY: wait
wait: ## Wait until every workload is available
	kubectl -n observability wait --for=condition=Available deployment --all --timeout=600s
	kubectl -n observability rollout status daemonset/otel-collector --timeout=600s
	kubectl -n testbed wait --for=condition=Available deployment --all --timeout=600s
	kubectl -n loadgen wait --for=condition=Available deployment --all --timeout=600s
	kubectl -n chaos wait --for=condition=Available deployment --all --timeout=600s

.PHONY: redeploy
redeploy: image ## Rebuild the services image and restart the Go services with it
	kubectl -n testbed rollout restart deployment api-gateway order-service inventory-service notification-worker
	kubectl -n loadgen rollout restart deployment loadgen
	$(MAKE) wait

.PHONY: status
status: ## Show pods and their memory use
	kubectl get pods -n testbed -o wide
	kubectl get pods -n observability
	kubectl get pods -n loadgen
	kubectl get pods -n chaos
	@echo
	kubectl top pods -n testbed --containers || true
	kubectl top pods -n observability || true
	kubectl top node || true

.PHONY: verify
verify: ## Check that live traffic shows up in Prometheus, Loki, Tempo and Grafana
	python3 scripts/verify_testbed.py

# ---- Incidents -----------------------------------------------------------------

# The incident tooling (src/oncall/chaos), run in the project's virtualenv.
CHAOS := uv run python -m oncall.chaos
# Where `make incident-check` writes its records. runs/ is gitignored; records
# worth keeping are written to results/ by passing OUT=.
OUT ?= runs/incident-checks

.PHONY: incidents
incidents: ## Validate the incident library and list it
	$(CHAOS) validate
	$(CHAOS) list

.PHONY: inject
inject: ## Inject one incident: make inject INCIDENT=<id>
	$(CHAOS) inject $(INCIDENT)

.PHONY: cleanup
cleanup: ## Undo one incident: make cleanup INCIDENT=<id>
	$(CHAOS) cleanup $(INCIDENT)

.PHONY: incident-check
incident-check: ## Inject, confirm it shows in telemetry, clean up, confirm recovery: make incident-check INCIDENT=<id> (or INCIDENT=--all)
	$(CHAOS) check $(INCIDENT) --out $(OUT)

.PHONY: healthy
healthy: ## Report whether the testbed is in its intended, alert-free state
	$(CHAOS) status

# ---- Development ---------------------------------------------------------------

.PHONY: test
test: ## Run the Go and Python unit tests (no cluster or API key needed)
	cd testbed && go test ./...
	uv run pytest -q

.PHONY: lint
lint: ## Lint and format-check Go and Python, and check that the manifests render
	cd testbed && go vet ./...
	cd testbed && golangci-lint run ./...
	cd testbed && golangci-lint fmt --diff ./...
	ruff check scripts deploy src tests results
	ruff format --check scripts deploy src tests results
	kubectl kustomize deploy >/dev/null
	$(CHAOS) validate

.PHONY: proto
proto: ## Regenerate Go code from testbed/proto (needs protoc; installs the Go plugins into ./bin)
	GOBIN=$(CURDIR)/bin go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.12
	GOBIN=$(CURDIR)/bin go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.6.2
	cd testbed && protoc -I proto \
		--go_out=gen --go_opt=module=$(GO_MODULE)/gen \
		--go-grpc_out=gen --go-grpc_opt=module=$(GO_MODULE)/gen \
		proto/shop/v1/shop.proto

.PHONY: dashboards
dashboards: ## Regenerate the Grafana dashboard JSON from its generator script
	python3 deploy/observability/dashboards/generate.py
