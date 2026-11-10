"""Export the value of every alert rule's expression over a time window.

Usage: export_alert_series.py START END OUT_FILE   (times as RFC 3339, UTC)

The alert thresholds in deploy/observability/config/alerts.yml are set against
measured values. Prometheus keeps its data on an emptyDir, so the measurements
are exported here to outlive the pod. Each entry holds the rule's expression
with the threshold comparison removed, and the series it returned.
"""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PROMETHEUS = "http://127.0.0.1:9090"
STEP_SECONDS = 15

# The left-hand side of each rule in alerts.yml (everything but the threshold).
# One difference: the error ratio's numerator falls back to zero, so that a
# route without errors is exported as 0 instead of being absent.
EXPRESSIONS = {
    "GatewayErrorRateHigh": (
        '(sum by (route) (rate(http_server_requests_total{job="api-gateway", code=~"5.."}[2m]))'
        ' or 0 * sum by (route) (rate(http_server_requests_total{job="api-gateway"}[2m])))'
        ' / sum by (route) (rate(http_server_requests_total{job="api-gateway"}[2m]))'
    ),
    "GatewayLatencyHigh": (
        "histogram_quantile(0.99, sum by (route, le) "
        '(rate(http_server_request_duration_seconds_bucket{job="api-gateway"}[2m])))'
    ),
    "ContainerRestartingRepeatedly": (
        'increase(kube_pod_container_status_restarts_total{namespace="testbed"}[5m])'
    ),
    "DeploymentReplicasUnavailable": (
        'kube_deployment_status_replicas_unavailable{namespace="testbed"}'
    ),
    "ConsumerLagHigh": 'jetstream_consumer_num_pending{namespace="testbed"}',
    "CacheHitRatioLow": (
        'sum(rate(inventory_cache_requests_total{result="hit"}[2m]))'
        " / sum(rate(inventory_cache_requests_total[2m]))"
    ),
    "DatabaseQuerySlow": (
        "histogram_quantile(0.5, sum by (job, query, le) "
        '(rate(db_query_duration_seconds_bucket{query!="stock.restock"}[2m])))'
    ),
    # Not expressions but states: which alerts were pending or firing, and when.
    "ALERTS": "ALERTS",
}


def query_range(expr: str, start: str, end: str) -> list[dict[str, object]]:
    params = {"query": expr, "start": start, "end": end, "step": str(STEP_SECONDS)}
    url = f"{PROMETHEUS}/api/v1/query_range?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)["data"]["result"]


def main() -> None:
    start, end, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    export = {
        "window": {"start": start, "end": end, "step_seconds": STEP_SECONDS},
        "series": {
            name: {"expression": expr, "result": query_range(expr, start, end)}
            for name, expr in EXPRESSIONS.items()
        },
    }
    out.write_text(json.dumps(export, indent=1) + "\n")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
