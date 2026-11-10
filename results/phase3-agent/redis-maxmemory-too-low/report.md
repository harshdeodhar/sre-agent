# Incident report

## Page

```
You have been paged. It is 2026-11-10 20:02:08 UTC.

Alerts firing in namespace testbed (1):

1. CacheHitRatioLow (severity: ticket)
   Fewer than 20% of inventory stock lookups are served from the cache.
   labels: {}
   condition true since: 19:59:56 UTC
   rule: sum(rate(inventory_cache_requests_total{result="hit"}[2m])) / sum(rate(inventory_cache_requests_total[2m])) < 0.2

Investigate, then file the incident report with submit_report.
```

## Summary

The inventory cache hit ratio fell to zero because Redis's runtime maxmemory was cut from 64 MB to 1 MB at 19:58:35 via CONFIG SET. Redis already held about 1.48 MB, so it could not accept new writes ("OOM command not allowed when used memory > 'maxmemory'"). inventory-service therefore served every stock lookup from Postgres, and the cache-hit alert fired. The deployment manifest still specifies --maxmemory 64mb, so the live value was changed outside the manifest.

## Root cause

| Component | Category | Confidence | Explanation |
|---|---|---|---|
| `redis` | `cache_memory_too_low` | high | The Redis instance's maxmemory was set to 1048576 bytes (1 MB) by a runtime CONFIG SET at 19:58:35, down from 64 MB (67108864) as the deployment args specify. The data set (about 1.48 MB) exceeds that ceiling, so Redis rejects writes with OOM errors and evicts entries. inventory-service's cache writes fail, and reads fall through to Postgres. Redis's own warning log at 19:58:35 confirms the new limit is below current usage. |

## Evidence

- **c1** `query_metrics` `promql='sum(rate(inventory_cache_requests_total[2m])) by (result)'` `minutes=30`: inventory_cache_requests_total rates: hit fell from about 4.5/s to 3.2 at 19:59, 0.75 at 20:00 and 0 from 20:01. Miss rose from about 2.7/s to 7/s. Error-result series is 0 since 19:46.
- **c11** `query_metrics` `promql='redis_config_maxmemory'` `minutes=60`: redis_config_maxmemory was 67108864 (64 MB) through 19:58:28 and 1048576 (1 MB) from 20:00:28 onward.
- **c13** `search_logs` `logql='{k8s_namespace_name="testbed", k8s_container_name="redis"}'` `minutes=60` `limit=30`: Redis log at 19:58:35.299: 'WARNING: the new maxmemory value set via CONFIG SET (1048576) is smaller than the current memory usage (1485288)'.
- **c7** `search_logs` `logql='{k8s_namespace_name="testbed", k8s_container_name="inventory-service"}'` `minutes=15` `limit=40`: inventory-service WARN logs at 20:02 on both pods: 'cache write failed ... OOM command not allowed when used memory > maxmemory'.
- **c10** `kube_get` `kind='deployments'` `name='redis'` `label_selector=''` `output='yaml'`: The redis Deployment spec (revision 1, created 06:40, unchanged) still has args --maxmemory 64mb --maxmemory-policy allkeys-lru. The live value differs, so the change was made at runtime, not through a rollout.
- **c9** `query_metrics` `promql='redis_config_maxmemory or redis_memory_max_bytes or redis_db_keys or redis_evicted_keys_total or redis_rejected_connections_total'` `minutes=0`: redis_memory_used_bytes is about 1.48 MB, which is far below the original 64 MB limit, so memory use was not the cause. The problem is the lowered ceiling.
- **c4** `deploy_history` `deployment='inventory-service'`: inventory-service has had the same revision (7) since 06:41 and its config is unchanged, so the inventory deployment did not cause this.

## Timeline

- 19:58:35: CONFIG SET maxmemory 1048576 applied to the redis instance; Redis warns that current usage (1.48 MB) exceeds the new limit.
- 19:59:56: CacheHitRatioLow condition becomes true; hit ratio starts falling.
- 20:00:28: redis_config_maxmemory metric shows 1 MB.
- 20:01:00: Cache hits drop to zero; all inventory lookups go to Postgres (miss rate about 7/s).
- 20:02:08: Alert investigated: inventory-service logs show continuous 'cache write failed: OOM' warnings.

## Recommended actions

- `revert_config` on `redis`: Restore the runtime maxmemory to 64 MB (CONFIG SET maxmemory 64mb), which matches the deployment spec. Confirm that redis_config_maxmemory returns to 67108864, that inventory writes stop failing with OOM, and that the hit ratio recovers. Also check the maxmemory-policy (allkeys-lru per spec) and whether the runtime change was made by a person or script, to stop it recurring. A redis pod restart would also reset to the spec value, but it would drop the cache contents.
- `manual` on `postgres`: Watch Postgres load while the cache is cold after the fix. Inventory lookups fell back to Postgres for about 10 minutes, so confirm connection use and latency have recovered.

Policy mode was `observe`: nothing in the cluster was changed.

## Run

| | |
|---|---|
| Model | `claude-haiku-5-5` (effort `medium`) |
| Outcome | `report` |
| Started | 2026-11-10 20:02:08 UTC |
| Duration | 33 s |
| Model requests | 8 |
| Tool calls | 15 (2 returned an error, 0 refused) |
| Rejected submissions | 0 |
| Tokens | 18 input, 20795 cache write, 102517 cache read, 4425 output |
| Cost | $0.0058 (prices from https://platform.claude.com/docs/en/about-claude/pricing.md, retrieved 2026-11-10) |
