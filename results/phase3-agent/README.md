# Phase 3: first live runs of the agent

Four investigations by `claude-haiku-5-5` in `observe` mode on 2026-11-10,
against three incidents from the library. Every number below is copied from
the `report.json` next to it, which is computed from the usage the API
reported.

**This is a smoke test, not a benchmark.** It shows that the agent runs end to
end against the real API and the real cluster: it is paged, investigates with
read-only tools, files a report, and leaves a trace that replays. One run per
incident says nothing about how often the model is right. There is no scoring
code yet (Phase 5); the "ground truth" column was compared by hand.

## Runs

All times UTC. Tokens are input / cache write / cache read / output.

| # | Directory | Paged | Root cause reported | Ground truth | Tool calls | Model requests | Duration | Tokens | Cost |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `before-markup-check/orders-inventory-timeout-too-short` | 19:51:05 | none (see below) | `order-service` / `bad_config_value` | 9 | 4 | 26 s | 10 / 23,080 / 42,208 / 2,872 | $0.0047 |
| 2 | `orders-inventory-timeout-too-short` | 19:53:30 | `order-service` / `bad_config_value` | `order-service` / `bad_config_value` | 7 | 4 | 18 s | 10 / 15,566 / 47,379 / 2,879 | $0.0039 |
| 3 | `redis-maxmemory-too-low` | 20:02:08 | `redis` / `cache_memory_too_low` | `redis` / `cache_memory_too_low` | 15 | 8 | 33 s | 18 / 20,795 / 102,517 / 4,425 | $0.0058 |
| 4 | `multi-worker-scaled-down-and-missing-index` | 20:08:07 | `postgres` / `missing_index` | `postgres` / `missing_index` **and** `notification-worker` / `insufficient_replicas` | 15 | 7 | 37 s | 16 / 31,004 / 101,966 / 4,524 | $0.0072 |

Total API spend: **$0.0216** for the four runs. No other API call was made in
Phase 3.

By hand: run 2 and run 3 name the cause in the ground truth. Run 4 names one of
the two causes and misses the other. Run 1 reached the right diagnosis and
filed it wrongly.

No report was rejected and no tool call was refused in any run. Two tool calls
in run 3 returned an error (LogQL the model wrote did not parse); the model
saw the error and carried on.

## What each run showed

**Run 1: a correct diagnosis filed as "no root cause".** The model found the
cause (`INVENTORY_TIMEOUT` changed from `1s` to `1ms` in revision 6 of
order-service) in 9 tool calls. Its `submit_report` call then put the summary,
a literal `</summary>`, the text `<parameter name="root_causes">` and the whole
root-cause list into the `summary` field, and left the real `root_causes` field
empty. The call satisfied the strict schema, so the API accepted it and so did
the loop. `report.md` in that directory shows the result as it was written.

The fix is a check in code, not prompt text: `check_submission` now rejects a
report in which any text field contains `<parameter name=`, and the model is
told why and may submit again, as for any other rejected report. It applies to
every incident and every model, and a rejection is recorded and counted. The
recorded call is a test fixture: `tests/agent/test_report.py` checks that it is
now rejected.

That trace cannot be replayed by today's code, and the replay says so
(`replay failed: the loop made a model request the trace does not contain`):
today's loop rejects the report and asks the model again, and the recording has
nothing more to give. The run is kept because it happened.

**Run 2: the same incident, after the fix.** The incident was still injected.
The model produced a well-formed report this time, so the new check did not
fire; this run does not show the check working on a live model. It names the
changed setting, the revision, and recommends `rollback_deployment`.

**Run 3: Redis.** The model noticed that the Deployment still says
`--maxmemory 64mb` while `redis_config_maxmemory` reports 1 MB, and concluded
the value was changed at runtime, which is what the injection does. In the
report, the finding attached to call `c9` quotes a metric that `c9` did not
query. The code checks that a cited call exists and was seen; it does not check
that the finding follows from it.

**Run 4: two causes, one found.** The agent is paged as soon as the first alert
fires. Here that was `GatewayLatencyHigh`, about 90 seconds after injection; the
alert for the second cause (`ConsumerLagHigh`) was not firing yet. The model's
first call listed the pods, and the list had no `notification-worker` pod in
it; the model did not notice the absence and never looked at Deployments. It
reported the dropped index and stopped. When to page for an incident whose
symptoms arrive at different times is a harness decision for Phase 5.

## Changes made after seeing a run

Disclosed because they were made between run 1 and run 2:

1. The markup check described above (`src/oncall/agent/report.py`).
2. `kubectl` now runs with `TZ=UTC` (`src/oncall/chaos/kubectl.py`). In run 1,
   `kubectl describe` printed times in the laptop's zone (`-0800`) while the
   page and all telemetry were UTC.

The system prompt and the tool descriptions were written before run 1 and have
not been changed since. Runs 2 to 4 used identical code.

## Conditions

- Before run 1 the cluster had been unhealthy for about 50 minutes with no
  incident injected: the laptop ran out of memory and swapped the Docker VM
  out. The runs started only after `make healthy` passed (19:49), and it
  passed again before each later injection (19:58, 20:06). Leftovers of that
  period are visible to the agent: node `NotReady` events in run 2, and a
  restart count of 1 on the gateway and inventory pods.
- After run 4 was cleaned up (20:08:51) the cluster did not go straight back
  to healthy. The incident's own alerts were still resolving at 20:09, and at
  about 20:11 the laptop ran short of memory again, with nothing injected:
  `GatewayLatencyHigh`, `GatewayErrorRateHigh` and `DatabaseQuerySlow` fired
  until 20:22. `make healthy` passed at 20:25. No agent run was made in that
  period.
- Prompt caching works across runs: a run that starts within five minutes of
  the previous one reads the cached tools and system prompt instead of writing
  them (run 2: 6,041 tokens read on its first request). Cost per run therefore
  depends a little on what ran just before.
- Each run: `make inject INCIDENT=<id>`, then `make agent ARGS="--wait <s>"`
  (`MODEL` defaults to `claude-haiku-5-5`, effort `medium`), then
  `make cleanup INCIDENT=<id>`, then `make healthy`.

## Since Phase 4

These runs were made in observe mode before the write tools existed. Observe
mode still sends the same system prompt and tools byte for byte, so the traces
still replay record for record (`tests/agent/test_replay.py`). From Phase 4,
`report.json` also lists the write actions; the report files here are in the
Phase 3 format, as the code of commit `a3666ba` wrote them, and are left as
they were.

## Reproducing

Offline, with no API key and no cluster:

```
make agent-replay TRACE=results/phase3-agent/redis-maxmemory-too-low/trace.jsonl
```

runs the loop against the recording and prints `Replay matches the recording.`
`tests/agent/test_replay.py` does this for runs 2 to 4 and also checks that the
committed `report.json` and `report.md` are exactly what the trace produces.

A new live run will differ: the model is not deterministic and the telemetry is
never the same twice.

## Files

Per run: `trace.jsonl` (every request hash, response, tool call and result),
`report.json`, `report.md`.

`rbac-check.txt`: what the agent's cluster identity may and may not do, asked
of the API server on the day.

`cluster-health.json`: gateway p95 latency, error ratio, request rate and the
state of every alert from 18:25 to 20:26, at a 5-second step, exported with
`results/phase2-incidents/export_queries.py`. It shows the unhealthy period
before the runs, the healthy window they were made in, each incident, and the
second unhealthy period after run 4.
