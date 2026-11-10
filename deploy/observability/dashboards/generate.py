#!/usr/bin/env python3
"""Generate the Grafana dashboard JSON for the testbed.

Grafana dashboards are large, repetitive JSON documents. Writing them by hand
makes it easy for panels to drift apart (different units, different colours for
the same service). This script is the source of truth; the JSON next to it is
its output and is what Grafana loads. Regenerate with `make dashboards`.

Conventions applied to every panel:
  * One y-axis per panel, always with a unit. Two measures with different
    units get two panels.
  * A service has the same colour everywhere (SERVICE_COLOURS). Colours are
    assigned by name, never by the order series happen to come back in.
  * Red, amber and green are reserved for state (errors, thresholds) and are
    never used to tell ordinary series apart.

Standard library only, so it runs with any Python 3.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

Panel = dict[str, Any]

PROMETHEUS = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}

# Categorical palette chosen for Grafana's dark theme. The order matters:
# neighbours in this list stay distinguishable under the common forms of
# colour blindness (checked with a palette validator, not by eye).
BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN, VIOLET = (
    "#3987e5",
    "#d95926",
    "#199e70",
    "#c98500",
    "#d55181",
    "#008300",
    "#9085e9",
)

SERVICE_COLOURS = {
    "api-gateway": BLUE,
    "order-service": ORANGE,
    "inventory-service": AQUA,
    "notification-worker": YELLOW,
    "postgres": MAGENTA,
    "redis": GREEN,
    "nats": VIOLET,
}

# State colours: only for "good / warning / critical".
GOOD, WARNING, CRITICAL = "#0ca30c", "#fab219", "#d03b3b"

# Rate window. Four times the 10 s scrape interval is the usual minimum; one
# minute smooths the load generator's randomness without hiding a short spike.
WINDOW = "1m"

GATEWAY = 'job="api-gateway"'


def colour_overrides(colours: dict[str, str]) -> list[dict[str, Any]]:
    """Pin each named series to a colour."""
    return [
        {
            "matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": colour}}],
        }
        for name, colour in colours.items()
    ]


def target(expr: str, legend: str, ref: str = "A", instant: bool = False) -> dict[str, Any]:
    return {
        "datasource": PROMETHEUS,
        "refId": ref,
        "expr": expr,
        "legendFormat": legend,
        "instant": instant,
        "range": not instant,
    }


def targets(*pairs: tuple[str, str]) -> list[dict[str, Any]]:
    """Build one query per (expression, legend) pair, with refIds A, B, C..."""
    return [target(expr, legend, ref=chr(ord("A") + i)) for i, (expr, legend) in enumerate(pairs)]


def row(title: str, y: int) -> Panel:
    return {
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
    }


def timeseries(
    title: str,
    description: str,
    queries: list[dict[str, Any]],
    unit: str,
    colours: dict[str, str],
    *,
    stacked: bool = False,
    max_value: float | None = None,
) -> Panel:
    defaults: dict[str, Any] = {
        "unit": unit,
        "min": 0,
        "custom": {
            "drawStyle": "line",
            "lineWidth": 2,
            # Lines only, unless the panel is a stack, where the filled area is
            # the quantity being read.
            "fillOpacity": 60 if stacked else 0,
            "showPoints": "never",
            "spanNulls": False,
            "stacking": {"mode": "normal" if stacked else "none"},
        },
    }
    if max_value is not None:
        defaults["max"] = max_value
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": PROMETHEUS,
        "targets": queries,
        "fieldConfig": {"defaults": defaults, "overrides": colour_overrides(colours)},
        "options": {
            "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
            # Hovering shows every series at that instant, largest first.
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def stat(
    title: str,
    description: str,
    expr: str,
    unit: str,
    *,
    thresholds: list[tuple[float | None, str]] | None = None,
    decimals: int | None = None,
) -> Panel:
    """A single current value with a sparkline of its recent history.

    Without thresholds the tile is neutral. With thresholds the tile's
    background shows the state, and the number itself stays readable text.
    """
    steps = thresholds or [(None, BLUE)]
    defaults: dict[str, Any] = {
        "unit": unit,
        "color": {"mode": "thresholds"},
        "thresholds": {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in steps]},
        "noValue": "no data",
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": PROMETHEUS,
        "targets": [target(expr, title)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "background" if thresholds else "none",
            "graphMode": "area",
            "textMode": "value",
        },
    }


def bar_gauge(
    title: str,
    description: str,
    expr: str,
    legend: str,
    unit: str,
    thresholds: list[tuple[float | None, str]],
    *,
    max_value: float | None = None,
    no_value: str = "no data",
) -> Panel:
    """Horizontal bars comparing the current value across a set of things."""
    defaults: dict[str, Any] = {
        "unit": unit,
        "min": 0,
        "color": {"mode": "thresholds"},
        "thresholds": {
            "mode": "absolute",
            "steps": [{"value": v, "color": c} for v, c in thresholds],
        },
        "noValue": no_value,
    }
    if max_value is not None:
        defaults["max"] = max_value
    return {
        "type": "bargauge",
        "title": title,
        "description": description,
        "datasource": PROMETHEUS,
        "targets": [target(expr, legend, instant=True)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "orientation": "horizontal",
            "displayMode": "basic",
            "valueMode": "text",
            "showUnfilled": True,
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
    }


def logs(title: str, description: str, expr: str) -> Panel:
    return {
        "type": "logs",
        "title": title,
        "description": description,
        "datasource": LOKI,
        "targets": [{"datasource": LOKI, "refId": "A", "expr": expr, "maxLines": 200}],
        "options": {
            "showTime": True,
            "wrapLogMessage": True,
            "enableLogDetails": True,
            "sortOrder": "Descending",
            "dedupStrategy": "none",
        },
    }


def place(panels: list[Panel], y: int, height: int) -> list[Panel]:
    """Lay panels out left to right in one band, sharing the 24-column grid equally."""
    width = 24 // len(panels)
    for i, panel in enumerate(panels):
        panel["gridPos"] = {"x": i * width, "y": y, "w": width, "h": height}
    return panels


def http_rate(selector: str) -> str:
    return f"sum(rate(http_server_requests_total{{{selector}}}[{WINDOW}]))"


def quantile(q: float, metric: str, selector: str, by: str = "") -> str:
    group = f"{by}, le" if by else "le"
    return (
        f"histogram_quantile({q}, sum by ({group}) (rate({metric}_bucket{{{selector}}}[{WINDOW}])))"
    )


def overview() -> dict[str, Any]:
    panels: list[Panel] = []

    # ---- Headline numbers ---------------------------------------------------
    panels.append(row("Is it working?", 0))
    panels += place(
        [
            stat(
                "Request rate",
                "Requests per second arriving at the api-gateway.",
                http_rate(GATEWAY),
                "reqps",
                decimals=1,
            ),
            stat(
                "Error ratio",
                "Share of gateway responses that are 5xx. Client errors (4xx) are not counted: "
                "a 409 for an out-of-stock product is the system working.",
                # "or vector(0)": with no 5xx at all the numerator has no
                # series, and the ratio should then read 0, not "no data".
                f"({http_rate(GATEWAY + ', code=~"5.."')} or vector(0)) / {http_rate(GATEWAY)}",
                "percentunit",
                thresholds=[(None, GOOD), (0.01, WARNING), (0.05, CRITICAL)],
                decimals=2,
            ),
            stat(
                "Latency p95",
                "95th percentile response time at the api-gateway.",
                quantile(0.95, "http_server_request_duration_seconds", GATEWAY),
                "s",
            ),
            stat(
                "Unavailable replicas",
                "Desired minus available replicas, summed over every Deployment in the testbed namespace.",
                'sum(kube_deployment_spec_replicas{namespace="testbed"}) '
                '- sum(kube_deployment_status_replicas_available{namespace="testbed"})',
                "none",
                thresholds=[(None, GOOD), (1, CRITICAL)],
            ),
            stat(
                "Notification lag p95",
                "Time from an order being stored to its notification being sent "
                "(order-service -> NATS JetStream -> notification-worker).",
                quantile(0.95, "notification_delivery_lag_seconds", 'job="notification-worker"'),
                "s",
            ),
            stat(
                "Node memory in use",
                "Working-set memory of the whole k3s node, i.e. everything in the cluster. "
                "The Docker VM it runs in has about 4 GB.",
                'container_memory_working_set_bytes{id="/"}',
                "bytes",
            ),
        ],
        y=1,
        height=4,
    )

    # ---- Gateway traffic ----------------------------------------------------
    routes = {
        "GET /api/products/{sku}": BLUE,
        "POST /api/orders": ORANGE,
        "GET /api/orders/{id}": AQUA,
        "GET /api/customers/{id}/orders": YELLOW,
    }
    panels.append(row("Traffic at the gateway", 5))
    panels += place(
        [
            timeseries(
                "Requests by route",
                "Requests per second at the api-gateway, by route.",
                targets(
                    (
                        f"sum by (route) (rate(http_server_requests_total{{{GATEWAY}}}[{WINDOW}]))",
                        "{{route}}",
                    )
                ),
                "reqps",
                routes,
            ),
            timeseries(
                "Responses by status class",
                "Gateway responses per second, stacked by HTTP status class.",
                targets(
                    (http_rate(GATEWAY + ', code=~"2.."'), "2xx"),
                    (http_rate(GATEWAY + ', code=~"4.."'), "4xx"),
                    (http_rate(GATEWAY + ', code=~"5.."'), "5xx"),
                ),
                "reqps",
                # Status classes are states, so they take the state colours.
                {"2xx": GOOD, "4xx": WARNING, "5xx": CRITICAL},
                stacked=True,
            ),
            timeseries(
                "Latency percentiles",
                "Gateway response time. p50 is the typical request; p95 and p99 show the slow tail.",
                targets(
                    (quantile(0.50, "http_server_request_duration_seconds", GATEWAY), "p50"),
                    (quantile(0.95, "http_server_request_duration_seconds", GATEWAY), "p95"),
                    (quantile(0.99, "http_server_request_duration_seconds", GATEWAY), "p99"),
                ),
                "s",
                # One hue, three steps: the three lines are one ordered family.
                {"p50": "#1c5cab", "p95": "#3987e5", "p99": "#9ec5f4"},
            ),
        ],
        y=6,
        height=8,
    )

    # ---- gRPC between services ---------------------------------------------
    grpc = 'namespace="testbed"'
    handled = f"sum by (job) (rate(grpc_server_handled_total{{{grpc}}}[{WINDOW}]))"
    # Codes that mean the caller asked for something impossible. Everything
    # else (Internal, Unavailable, DeadlineExceeded, ...) is the service or one
    # of its dependencies failing. Same split as rpc.IsCallerError in the Go code.
    caller_codes = "OK|InvalidArgument|NotFound|FailedPrecondition|AlreadyExists|Canceled"
    failed = f'sum by (job) (rate(grpc_server_handled_total{{{grpc}, grpc_code!~"{caller_codes}"}}[{WINDOW}]))'
    grpc_services = {k: SERVICE_COLOURS[k] for k in ("order-service", "inventory-service")}
    panels.append(row("Calls between services (gRPC, measured by the service being called)", 14))
    panels += place(
        [
            timeseries(
                "Calls handled",
                "gRPC calls per second handled by each service.",
                targets((handled, "{{job}}")),
                "reqps",
                grpc_services,
            ),
            timeseries(
                "Failure ratio",
                "Share of gRPC calls that failed because of the service or its dependencies. "
                "Caller mistakes (NotFound, FailedPrecondition, InvalidArgument) are excluded.",
                # "or handled * 0": a service with no failures still gets a line at zero.
                targets((f"({failed} or {handled} * 0) / {handled}", "{{job}}")),
                "percentunit",
                grpc_services,
            ),
            timeseries(
                "Handling time p95",
                "95th percentile time each service takes to handle a gRPC call.",
                targets(
                    (quantile(0.95, "grpc_server_handling_seconds", grpc, by="job"), "{{job}}")
                ),
                "s",
                grpc_services,
            ),
        ],
        y=15,
        height=8,
    )

    # ---- Dependencies -------------------------------------------------------
    db_services = {
        k: SERVICE_COLOURS[k] for k in ("order-service", "inventory-service", "notification-worker")
    }
    queries = {
        "orders.insert": BLUE,
        "orders.get": ORANGE,
        "orders.list_by_customer": AQUA,
        "stock.get": YELLOW,
        "stock.reserve": MAGENTA,
        "stock.restock": GREEN,
        "notifications.insert": VIOLET,
    }
    panels.append(row("Dependencies: Postgres, Redis, NATS", 23))
    panels += place(
        [
            timeseries(
                "Postgres pool in use",
                "Connections checked out of each service's pool, as a share of the pool's size "
                "(summed over replicas). At 100% requests queue for a connection.",
                targets(
                    (
                        "sum by (job) (db_pool_acquired_connections) / sum by (job) (db_pool_max_connections)",
                        "{{job}}",
                    )
                ),
                "percentunit",
                db_services,
                max_value=1,
            ),
            timeseries(
                "Query time p95",
                "95th percentile duration of each named SQL query, measured in the services.",
                targets(
                    (quantile(0.95, "db_query_duration_seconds", grpc, by="query"), "{{query}}")
                ),
                "s",
                queries,
            ),
            timeseries(
                "Cache hit ratio",
                "Share of stock lookups in inventory-service answered from Redis.",
                targets(
                    (
                        f'sum(rate(inventory_cache_requests_total{{result="hit"}}[{WINDOW}])) '
                        f"/ sum(rate(inventory_cache_requests_total[{WINDOW}]))",
                        "hit ratio",
                    )
                ),
                "percentunit",
                {"hit ratio": SERVICE_COLOURS["redis"]},
                max_value=1,
            ),
            timeseries(
                "Event backlog",
                "orders.created events in JetStream that notification-worker has not finished: "
                "not yet delivered, or delivered and not yet acknowledged.",
                targets(
                    (
                        'sum(jetstream_consumer_num_pending{consumer_name="notification-worker"})',
                        "not yet delivered",
                    ),
                    (
                        'sum(jetstream_consumer_num_ack_pending{consumer_name="notification-worker"})',
                        "delivered, not acknowledged",
                    ),
                ),
                "short",
                {"not yet delivered": VIOLET, "delivered, not acknowledged": BLUE},
            ),
        ],
        y=24,
        height=8,
    )

    # ---- Resources ----------------------------------------------------------
    containers = 'namespace="testbed", container!=""'
    main_containers = 'namespace="testbed", container!="", container!~".*-exporter"'
    panels.append(row("Resources (testbed namespace)", 32))
    panels += place(
        [
            bar_gauge(
                "Memory against limit",
                "Working-set memory of each container as a share of its memory limit "
                "(the fullest replica). A container that reaches 100% is OOM-killed.",
                f"max by (container) (container_memory_working_set_bytes{{{containers}}}) "
                f"/ max by (container) (container_spec_memory_limit_bytes{{{containers}}} > 0)",
                "{{container}}",
                "percentunit",
                [(None, BLUE), (0.8, WARNING), (0.95, CRITICAL)],
                max_value=1,
            ),
            timeseries(
                "CPU by workload",
                "CPU cores used, summed over the replicas of each workload. Exporter sidecars are left out.",
                targets(
                    (
                        f"sum by (container) (rate(container_cpu_usage_seconds_total{{{main_containers}}}[{WINDOW}]))",
                        "{{container}}",
                    )
                ),
                "none",
                SERVICE_COLOURS,
            ),
            bar_gauge(
                "Container restarts, last hour",
                "Containers that restarted in the last hour, and how often. Empty is the healthy state.",
                'sum by (container) (increase(kube_pod_container_status_restarts_total{namespace="testbed"}[1h])) > 0',
                "{{container}}",
                "none",
                [(None, CRITICAL)],
                no_value="no restarts",
            ),
        ],
        y=33,
        height=9,
    )

    # ---- Logs ---------------------------------------------------------------
    panels.append(row("Logs", 42))
    panels += place(
        [
            logs(
                "Warnings and errors",
                "Log lines at WARN or ERROR from the testbed namespace. Expand a line and follow "
                "its trace_id to open the request's trace in Tempo.",
                '{k8s_namespace_name="testbed"} | detected_level=~"warn|error"',
            )
        ],
        y=43,
        height=10,
    )

    for panel_id, panel in enumerate(panels, start=1):
        panel["id"] = panel_id

    return {
        "uid": "testbed-overview",
        "title": "Testbed overview",
        "description": "Health of the shop testbed: traffic, service-to-service calls, dependencies, resources, logs.",
        "tags": ["sre-testbed"],
        "schemaVersion": 41,
        "editable": True,
        "graphTooltip": 1,  # shared crosshair: hovering one panel marks the same instant on all
        "time": {"from": "now-15m", "to": "now"},
        "refresh": "10s",
        "timezone": "browser",
        "panels": panels,
    }


def main() -> None:
    out = Path(__file__).with_name("testbed-overview.json")
    out.write_text(json.dumps(overview(), indent=2) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
