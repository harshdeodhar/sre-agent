# Incident report

## Page

```
You have been paged. It is 2026-11-16 05:15:12 UTC.

Alerts firing in namespace testbed (1):

1. ConsumerLagHigh (severity: ticket)
   Consumer notification-worker on stream ORDERS has more than 100 messages pending.
   labels: {account="$G", account_id="$G", consumer_name="notification-worker", instance="nats-74f948cf6c-wwsw5", is_consumer_leader="true", is_meta_leader="true", is_stream_leader="true", job="nats", namespace="testbed", pod="nats-74f948cf6c-wwsw5", server_id="http://localhost:8222", server_name="NDK6K2KXTKQRN3LNHVTEKNIP55OR7C5HIALHZO7K4ZNHTHSQ3HPEM6F3", stream_leader="NDK6K2KXTKQRN3LNHVTEKNIP55OR7C5HIALHZO7K4ZNHTHSQ3HPEM6F3", stream_name="ORDERS"}
   condition true since: 05:14:06 UTC
   rule: jetstream_consumer_num_pending{namespace="testbed"} > 100

Investigate, then file the incident report with submit_report.
```

## Summary

The notification-worker Deployment was scaled from 1 to 0 replicas at about 05:13 UTC, so nothing consumed the ORDERS stream and the notification-worker durable consumer's pending count rose from 0 to 288 by 05:15:32, which tripped ConsumerLagHigh. I scaled the Deployment back to 1 replica (its declared count); the new pod started at 05:15:31 and pending fell to 0 by 05:15:42. The only firing alert was ConsumerLagHigh, and the api-gateway request rate was steady throughout.

## Root cause

| Component | Category | Confidence | Explanation |
|---|---|---|---|
| `notification-worker` | `insufficient_replicas` | high | The notification-worker Deployment's spec.replicas was 0 (generation 16, scaled 1->0 at ~05:13, per kube events and the Deployment spec), while its last-applied configuration declares 1 replica. With no running consumer, messages on the ORDERS stream accumulated (jetstream_consumer_num_pending 0 -> 288), which fired ConsumerLagHigh. The fix is to run the declared 1 replica. Who performed the scale-down is not visible in the telemetry. |

## Evidence

- **c1** `query_metrics` `promql='jetstream_consumer_num_pending{namespace="testbed"}'` `minutes=30`: jetstream_consumer_num_pending for notification-worker on stream ORDERS was 0 until 05:13:15 (5), then 122 at 05:14:15 and 247 at 05:15:15, rising steadily.
- **c2** `kube_get` `kind='pods'` `name=''` `label_selector=''` `output='wide'`: No notification-worker pod was listed at 05:15; all other workloads were running.
- **c4** `deploy_history` `deployment='notification-worker'`: deploy_history for notification-worker: revision 5 (unchanged since 2026-11-10) has DESIRED 0, READY 0. The image, env (DB_MAX_CONNS 4, SEND_DELAY 25ms) and limits did not change, so the cause is a replica count, not a rollout.
- **c7** `kube_get` `kind='deployments'` `name=''` `label_selector=''` `output='wide'`: deployments: notification-worker 0/0 ready, 0 available. The other deployments were 2/2, 1/1 and 1/1.
- **c9** `kube_get` `kind='deployments'` `name='notification-worker'` `label_selector=''` `output='yaml'`: Deployment yaml: spec.replicas 0, generation 16. The last-applied-configuration annotation declares replicas 1. The Available condition went True at 05:02:46 and the replica count then changed to 0.
- **c10** `kube_events` `object_name='notification-worker'` `warnings_only=False`: Events: 'Scaled down replica set notification-worker-76d49cdc7c from 1 to 0' about 2m16s before the query, i.e. ~05:13. Before that, the set went 2->1 28 minutes earlier.
- **c14** `scale_deployment` `deployment='notification-worker'` `replicas=1` `reason='notification-worker was scaled 1->0 at ~05:13 (kube events c10; deployment spec replicas 0, last-applied config replicas 1). No consumer runs, so jetstream_consumer_num_pending for ORDERS rose from 5 to 247 (c1). Restoring the declared replica count of 1.'`: Executed scale_deployment notification-worker 0 -> 1 (auto mode, low risk).
- **c18** `search_logs` `logql='{k8s_namespace_name="testbed", k8s_pod_name=~"notification-worker.*"}'` `minutes=3` `limit=10`: Logs: the old pod 6npbc stopped at 05:13:07; new pod czbn9 started at 05:15:31 with stream ORDERS and consumer notification-worker.
- **c23** `query_metrics` `promql='jetstream_consumer_num_pending{namespace="testbed", consumer_name="notification-worker"}'` `minutes=1`: After the scale-up, jetstream_consumer_num_pending fell from 288 at 05:15:32 to 0 at 05:15:42, so the backlog drained.
- **c12** `query_metrics` `promql='ALERTS{alertstate="firing"}'` `minutes=0`: ALERTS firing: only ConsumerLagHigh. No other alert was firing.
- **c13** `query_metrics` `promql='sum by (pod) (rate(http_server_requests_total{job="api-gateway"}[1m]))'` `minutes=0`: api-gateway request rate was steady at about 5 req/s per pod (2 pods, 10 req/s total) at the time of the check.
- **c6** `query_metrics` `promql='histogram_quantile(0.95, sum by (le, query) (rate(db_query_duration_seconds_bucket{job="notification-worker"}[2m])))'` `minutes=30`: Earlier, notifications.insert p95 spiked to 8-9s at 05:01-05:03 with 'context deadline exceeded' errors (c3), coinciding with NodeNotReady events (c8). It recovered by 05:05 and is not part of this lag incident.

## Timeline

- 05:00:16: notification-worker logs 'record notification: context deadline exceeded' errors; the notifications.insert p95 reached ~8-9s at 05:01-05:03 during the node NotReady period, then recovered by 05:05.
- 05:13:07: notification-worker pod 6npbc stopped: the Deployment was scaled 1 -> 0 (ScalingReplicaSet event).
- 05:13:15: jetstream_consumer_num_pending for ORDERS starts rising (5), with no consumer running.
- 05:14:06: ConsumerLagHigh condition became true (pending > 100).
- 05:15:12: Alert paged; pending 247.
- 05:15:31: Scaled notification-worker 0 -> 1 (executed); new pod czbn9 started and consumed ORDERS.
- 05:15:42: Pending back to 0; backlog drained.

## Recommended actions

- `scale_deployment` on `notification-worker`: Done: scaled notification-worker from 0 to 1 replica, the declared count in its last-applied configuration. Pending dropped from 288 to 0 within about 10 s of the new pod starting. Keep at 1 (or raise to 2 only if the single consumer cannot keep up with 10 orders/s).
- `manual` on `notification-worker`: Find out who or what scaled notification-worker to 0 at about 05:13 (audit logs or CI/operator history), and add a guard against it. The replica count should be 1 unless a human chose otherwise.

## Write actions

Policy mode: `auto`.

1. **executed**: scale deployment/notification-worker from 0 to 1 replicas
   - Risk: low (adds replicas; the pods that are running are not touched)
   - deployment/notification-worker now has the new replicas
   - Decided by: auto mode: low risk

   ```diff
   --- /spec/replicas (now)
   +++ /spec/replicas (after the change, from a server dry run)
   @@ -1 +1 @@
   -0
   +1
   ```

## Run

| | |
|---|---|
| Model | `claude-haiku-5-5` (effort `medium`) |
| Outcome | `report` |
| Started | 2026-11-16 05:15:12 UTC |
| Duration | 39 s |
| Model requests | 12 |
| Tool calls | 24 (1 returned an error, 0 refused) |
| Rejected submissions | 0 |
| Write actions | 1 executed, 0 proposed, 0 declined, 0 rejected, 0 failed |
| Tokens | 26 input, 29585 cache write, 239091 cache read, 6333 output |
| Cost | $0.0093 (prices from https://platform.claude.com/docs/en/about-claude/pricing.md, retrieved 2026-11-10) |
