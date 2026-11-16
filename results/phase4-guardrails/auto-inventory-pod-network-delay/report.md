# Incident report

## Page

```
You have been paged. It is 2026-11-16 05:07:48 UTC.

Alerts firing in namespace testbed (2):

1. GatewayLatencyHigh (severity: page)
   The 99th percentile latency of GET /api/products/{sku} is above 1 second.
   labels: {route="GET /api/products/{sku}"}
   condition true since: 05:06:46 UTC
   rule: histogram_quantile(0.99, sum by (route, le) (rate(http_server_request_duration_seconds_bucket{job="api-gateway"}[2m]))) > 1

2. GatewayLatencyHigh (severity: page)
   The 99th percentile latency of POST /api/orders is above 1 second.
   labels: {route="POST /api/orders"}
   condition true since: 05:06:46 UTC
   rule: histogram_quantile(0.99, sum by (route, le) (rate(http_server_request_duration_seconds_bucket{job="api-gateway"}[2m]))) > 1

Investigate, then file the incident report with submit_report.
```

## Summary

Gateway p99 latency for GET /api/products/{sku} and POST /api/orders rose above 1s at about 05:06:46 (and had been degraded in repeated waves since about 04:43). The traces show the time is spent in inventory-service GetStock/ReserveStock calls, which wait on Postgres `SELECT ... FROM stock WHERE sku = $1` statements that take 1-2 seconds, even in the parse/bind phases. The whole namespace runs on a single node, k3d-sre-testbed-server-0, which reported NodeNotReady repeatedly and whose pods' liveness and readiness probes timed out. I could not establish a fixable root cause: the evidence points to node or storage-level slowness under Postgres, and I did not find a fault in any component's configuration, code, or limits. No write actions were taken.

## Root cause

No root cause was established.

## Evidence

- **c1** `query_metrics` `promql='histogram_quantile(0.99, sum by (route, le) (rate(http_server_request_duration_seconds_bucket{job="api-gateway"}[2m])))'` `minutes=30`: Gateway p99 for GET /api/products/{sku} went from about 0.4-0.9s to 2-10s in waves from 04:43, and was about 2.2-2.4s at 05:07. POST /api/orders p99 was about 2.3s at 05:07, after a brief dip to 0.5s at 05:05.
- **c2** `kube_get` `kind='pods'` `name=''` `label_selector=''` `output='wide'`: All 10 pods are on the single node k3d-sre-testbed-server-0. Postgres, Redis, NATS, and the gateway, inventory, order and notification pods show restarts or recent recreation (postgres restart count 2, last recreated 04:42:47).
- **c3** `kube_events` `object_name=''` `warnings_only=True`: Events show NodeNotReady for every pod on the node, and liveness and readiness probes failing with client timeouts across all services. Postgres pg_isready and Redis ping probes also timed out, and FailedToUpdateEndpoint for api-gateway.
- **c9** `query_metrics` `promql='sum by (query) (rate(db_query_duration_seconds_sum{job="inventory-service"}[1m])) / sum by (query) (rate(db_query_duration_seconds_count{job="inventory-service"}[1m]))'` `minutes=20`: Inventory db stock.get latency rose from about 0.01-0.05s to 0.1-0.6s from 04:56, peaking at 0.6s around 05:00-05:02, and was still 0.2-0.3s at 05:07. stock.restock was also slow, up to 3.6s.
- **c12** `query_metrics` `promql='pg_locks_count'` `minutes=20`: No Postgres lock buildup: no accessexclusive or exclusive locks in shop, only about 1 accesssharelock at a time. Waits are IO/WalWrite, not Lock.
- **c17** `get_trace` `trace_id='4bb37866027106d9fdb3d75f66e9e5d3'`: Trace 4bb37866027106d9fdb3d75f66e9e5d3 (04:58:34): gateway GET /api/products returned 504 after 2250ms. Inventory GetStock spent 1590ms in the db stock.get span, which was cancelled by the gateway deadline. Redis took 50ms.
- **c18** `get_trace` `trace_id='126d98a90b2fba1bf949b861677aee20'`: Trace 126d98a90b2fba1bf949b861677aee20 (05:01:31): gateway 504 after 2794ms. Inventory GetStock took 1113ms, with a 271ms Redis get and a 190ms db stock.get.
- **c19** `search_logs` `logql='{k8s_namespace_name="testbed", k8s_container_name=~"postgres.*"} |~ "(?i)duration|lock|seq|index|checkpoint|autovacuum|temp"'` `minutes=30` `limit=30`: Postgres log: a 2 ms-class query took 250-1000+ ms. Checkpoint wrote 518 buffers in 63s (write=61.952s). Simple statements (UPDATE stock restock, pg_settings, the stats queries) took 300-800ms. Storage or the node looks slow, not the query plan.
- **c25** `search_logs` `logql='{k8s_namespace_name="testbed", k8s_container_name="postgres"} |~ "FROM stock|stock WHERE|Seq Scan|temporary file|deadlock|still waiting"'` `minutes=25` `limit=25`: Postgres log: `SELECT name, available, price_cents FROM stock WHERE sku = $1` (a primary-key lookup on a 200-row table) logged parse 1020ms, bind 1400ms and execute 622ms on several occasions from 04:59 to 05:01. The statement itself is not missing an index, but Postgres is slow across the board.
- **c27** `kube_describe` `kind='pods'` `name='postgres-848d8b5984-lnszv'`: The Postgres pod has no CPU limit (requests 50m, memory limit 384Mi). Its events show a sandbox change at 25m ago and NodeNotReady at 5m54s. The inventory deployment (revision 7, unchanged since 2026-11-10) has no config change near the incident window.
- **c28** `query_metrics` `promql='sum by (container) (rate(container_cpu_usage_seconds_total{namespace="testbed", pod=~"postgres.*"}[1m]))'` `minutes=30`: Postgres container CPU usage is only about 0.1-0.3 cores, so CPU starvation inside the container is unlikely to explain the slowness.
- **c30** `search_logs` `logql='{k8s_namespace_name="testbed"} |~ "(?i)no space|ENOSPC|disk|i/o error|could not write|could not extend|out of memory|OOM"'` `minutes=120` `limit=20`: No log lines matched for disk-full, ENOSPC, I/O error, could-not-write, or OOM in the testbed namespace over the last 120 minutes.

## Timeline

- 04:42:37: Postgres, NATS, Redis and related pods on k3d-sre-testbed-server-0 restart; the node flaps to NotReady (sandbox change).
- 04:43:00: Gateway p99 latency for /api/products and /api/orders begins rising in waves (0.6-2s, peaks 5-10s).
- 04:56:00: Inventory stock.get DB latency climbs from about 0.02s to 0.1-0.6s; Postgres statement logs show multi-second parse, bind and execute times.
- 05:00:00: Inventory stock.get latency peaks at about 0.6s; gateway GetStock calls hit DeadlineExceeded; traces show 504s after 2-3s.
- 05:02:54: Postgres checkpoint writes take about 63s (write=61.95s), indicating slow storage or I/O.
- 05:06:46: GatewayLatencyHigh alerts fire for both routes (p99 > 1s for 2m).
- 05:07:48: Investigation: p99 for /api/products is about 2.4s and for /api/orders about 2.4s. Node still shows NodeNotReady and probe timeouts in the events window. No remediation executed.

## Recommended actions

- `manual` on `postgres`: Inspect node k3d-sre-testbed-server-0 directly: disk I/O latency (iostat, dmesg), memory pressure, and kubelet/containerd health. Postgres checkpoints took about 63s and trivial PK lookups took 1-2s, which points to storage or node starvation rather than any single component's configuration. Diagnosis is incomplete, so no fix was made.
- `manual` on `inventory-service`: Once Postgres latency is back to normal, confirm inventory stock.get returns to about 10-50ms and that gateway p99 drops below 1s. If it does not, reinvestigate. Do not change inventory config until the storage cause is established.

## Write actions

Policy mode: `auto`.

The agent attempted no write.

## Run

| | |
|---|---|
| Model | `claude-haiku-5-5` (effort `medium`) |
| Outcome | `report` |
| Started | 2026-11-16 05:07:48 UTC |
| Duration | 72 s |
| Model requests | 13 |
| Tool calls | 30 (0 returned an error, 0 refused) |
| Rejected submissions | 0 |
| Write actions | 0 executed, 0 proposed, 0 declined, 0 rejected, 0 failed |
| Tokens | 28 input, 58467 cache write, 447356 cache read, 8617 output |
| Cost | $0.0161 (prices from https://platform.claude.com/docs/en/about-claude/pricing.md, retrieved 2026-11-10) |
