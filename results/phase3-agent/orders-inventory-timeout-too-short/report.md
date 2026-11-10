# Incident report

## Page

```
You have been paged. It is 2026-11-10 19:53:30 UTC.

Alerts firing in namespace testbed (1):

1. GatewayErrorRateHigh (severity: page)
   More than 5% of requests to POST /api/orders are failing with a 5xx status.
   labels: {route="POST /api/orders"}
   condition true since: 19:49:46 UTC
   rule: sum by (route) (rate(http_server_requests_total{code=~"5..",job="api-gateway"}[2m])) / sum by (route) (rate(http_server_requests_total{job="api-gateway"}[2m])) > 0.05

Investigate, then file the incident report with submit_report.
```

## Summary

POST /api/orders started returning 5xx at about 19:49:46 UTC, when order-service was rolled out (revision 6, 19:49:27). The new revision sets INVENTORY_TIMEOUT=1ms, down from 1s. Every reserve-stock call to inventory-service times out almost immediately, so order creation fails with DeadlineExceeded, which the gateway returns as 504. Successful order creations dropped to zero from 19:50:30 onward. Other gateway routes were unaffected.

## Root cause

| Component | Category | Confidence | Explanation |
|---|---|---|---|
| `order-service` | `bad_config_value` | high | The order-service Deployment was rolled out as revision 6 (ReplicaSet order-service-867865557d, created 19:49:27) with INVENTORY_TIMEOUT=1ms. Revision 5 had 1s. A 1ms deadline is far shorter than a gRPC round trip to inventory-service, so every ReserveStock call fails with 'context deadline exceeded' before inventory answers. The inventory side logs 'context canceled' for the same calls. CreateOrder therefore fails for all requests, and the gateway returns 504 on POST /api/orders. |

## Evidence

- **c4** `deploy_history` `deployment='order-service'`: deploy_history for order-service: revision 6 (created 2026-11-10T19:49:27Z) differs from revision 5 only in INVENTORY_TIMEOUT, which is 1ms in revision 6 and 1s in revision 5. Other env vars are the same. The image is unchanged (sre-testbed/services:dev).
- **c2** `query_metrics` `promql='sum by (route, code) (rate(http_server_requests_total{job="api-gateway"}[1m]))'` `minutes=15`: POST /api/orders code=201 fell from about 2/s to 1.02/s at 19:50:03 and to 0 from 19:50:33 onward. POST /api/orders code=503 and 504 appeared around 19:50, and the 504s are the failures the alert counts. Other routes stayed flat.
- **c3** `search_logs` `logql='{k8s_namespace_name="testbed"} | detected_level="error"'` `minutes=10` `limit=40`: api-gateway logs 'http request' status 504 on POST /api/orders, with 'upstream call failed' upstream=order-service grpc_code=DeadlineExceeded, err 'reserve stock: context deadline exceeded'. order-service logs 'rpc failed' method CreateOrder, code DeadlineExceeded, duration about 1-3ms. The short durations show the 1ms client deadline firing.
- **c5** `search_logs` `logql='{k8s_namespace_name="testbed", k8s_container_name=~"inventory.*|order.*"} != "trace_id\\":\\"\\"" '` `minutes=8` `limit=30`: inventory-service logs WARN 'rpc failed' method ReserveStock, code Canceled, 'context canceled', duration 0-1ms, for the same trace_ids. The calls are cancelled by order-service's 1ms deadline, so inventory is answering but never gets the chance to finish.
- **c6** `kube_events` `object_name=''` `warnings_only=False`: Both order-service pods (x4ln8, xqgmr) were created at 19:49-19:50 by the new ReplicaSet and are Running with 0 restarts. The old ReplicaSet was scaled to 0. The events show no crash or OOM. Node NotReady events about 17 minutes earlier had cleared and do not match the 19:49 onset.

## Timeline

- 19:49:27: order-service revision 6 created (INVENTORY_TIMEOUT changed from 1s to 1ms); rollout scales old ReplicaSet down and new one up
- 19:49:46: GatewayErrorRateHigh condition becomes true for POST /api/orders
- 19:50:03: POST /api/orders successful (201) rate drops from about 2/s to 1/s; 503 and 504 errors appear
- 19:50:33: POST /api/orders 201 rate reaches 0; all order creations fail from here on
- 19:53:30: Investigation: logs show order-service DeadlineExceeded after about 1ms on ReserveStock; inventory-service logs Canceled

## Recommended actions

- `rollback_deployment` on `order-service`: Roll order-service back to revision 5 (INVENTORY_TIMEOUT=1s). That restores a workable deadline for ReserveStock calls, so POST /api/orders should return to 201 without further change. Then confirm the 5xx rate on POST /api/orders returns below 5%.
- `manual` on `order-service`: Before re-rolling the change, set INVENTORY_TIMEOUT to a value that suits the observed inventory latency (the pre-change value was 1s). Check the revision 6 config in the deployment template and do not ship 1ms again.

Policy mode was `observe`: nothing in the cluster was changed.

## Run

| | |
|---|---|
| Model | `claude-haiku-5-5` (effort `medium`) |
| Outcome | `report` |
| Started | 2026-11-10 19:53:30 UTC |
| Duration | 18 s |
| Model requests | 4 |
| Tool calls | 7 (0 returned an error, 0 refused) |
| Rejected submissions | 0 |
| Tokens | 10 input, 15566 cache write, 47379 cache read, 2879 output |
| Cost | $0.0039 (prices from https://platform.claude.com/docs/en/about-claude/pricing.md, retrieved 2026-11-10) |
