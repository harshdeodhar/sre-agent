"""Export the raw Prometheus series behind the Phase 1 soak numbers.

Usage: python3 export_prometheus.py

Writes one JSON file per query (the unmodified Prometheus API response) next to
this script, then prints a summary computed from Prometheus for each soak
window. The summary is what docs/ARCHITECTURE.md quotes; the JSON files are the
evidence for it.

The times are fixed because this records two specific runs on one cluster. It
can only be re-run while that cluster's Prometheus still holds the data (12 h
retention), so the output is committed.
"""

import json
import urllib.parse
import urllib.request
from pathlib import Path

PROMETHEUS = "http://127.0.0.1:9090"
OUT = Path(__file__).parent

# The cluster was created at about 06:41 UTC.
RANGE_START = "2026-11-10T06:40:00Z"
RANGE_END = "2026-11-10T07:56:00Z"
STEP = "30s"

# name: (start, end, duration in seconds, what was different)
WINDOWS = {
    "soak-a": ("2026-11-10T06:42:41Z", "2026-11-10T07:07:53Z", 1512, "committed Tempo config"),
    "soak-b": (
        "2026-11-10T07:22:27Z",
        "2026-11-10T07:47:33Z",
        1506,
        "Tempo parquet_row_group_size_bytes=10000000 (tried, then reverted)",
    ),
    "whole-run": (RANGE_START, RANGE_END, 4560, "cluster creation to the end of the measurements"),
}

PODS = 'namespace=~"testbed|observability|loadgen",container!="",container!="POD"'
GATEWAY = 'job="api-gateway"'

RANGE_QUERIES = {
    "memory_working_set_bytes": f"max by (namespace, container) (container_memory_working_set_bytes{{{PODS}}})",
    "memory_rss_bytes": f"max by (namespace, container) (container_memory_rss{{{PODS}}})",
    "node_memory_working_set_bytes": 'container_memory_working_set_bytes{id="/"}',
    "node_cpu_cores": 'rate(container_cpu_usage_seconds_total{id="/"}[1m])',
    "cpu_cores_by_container": f"sum by (namespace, container) (rate(container_cpu_usage_seconds_total{{{PODS}}}[1m]))",
    "gateway_requests_total_by_code": f"sum by (code) (http_server_requests_total{{{GATEWAY}}})",
    "gateway_p50_seconds": (
        f"histogram_quantile(0.50, sum by (le) (rate(http_server_request_duration_seconds_bucket{{{GATEWAY}}}[2m])))"
    ),
    "gateway_p99_seconds": (
        f"histogram_quantile(0.99, sum by (le) (rate(http_server_request_duration_seconds_bucket{{{GATEWAY}}}[2m])))"
    ),
    "testbed_restarts_total": 'sum by (container) (kube_pod_container_status_restarts_total{namespace="testbed"})',
    "oom_events_total": f"sum by (namespace, pod, container) (container_oom_events_total{{{PODS}}})",
}


def api(path: str, params: dict[str, str]) -> dict:
    """Call the Prometheus HTTP API and return the decoded response."""
    url = f"{PROMETHEUS}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def instant(query: str, at: str) -> list[dict]:
    """Run an instant query at a fixed time and return its result vector."""
    return api("/api/v1/query", {"query": query, "time": at})["data"]["result"]


def summarise(name: str, start: str, end: str, seconds: int, note: str) -> None:
    """Print the figures for one soak window, all evaluated at its end time."""
    window = f"{seconds}s"
    print(f"== {name}: {start} to {end} ({seconds} s), {note}")

    by_code = {
        r["metric"]["code"]: float(r["value"][1])
        for r in instant(
            f"sum by (code) (increase(http_server_requests_total{{{GATEWAY}}}[{window}]))", end
        )
    }
    total = sum(by_code.values())
    print(
        f"gateway requests: {total:.0f} total; "
        + ", ".join(f"{c}: {n:.0f}" for c, n in sorted(by_code.items()))
    )
    errors = sum(n for c, n in by_code.items() if c.startswith("5"))
    print(f"gateway 5xx share: {100 * errors / total:.1f}%")

    # The histogram's top bucket is 10 s, so 10000 ms means "10 s or more".
    p99 = f"({RANGE_QUERIES['gateway_p99_seconds']})[{window}:30s]"
    for label, expr in (
        ("max", f"max_over_time({p99})"),
        ("median", f"quantile_over_time(0.5, {p99})"),
    ):
        result = instant(expr, end)
        print(
            f"gateway p99 latency, {label} over the window: {1000 * float(result[0]['value'][1]):.0f} ms"
        )

    restarts = instant(
        f'sum(increase(kube_pod_container_status_restarts_total{{namespace="testbed"}}[{window}]))',
        end,
    )
    print(f"testbed container restarts: {float(restarts[0]['value'][1]):.0f}")

    node = instant(f'max_over_time(container_memory_working_set_bytes{{id="/"}}[{window}])', end)
    print(f"node working set peak: {float(node[0]['value'][1]) / 2**20:.0f} MiB")

    peaks = instant(
        f"max by (namespace, container) (max_over_time(container_memory_working_set_bytes{{{PODS}}}[{window}]))",
        end,
    )
    print("peak working set per container (MiB):")
    for r in sorted(peaks, key=lambda r: -float(r["value"][1])):
        print(
            f"  {r['metric']['namespace']:14s} {r['metric']['container']:20s} {float(r['value'][1]) / 2**20:4.0f}"
        )
    print()


def main() -> None:
    for name, query in RANGE_QUERIES.items():
        response = api(
            "/api/v1/query_range",
            {"query": query, "start": RANGE_START, "end": RANGE_END, "step": STEP},
        )
        response["query"] = query
        (OUT / f"{name}.json").write_text(json.dumps(response, indent=1) + "\n")
    for name, (start, end, seconds, note) in WINDOWS.items():
        summarise(name, start, end, seconds, note)


if __name__ == "__main__":
    main()
