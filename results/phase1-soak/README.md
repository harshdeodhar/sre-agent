# Phase 1 soak measurements

Two 25-minute runs on one freshly created cluster on 2026-11-10 (UTC), at the
default load of 10 requests per second, with no fault injected. The purpose was
to measure memory per container and to check that the gateway is error-free at
rest. These files are the evidence for the numbers quoted in
`docs/ARCHITECTURE.md`.

**Neither run was clean, and the cause was the host, not the cluster.** The Mac
(8 GB RAM, 4 GB Docker VM) had a browser and an editor open and was swapping
heavily throughout. Treat these as measurements of the testbed under host
memory pressure. A measurement on a quiet host has not been made yet.

| | soak-a | soak-b |
|---|---|---|
| Window (UTC) | 06:42:41 to 07:07:53 | 07:22:27 to 07:47:33 |
| Tempo config | as committed | `parquet_row_group_size_bytes: 10000000` (tried, then reverted) |
| Gateway requests | 14,089 | 12,968 |
| Gateway 504 responses | 205 (1.5%) | 843 (6.5%) |
| Gateway p99 latency, median over the window | 1.5 s | 2.5 s |
| Testbed container restarts | 0 | 0 |
| Tempo restarts | 0 | 2 |
| Tempo peak working set (limit 384 MiB) | 356 MiB | 354 MiB |
| Host swap traffic (in + out) | not recorded during the run | 724 MiB/s on average |

The same figures for every container, and for the whole 76 minutes from cluster
creation, are in `summary.txt`.

## What the two runs do and do not show

- The testbed itself is fast and error-free when the host lets it be: in
  soak-a there is no new 504 between 06:49 and 06:56, and from 06:53 to 06:56
  the p99 is 25 to 56 ms (`gateway_p99_seconds.json`,
  `gateway_requests_total_by_code.json`). Every container's CPU use rose and
  fell together outside that window, which points at the host, not a service.
- No testbed container restarted in either run, with the host load average
  above 20 in soak-b. The slow liveness probes did their job.
- The smaller Parquet row group did not reduce Tempo's peak memory, and Tempo
  restarted twice with it and not at all without it. Host pressure was not
  recorded during soak-a, so the runs are not a controlled comparison; the
  setting was reverted because nothing here shows a benefit.
- `oom_events_total.json` is zero for every container although Tempo was
  OOM-killed. `container_oom_events_total` cannot be used to detect an OOM kill
  of a container that restarts.

## Files

| File | What it is |
|---|---|
| `export_prometheus.py` | Exports the series below from the cluster's Prometheus and prints `summary.txt`. |
| `summary.txt` | Output of that script: per-window gateway totals, latency, restarts, peak memory per container. |
| `*.json` | Unmodified Prometheus `query_range` responses, 06:40 to 07:56 UTC, 30 s step. The query is recorded in each file. |
| `soak_sampler.py` | Samples Tempo's and Loki's Go runtime metrics and the host's swap counters every 5 s. Ran on the host during soak-b. |
| `soak-b_tempo_loki_runtime.csv` | Its samples. |
| `soak-b_host_swap.csv` | Cumulative pages swapped in plus out (16 KiB pages), from `vm_stat`. |
| `soak-b_sampler_summary.txt` | What the sampler printed, minus repeated macOS `MallocStackLogging` warnings. |
| `observability_events.txt` | `kubectl get events -n observability`, taken at 08:00 UTC. |
| `tempo_heap_soak-a.pb.gz` | Tempo heap profile at 07:15 UTC, after soak-a, same process. Read with `go tool pprof`. |
| `tempo_heap_soak-b.pb.gz` | Tempo heap profile at 07:51 UTC, 7 minutes after its last restart. |

## Caveats on these files

- The sampler's CSV for soak-a was overwritten by soak-b's, so soak-a's Go
  runtime figures for Tempo are not here and are not quoted anywhere. Soak-a's
  memory figures come from cAdvisor through Prometheus.
- The sampler's "GC cycles" figure for Tempo in soak-b is wrong: the counter
  reset when the process restarted.
- 78 of the sampler's fetches timed out in soak-b, so its peaks are lower
  bounds.
- Gateway p99 is capped by the histogram's top bucket (10 s).

## Recorded by hand during the session

These were read off the terminal and are not in any file above.

Tempo's container status at 07:51 UTC, before the pod was replaced:

```
tempo restarts=2 last={"terminated":{"exitCode":137,"finishedAt":"2026-11-10T07:44:07Z","reason":"OOMKilled","startedAt":"2026-11-10T07:36:01Z"}}
```

Kubernetes keeps only the last termination, so the first restart (at about
07:36) has no recorded reason.

Host state at the end of soak-b (07:48 UTC):

```
vm.swapusage: total = 16384.00M  used = 15075.12M  free = 1308.88M  (encrypted)
load averages: 28.19 19.29 13.50
```
