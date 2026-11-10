# Architecture

> **Status: Phase 1 built** (testbed, observability, load generator). The
> incident library, agent and harness sections are still design. Each section is
> updated when its phase lands, and anything marked "to verify" is an assumption
> that a later phase must confirm by running it.

## What this is

An AI on-call agent that diagnoses and remediates failures injected into a small
Go microservice system on local Kubernetes, plus a benchmark that measures how
well the agent does across Claude models. Three parts, deliberately separated:

| Part | Language | Role |
|---|---|---|
| **Testbed** | Go | The system under test: 4 services, data stores, telemetry, load. |
| **Incident library** | YAML + Python | Declarative, reversible failures with ground truth. |
| **Agent + harness** | Python | Investigates through tools; the harness scores it. |

The agent only ever sees the testbed through its tools. It never reads the
incident library, and the harness never tells it what was injected.

## Diagram

```mermaid
flowchart TB
  subgraph host["Host (macOS)"]
    harness["Bench harness<br/>reset → inject → run agent → score → cleanup"]
    injector["Incident injector<br/>(reads incidents/*.yaml)"]
    agent["On-call agent<br/>tool loop + guardrails"]
    api(["Anthropic API"])
    traces[("runs/<br/>traces + reports")]
  end

  subgraph cluster["k3d cluster (single node)"]
    subgraph lgns["namespace: loadgen"]
      lg["loadgen"]
    end
    subgraph tb["namespace: testbed  (the only namespace the agent can touch)"]
      lg -->|HTTP| gw["api-gateway"]
      gw -->|"gRPC (orders)"| ord["order-service"]
      gw -->|"gRPC (product reads)"| inv["inventory-service"]
      ord -->|"gRPC (reserve stock)"| inv
      ord --> pg[("Postgres")]
      inv --> pg
      inv --> rd[("Redis cache")]
      ord -->|orders.created| js[["NATS JetStream"]]
      js --> nw["notification-worker"]
      nw --> pg
    end
    subgraph ob["namespace: observability"]
      otel["OTel Collector"] --> loki["Loki"]
      otel --> tempo["Tempo"]
      prom["Prometheus"]
      graf["Grafana"]
    end
    subgraph ch["namespace: chaos  (invisible to the agent)"]
      net["netem helper"]
    end
  end

  harness --> injector
  harness --> agent
  injector -->|"kubectl / SQL / redis-cli / tc"| tb
  injector --> net
  agent <-->|"messages + tool calls"| api
  agent -->|"read: PromQL, LogQL, traces"| ob
  agent -->|"read + gated writes<br/>(scoped ServiceAccount)"| tb
  agent --> traces
  harness -->|"independent recovery check (PromQL)"| prom
  tb -. "metrics, logs, traces" .-> ob
```

## Testbed

**Request flow.** `loadgen` sends 10 requests per second to `api-gateway` over
HTTP: 70% product lookups, 20% new orders, 5% order lookups, 5% order lists.

| Route | Path through the system |
|---|---|
| `GET /api/products/{sku}` | gateway → `inventory-service` → Redis cache, falling back to Postgres |
| `POST /api/orders` | gateway → `order-service` → `inventory-service` (reserve stock) → Postgres, then publish `orders.created` to NATS JetStream |
| `GET /api/orders/{id}`, `GET /api/customers/{id}/orders` | gateway → `order-service` → Postgres |

`notification-worker` consumes `orders.created` with a durable pull consumer and
records each notification in Postgres.

This gives one synchronous chain three services deep (for cascading failures and
latency propagation) and one asynchronous branch (for consumer lag), with a
cache and a database that can each fail in their own ways. The gateway reads
products from `inventory-service` directly rather than through `order-service`,
so a fault in one of the two back-end services degrades some routes and not
others, which is what makes the per-route panels worth looking at.

**Stack.** Go 1.26, one module, one image holding all five binaries (each
Deployment picks its binary with `command`): one build, and one image to load
into the cluster. Standard library `net/http` and `log/slog` (JSON logs carrying
`trace_id` and `span_id`), `grpc-go`, `pgx`, `go-redis`, `nats.go`,
`prometheus/client_golang`, and the OpenTelemetry SDK.

**Trace context over NATS.** The trace has to survive the hop through the
message broker for one trace to cover a whole order. OpenTelemetry's stock
header carrier writes `Traceparent`; NATS header names are case-sensitive and
the server rewrites that header to lower-case `traceparent`, so the stock
carrier silently finds nothing on the consuming side. `internal/events` has its
own small carrier and a round-trip test for this.

**Cluster.** k3d (k3s in Docker), single node. Chosen over kind because k3s
idles at a lower memory footprint, and this project has to fit on an 8 GB
laptop. Manifests are plain YAML with kustomize (built into `kubectl`), not
Helm: every line deployed is in the repo and readable.

`make up` writes a kubeconfig containing only this cluster into the repository
directory (gitignored), and every command uses that file. `~/.kube/config` is
never read or written, so nothing here can act on another cluster by mistake.
All published ports are bound to 127.0.0.1.

**Deploy order.** `make deploy` applies Postgres, Redis and NATS first and waits
for them, then applies everything else. The services would get there anyway by
retrying and restarting, but then a freshly created cluster shows a different
restart count on healthy pods each time, and restart counts are evidence the
agent reads.

**Probes.** Readiness turns true once a service has connected to its
dependencies at startup and does not re-check them afterwards: if every replica
went unready whenever Postgres was slow, the service would vanish from its
Service instead of failing requests individually and visibly. Liveness is
deliberately slow: a restart only after a full minute without an answer. It exists to catch a hung
process, not a slow one. This was learned by breaking it: with the default
liveness settings, a 20-second stall of the kubelet itself (the host was short
of memory during a large compile) made every probe time out at once, and every
healthy Go container was restarted in the same second.

**The load generator has its own namespace** (`loadgen`), outside the one the
agent can touch. It is the customer, not part of the system being operated.

**Observability, sized for 8 GB.**

| Signal | Component | Notes |
|---|---|---|
| Metrics | Prometheus, kube-state-metrics, kubelet cAdvisor, exporter sidecars for Postgres, Redis and NATS | Plain Prometheus, 10 s scrape, 12 h retention. No Alertmanager: Prometheus evaluates the alert rules in `deploy/observability/config/alerts.yml` every 10 s and the harness reads their state from its API. |
| Logs | Loki (single binary) | Filesystem storage, 24 h retention. |
| Traces | Tempo (single binary) | Local disk, 12 h retention. Chosen over Jaeger because Grafana links traces to logs natively. |
| Collection | One OTel Collector (DaemonSet) | Tails container log files to Loki and receives OTLP traces for Tempo. Metrics do not pass through it. |
| Dashboards | Grafana | Datasources and one dashboard provisioned from files in the repo. The dashboard JSON is generated by `deploy/observability/dashboards/generate.py`, so its queries are reviewable as code. |

**Telemetry is scoped on purpose.** The collector reads only
`/var/log/pods/testbed_*`, kube-state-metrics watches only the `testbed`
namespace, and Prometheus discovers pods only in `testbed`, `loadgen` and
`observability`. Whatever the incident injector does in the `chaos` namespace
cannot show up in a log line or a metric, so the agent cannot find the answer by
reading the injector's telemetry.

**Three things the collector does that were learned by running it:**

- It lower-cases log severity. Go's `slog` writes `ERROR` and Loki's own
  detection on plain-text lines yields `error`; without this, one filter cannot
  cover the namespace.
- It keeps its read positions on the node's disk. A restarted collector
  otherwise re-reads every log file from the top, which was measured to trip its
  memory limiter and then Loki's ingestion limits.
- It caps batch size, because an uncapped backlog went to Loki as requests
  larger than Loki's per-second ingestion limit and was rejected whole.

`make verify` (`scripts/verify_testbed.py`) is the acceptance check for all of
this: it sends real requests through the gateway and then asserts that
Prometheus has every expected scrape target and live traffic, that every query
on the dashboard returns data, that Loki has logs from each service with trace
IDs and from no other namespace, that Tempo holds a single trace spanning all
four services, and that Grafana can reach all three data sources.

**Memory.** Every container has an explicit memory request and limit. There are
no CPU limits: on a single shared node they would only add throttling noise, and
a CPU limit is something an incident can introduce.

Memory in MiB. "Peak" is the highest working set cAdvisor reported for any
replica in the first 76 minutes of a fresh cluster at the default load. The host
was short of memory for most of that time (see "Known limits" below), so read
the peaks as "under stress", not as a quiet-host baseline. Raw data:
[`results/phase1-soak/`](../results/phase1-soak/).

| Container | Replicas | Request | Limit | Peak |
|---|---|---|---|---|
| tempo | 1 | 96 | 384 | 371 |
| grafana | 1 | 96 | 256 | 221 |
| loki | 1 | 96 | 256 | 213 |
| otel-collector | 1 | 96 | 200 | 152 |
| postgres | 1 | 192 | 384 | 124 |
| prometheus | 1 | 128 | 384 | 114 |
| order-service | 2 | 32 | 96 | 31 |
| api-gateway | 2 | 32 | 96 | 30 |
| notification-worker | 1 | 32 | 96 | 30 |
| inventory-service | 2 | 32 | 96 | 29 |
| nats | 1 | 32 | 128 | 28 |
| nats-exporter | 1 | 16 | 64 | 22 |
| loadgen | 1 | 24 | 64 | 22 |
| kube-state-metrics | 1 | 24 | 64 | 21 |
| redis-exporter | 1 | 16 | 64 | 18 |
| postgres-exporter | 1 | 24 | 64 | 15 |
| redis | 1 | 32 | 128 | 15 |
| netem-helper (namespace `chaos`) | 1 | 8 | 32 | added in Phase 2, after this measurement |
| **Total across replicas** | | **1104** | **3144** | |

The whole node, k3s included, peaked at a 2500 MiB working set inside the
4 GB Docker VM.

Go processes (the collector, Tempo, Loki and the testbed services) get a
`GOMEMLIMIT` at roughly 80% of their container limit, so the runtime collects
garbage harder near the ceiling instead of being OOM-killed. It is a soft limit
and not a guarantee, as Tempo shows below.

**Known limits of a laptop-sized testbed.**

- *Warm-up.* For the first minutes after `make up`, while everything starts at
  once, the gateway returns some errors. The harness must wait for a
  steady-state check to pass before injecting anything (Phase 5).
- *Host memory.* A 4 GB VM on an 8 GB Mac leaves little for anything else. With
  a browser and an editor open, macOS swapped the VM's memory in and out
  continuously, and the testbed slowed down as a whole: every container's CPU
  use rose several-fold at the same moments, including idle ones. In two
  25-minute runs with no fault injected, the gateway returned 504 for 1.5% and
  6.5% of requests, with a median p99 of 1.5 s and 2.5 s. In a quiet stretch of
  the first run the p99 was 25 to 56 ms with no errors, which is what the
  testbed does when the host lets it. **A benchmark run is only meaningful on a
  quiet host.**
- *Host stalls restart containers.* During a large compile on the Mac the
  kubelet missed its own lease renewals and healthy containers were restarted.
  Liveness probes are now tolerant of this: no testbed container restarted in
  either run above. *To do in Phase 5:* the harness records restart counts,
  scrape gaps and baseline error rate around each incident and discards, rather
  than scores, a run where the platform itself misbehaved.
- *Tempo is the tightest fit.* It stores very little data at this load, but
  writing and compacting its Parquet blocks allocates memory in bursts: those
  two code paths made 72% of all allocations in a heap profile of the first
  run, and the live heap was 172 MiB at one profile and 71 MiB at another. Its
  working set peaked at 356 MiB of 384 in the first run. In the second it
  restarted twice, at least once OOM-killed. A smaller Parquet row group
  (`parquet_row_group_size_bytes`) was tried in that second run and reverted:
  it did not lower the peak. *Open:* re-measure on a quiet host before deciding
  between a higher limit and a different setting.
- *OOM kills do not show in cAdvisor's counter.* `container_oom_events_total`
  stayed at zero although Tempo was OOM-killed. Verified in Phase 2: it also
  stayed at zero while the inventory service was OOM-killed eight times. The
  signal that works is kube-state-metrics'
  `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}`,
  together with the restart counter; `kubectl describe pod` shows the same
  thing as `Last State: Terminated, Reason: OOMKilled, Exit Code: 137`
  ([data](../results/phase2-incidents/calibration/memory_limit_probe.json)).

## Incident library

Each incident is one YAML file in `incidents/`, named after its `id`. The
format is defined by pydantic models
([`src/oncall/chaos/incident.py`](../src/oncall/chaos/incident.py)); the JSON
schema generated from them is checked in as `incidents/schema.json`, and a test
fails if the two drift apart.

| Field | Meaning |
|---|---|
| `id`, `title`, `description` | For people. The description says what a user or operator would experience. |
| `scenarios` | Symptom classes from the project brief (crash loop, consumer lag, ...), used only to report coverage. |
| `inject`, `cleanup` | Lists of typed steps, see below. |
| `ground_truth.causes` | One or more (component, fault category) pairs from a closed vocabulary. See [`RUBRIC.md`](RUBRIC.md). |
| `affected_services` | Components whose behaviour degrades. |
| `detection` | The alerts expected to fire (the first is the page the agent starts from), a deadline, and evidence queries. |
| `acceptable_remediations` | Write actions that fix it, or `manual` when none of the agent's tools can. |

Design rules:

1. **Inject from outside where possible.** Dropping a Postgres index, shrinking
   Redis `maxmemory`, changing a resource limit, or pushing a bad config value
   are real operations, not test hooks. A few faults (a memory leak) need a code
   path in a service, enabled by config.
2. **No tell-tale names.** No env var, label, annotation, log line, or metric
   may name the fault. `FAULT_LEAK=true` would turn diagnosis into string
   matching. Config that enables a buggy path looks like an ordinary setting.
   Steps cannot set labels or annotations at all, and the parser rejects
   environment variable names containing words such as `FAULT` or `CHAOS`.
3. **Config changes are real rollouts.** Service config lives in the Deployment
   spec, so a bad config push creates a new ReplicaSet revision. That makes
   "recent deploy history" and "rollback" the genuine Kubernetes operations.
   Faults injected elsewhere (database, Redis, network) leave no entry in
   deploy history, which is also realistic.
4. **Cleanup is verified, not assumed.** See "Proving an incident works" below.
   An incident that does not return the cluster to healthy is a bug in the
   incident.

**Steps are a closed list of typed actions, not shell.** An incident file can
only say things like "set this environment variable on this Deployment"; code
turns that into a `kubectl` argument list and runs it without a shell
([`steps.py`](../src/oncall/chaos/steps.py)). A typo is a validation error
instead of a command that does something else, and reviewing an incident does
not require reading shell.

| Inject step | What it does |
|---|---|
| `set_env` | Change environment variables of a service (a rollout). |
| `set_resources` | Change a service container's CPU or memory requests and limits (a rollout). |
| `scale` | Change a service's replica count. |
| `postgres_sql` | Run one SQL statement with `psql` inside the Postgres pod. |
| `redis_command` | Run one Redis command with `redis-cli` inside the Redis pod. |
| `netem` | Delay or drop packets leaving one or all pods of a service. |

**Cleanup states the end state instead of undoing steps.** The cleanup step for
anything done to a Deployment is `restore_deployment`: replace the live object
with the one rendered from `deploy/`. It is therefore also correct after the
agent has rolled back, scaled or restarted things in between, and harmless to
run twice. Two details:

- It uses `kubectl replace`, not `apply`. Apply merges, so it would restore a
  changed value but leave behind a field that an incident (or the agent)
  added, such as a CPU limit.
- It then deletes the ReplicaSets left at zero replicas. Otherwise the faulty
  revision of one incident would still be in the rollout history during the
  next: a red herring at best and a hint at worst. One revision remains, the
  good one, which is also what a rollback in the next incident returns to.

The other cleanup steps are `clear_netem`, `postgres_sql` and `redis_command`.
The data stores have no `restore_deployment`: their data is on `emptyDir`, and
recreating their pods would wipe it.

**Network faults** use Linux `tc netem` on the `eth0` of a target pod, applied
from outside the pod by a helper in the `chaos` namespace
([`deploy/chaos/`](../deploy/chaos/)). The helper is a pod that sleeps (request
8 MiB, limit 32 MiB) and shares the node's PID namespace; the injector
`kubectl exec`s a small script in it that finds a process of the target pod by
the pod UID in its cgroup path, enters that process's network namespace with
`nsenter`, and runs `tc`. Nothing about the target pod's spec, status or events
changes, which is what makes the fault realistic to diagnose. The helper is the
one privileged workload in the cluster; that is acceptable only because the
cluster is a throwaway on a laptop. Docker Desktop's kernel
(6.10.14-linuxkit) has `sch_netem` built in; the k3s node image has no `tc`
binary, which is why the helper has its own image (Alpine plus `iproute2-tc`).

**Alert rules are symptoms.** The seven rules in `alerts.yml` describe what a
user or operator would notice (error rate, latency, restarts, unavailable
replicas, consumer lag, cache hit ratio, slow statements) and never a cause.
Each threshold is set against a measured healthy baseline, noted next to the
rule. One was wrong at first: `DatabaseQuerySlow` asked for a median above
50 ms, a table scan of `orders` takes 42 to 58 ms here, and the alert never
fired. It is now 25 ms, against a baseline below 10 ms. A threshold may be
moved like this when the testbed's own signal shows it is misplaced; it may not
be moved to make an incident easier or harder for the agent.

**"Healthy" is defined once** ([`health.py`](../src/oncall/chaos/health.py)):
every Deployment in `testbed` has the replica count its manifest asks for, with
all replicas updated and available, and no alert is pending or firing. Because
latency spikes come and go on this machine, a single clean reading is not
enough: the state has to hold for a stretch (60 s before an injection, 30 s
after a cleanup).

### Proving an incident works

`make incident-check INCIDENT=<id>` ([`check.py`](../src/oncall/chaos/check.py))
runs one incident against the live cluster with no agent involved:

1. Wait until the cluster has been healthy for 60 s. If that does not happen
   within 10 minutes, or if one of the incident's evidence queries already
   returns something, record `not_run` and inject nothing.
2. Record the gateway's error share and p99 as the baseline, then inject.
3. Poll until every expected alert is firing and every evidence query has
   returned a result, or the incident's deadline passes.
4. Run the cleanup steps, whatever happened before.
5. Wait until the cluster has been healthy, **and every evidence query has
   returned nothing**, for 30 s in a row.

An evidence query is written like an alert expression: it returns a result only
while the fault is visible. Using the same queries in steps 3 and 5 is what
makes "cleans up fully" a measurement against telemetry instead of an
assumption that the cleanup commands worked. Every value in the resulting JSON
record is read from Prometheus, Loki, the Kubernetes API or the clock during
the run.

The records for the first eight incidents, and an account of the three that
failed a first pass and why, are in
[`results/phase2-incidents/`](../results/phase2-incidents/README.md). Two
things from those runs matter for later phases:

- An incident usually raises more than its "own" alert. A slow inventory
  replica raises latency and error-rate alerts at the gateway and makes
  `stock.get` look like a slow database statement. The agent starts from a
  symptom that several different faults share.
- The check ran each incident once. Phase 5 repeats runs before quoting any
  timing as typical.

## Agent

**Loop.** A hand-written tool-use loop over the Anthropic Messages API (official
Python SDK). Hand-written rather than the SDK's tool runner because the loop is
where caps, tracing, and the write gate live, and those need to be code I can
point at and test. The conversation history is append-only.

**Trigger.** The agent starts from an alert payload containing only a symptom
(for example "5xx rate on api-gateway above 5% for 2 minutes"), the way a human
on-call would.

**Tools take typed parameters, never commands.** The model cannot pass a shell
string or a raw `kubectl` line. Each tool has a JSON schema with enumerated
verbs and resource kinds; code builds the argument list and runs it without a
shell.

| Read-only | Write (gated) |
|---|---|
| `query_metrics` (PromQL) | `rollback_deployment` |
| `search_logs` (LogQL) | `scale_deployment` |
| `get_trace` / `search_traces` | `restart_pod` |
| `kube_get`, `kube_describe`, `kube_events` | `revert_config` |
| `deploy_history` | |

Tool output is bounded. When a result is cut, the agent is told how much was
omitted so it can narrow the query; nothing is dropped silently.

**Guardrails are enforced twice.**

*In code*, every write request passes through one function, in this order. Any
step can reject, and every rejection is recorded and counted in the benchmark:

1. Schema validation of the parameters.
2. Scope: namespace must be `testbed`; the target must be a known resource.
3. Allow-list: the action and its parameter values must be explicitly permitted.
4. Budget: write-action cap, tool-call cap, wall-clock cap.
5. Policy mode:
   - `observe` rejects all writes.
   - `propose` records the action and its diff for a human to approve.
   - `auto` executes only actions classified low-risk; the rest become proposals.
6. Server-side dry run and diff, saved to the trace before anything executes.
7. Execute, then re-read the resource to confirm the change applied.

*In Kubernetes*, the agent uses its own ServiceAccount. Its Role exists only in
`testbed`, grants read verbs plus the few write verbs the tools need, and
excludes Secrets. `observe` mode uses a read-only identity. If the code gate had
a bug, the API server would still refuse.

**Output.** The agent finishes by calling a `submit_report` tool whose strict
schema is the incident report: timeline, evidence, root-cause hypothesis
(component, fault category, free-text explanation, confidence), actions taken or
proposed, and recovery verification. Every evidence item must cite the ID of a
tool call from the run; code rejects citations to calls that never happened. The
report is written as JSON and rendered to Markdown.

**Trace.** Every model request, response, tool call, tool result, and guardrail
decision is appended to a JSONL file under `runs/`. The API key is never written
there or to logs. A recorded trace can be replayed through the loop without
calling the API, which is how the loop is unit-tested and how CI runs with no
key.

## Evaluation harness

For each incident: reset to healthy → inject → wait for the alert → run the
agent → score → cleanup → verify healthy.

| Metric | Source |
|---|---|
| Root-cause accuracy | Deterministic comparison of the report's structured cause(s) to ground truth. |
| Time to diagnosis | Wall clock from agent start to `submit_report`. |
| Tool calls, rejected actions | Counted from the trace. |
| Recovery success | Measured by the harness from Prometheus, independent of what the agent claims. |
| Tokens and cost | Token counts as reported by the API, multiplied by a checked-in price table that records its source and retrieval date. |

**Rubric ([`docs/RUBRIC.md`](RUBRIC.md), fixed in Phase 2 before any agent existed).** Scoring is by
code, not by a model, so the same report always gets the same score. Ground
truth is a set of causes; the report gives a set of causes; credit is awarded
for matching component and matching fault category, with set-based precision and
recall for multi-cause incidents. The fault taxonomy is fixed and larger than
the set of faults actually used, so the list of categories is not an answer key.

**Router.** A cheap model triages first and either submits a report or
escalates, with its findings, to Fable. Benchmarked as its own configuration
against every single-model run.

**Budget.** Total API spend for the project is capped at $50. Consequences:
development uses Haiku and replayed traces; the harness has a hard `--max-cost`
abort; per-incident cost is measured on a few incidents before any full run and
the projection is reviewed first; expensive models may run a fixed, documented
subset chosen before seeing results. One trial per incident means no variance
estimate, and the report will say so.

## Repository layout

```
.
├── Makefile                 # up, down, verify, test, lint, inject, cleanup,
│                            # incident-check; later bench
├── CLAUDE.md                # conventions for AI-assisted work on this repo
├── .env.example
├── pyproject.toml           # the Python package `oncall` and its tool config
├── docs/                    # ARCHITECTURE.md, RUBRIC.md, later GUARDRAILS.md
├── scripts/                 # verify_testbed.py: the Phase 1 acceptance check
├── testbed/                 # Go module
│   ├── cmd/                 # api-gateway, order-service, inventory-service,
│   │                        # notification-worker, loadgen
│   ├── internal/            # shared: config, telemetry, ops (health, metrics),
│   │                        # rpc, store, events, retry
│   ├── proto/               # gRPC definitions; generated code in gen/
│   └── Dockerfile
├── deploy/                  # kustomize manifests
│   ├── cluster/             # k3d cluster config
│   ├── testbed/             # stores/ (Postgres, Redis, NATS), services/
│   ├── loadgen/
│   ├── observability/       # Prometheus, Loki, Tempo, collector, Grafana,
│   │                        # kube-state-metrics; config/ and dashboards/
│   ├── rbac/                # (Phase 3) agent ServiceAccounts and Roles
│   └── chaos/               # netem helper: manifest, image, script
├── incidents/               # one YAML per incident + JSON schema
├── src/oncall/              # Python package
│   ├── agent/               # loop, llm client, tools/, guardrails/, report, trace
│   ├── chaos/               # incident parsing, inject, cleanup, health,
│   │                        # incident-check
│   └── bench/               # harness, scoring, report generation, router
├── tests/                   # pytest: guardrails, scoring, incident parsing, replay
├── results/                 # committed outputs of real benchmark runs
└── runs/                    # gitignored: full traces
```

## Language choices

**Go for the testbed** because the services are the kind of system Go is used
for, and the failure modes (connection pools, goroutine and memory behaviour,
gRPC deadlines) are real ones.

**Python for the agent and harness** because the Anthropic Python SDK is the
most complete, prompt and tool iteration is fastest there, and scoring reports
with tables and charts are a few lines with pandas and matplotlib. The cost is
two toolchains in one repo. Guardrails being in Python does not make them
softer: they are ordinary functions with unit tests, backed by Kubernetes RBAC.

## Open items (decided in the phase noted)

- Risk classification of each write action for `auto` mode (Phase 4).
- Which policy mode the benchmark uses for recovery scoring, and whether
  `propose` mode is benchmarked with a scripted approver (Phase 5).
- Which incidents form the fixed subset for expensive models, if the budget
  requires one (Phase 5, before any results exist).
