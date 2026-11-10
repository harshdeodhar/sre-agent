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
