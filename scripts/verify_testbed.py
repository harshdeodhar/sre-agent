#!/usr/bin/env python3
"""Check that the testbed is serving traffic and that telemetry is flowing.

This is the Phase 1 acceptance test, run with `make verify` against a cluster
started by `make up`. It asks each system what it actually has:

  gateway     the API answers, and an order can be created and read back
  Prometheus  every scrape target is up, traffic is non-zero, and every query
              used by the Grafana dashboard returns data
  Loki        each Go service has recent log lines
  Tempo       a recent order's trace crosses all four services
  Grafana     healthy, data sources reachable through it, dashboard loaded

Every check prints what it measured. Exit status is non-zero if any check
fails. Standard library only, so it runs before the Python project exists.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

GATEWAY = "http://localhost:8080"
PROMETHEUS = "http://localhost:9090"
LOKI = "http://localhost:3100"
TEMPO = "http://localhost:3200"
GRAFANA = "http://localhost:3000"

DASHBOARD = (
    Path(__file__).resolve().parent.parent / "deploy/observability/dashboards/testbed-overview.json"
)

SERVICES = ["api-gateway", "order-service", "inventory-service", "notification-worker"]

# Scrape jobs that must exist, with the number of targets each should have.
EXPECTED_TARGETS = {
    "api-gateway": 2,
    "order-service": 2,
    "inventory-service": 2,
    "notification-worker": 1,
    "postgres": 1,
    "redis": 1,
    "nats": 1,
    "loadgen": 1,
    "otel-collector": 1,
    "kube-state-metrics": 1,
    "kubelet-cadvisor": 1,
}

# Dashboard panels that are empty when the system is healthy.
EMPTY_WHEN_HEALTHY = {"Container restarts, last hour"}

failures = 0


def report(ok: bool, name: str, detail: str) -> None:
    global failures
    if not ok:
        failures += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


def http(url: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    """GET (or POST JSON when body is given). Returns (status, parsed JSON or text)."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw.decode(errors="replace")


def url_with(base: str, **params: str) -> str:
    return f"{base}?{urllib.parse.urlencode(params)}"


def prom_query(expr: str) -> list[dict[str, Any]]:
    """Run an instant PromQL query and return the result series."""
    status, payload = http(url_with(f"{PROMETHEUS}/api/v1/query", query=expr))
    if status != 200 or payload.get("status") != "success":
        raise RuntimeError(f"query failed ({status}): {payload}")
    return payload["data"]["result"]


def check_gateway() -> None:
    status, product = http(f"{GATEWAY}/api/products/SKU-0001")
    report(
        status == 200 and product.get("sku") == "SKU-0001",
        "gateway: read product",
        f"HTTP {status} {product}",
    )

    status, order = http(
        f"{GATEWAY}/api/orders", {"customer_id": "cust-00001", "sku": "SKU-0001", "quantity": 1}
    )
    order_id = order.get("id", "") if isinstance(order, dict) else ""
    report(
        status == 201 and bool(order_id), "gateway: create order", f"HTTP {status} id={order_id!r}"
    )

    status, fetched = http(f"{GATEWAY}/api/orders/{order_id}")
    same = isinstance(fetched, dict) and fetched.get("id") == order_id
    report(status == 200 and same, "gateway: read the order back", f"HTTP {status}")

    # The list is newest first and capped, and the load generator orders for
    # this customer too, so check membership, not position.
    status, listed = http(f"{GATEWAY}/api/customers/cust-00001/orders")
    ids = [item.get("id") for item in listed] if isinstance(listed, list) else []
    report(
        status == 200 and order_id in ids,
        "gateway: list customer orders",
        f"HTTP {status}, {len(ids)} orders, includes the one just created: {order_id in ids}",
    )


def check_prometheus() -> None:
    up = prom_query("up")
    targets: dict[str, list[float]] = {}
    for series in up:
        targets.setdefault(series["metric"]["job"], []).append(float(series["value"][1]))
    for job, want in EXPECTED_TARGETS.items():
        got = targets.get(job, [])
        healthy = sum(1 for v in got if v == 1)
        report(
            healthy == want and len(got) == want,
            f"prometheus: target {job}",
            f"{healthy}/{want} up",
        )
    unexpected = sorted(set(targets) - set(EXPECTED_TARGETS))
    report(not unexpected, "prometheus: no unexpected scrape jobs", f"{unexpected or 'none'}")

    rate = prom_query('sum(rate(http_server_requests_total{job="api-gateway"}[1m]))')
    rps = float(rate[0]["value"][1]) if rate else 0.0
    report(
        rps > 1,
        "prometheus: live traffic at the gateway",
        f"{rps:.1f} requests/s over the last minute",
    )

    errors = prom_query(
        'sum(rate(http_server_requests_total{job="api-gateway",code=~"5.."}[1m])) or vector(0)'
    )
    report(
        float(errors[0]["value"][1]) == 0,
        "prometheus: no 5xx at the gateway",
        f"{float(errors[0]['value'][1]):.3f} errors/s",
    )


def check_dashboard_queries() -> None:
    """Every Prometheus query on the dashboard must return data on a healthy system.

    A panel that silently shows "No data" because of a mistyped metric or label
    name looks the same as a healthy quiet panel, so this is checked by running
    the queries, not by looking.
    """
    dashboard = json.loads(DASHBOARD.read_text())
    total = empty = 0
    for panel in dashboard["panels"]:
        for query in panel.get("targets", []):
            if query["datasource"]["type"] != "prometheus":
                continue
            total += 1
            try:
                result = prom_query(query["expr"])
            except RuntimeError as err:
                report(False, f"dashboard: {panel['title']}", str(err))
                continue
            if result or panel["title"] in EMPTY_WHEN_HEALTHY:
                continue
            # A status-class series (4xx, 5xx) may legitimately be absent.
            if panel["title"] == "Responses by status class" and query["legendFormat"] != "2xx":
                continue
            empty += 1
            report(False, f"dashboard: {panel['title']}", f"no data for: {query['expr']}")
    report(
        empty == 0,
        "dashboard: Prometheus queries return data",
        f"{total - empty}/{total} queries checked",
    )

    # The logs panel shows warnings and errors, which a healthy system may not
    # have, so only check that Loki accepts the query.
    for panel in dashboard["panels"]:
        for query in panel.get("targets", []):
            if query["datasource"]["type"] != "loki":
                continue
            status, payload = http(
                url_with(f"{LOKI}/loki/api/v1/query_range", query=query["expr"], limit="5")
            )
            streams = len(payload["data"]["result"]) if status == 200 else 0
            report(
                status == 200,
                f"dashboard: {panel['title']} (Loki)",
                f"HTTP {status}, {streams} streams",
            )


def loki_count(expr: str) -> list[dict[str, Any]]:
    """Run an instant LogQL metric query and return the result series."""
    status, payload = http(url_with(f"{LOKI}/loki/api/v1/query", query=expr))
    if status != 200:
        raise RuntimeError(f"query failed ({status}): {payload}")
    return payload["data"]["result"]


def check_loki() -> None:
    # 30 minutes, not 5: the quieter services (the worker logs only at startup
    # and on errors) can go a long time without writing a line.
    for service in SERVICES:
        result = loki_count(
            f'sum(count_over_time({{service_name="{service}", k8s_namespace_name="testbed"}}[30m]))'
        )
        lines = int(float(result[0]["value"][1])) if result else 0
        report(lines > 0, f"loki: logs from {service}", f"{lines} lines in the last 30 minutes")

    # Logs written inside a request must carry the trace ID, or logs and traces
    # cannot be joined.
    result = loki_count('sum(count_over_time({service_name="api-gateway"} | trace_id != "" [5m]))')
    lines = int(float(result[0]["value"][1])) if result else 0
    report(
        lines > 0,
        "loki: gateway logs carry trace_id",
        f"{lines} lines with a trace_id in the last 5 minutes",
    )

    # One spelling per level across every container, so a filter such as
    # detected_level="error" cannot silently miss a service.
    result = loki_count(
        'sum by (detected_level) (count_over_time({k8s_namespace_name="testbed"}[5m]))'
    )
    levels = sorted(series["metric"].get("detected_level", "") for series in result)
    report(
        bool(levels) and all(level == level.lower() for level in levels),
        "loki: log levels are lower-case",
        f"levels seen in the last 5 minutes: {levels}",
    )

    # Only the testbed namespace may be ingested.
    status, payload = http(f"{LOKI}/loki/api/v1/label/k8s_namespace_name/values")
    namespaces = payload.get("data", []) if status == 200 else []
    report(
        namespaces == ["testbed"],
        "loki: only testbed logs are ingested",
        f"namespaces seen: {namespaces}",
    )


def check_tempo() -> None:
    # Find a recent trace that reached the notification worker, then check the
    # whole path is in it: HTTP at the gateway, gRPC to order-service and
    # inventory-service, and the hop through NATS to the worker.
    #
    # The search window ends 30 s ago: each service exports its spans in
    # batches every few seconds, so a trace younger than that may not have all
    # of its spans in Tempo yet.
    query = '{resource.service.name="notification-worker"}'
    now = int(time.time())
    status, payload = http(
        url_with(f"{TEMPO}/api/search", q=query, limit="5", start=str(now - 300), end=str(now - 30))
    )
    traces = payload.get("traces", []) if status == 200 and isinstance(payload, dict) else []
    report(
        bool(traces),
        "tempo: recent traces reach notification-worker",
        f"{len(traces)} found (HTTP {status})",
    )
    if not traces:
        return

    trace_id = traces[0]["traceID"]
    status, trace = http(f"{TEMPO}/api/traces/{trace_id}")
    services: set[str] = set()
    spans = 0
    for batch in trace.get("batches", []) if isinstance(trace, dict) else []:
        for attribute in batch["resource"]["attributes"]:
            if attribute["key"] == "service.name":
                services.add(attribute["value"]["stringValue"])
        spans += sum(len(scope.get("spans", [])) for scope in batch.get("scopeSpans", []))
    report(
        services >= set(SERVICES),
        "tempo: one trace spans all four services",
        f"trace {trace_id}: {spans} spans from {sorted(services)}",
    )


def check_grafana() -> None:
    status, health = http(f"{GRAFANA}/api/health")
    database = health.get("database") if isinstance(health, dict) else None
    report(
        status == 200 and database == "ok",
        "grafana: healthy",
        f"HTTP {status}, database {database}",
    )

    # Ask Grafana itself to reach each data source, proving its side of the wiring.
    for uid in ("prometheus", "loki"):
        status, payload = http(f"{GRAFANA}/api/datasources/uid/{uid}/health")
        message = payload.get("message", payload) if isinstance(payload, dict) else payload
        report(status == 200, f"grafana: data source {uid}", f"HTTP {status}: {message}")
    # Grafana's Tempo data source has no health endpoint (it answers 404
    # "Method not implemented"), so call Tempo's echo endpoint through Grafana's
    # data source proxy instead.
    status, payload = http(f"{GRAFANA}/api/datasources/proxy/uid/tempo/api/echo")
    # Tempo answers "echo" followed by a newline.
    answer = payload.strip() if isinstance(payload, str) else payload
    report(
        status == 200 and answer == "echo",
        "grafana: data source tempo",
        f"HTTP {status}: {payload!r}",
    )

    status, payload = http(f"{GRAFANA}/api/dashboards/uid/testbed-overview")
    panels = len(payload["dashboard"]["panels"]) if status == 200 else 0
    report(
        status == 200 and panels > 0,
        "grafana: dashboard provisioned",
        f"HTTP {status}, {panels} panels",
    )

    # And one real query through Grafana, the way a dashboard panel runs it.
    body = {
        "from": "now-5m",
        "to": "now",
        "queries": [
            {
                "refId": "A",
                "datasource": {"type": "prometheus", "uid": "prometheus"},
                "expr": 'sum(rate(http_server_requests_total{job="api-gateway"}[1m]))',
                "instant": True,
            }
        ],
    }
    status, payload = http(f"{GRAFANA}/api/ds/query", body)
    value = None
    if status == 200:
        frames = payload["results"]["A"].get("frames", [])
        if frames and frames[0]["data"]["values"]:
            value = frames[0]["data"]["values"][-1][-1]
    report(
        value is not None and value > 1,
        "grafana: live traffic through Grafana",
        f"request rate {value if value is None else round(value, 1)} requests/s",
    )


def main() -> int:
    checks = [
        check_gateway,
        check_prometheus,
        check_dashboard_queries,
        check_loki,
        check_tempo,
        check_grafana,
    ]
    for check in checks:
        try:
            check()
        except (
            urllib.error.URLError,
            OSError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            RuntimeError,
        ) as err:
            report(False, check.__name__, f"{type(err).__name__}: {err}")
    print()
    print("All checks passed." if failures == 0 else f"{failures} check(s) FAILED.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
