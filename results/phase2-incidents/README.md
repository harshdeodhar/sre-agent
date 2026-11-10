# Phase 2 incident checks

Evidence that each of the first eight incidents shows up in telemetry and
cleans up fully. No agent and no model is involved: this checks the incidents,
not anything that diagnoses them.

One pass over the whole library on 2026-11-10, 11:04 to 11:52 UTC, on the
cluster as committed:

```
make incident-check INCIDENT=--all OUT=results/phase2-incidents
```

**All eight passed.** What `passed` means, for each incident
([`check.py`](../../src/oncall/chaos/check.py)):

1. the cluster was healthy for 60 s in a row before the injection, and none of
   the incident's evidence queries returned anything;
2. after the injection, every alert the incident expects was firing and every
   evidence query had returned a result before the incident's deadline;
3. after the cleanup, every Deployment was back at its manifest state, no alert
   was pending or firing, and every evidence query returned nothing, for 30 s
   in a row.

The table is `summary.md`, generated from the records by
`python -m oncall.chaos summary results/phase2-incidents`. Times are seconds
after the injection, except "healthy again", which is seconds after the cleanup
started.

| Incident | Outcome | Alerts (time to fire) | Evidence seen | All signals seen after | Healthy again after | 5xx / p99 before | Host load |
|---|---|---|---|---|---|---|---|
| `gateway-crashloop-bad-duration` | passed | ContainerRestartingRepeatedly (70 s), DeploymentReplicasUnavailable (151 s) | 2 of 2 | 151 s | 238 s | 0.0% / 31 ms | 5.26 |
| `inventory-memory-limit-too-low` | passed | ContainerRestartingRepeatedly (166 s) | 1 of 1 | 166 s | 311 s | 0.0% / 24 ms | 3.8 |
| `inventory-pod-network-delay` | passed | GatewayLatencyHigh (91 s), GatewayErrorRateHigh (91 s) | 2 of 2 | 91 s | 98 s | 0.0% / 37 ms | 7.04 |
| `multi-worker-scaled-down-and-missing-index` | passed | ConsumerLagHigh (126 s), DatabaseQuerySlow (197 s) | 4 of 4 | 197 s | 90 s | 0.0% / 25 ms | 4.43 |
| `notifications-slow-consumer` | passed | ConsumerLagHigh (161 s) | 2 of 2 | 161 s | 64 s | 0.0% / 25 ms | 3.12 |
| `orders-inventory-timeout-too-short` | passed | GatewayErrorRateHigh (91 s) | 3 of 3 | 91 s | 151 s | 0.0% / 24 ms | 3.12 |
| `orders-missing-index` | passed | DatabaseQuerySlow (186 s) | 2 of 2 | 186 s | 88 s | 0.0% / 25 ms | 2.1 |
| `redis-maxmemory-too-low` | passed | CacheHitRatioLow (221 s) | 3 of 3 | 221 s | 93 s | 0.0% / 25 ms | 2.41 |

"Host load" is the Mac's 1-minute load average when the check started.

## This was the second pass. The first one failed three incidents.

A calibration pass ran earlier the same day (09:52 to 10:45 UTC); its records
are in `calibration/`. Five incidents passed and three failed. Each failure was
a mistake in the incident or in an alert threshold, and each was fixed from
what was measured. The checker, its pass criteria and the deadlines were not
changed between the two passes.

**1. `DatabaseQuerySlow` never fired for the missing index** (two incidents).
The rule's threshold was a median above 50 ms. Without the index, the median of
`orders.list_by_customer` over the rule's 2-minute window was 42 to 58 ms: the
threshold sat on the signal, and the alert went pending and came back without
firing. The threshold is now 25 ms. Outside incidents, no statement's median
was above 10 ms during the calibration hour, so 25 ms keeps a margin on both
sides. The incidents' own evidence query moved from 50 ms to 25 ms with it. In
the final pass the same median was 43 to 47 ms, so the old rule would have
failed again.

This is a change to the testbed's alerting, not to an incident's difficulty:
the fault injected is the same, and what the agent will be scored on (naming
the missing index) is the same.

**2. The memory-limit incident could not be injected, and then did not kill
anything.** The first version set only the limit, to 20Mi, which Kubernetes
rejects because the request (32Mi) was higher. `set_resources` now sets
requests as well. With request 16Mi and limit 20Mi the rollout went through,
but in the three and a half minutes it was watched neither replica restarted:
their working sets peaked at 18.9 MiB and settled near 15 MiB. The 27 to 30 MiB
seen on long-running replicas, which the 20Mi figure was based on, is not what
the service needs. At 12Mi each replica was OOM-killed four times in under four
minutes. The incident now injects 12Mi (request 8Mi). The series for all three
settings are in `calibration/memory_limit_probe.json`.

The same probe settles a Phase 1 question: `container_oom_events_total` stayed
at 0 while the two pods were killed and restarted eight times, and kube-state-metrics reported
`last_terminated_reason="OOMKilled"` for both pods.

**3. The network-delay incident raises a second alert.** In calibration
`GatewayErrorRateHigh` came up alongside the expected `GatewayLatencyHigh`:
order-service gives `ReserveStock` a one-second deadline, and calls that land
on the slow replica can exceed it. It was recorded as an unexpected alert. It
is a real consequence of the fault, so it is now listed as expected, and in the
final pass both fired at the same poll.

## What else the two passes show

- **One sample each.** Every incident ran once in the final pass. The times in
  the table are single measurements, not distributions.
- **The host was not idle.** The load average was between 2.1 and 7.0 at the
  start of the eight checks. The baseline before every injection was still
  clean (no 5xx, p99 of 24 to 37 ms). The first calibration check started at a
  load of 9.1 because tests and linters were running on the Mac at the same
  time, and `GatewayLatencyHigh` fired during it although the fault does not
  cause latency. Nothing like that happened in the final pass.
- **Alerts that were not listed as expected.** One in the final pass:
  `GatewayErrorRateHigh` during `inventory-memory-limit-too-low`, because both
  inventory replicas are down at the same moments. It is not listed as expected
  because the check stops as soon as the listed signals are seen, and this pass
  is a single observation of it.
- **Recovery time is mostly alert windows, not repair time.** The cleanup
  commands took 0.2 to 3.1 s. The two crash-loop incidents need 238 s and 311 s
  to count as healthy because `ContainerRestartingRepeatedly` looks back five
  minutes.
- **A rollout can drop requests.** In calibration, `GatewayErrorRateHigh` fired
  for two read-only routes for one to two minutes around the rollouts of
  order-service in `orders-inventory-timeout-too-short`
  (`calibration/alert_expressions.json`, 10:27 to 10:29 UTC). The fault itself
  only affects order creation. This did not recur in the final pass and has not
  been investigated.
- **The slow consumer causes redeliveries.** With a 2 s delay per message, some
  messages wait past the consumer's 30 s acknowledgement deadline and are
  delivered again: up to 9 were in redelivery at once
  (`telemetry/slow_consumer_probe.json`). The backlog and the redeliveries were
  both back at zero within three minutes of the cleanup.
- **The missing-index fault grows slowly with uptime.** A table scan takes
  longer as `orders` grows, and the load generator adds rows continuously. See
  the hand-recorded row count below.

## Files

| File | What it is |
|---|---|
| `<incident>.json` | The record of one check from the final pass: timestamps, baseline, every alert and evidence query with when it was first seen and what it returned, cleanup duration, recovery time, and the cluster state at each stage. Written by `check.py`; every value was read from Prometheus, Loki, the Kubernetes API or the clock. |
| `summary.md` | The table above, generated from those records. |
| `check.log` | What the pass printed. |
| `telemetry/alert_expressions.json` | The value of every alert rule's expression, and the `ALERTS` series, 11:02 to 11:53 UTC at a 15 s step. |
| `telemetry/slow_consumer_probe.json` | JetStream consumer counters around `notifications-slow-consumer`, 11:30 to 11:37 UTC. |
| `calibration/<incident>.json` | Records of the calibration pass. |
| `calibration/alert_expressions.json` | The same export for the calibration hour, 09:50 to 10:50 UTC. The thresholds in `alerts.yml` cite it. |
| `calibration/memory_limit_probe.json` | Working set, limit, restarts, termination reason and OOM counter of the inventory pods while limits of 20Mi and 12Mi were tried, 10:50 to 11:01 UTC. |
| `export_alert_series.py`, `export_queries.py` | The scripts that wrote the exports. Prometheus keeps its data on an `emptyDir`, and changing an alert rule recreates the pod, so anything quoted has to be exported first. |

## Recorded by hand during the session

These were read off the terminal and are not in any file above.

The `orders` table at 10:50 UTC, between the two passes:

```
 orders | customers |              min              |              max              | heap
--------+-----------+-------------------------------+-------------------------------+-------
 328376 |      5000 | 2026-08-12 06:41:01.188605+00 | 2026-11-10 10:50:06.667108+00 | 32 MB
```

The state of one inventory pod while the 12Mi limit was applied (10:59 UTC):

```
inventory-service-79755749c4-d2gpp 4 {"terminated": {"exitCode": 137, "finishedAt": "2026-11-10T10:59:31Z", "reason": "OOMKilled", "startedAt": "2026-11-10T10:59:04Z"}}
```

The cluster after the final pass (11:52 UTC): `make healthy` printed `healthy`;
every Deployment in `testbed` had one ReplicaSet and all replicas ready; the
`orders` table had both `orders_pkey` and `orders_customer_created_idx`; Redis
reported `maxmemory 67108864`; both inventory pods had `qdisc noqueue` on
`eth0` (no netem).
