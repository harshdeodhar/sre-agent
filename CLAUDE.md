# CLAUDE.md

Open-source AI on-call (SRE) agent with an incident benchmark. Portfolio
project: the owner must be able to defend every line in an interview. Read
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before making structural changes.

## Non-negotiable rules

1. **No fabricated results.** Every number in a report, README, or doc comes
   from a real run whose raw output is in `results/`. If something cannot run,
   say so plainly. Never hardcode, simulate, or estimate a benchmark result.
2. **Never tilt the benchmark.** Do not weaken tests, narrow scope, or
   special-case incidents to make the agent look better. The agent must never
   read `incidents/`, and nothing in the cluster may name the injected fault
   (no `FAULT_*` env vars, labels, annotations, log lines, or metric names).
3. **Local only.** Everything runs in the k3d cluster `sre-testbed`. No cloud
   resources, no external systems other than the Anthropic API.
4. **Secrets stay out of git and logs.** The API key comes from the
   `ANTHROPIC_API_KEY` environment variable only. Never hardcode it, print it,
   or write it to a trace.
5. **API budget: $50 total for the whole project.** Develop against
   `claude-haiku-5-5` and replayed traces. Before any run expected to cost more
   than about $2, show a projection based on measured per-incident cost and get
   approval.

## How to work

Work in the phases listed in the project brief. At the end of each phase: run
it, show the commands and their output as evidence, summarise what was built and
the decisions made, then **stop and wait for go-ahead**. Do not start the next
phase unprompted.

Prefer clarity over cleverness. Comment the *why* of non-obvious decisions.
Prefer boring, well-known libraries. Update `docs/ARCHITECTURE.md` when a design
decision changes, and resolve its "to verify" notes when a phase confirms them.

## Constraints of the dev machine

Apple M1, 8 GB RAM. The whole cluster should fit in a 4 GB Docker VM.

- Every container has explicit memory requests and limits.
- Single-binary Loki and Tempo, plain Prometheus. No Helm, no operators, no
  kube-prometheus-stack, no Chaos Mesh.
- Do not add a component without stating its memory cost.

## Layout

| Path | Contents |
|---|---|
| `testbed/` | Go module: services under `cmd/`, shared code under `internal/` |
| `deploy/` | kustomize manifests (cluster, testbed, loadgen, observability, rbac, chaos) |
| `scripts/` | `verify_testbed.py`, the acceptance check behind `make verify` |
| `incidents/` | One YAML per incident, validated against a JSON schema |
| `src/oncall/agent/` | Tool loop, tools, guardrails, report, trace |
| `src/oncall/chaos/` | Incident parsing, inject, cleanup, health definition, incident check |
| `src/oncall/bench/` | Harness, scoring, report generation, router |
| `tests/` | pytest |
| `results/` | Committed outputs of real benchmark runs |
| `runs/` | Gitignored per-run traces |

## Commands

Added as phases land; do not document a target here until it exists and works.

`make help` lists every target. All of them use the repo-local kubeconfig
(`sre-testbed.kubeconfig`); for ad-hoc `kubectl`, export `KUBECONFIG` to it.

| Command | What it does |
|---|---|
| `make up` | Create the cluster, build and load the image, deploy everything, wait until available |
| `make down` | Delete the cluster and its kubeconfig |
| `make verify` | Assert that live traffic shows up in Prometheus, Loki, Tempo and Grafana |
| `make status` | Pods, restarts, and memory use |
| `make deploy` | Re-apply manifests after editing `deploy/` |
| `make redeploy` | Rebuild the services image and roll the Go services |
| `make test` | Go and Python unit tests |
| `make lint` | `go vet`, `golangci-lint`, `ruff`, a kustomize render check, and incident validation |
| `make incidents` | Validate the incident library and list it |
| `make inject INCIDENT=<id>` | Inject one incident |
| `make cleanup INCIDENT=<id>` | Undo one incident |
| `make incident-check INCIDENT=<id>` | Inject, confirm the fault shows in telemetry, clean up, confirm recovery (`INCIDENT=--all` for every one, `OUT=<dir>` for the records) |
| `make healthy` | Report whether the testbed is at its manifest state with no alert pending or firing |
| `make dashboards` | Regenerate the Grafana dashboard JSON from `generate.py` |
| `make proto` | Regenerate gRPC code from `testbed/proto` |

Do not run a Go build, `make image`, `make redeploy`, `make test` or `make lint`
while a benchmark or a measurement is in progress: on this machine it starves
the cluster (see "Known limits" in `docs/ARCHITECTURE.md`).

Editing `alerts.yml` or `prometheus.yml` recreates the Prometheus pod and wipes
its history. Export anything a document quotes to `results/` first.

After changing a model in `src/oncall/chaos/incident.py`, regenerate the schema:
`uv run python -m oncall.chaos schema --write`.

## Conventions

**Go.** `gofmt`, `go vet`, `golangci-lint` clean. Standard library first
(`net/http`, `log/slog`). Wrap errors with `%w`. Pass `context.Context` through
every call that does I/O. Table-driven tests.

**Python.** 3.13, managed with `uv`, config in `pyproject.toml`. `ruff` for lint
and format. Type hints on all functions; `pydantic` models for anything parsed
from YAML, JSON, or the model. `argparse` for the CLI. `pytest` for tests.

**Kubernetes.** Plain YAML plus kustomize. Pin image versions (no `latest`).
Namespaces: `testbed` (the only one the agent may touch), `observability`,
`chaos`.

**Agent safety.**
- Tools take typed parameters. Never accept a shell string or raw `kubectl`
  line from the model. Run subprocesses with an argument list, never
  `shell=True`.
- Every write goes through the single guardrail gate. No tool may execute a
  write directly.
- Guardrails are code with unit tests, not prompt text. A new write action
  needs an allow-list entry, a risk level, and tests for both the allowed and
  the blocked case.
- A rejected action is recorded and counted, never hidden.

**Tests.** Required for guardrails, scoring, and incident parsing. Unit tests
must not need a cluster or an API key: the model loop is tested by replaying
recorded traces.

**Git.** Small, focused commits, one or more per phase. Imperative subject under
72 characters, with a body explaining why when it is not obvious.

## Anthropic API notes

Confirm against the SDK documentation when implementing (Phase 3).

- Models: `claude-fable-5-2`, `claude-opus-5-5`, `claude-sonnet-5-5`,
  `claude-haiku-5-5`. Validate the configured ID against the Models API at
  startup and fail fast.
- Forced tool choice (`tool_choice` of type `any` or `tool`) is rejected by the
  larger models. Use `auto` with `strict: true` tools and steer from the prompt.
- Thinking cannot be disabled on the larger models; control depth and cost with
  `output_config.effort`. Do not send `temperature`, `top_p`, or `top_k`.
- Keep the message history append-only and pass response content blocks back
  unchanged.
- Check `stop_reason` before reading content; handle `refusal` and `max_tokens`
  explicitly and record them in the trace.
- Use prompt caching: tools and system prompt are byte-stable, with no
  timestamps or run IDs in them.
- Cost is computed from the usage the API reports, multiplied by a checked-in
  price table that records its source URL and retrieval date. Never guess a
  price.
