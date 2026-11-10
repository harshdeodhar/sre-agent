"""The page: the alert text the agent starts from.

There is no Alertmanager in the testbed. Prometheus evaluates the alert rules
and this module reads their state from its API, the way a pager integration
would. The page contains what a pager message contains: which alerts fire,
since when, their labels, their summary and the rule's expression. Every rule
describes a symptom (deploy/observability/config/alerts.yml), so the page says
what is wrong and never why.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from oncall.agent.tools.http import PROMETHEUS_URL, Fetch
from oncall.agent.tools.render import labels_text


class _Api(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Alert(_Api):
    state: str
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}
    activeAt: datetime | None = None


class _Rule(_Api):
    name: str
    query: str = ""
    type: str = ""
    alerts: list[_Alert] = []


class _Group(_Api):
    rules: list[_Rule] = []


class _Data(_Api):
    groups: list[_Group] = []


class _RulesResponse(_Api):
    data: _Data


class FiringAlert(BaseModel):
    name: str
    severity: str
    summary: str
    labels: dict[str, str]
    # When the rule's condition became true. The alert fires once the
    # condition has held for the rule's `for` duration.
    active_since: datetime | None
    expression: str


def firing_alerts(fetch: Fetch) -> list[FiringAlert]:
    """Every alert Prometheus reports as firing, in a stable order."""
    body = fetch(f"{PROMETHEUS_URL}/api/v1/rules", {"type": "alert"})
    alerts = []
    for group in _RulesResponse.model_validate(body).data.groups:
        for rule in group.rules:
            for alert in rule.alerts:
                if alert.state != "firing":
                    continue
                # alertname and severity are shown on their own; the rest
                # (route, pod, deployment) says what the alert is about.
                labels = {
                    name: value
                    for name, value in alert.labels.items()
                    if name not in ("alertname", "severity")
                }
                alerts.append(
                    FiringAlert(
                        name=rule.name,
                        severity=alert.labels.get("severity", "unknown"),
                        summary=alert.annotations.get("summary", ""),
                        labels=labels,
                        active_since=alert.activeAt,
                        expression=" ".join(rule.query.split()),
                    )
                )
    return sorted(alerts, key=lambda a: (a.name, labels_text(a.labels)))


def render_page(alerts: list[FiringAlert], now: datetime) -> str:
    """The first message of the conversation.

    The current time is here and not in the system prompt: the system prompt
    is cached across runs and has to stay byte-identical.
    """
    lines = [
        f"You have been paged. It is {now.astimezone(UTC):%Y-%m-%d %H:%M:%S} UTC.",
        "",
        f"Alerts firing in namespace testbed ({len(alerts)}):",
    ]
    for index, alert in enumerate(alerts, start=1):
        since = (
            f"{alert.active_since.astimezone(UTC):%H:%M:%S} UTC"
            if alert.active_since
            else "unknown"
        )
        lines += [
            "",
            f"{index}. {alert.name} (severity: {alert.severity})",
            f"   {alert.summary}",
            f"   labels: {labels_text(alert.labels)}",
            f"   condition true since: {since}",
            f"   rule: {alert.expression}",
        ]
    lines += ["", "Investigate, then file the incident report with submit_report."]
    return "\n".join(lines)
