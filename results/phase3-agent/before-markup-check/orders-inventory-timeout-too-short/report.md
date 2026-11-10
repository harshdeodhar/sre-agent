# Incident report

## Page

```
You have been paged. It is 2026-11-10 19:51:05 UTC.

Alerts firing in namespace testbed (1):

1. GatewayErrorRateHigh (severity: page)
   More than 5% of requests to POST /api/orders are failing with a 5xx status.
   labels: {route="POST /api/orders"}
   condition true since: 19:49:46 UTC
   rule: sum by (route) (rate(http_server_requests_total{code=~"5..",job="api-gateway"}[2m])) / sum by (route) (rate(http_server_requests_total{job="api-gateway"}[2m])) > 0.05

Investigate, then file the incident report with submit_report.
```

## Summary

POST /api/orders started returning 504s at about 19:49:27 UTC, when order-service was rolled out (revision 6). The new pods set INVENTORY_TIMEOUT to 1ms instead of 1s, so almost every reserve-stock call to inventory-service times out before it can answer. The gateway turns these into 5xx responses, which pushed the route's error ratio above the 5% alert threshold. Inventory-service itself is healthy (p95 about 5-20 ms).</summary>
<parameter name="root_causes">[{"component": "order-service", "category": "bad_config_value", "explanation": "Revision 6 of the order-service Deployment (rolled out 19:49:27 UTC, ReplicaSet order-service-867865557d) sets INVENTORY_TIMEOUT=1ms; revision 5 had 1s. A 1ms client deadline is far below the normal inventory reserve latency, so CreateOrder fails with gRPC DeadlineExceeded ('reserve stock: context deadline exceeded') before inventory-service responds. The gateway returns 504 for POST /api/orders. Only the order-creation path is affected; the GET routes are unaffected.", "confidence": "high"}]

## Root cause

No root cause was established.

## Evidence

- **c1** `query_metrics` `promql='sum by (route, code) (rate(http_server_requests_total{job="api-gateway"}[2m]))'` `minutes=15`: POST /api/orders 5xx: 503 and 504 series rising from about 19:49:46. Successful 201 rate fell from about 1.9/s to 0.34/s by 19:51. Other routes stayed at normal levels.
- **c3** `search_logs` `logql='{k8s_namespace_name="testbed"} | detected_level="error"'` `minutes=15` `limit=40`: Gateway logs: 'upstream call failed' upstream=order-service grpc_code=DeadlineExceeded err='reserve stock: context deadline exceeded', then http 504 on POST /api/orders at about 3 ms duration. Order-service logs show the same DeadlineExceeded on /shop.v1.OrderService/CreateOrder during reserve stock.
- **c4** `deploy_history` `deployment='order-service'`: deploy_history for order-service: revision 6 (ReplicaSet order-service-867865557d) created 2026-11-10T19:49:27Z. Its only env change from revision 5 is INVENTORY_TIMEOUT 1s -> 1ms. Other settings are the same, including PUBLISH_TIMEOUT 500ms and DB_MAX_CONNS 10.
- **c5** `kube_describe` `kind='pods'` `name='order-service-867865557d-x4ln8'`: Pod order-service-867865557d-x4ln8 runs with INVENTORY_TIMEOUT=1ms. It started at 11:49:27 PST (19:49:27 UTC), has 0 restarts, and is Ready. The pod's own state is healthy; the failures are client deadlines.
- **c8** `query_metrics` `promql='sum by (job, grpc_code) (rate(grpc_client_handled_total{job="order-service", grpc_code!="OK"}[1m]))'` `minutes=10`: order-service grpc_client_handled_total DeadlineExceeded rate was 0 until about 19:49:44, then rose to about 0.9/s at 19:50:04 and about 2.1/s at 19:51:24.
- **c9** `query_metrics` `promql='histogram_quantile(0.95, sum by (le) (rate(grpc_server_handling_seconds_bucket{job="inventory-service"}[2m])))'` `minutes=10`: inventory-service gRPC server p95 latency stayed at about 5-20 ms throughout, including after the change. Inventory is not slow, so the 1ms client timeout is the cause.

## Timeline

- 19:49:27: order-service revision 6 rolled out (ReplicaSet order-service-867865557d) with INVENTORY_TIMEOUT=1ms, down from 1s.
- 19:49:44: order-service DeadlineExceeded errors on reserve-stock calls begin.
- 19:49:46: GatewayErrorRateHigh condition becomes true: POST /api/orders 5xx ratio above 5%.
- 19:50:04: DeadlineExceeded rate reaches about 0.9/s and keeps rising; successful order creations drop.
- 19:51:05: Alert paged. POST /api/orders 201 rate is about 0.3/s, down from about 1.9/s.

## Recommended actions

- `rollback_deployment` on `order-service`: Roll order-service back to revision 5 (ReplicaSet order-service-c778f8c59), which has INVENTORY_TIMEOUT=1s. That restores the reserve-stock deadline to a sensible value and should bring POST /api/orders back to normal. Alternatively, set INVENTORY_TIMEOUT back to 1s on the current deployment.

Policy mode was `observe`: nothing in the cluster was changed.

## Run

| | |
|---|---|
| Model | `claude-haiku-5-5` (effort `medium`) |
| Outcome | `report` |
| Started | 2026-11-10 19:51:05 UTC |
| Duration | 26 s |
| Model requests | 4 |
| Tool calls | 9 (0 returned an error, 0 refused) |
| Rejected submissions | 0 |
| Tokens | 10 input, 23080 cache write, 42208 cache read, 2872 output |
| Cost | $0.0047 (prices from https://platform.claude.com/docs/en/about-claude/pricing.md, retrieved 2026-11-10) |
