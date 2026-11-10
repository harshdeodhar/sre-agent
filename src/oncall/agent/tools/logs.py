"""Loki tool: search log lines, or run a LogQL metric query."""

from collections.abc import Callable
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from oncall.agent.tools.base import Params, Tool, ToolError, between
from oncall.agent.tools.http import LOKI_URL, Fetch
from oncall.agent.tools.render import Series, clock_time, render_series

MAX_LINES = 100
# A log line here is one JSON object of 200 to 400 characters. A line cut at
# this length still shows its level, message and error.
MAX_LINE_CHARS = 500
MAX_METRIC_SERIES = 12
METRIC_POINTS = 30


class SearchLogs(Params):
    logql: str = Field(
        description=(
            "A LogQL query. A log query returns lines, e.g. "
            '{service_name="order-service"} |= "error". A metric query returns numbers, '
            'e.g. sum by (service_name) (count_over_time({k8s_namespace_name="testbed"} '
            '| detected_level="error" [1m])).'
        )
    )
    minutes: Annotated[int, between(1, 360)] = Field(
        description="How far back to search, in minutes before now (1 to 360)."
    )
    limit: Annotated[int, between(1, MAX_LINES)] = Field(
        description=f"The most log lines to return (1 to {MAX_LINES}). The newest are kept."
    )


class _Stream(BaseModel):
    model_config = ConfigDict(extra="ignore")

    stream: dict[str, str] = {}
    values: list[tuple[str, str]] = []  # (Unix time in nanoseconds, line)


class _Data(BaseModel):
    model_config = ConfigDict(extra="ignore")

    resultType: str
    result: list[Any]


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")

    data: _Data


def logs_tools(fetch: Fetch, now: Callable[[], float]) -> list[Tool[Any]]:
    def search_logs(params: SearchLogs) -> str:
        end = now()
        span = params.minutes * 60
        body = fetch(
            f"{LOKI_URL}/loki/api/v1/query_range",
            {
                "query": params.logql,
                "start": f"{(end - span) * 1e9:.0f}",
                "end": f"{end * 1e9:.0f}",
                "limit": str(params.limit),
                # Newest first, so that `limit` keeps the most recent lines.
                "direction": "backward",
                # Only used by metric queries.
                "step": str(max(10, span // METRIC_POINTS)),
            },
        )
        try:
            data = _Response.model_validate(body).data
            if data.resultType == "streams":
                streams = [_Stream.model_validate(item) for item in data.result]
                return _render_lines(streams, params.limit)
            series = [Series.model_validate(item) for item in data.result]
        except ValidationError as err:
            raise ToolError(f"unexpected response from Loki: {err}") from err
        return "Times are UTC.\n" + render_series(series, MAX_METRIC_SERIES)

    return [
        Tool(
            name="search_logs",
            description=(
                "Search the logs of the testbed's containers in Loki with LogQL, over the "
                "last N minutes. Stream labels: service_name, k8s_pod_name, "
                "k8s_container_name, k8s_namespace_name. The severity is available as "
                'detected_level ("error", "warn", "info"). The services log one JSON object '
                "per line (fields: time, level, msg, err, trace_id and others), so "
                "`| json` makes their fields filterable. Logs of containers that have "
                "since crashed or been replaced are kept. Lines are returned oldest first."
            ),
            params=SearchLogs,
            run=search_logs,
        )
    ]


def _render_lines(streams: list[_Stream], limit: int) -> str:
    entries = [
        (int(timestamp), stream.stream.get("k8s_pod_name", "?"), line)
        for stream in streams
        for timestamp, line in stream.values
    ]
    if not entries:
        return "No log lines matched."
    # Loki applies the limit per query, but sorts within each stream only.
    entries.sort(key=lambda entry: entry[0])
    entries = entries[-limit:]
    lines = []
    for timestamp, pod, line in entries:
        if len(line) > MAX_LINE_CHARS:
            line = f"{line[:MAX_LINE_CHARS]}...[+{len(line) - MAX_LINE_CHARS} chars]"
        lines.append(f"{clock_time(timestamp / 1e9, millis=True)} {pod} {line}")
    note = (
        f"[{len(entries)} lines, the limit: there may be older matching lines in the window]"
        if len(entries) >= limit
        else f"[{len(entries)} lines: every match in the window]"
    )
    return "\n".join([*lines, note])
