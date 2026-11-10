"""The Prometheus, Loki and Tempo tools: which request they make, how they render the answer.

The response bodies are trimmed copies of what the testbed's Prometheus 3.5,
Loki and Tempo 2.8 return.
"""

from typing import Any

import pytest

from oncall.agent.tools import read_only_toolbox
from oncall.agent.tools.base import Toolbox
from oncall.agent.tools.http import LOKI_URL, PROMETHEUS_URL, TEMPO_URL
from tests.agent.conftest import NOW, FakeFetch, now
from tests.chaos.conftest import FakeKubectl


def toolbox(fetch: FakeFetch) -> Toolbox:
    return read_only_toolbox(fetch, FakeKubectl(), now)


def vector(*series: tuple[dict[str, str], str]) -> dict[str, Any]:
    result = [{"metric": labels, "value": [NOW, value]} for labels, value in series]
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


# ---- query_metrics ----------------------------------------------------------------


def test_instant_query(fetch: FakeFetch) -> None:
    fetch.body = vector(({"code": "200"}, "9.123456"), ({"code": "503"}, "0.5"))
    outcome = toolbox(fetch).call("query_metrics", {"promql": "sum by (code) (x)", "minutes": 0})
    assert fetch.requests == [(f"{PROMETHEUS_URL}/api/v1/query", {"query": "sum by (code) (x)"})]
    assert outcome.text == '{code="200"} 9.123\n{code="503"} 0.5'


def test_range_query_covers_the_window_in_about_thirty_points(fetch: FakeFetch) -> None:
    fetch.body = {
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {}, "values": [[NOW - 60, "1"], [NOW, "2.5"]]}],
        }
    }
    outcome = toolbox(fetch).call("query_metrics", {"promql": "x", "minutes": 30})
    assert fetch.requests == [
        (
            f"{PROMETHEUS_URL}/api/v1/query_range",
            {"query": "x", "start": f"{NOW - 1800:.0f}", "end": f"{NOW:.0f}", "step": "60"},
        )
    ]
    assert outcome.text == "Times are UTC; one point every 60 s.\n{}\n  11:59:00=1  12:00:00=2.5"


def test_range_query_step_is_never_finer_than_the_scrape_interval(fetch: FakeFetch) -> None:
    fetch.body = {"data": {"resultType": "matrix", "result": []}}
    toolbox(fetch).call("query_metrics", {"promql": "x", "minutes": 2})
    assert fetch.requests[0][1]["step"] == "10"


def test_no_series_is_said_in_words(fetch: FakeFetch) -> None:
    fetch.body = vector()
    outcome = toolbox(fetch).call("query_metrics", {"promql": "x", "minutes": 0})
    assert outcome.text == "The query returned no series."


def test_scalar_result(fetch: FakeFetch) -> None:
    fetch.body = {"data": {"resultType": "scalar", "result": [NOW, "42"]}}
    assert toolbox(fetch).call("query_metrics", {"promql": "42", "minutes": 0}).text == "42"


def test_too_many_series_are_cut_with_a_count(fetch: FakeFetch) -> None:
    fetch.body = vector(*[({"pod": f"p{i}"}, "1") for i in range(45)])
    lines = toolbox(fetch).call("query_metrics", {"promql": "x", "minutes": 0}).text.splitlines()
    assert len(lines) == 41
    assert lines[-1] == "[showing 40 of 45 series; aggregate or filter to see the rest]"


@pytest.mark.parametrize("minutes", [-1, 361])
def test_minutes_out_of_range_is_rejected_before_any_request(
    fetch: FakeFetch, minutes: int
) -> None:
    outcome = toolbox(fetch).call("query_metrics", {"promql": "x", "minutes": minutes})
    assert outcome.is_error
    assert fetch.requests == []


def test_an_unexpected_body_is_a_tool_error(fetch: FakeFetch) -> None:
    fetch.body = {"status": "success"}
    outcome = toolbox(fetch).call("query_metrics", {"promql": "x", "minutes": 0})
    assert outcome.is_error
    assert "unexpected response from Prometheus" in outcome.text


# ---- list_metrics -----------------------------------------------------------------


def test_list_metrics_matches_whole_names(fetch: FakeFetch) -> None:
    fetch.body = {"data": ["db_pool_idle_connections", "db_pool_max_connections"]}
    outcome = toolbox(fetch).call("list_metrics", {"name_regex": "db_pool_.*"})
    assert fetch.requests == [
        (
            f"{PROMETHEUS_URL}/api/v1/label/__name__/values",
            {"match[]": '{__name__=~"db_pool_.*"}'},
        )
    ]
    assert outcome.text == "db_pool_idle_connections\ndb_pool_max_connections"


@pytest.mark.parametrize("pattern", ['x"} or {job="y', "a\\b"])
def test_list_metrics_refuses_a_pattern_that_could_end_the_matcher(
    fetch: FakeFetch, pattern: str
) -> None:
    outcome = toolbox(fetch).call("list_metrics", {"name_regex": pattern})
    assert outcome.is_error
    assert fetch.requests == []


def test_list_metrics_cuts_a_long_list(fetch: FakeFetch) -> None:
    fetch.body = {"data": [f"m{i}" for i in range(200)]}
    text = toolbox(fetch).call("list_metrics", {"name_regex": ".*"}).text
    assert text.endswith("[showing 150 of 200 names; use a narrower pattern]")


# ---- search_logs ------------------------------------------------------------------


def streams(*entries: tuple[str, float, str]) -> dict[str, Any]:
    """(pod, Unix seconds, line) -> a Loki `streams` result with one stream per entry."""
    result = [
        {"stream": {"k8s_pod_name": pod}, "values": [[f"{seconds * 1e9:.0f}", line]]}
        for pod, seconds, line in entries
    ]
    return {"data": {"resultType": "streams", "result": result}}


def test_search_logs_request(fetch: FakeFetch) -> None:
    fetch.body = streams()
    query = '{service_name="order-service"} |= "error"'
    outcome = toolbox(fetch).call("search_logs", {"logql": query, "minutes": 10, "limit": 20})
    assert fetch.requests == [
        (
            f"{LOKI_URL}/loki/api/v1/query_range",
            {
                "query": query,
                "start": f"{(NOW - 600) * 1e9:.0f}",
                "end": f"{NOW * 1e9:.0f}",
                "limit": "20",
                "direction": "backward",
                "step": "20",
            },
        )
    ]
    assert outcome.text == "No log lines matched."


def test_log_lines_are_merged_across_streams_oldest_first(fetch: FakeFetch) -> None:
    fetch.body = streams(
        ("order-b", NOW - 1, '{"msg":"second"}'),
        ("order-a", NOW - 2.5, '{"msg":"first"}'),
    )
    outcome = toolbox(fetch).call("search_logs", {"logql": "{}", "minutes": 5, "limit": 10})
    assert outcome.text.splitlines() == [
        '11:59:57.500 order-a {"msg":"first"}',
        '11:59:59.000 order-b {"msg":"second"}',
        "[2 lines: every match in the window]",
    ]


def test_log_limit_keeps_the_newest_and_says_there_may_be_more(fetch: FakeFetch) -> None:
    fetch.body = streams(("a", NOW - 3, "first"), ("b", NOW - 2, "mid"), ("c", NOW - 1, "new"))
    lines = toolbox(fetch).call("search_logs", {"logql": "{}", "minutes": 5, "limit": 2}).text
    assert "first" not in lines
    assert "mid" in lines
    assert "new" in lines
    assert lines.endswith("[2 lines, the limit: there may be older matching lines in the window]")


def test_a_long_log_line_is_cut_and_says_by_how_much(fetch: FakeFetch) -> None:
    fetch.body = streams(("a", NOW, "x" * 650))
    text = toolbox(fetch).call("search_logs", {"logql": "{}", "minutes": 5, "limit": 5}).text
    assert "x" * 500 + "...[+150 chars]" in text
    assert "x" * 501 not in text


def test_a_logql_metric_query_is_rendered_as_series(fetch: FakeFetch) -> None:
    fetch.body = {
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {"service_name": "order-service"}, "values": [[NOW, "12"]]}],
        }
    }
    text = toolbox(fetch).call("search_logs", {"logql": "sum(...)", "minutes": 5, "limit": 5}).text
    assert text == 'Times are UTC.\n{service_name="order-service"}\n  12:00:00=12'


# ---- search_traces, get_trace ------------------------------------------------------


def test_search_traces(fetch: FakeFetch) -> None:
    fetch.body = {
        "traces": [
            {
                "traceID": "4ad88e3c25f058c6585f8fe00b91c96",
                "rootServiceName": "api-gateway",
                "rootTraceName": "POST /api/orders",
                "startTimeUnixNano": f"{NOW * 1e9:.0f}",
                "durationMs": 4620,
                "spanSet": {"matched": 2},
            }
        ]
    }
    query = "{ status = error }"
    outcome = toolbox(fetch).call("search_traces", {"traceql": query, "minutes": 10, "limit": 5})
    assert fetch.requests == [
        (
            f"{TEMPO_URL}/api/search",
            {"q": query, "start": f"{NOW - 600:.0f}", "end": f"{NOW:.0f}", "limit": "5"},
        )
    ]
    assert outcome.text.splitlines()[1] == (
        "4ad88e3c25f058c6585f8fe00b91c96  12:00:00  4620 ms  api-gateway  POST /api/orders  2"
    )


def span(
    span_id: str, parent: str, name: str, start_ms: float, end_ms: float, **extra: object
) -> dict[str, Any]:
    return {
        "spanId": span_id,
        "parentSpanId": parent,
        "name": name,
        "kind": "SPAN_KIND_CLIENT",
        "startTimeUnixNano": f"{NOW * 1e9 + start_ms * 1e6:.0f}",
        "endTimeUnixNano": f"{NOW * 1e9 + end_ms * 1e6:.0f}",
        **extra,
    }


def batch(service: str, *spans: dict[str, Any]) -> dict[str, Any]:
    return {
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
        "scopeSpans": [{"spans": list(spans)}],
    }


def test_get_trace_renders_a_tree_with_errors(fetch: FakeFetch) -> None:
    failed = {"code": "STATUS_CODE_ERROR", "message": "context deadline exceeded"}
    fetch.body = {
        "batches": [
            # Deliberately out of order: the tree is built from parent IDs and start times.
            batch(
                "order-service",
                span("Yg==", "YQ==", "shop.v1.InventoryService/ReserveStock", 2, 12, status=failed),
            ),
            batch(
                "api-gateway",
                span(
                    "YQ==",
                    "",
                    "POST /api/orders",
                    0,
                    15,
                    attributes=[{"key": "http.response.status_code", "value": {"intValue": "503"}}],
                ),
            ),
        ]
    }
    outcome = toolbox(fetch).call("get_trace", {"trace_id": "ABC123"})
    assert fetch.requests == [(f"{TEMPO_URL}/api/traces/abc123", {})]
    assert outcome.text.splitlines() == [
        "Trace abc123: 2 spans across api-gateway, order-service; "
        "started 12:00:00.000 UTC, 15.0 ms in all.",
        "service | operation | kind | start offset | duration | details",
        "api-gateway | POST /api/orders | client | +0.0 ms | 15.0 ms | "
        "http.response.status_code=503",
        "  order-service | shop.v1.InventoryService/ReserveStock | client | +2.0 ms | 10.0 ms | "
        "ERROR: context deadline exceeded",
    ]


def test_get_trace_shows_at_most_sixty_spans(fetch: FakeFetch) -> None:
    spans = [span(f"s{i}", "", f"op{i}", i, i + 1) for i in range(70)]
    fetch.body = {"batches": [batch("api-gateway", *spans)]}
    lines = toolbox(fetch).call("get_trace", {"trace_id": "abc"}).text.splitlines()
    assert len(lines) == 2 + 60 + 1
    assert lines[-1] == "[showing 60 of 70 spans]"


@pytest.mark.parametrize("trace_id", ["../../loki/api/v1/labels", "abc?x=1", "", "g" * 8, "a" * 33])
def test_get_trace_only_accepts_a_hexadecimal_id(fetch: FakeFetch, trace_id: str) -> None:
    """The ID becomes part of a URL path."""
    outcome = toolbox(fetch).call("get_trace", {"trace_id": trace_id})
    assert outcome.is_error
    assert fetch.requests == []
