"""Turning query results into compact text.

The JSON that Prometheus and Loki return is mostly punctuation. Rendering it as
one line per series costs a fraction of the tokens and is easier to read, for
a model as for a person.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict


class Series(BaseModel):
    """One series of a Prometheus-style result (Loki metric queries use the same shape)."""

    model_config = ConfigDict(extra="ignore")

    metric: dict[str, str] = {}
    value: tuple[float, str] | None = None  # instant queries
    values: list[tuple[float, str]] = []  # range queries


def clock_time(unix_seconds: float, *, millis: bool = False) -> str:
    """HH:MM:SS in UTC. Every investigation here spans minutes, not days."""
    moment = datetime.fromtimestamp(unix_seconds, tz=UTC)
    return moment.strftime("%H:%M:%S.%f")[:12] if millis else moment.strftime("%H:%M:%S")


def labels_text(labels: dict[str, str]) -> str:
    if not labels:
        return "{}"
    return "{" + ", ".join(f'{name}="{value}"' for name, value in sorted(labels.items())) + "}"


def number(text: str) -> str:
    """A sample value at four significant figures: 0.012345678 -> 0.01235."""
    try:
        return f"{float(text):.4g}"
    except ValueError:
        return text


def render_series(series: list[Series], max_series: int) -> str:
    """One line per series, with a count of what was left out."""
    if not series:
        return "The query returned no series."
    lines = []
    for one in series[:max_series]:
        if one.value is not None:
            lines.append(f"{labels_text(one.metric)} {number(one.value[1])}")
        else:
            points = "  ".join(f"{clock_time(t)}={number(v)}" for t, v in one.values)
            lines.append(f"{labels_text(one.metric)}\n  {points}")
    if len(series) > max_series:
        lines.append(
            f"[showing {max_series} of {len(series)} series; aggregate or filter to see the rest]"
        )
    return "\n".join(lines)
