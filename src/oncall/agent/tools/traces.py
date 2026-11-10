"""Tempo tools: search for traces with TraceQL, and read one trace as a tree."""

import re
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from oncall.agent.tools.base import Params, Tool, ToolError, between
from oncall.agent.tools.http import TEMPO_URL, Fetch
from oncall.agent.tools.render import clock_time

MAX_TRACES = 20
MAX_SPANS = 60
# Span attributes worth a place on the span's line. Everything else (ports,
# user agents, code locations) is left out to keep a trace readable.
SHOWN_ATTRIBUTES = (
    "http.response.status_code",
    "rpc.response.status_code",
    "server.address",
    "messaging.destination.name",
)
# The trace ID becomes part of a URL path, so its form is checked.
_TRACE_ID = re.compile(r"^[0-9a-f]{1,32}$")


class SearchTraces(Params):
    traceql: str = Field(
        description=(
            "A TraceQL query, e.g. "
            '{ resource.service.name = "api-gateway" && status = error } or '
            '{ name = "POST /api/orders" && duration > 500ms }.'
        )
    )
    minutes: Annotated[int, between(1, 360)] = Field(
        description="How far back to search, in minutes before now (1 to 360)."
    )
    limit: Annotated[int, between(1, MAX_TRACES)] = Field(
        description=f"The most traces to return (1 to {MAX_TRACES})."
    )


class GetTrace(Params):
    trace_id: str = Field(description="A trace ID as returned by search_traces or found in a log.")


# ---- Tempo's JSON, reduced to the fields used -----------------------------------


class _Tempo(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _SpanSet(_Tempo):
    matched: int = 0


class _TraceSummary(_Tempo):
    traceID: str
    rootServiceName: str = "?"
    rootTraceName: str = "?"
    startTimeUnixNano: int = 0
    durationMs: int = 0
    spanSet: _SpanSet = _SpanSet()


class _SearchResponse(_Tempo):
    traces: list[_TraceSummary] = []


class _Attribute(_Tempo):
    key: str
    value: dict[str, Any] = {}

    def text(self) -> str:
        # OTLP wraps each value in an object keyed by its type: {"stringValue": "x"}.
        return str(next(iter(self.value.values()), ""))


class _Status(_Tempo):
    code: str = ""
    message: str = ""


class _Event(_Tempo):
    name: str = ""
    attributes: list[_Attribute] = []


class _Span(_Tempo):
    spanId: str
    parentSpanId: str = ""
    name: str = ""
    kind: str = ""
    startTimeUnixNano: int
    endTimeUnixNano: int
    attributes: list[_Attribute] = []
    status: _Status = _Status()
    events: list[_Event] = []


class _ScopeSpans(_Tempo):
    spans: list[_Span] = []


class _Resource(_Tempo):
    attributes: list[_Attribute] = []


class _Batch(_Tempo):
    resource: _Resource = _Resource()
    scopeSpans: list[_ScopeSpans] = []


class _TraceResponse(_Tempo):
    batches: list[_Batch] = []


# ---- Tools -------------------------------------------------------------------------


def traces_tools(fetch: Fetch, now: Callable[[], float]) -> list[Tool[Any]]:
    def search_traces(params: SearchTraces) -> str:
        end = now()
        body = fetch(
            f"{TEMPO_URL}/api/search",
            {
                "q": params.traceql,
                "start": f"{end - params.minutes * 60:.0f}",
                "end": f"{end:.0f}",
                "limit": str(params.limit),
            },
        )
        try:
            traces = _SearchResponse.model_validate(body).traces
        except ValidationError as err:
            raise ToolError(f"unexpected response from Tempo: {err}") from err
        if not traces:
            return "No trace matched."
        lines = ["trace_id  start(UTC)  duration  root_service  root_span  matching_spans"]
        for trace in sorted(traces, key=lambda t: t.startTimeUnixNano):
            lines.append(
                f"{trace.traceID}  {clock_time(trace.startTimeUnixNano / 1e9)}  "
                f"{trace.durationMs} ms  {trace.rootServiceName}  {trace.rootTraceName}  "
                f"{trace.spanSet.matched}"
            )
        if len(traces) >= params.limit:
            lines.append(f"[{len(traces)} traces, the limit: there may be more matches]")
        return "\n".join(lines)

    def get_trace(params: GetTrace) -> str:
        trace_id = params.trace_id.strip().lower()
        if not _TRACE_ID.fullmatch(trace_id):
            raise ToolError("trace_id must be 1 to 32 hexadecimal characters")
        body = fetch(f"{TEMPO_URL}/api/traces/{trace_id}", {})
        try:
            trace = _TraceResponse.model_validate(body)
        except ValidationError as err:
            raise ToolError(f"unexpected response from Tempo: {err}") from err
        return _render_trace(trace_id, trace)

    return [
        Tool(
            name="search_traces",
            description=(
                "Search Tempo for request traces with TraceQL, over the last N minutes. "
                "Every request through the gateway is traced across all services, the "
                "database, the cache and the message broker. Returns one line per trace; "
                "use get_trace to see where a trace spent its time or failed."
            ),
            params=SearchTraces,
            run=search_traces,
        ),
        Tool(
            name="get_trace",
            description=(
                "Show one trace as a tree of spans: service, operation, when it started "
                "relative to the trace, how long it took, and any error. Children are "
                "indented under the span that called them."
            ),
            params=GetTrace,
            run=get_trace,
        ),
    ]


# ---- Rendering a trace ---------------------------------------------------------------


def _render_trace(trace_id: str, trace: _TraceResponse) -> str:
    spans: list[tuple[str, _Span]] = []  # (service, span)
    for batch in trace.batches:
        attributes = {a.key: a.text() for a in batch.resource.attributes}
        service = attributes.get("service.name", "?")
        spans.extend((service, span) for scope in batch.scopeSpans for span in scope.spans)
    if not spans:
        return f"Trace {trace_id} has no spans."

    spans.sort(key=lambda pair: pair[1].startTimeUnixNano)
    known = {span.spanId for _, span in spans}
    children: dict[str, list[tuple[str, _Span]]] = {}
    for pair in spans:
        # A span whose parent was never received is shown at the top level.
        parent = pair[1].parentSpanId if pair[1].parentSpanId in known else ""
        children.setdefault(parent, []).append(pair)

    start = spans[0][1].startTimeUnixNano
    end = max(span.endTimeUnixNano for _, span in spans)
    services = sorted({service for service, _ in spans})
    lines = [
        f"Trace {trace_id}: {len(spans)} spans across {', '.join(services)}; "
        f"started {clock_time(start / 1e9, millis=True)} UTC, {(end - start) / 1e6:.1f} ms in all.",
        "service | operation | kind | start offset | duration | details",
    ]

    def walk(parent: str, depth: int) -> None:
        for service, span in children.get(parent, []):
            if len(lines) - 2 >= MAX_SPANS:
                return
            lines.append(_span_line(service, span, start, depth))
            walk(span.spanId, depth + 1)

    walk("", 0)
    if len(spans) > MAX_SPANS:
        lines.append(f"[showing {MAX_SPANS} of {len(spans)} spans]")
    return "\n".join(lines)


def _span_line(service: str, span: _Span, trace_start: int, depth: int) -> str:
    attributes = {a.key: a.text() for a in span.attributes}
    details = [f"{key}={attributes[key]}" for key in SHOWN_ATTRIBUTES if key in attributes]
    if span.status.code == "STATUS_CODE_ERROR":
        details.append(f"ERROR: {span.status.message or _exception_message(span) or 'no message'}")
    kind = span.kind.removeprefix("SPAN_KIND_").lower()
    return (
        f"{'  ' * depth}{service} | {span.name} | {kind} | "
        f"+{(span.startTimeUnixNano - trace_start) / 1e6:.1f} ms | "
        f"{(span.endTimeUnixNano - span.startTimeUnixNano) / 1e6:.1f} ms"
        + (f" | {'; '.join(details)}" if details else "")
    )


def _exception_message(span: _Span) -> str:
    for event in span.events:
        for attribute in event.attributes:
            if attribute.key == "exception.message":
                return attribute.text()
    return ""
