# Incident report

## Page

```
You have been paged. It is 2026-11-10 20:08:07 UTC.

Alerts firing in namespace testbed (1):

1. GatewayLatencyHigh (severity: page)
   The 99th percentile latency of GET /api/customers/{id}/orders is above 1 second.
   labels: {route="GET /api/customers/{id}/orders"}
   condition true since: 20:06:56 UTC
   rule: histogram_quantile(0.99, sum by (route, le) (rate(http_server_request_duration_seconds_bucket{job="api-gateway"}[2m]))) > 1

Investigate, then file the incident report with submit_report.
```

## Summary

GET /api/customers/{id}/orders p99 went above 1s at 20:06:56 because the Postgres query behind it (orders.list_by_customer: SELECT ... FROM orders WHERE customer_id = $1 ORDER BY created_at DESC LIMIT 20) lost its supporting index. At 20:06:40 the index orders_customer_created_idx was dropped; the query went from about 6 ms to 300-1900 ms with sequential scans on orders, which exhausted order-service's latency budget and produced DeadlineExceeded errors on this route. Only this route's alert fired, and the customer-orders latency pattern matches the index drop.

## Root cause

| Component | Category | Confidence | Explanation |
|---|---|---|---|
| `postgres` | `missing_index` | high | The index orders_customer_created_idx, which serves orders filtered by customer_id and ordered by created_at, was dropped at 20:06:40 (Postgres log: 'DROP INDEX IF EXISTS orders_customer_created_idx'). The orders.list_by_customer query now sequentially scans the orders table, so each GET /api/customers/{id}/orders takes roughly 0.3-2 s instead of a few ms. The gateway p99 exceeds 1 s and order-service returns DeadlineExceeded (list orders: timeout). |

## Evidence

- **c1** `query_metrics` `promql='histogram_quantile(0.99, sum by (route, le) (rate(http_server_request_duration_seconds_bucket{job="api-gateway"}[2m])))'` `minutes=30`: api-gateway p99 for GET /api/customers/{id}/orders was 0.02-0.09 s through 20:06 and rose to 2.28 s at 20:07 and 2.38 s at 20:08. Other routes were at baseline in that window.
- **c9** `query_metrics` `promql='sum by (query) (rate(db_query_duration_seconds_sum{job="order-service"}[2m])) / sum by (query) (rate(db_query_duration_seconds_count{job="order-service"}[2m]))'` `minutes=30`: order-service db_query p-mean for orders.list_by_customer was about 0.006-0.014 s before 20:06, then 0.1056 s at 20:07 and 0.2596 s at 20:08. orders.get and orders.insert stayed flat.
- **c5** `search_traces` `traceql='{ name = "GET /api/customers/{id}/orders" && duration > 500ms }'` `minutes=15` `limit=3`: Traces for GET /api/customers/{id}/orders at 20:07:39-20:07:44 took 0.78-1.74 s.
- **c7** `get_trace` `trace_id='a0be601dfd06c7f99c35ec568d8fd692'`: Trace a0be601dfd06c7f99c35ec568d8fd692: the api-gateway call took 1738 ms, order-service ListCustomerOrders 1642 ms, and the db span 'db orders.list_by_customer' 1638 ms. The time is spent in the database.
- **c8** `search_logs` `logql='{k8s_namespace_name="testbed", k8s_container_name="order-service"} | json | msg!="" | line_format "{{.time}} {{.level}} {{.msg}} {{.err}} {{.duration_ms}}"'` `minutes=8` `limit=40`: order-service log at 20:06:44: 'rpc failed' code DeadlineExceeded, 'list orders: timeout: context deadline exceeded', duration 2064 ms.
- **c11** `search_logs` `logql='{k8s_namespace_name="testbed", k8s_container_name="postgres"}'` `minutes=30` `limit=30`: Postgres slow-query log shows the orders.list_by_customer statement taking 271-1618 ms from 20:07:41 onward, for example 1104 ms and 1292 ms.
- **c12** `query_metrics` `promql='rate(pg_stat_user_tables_seq_scan{relname="orders"}[2m])'` `minutes=30`: pg_stat_user_tables seq_scan on the orders table was 0 until 20:06 and then 0.55/s at 20:07 and 1.27/s at 20:08. This is the sequential-scan signature of a missing index.
- **c14** `search_logs` `logql='{k8s_namespace_name="testbed", k8s_container_name="postgres"} |= "LOG:  duration" or "index" or "DROP" or "CREATE"'` `minutes=360` `limit=20`: Postgres log at 20:06:40.496 (pid 71064): 'DROP INDEX IF EXISTS orders_customer_created_idx' took 256 ms. Immediately after, at 20:06:44 the list_by_customer statements took 1163-1948 ms, the first slow query after the drop.
- **c15** `search_logs` `logql='{k8s_namespace_name="testbed", k8s_container_name="postgres"} |= "orders_customer_created_idx" or "[71064]"'` `minutes=60` `limit=20`: The only log line matching the index name or the dropping session is the DROP INDEX statement at 20:06:40.496. The log does not record which client issued it.

## Timeline

- 19:38:00: Earlier latency bursts on several routes, including POST /api/orders and GET /api/products, related to the 19:31-19:50 node NotReady and probe-failure period. These are not the customer-orders alert.
- 20:06:40: DROP INDEX IF EXISTS orders_customer_created_idx executed on postgres.
- 20:06:44: orders.list_by_customer statements begin taking 1.1-1.9 s. order-service logs DeadlineExceeded for ListCustomerOrders.
- 20:06:56: GatewayLatencyHigh condition becomes true (p99 for GET /api/customers/{id}/orders > 1 s).
- 20:07:00: Sequential scans on orders begin to appear in pg_stat_user_tables. Gateway p99 reaches 2.28 s at 20:07.
- 20:08:07: Alert fires and I am paged. Gateway p99 is still 2.38 s.

## Recommended actions

- `manual` on `postgres`: Recreate the index that was dropped, using the original definition: CREATE INDEX CONCURRENTLY orders_customer_created_idx ON orders (customer_id, created_at DESC); (confirm column order against the schema or migration history). Then confirm that EXPLAIN on the orders.list_by_customer query shows an index scan and that the latency returns to about 6 ms. CONCURRENTLY avoids blocking writes on the orders table.
- `manual` on `postgres`: Find out who issued DROP INDEX at 20:06:40 (pg_stat_activity history, audit logs, or the deploy and migration tooling) and stop it from happening again, for example by restricting DDL permissions on the shop schema or adding a migration check. This is needed so the index does not disappear again.

Policy mode was `observe`: nothing in the cluster was changed.

## Run

| | |
|---|---|
| Model | `claude-haiku-5-5` (effort `medium`) |
| Outcome | `report` |
| Started | 2026-11-10 20:08:07 UTC |
| Duration | 37 s |
| Model requests | 7 |
| Tool calls | 15 (0 returned an error, 0 refused) |
| Rejected submissions | 0 |
| Tokens | 16 input, 31004 cache write, 101966 cache read, 4524 output |
| Cost | $0.0072 (prices from https://platform.claude.com/docs/en/about-claude/pricing.md, retrieved 2026-11-10) |
