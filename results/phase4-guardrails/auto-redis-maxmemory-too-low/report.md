# Incident report

## Page

```
You have been paged. It is 2026-11-16 05:20:55 UTC.

Alerts firing in namespace testbed (1):

1. CacheHitRatioLow (severity: ticket)
   Fewer than 20% of inventory stock lookups are served from the cache.
   labels: {}
   condition true since: 05:18:46 UTC
   rule: sum(rate(inventory_cache_requests_total{result="hit"}[2m])) / sum(rate(inventory_cache_requests_total[2m])) < 0.2

Investigate, then file the incident report with submit_report.
```

## Summary

The cache hit ratio fell to 0% at about 05:18 because Redis's runtime maxmemory was lowered from 64 MB to 1 MB (1048576 bytes) with CONFIG SET at 05:17:23. Redis already held about 1.44 MB, so it rejects writes with OOM. Inventory-service therefore serves every stock lookup from Postgres, and the cache cannot be refilled. The user-facing impact so far is extra Postgres load and latency; gateway 5xx rates are back to 0 after an earlier node-not-ready incident (04:51-05:12), which is a separate, already-recovered event.

## Root cause

| Component | Category | Confidence | Explanation |
|---|---|---|---|
| `redis` | `cache_memory_too_low` | high | The running Redis instance has maxmemory = 1 MB, set at runtime by CONFIG SET maxmemory 1048576 at 05:17:23. The deployment still specifies --maxmemory 64mb, so the live value differs from the spec. Redis's data set (about 1.44 MB) is above the new ceiling, so writes fail with 'OOM command not allowed'. inventory-service logs 'cache write failed' for every miss, and cache reads return nothing, so every stock lookup falls through to Postgres and the hit ratio drops to 0. |

## Evidence

- **c1** `query_metrics` `promql='sum by (result) (rate(inventory_cache_requests_total[1m]))'` `minutes=30`: inventory_cache_requests_total rate: hit went from about 4 req/s to 0 at 05:18; miss rose from about 2.6 to about 7 req/s at 05:17-05:18. Error results 05:06-05:10 are from the earlier node incident.
- **c4** `search_logs` `logql='{k8s_namespace_name="testbed", k8s_container_name=~"inventory.*|redis.*"} | detected_level=~"error|warn"'` `minutes=30` `limit=40`: inventory-service logs repeated WARN 'cache write failed' with err 'OOM command not allowed when used memory > maxmemory' from 05:20 onward, on both inventory pods.
- **c8** `kube_get` `kind='deployments'` `name='redis'` `label_selector=''` `output='yaml'`: redis deployment (revision 1, created 2026-11-10, unchanged) runs redis:7.4.5-alpine with args --maxmemory 64mb --maxmemory-policy allkeys-lru, so the spec does not explain the 1 MB value.
- **c9** `query_metrics` `promql='redis_config_maxmemory'` `minutes=360`: redis_config_maxmemory was 67108864 (64 MB) through 05:09 and became 1048576 (1 MB) by 05:21.
- **c10** `search_logs` `logql='{k8s_namespace_name="testbed", k8s_pod_name=~"redis.*"}'` `minutes=60` `limit=40`: Redis log at 05:17:23: 'WARNING: the new maxmemory value set via CONFIG SET (1048576) is smaller than the current memory usage (1439032)'. The pod was not restarted (it started 04:42:45), so the change came from a runtime CONFIG SET.
- **c12** `query_metrics` `promql='sum(rate(redis_commands_total[1m])) by (cmd)'` `minutes=30`: redis_commands cmd=config|set went to 0 rate at 05:18:16 after the earlier set traffic, consistent with a single manual CONFIG SET. redis_commands get rate stayed about 7/s, and redis_memory_used_bytes stayed about 1.44 MB.
- **c14** `query_metrics` `promql='sum(rate(http_server_requests_total{code=~"5.."}[2m])) / sum(rate(http_server_requests_total[2m]))'` `minutes=30`: Gateway 5xx ratio is 0 from 05:13 to 05:21. The earlier spike (04:59-05:12, up to 26%) is the node-not-ready incident, now recovered. No user-facing errors are linked to the cache change yet.

## Timeline

- 04:42:45: redis pod started with --maxmemory 64mb (restart count from the earlier node incident)
- 05:17:23: CONFIG SET maxmemory 1048576 (1 MB) applied to the running redis, below its 1.44 MB usage; Redis warns about it
- 05:18:16: config|set traffic seen in redis_commands; inventory cache hit rate drops to 0
- 05:18:46: CacheHitRatioLow condition becomes true
- 05:20:55: inventory-service logs 'cache write failed: OOM command not allowed' (ongoing)
- 05:21:11: redis_config_maxmemory scrape shows 1048576
- 05:21: restart_pod on redis-7d9d88cc-lnhpm proposed (not executed): it is the only redis replica, so it needs human approval

## Recommended actions

- `manual` on `redis`: Restore the runtime maxmemory to the spec value 64mb, either with CONFIG SET maxmemory 67108864 on the running redis (keeps the data) or by restarting the pod, which reloads --maxmemory 64mb from the deployment args. The restart proposal (restart_pod on redis-7d9d88cc-lnhpm) is pending human approval because redis has a single replica. Afterwards, confirm redis_config_maxmemory = 67108864, inventory cache writes succeed, and the hit ratio returns to about 80%+. Also find out who ran CONFIG SET, and consider blocking runtime CONFIG changes (rename-command CONFIG) or alerting on redis_config_maxmemory drift from the spec.

## Write actions

Policy mode: `auto`.

1. **proposed**: delete pod/redis-7d9d88cc-lnhpm so that deployment/redis replaces it with a new pod
   - Risk: high (this is the only available replica: the component is down until its replacement is ready)

   ```diff
   - pod/redis-7d9d88cc-lnhpm
     (deployment/redis creates a replacement)
   ```

## Run

| | |
|---|---|
| Model | `claude-haiku-5-5` (effort `medium`) |
| Outcome | `report` |
| Started | 2026-11-16 05:20:55 UTC |
| Duration | 40 s |
| Model requests | 8 |
| Tool calls | 14 (1 returned an error, 0 refused) |
| Rejected submissions | 0 |
| Write actions | 0 executed, 1 proposed, 0 declined, 0 rejected, 0 failed |
| Tokens | 18 input, 26378 cache write, 125224 cache read, 5045 output |
| Cost | $0.0071 (prices from https://platform.claude.com/docs/en/about-claude/pricing.md, retrieved 2026-11-10) |
