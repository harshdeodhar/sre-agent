"""Read-only clients for Prometheus and Loki, as exposed on localhost by k3d.

Standard library HTTP only: two GET endpoints do not justify a dependency.
"""

import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROMETHEUS_URL = "http://127.0.0.1:9090"
LOKI_URL = "http://127.0.0.1:3100"
# The alert rules Prometheus evaluates, relative to the repo root.
ALERT_RULES = Path("deploy/observability/config/alerts.yml")


class TelemetryError(RuntimeError):
    """Prometheus or Loki could not be reached, or answered with an error."""


class Sample(BaseModel):
    """One series of an instant query: its labels and current value."""

    metric: dict[str, str]
    value: float


class Alert(BaseModel):
    """An alert Prometheus currently holds as pending or firing."""

    name: str
    state: str  # "pending" or "firing"
    labels: dict[str, str]


# ---- Wire formats ----------------------------------------------------------------


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class _VectorItem(_Wire):
    metric: dict[str, str]
    value: tuple[float, str]  # [unix time, value as a string]


class _VectorData(_Wire):
    result_type: Literal["vector"] = Field(alias="resultType")
    result: list[_VectorItem]


class _QueryResponse(_Wire):
    data: _VectorData


class _AlertItem(_Wire):
    labels: dict[str, str]
    state: str


class _AlertsData(_Wire):
    alerts: list[_AlertItem]


class _AlertsResponse(_Wire):
    data: _AlertsData


def _get(url: str, params: dict[str, str]) -> bytes:
    full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    try:
        with urllib.request.urlopen(full, timeout=20) as response:
            return response.read()
    except urllib.error.HTTPError as err:
        # Prometheus and Loki explain a rejected query in the body.
        detail = err.read().decode(errors="replace")[:500]
        raise TelemetryError(f"{url}: HTTP {err.code}: {detail}") from err
    except OSError as err:  # refused, reset, timed out
        raise TelemetryError(f"{url}: {err}") from err


def _instant_vector(url: str, expr: str) -> list[Sample]:
    try:
        parsed = _QueryResponse.model_validate_json(_get(url, {"query": expr}))
    except ValidationError as err:
        raise TelemetryError(f"{url}: query did not return an instant vector: {expr}") from err
    return [Sample(metric=item.metric, value=float(item.value[1])) for item in parsed.data.result]


class Prometheus:
    def __init__(self, base_url: str = PROMETHEUS_URL) -> None:
        self._base = base_url.rstrip("/")

    def query(self, expr: str) -> list[Sample]:
        """Evaluate an instant query now."""
        return _instant_vector(f"{self._base}/api/v1/query", expr)

    def alerts(self) -> list[Alert]:
        """Alerts that are pending (condition true, `for` not yet served) or firing."""
        try:
            parsed = _AlertsResponse.model_validate_json(_get(f"{self._base}/api/v1/alerts", {}))
        except ValidationError as err:
            raise TelemetryError(f"{self._base}: unexpected alerts response") from err
        return [
            Alert(name=a.labels.get("alertname", "?"), state=a.state, labels=a.labels)
            for a in parsed.data.alerts
            if a.state in ("pending", "firing")
        ]


class Loki:
    def __init__(self, base_url: str = LOKI_URL) -> None:
        self._base = base_url.rstrip("/")

    def query(self, expr: str) -> list[Sample]:
        """Evaluate a LogQL metric query (count_over_time and the like) now."""
        return _instant_vector(f"{self._base}/loki/api/v1/query", expr)


def load_alert_names(rules_file: Path) -> set[str]:
    """Names of the alert rules defined in a Prometheus rules file."""
    document: Any = yaml.safe_load(rules_file.read_text())
    return {
        rule["alert"]
        for group in document.get("groups", [])
        for rule in group.get("rules", [])
        if "alert" in rule
    }
