"""Prometheus tools: run a PromQL query, and find metric names."""

from collections.abc import Callable
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from oncall.agent.tools.base import Params, Tool, ToolError, between
from oncall.agent.tools.http import PROMETHEUS_URL, Fetch
from oncall.agent.tools.render import Series, number, render_series

MAX_INSTANT_SERIES = 40
MAX_RANGE_SERIES = 12
# A range query returns about this many points per series whatever its length:
# enough to see a step change or a trend, and a bounded number of tokens.
RANGE_POINTS = 30
MIN_STEP_SECONDS = 10  # the scrape interval; a finer step shows nothing new
MAX_METRIC_NAMES = 150


class QueryMetrics(Params):
    promql: str = Field(description="A PromQL expression.")
    minutes: Annotated[int, between(0, 360)] = Field(
        description=(
            "0 evaluates the expression now and returns one value per series. "
            "1 to 360 evaluates it over that many minutes ending now and returns "
            f"about {RANGE_POINTS} points per series, which shows when something changed."
        )
    )


class ListMetrics(Params):
    name_regex: str = Field(
        description='A regular expression matched against whole metric names, e.g. "redis_.*".'
    )


class _Data(BaseModel):
    model_config = ConfigDict(extra="ignore")

    resultType: str  # the API's field name
    result: Any


class _QueryResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    data: _Data


class _NamesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    data: list[str]


def metrics_tools(fetch: Fetch, now: Callable[[], float]) -> list[Tool[Any]]:
    """The Prometheus tools. `now` returns the current Unix time."""

    def query_metrics(params: QueryMetrics) -> str:
        if params.minutes == 0:
            body = fetch(f"{PROMETHEUS_URL}/api/v1/query", {"query": params.promql})
            return _render(body, MAX_INSTANT_SERIES)
        end = now()
        span = params.minutes * 60
        step = max(MIN_STEP_SECONDS, span // RANGE_POINTS)
        body = fetch(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            {
                "query": params.promql,
                "start": f"{end - span:.0f}",
                "end": f"{end:.0f}",
                "step": str(step),
            },
        )
        return f"Times are UTC; one point every {step} s.\n" + _render(body, MAX_RANGE_SERIES)

    def list_metrics(params: ListMetrics) -> str:
        # The pattern travels as a label matcher, where a double quote would
        # end the string. A metric name cannot contain one, so nothing is lost.
        if '"' in params.name_regex or "\\" in params.name_regex:
            raise ToolError("name_regex must not contain double quotes or backslashes")
        body = fetch(
            f"{PROMETHEUS_URL}/api/v1/label/__name__/values",
            {"match[]": f'{{__name__=~"{params.name_regex}"}}'},
        )
        try:
            names = _NamesResponse.model_validate(body).data
        except ValidationError as err:
            raise ToolError(f"unexpected response from Prometheus: {err}") from err
        if not names:
            return "No metric name matches."
        shown = names[:MAX_METRIC_NAMES]
        text = "\n".join(shown)
        if len(names) > len(shown):
            text += f"\n[showing {len(shown)} of {len(names)} names; use a narrower pattern]"
        return text

    return [
        Tool(
            name="query_metrics",
            description=(
                "Run a PromQL query against Prometheus, either as of now or over the last "
                "N minutes. Use it for rates, error ratios, latency quantiles, resource use, "
                "Kubernetes object state (kube-state-metrics) and alert state (the ALERTS "
                f"series). At most {MAX_INSTANT_SERIES} series are shown for an instant query "
                f"and {MAX_RANGE_SERIES} for a range query, so aggregate with sum by (...) or "
                "topk. An empty result means no series matched."
            ),
            params=QueryMetrics,
            run=query_metrics,
        ),
        Tool(
            name="list_metrics",
            description=(
                "List the metric names in Prometheus that match a regular expression. Use it "
                "to find out what a component exports before querying it."
            ),
            params=ListMetrics,
            run=list_metrics,
        ),
    ]


def _render(body: object, max_series: int) -> str:
    try:
        data = _QueryResponse.model_validate(body).data
        if data.resultType in ("scalar", "string"):
            return number(str(data.result[1]))
        series = [Series.model_validate(item) for item in data.result]
    except (ValidationError, IndexError, TypeError) as err:
        raise ToolError(f"unexpected response from Prometheus: {err}") from err
    return render_series(series, max_series)
