"""What the agent is told before it looks at anything: the system prompt and the page."""

import re
from datetime import UTC, datetime
from pathlib import Path

import yaml

from oncall.agent.guardrails.gate import tool_definitions
from oncall.agent.guardrails.policy import PolicyMode
from oncall.agent.page import firing_alerts, render_page
from oncall.agent.prompt import CHOOSING_RULES, SYSTEM_PROMPT, build_system_prompt, system_prompt
from oncall.agent.report import submit_report_definition
from oncall.agent.tools.http import PROMETHEUS_URL
from oncall.agent.trace import read_trace
from oncall.chaos.taxonomy import CATEGORY_DEFINITIONS, Component
from tests.agent.conftest import REPO, FakeFetch

RUBRIC = (REPO / "docs" / "RUBRIC.md").read_text()
INCIDENTS = REPO / "incidents"

# ---- The system prompt ---------------------------------------------------------------


def test_the_prompt_is_byte_stable() -> None:
    """It is cached by the API: a prompt that differed between runs would never hit the cache."""
    assert build_system_prompt() == SYSTEM_PROMPT
    for mode in PolicyMode:
        assert build_system_prompt(mode) == system_prompt(mode)
        # Nothing that looks like a date, a clock time or a run ID.
        assert not re.search(r"20\d\d-\d\d-\d\d|\d\d:\d\d:\d\d", system_prompt(mode))


def test_observe_mode_is_told_exactly_what_the_phase_3_runs_were_told() -> None:
    """Adding write tools did not change the observe-mode agent those runs measured."""
    trace = REPO / "results" / "phase3-agent" / "redis-maxmemory-too-low" / "trace.jsonl"
    start = next(record for record in read_trace(trace) if record["kind"] == "run_start")
    assert start["system"] == system_prompt(PolicyMode.OBSERVE)
    assert start["tools"] == [
        *tool_definitions(PolicyMode.OBSERVE),
        submit_report_definition(PolicyMode.OBSERVE),
    ]


def test_the_modes_differ_only_in_what_they_say_about_access() -> None:
    """Everything from the description of the system onwards is the same text."""
    tails = {system_prompt(mode).split("# The system", 1)[1] for mode in PolicyMode}
    assert len(tails) == 1
    observe, propose, auto = (system_prompt(mode) for mode in PolicyMode)
    assert "read-only: you cannot change anything" in observe
    for prompt in (propose, auto):
        for tool in ("rollback_deployment", "scale_deployment", "restart_pod", "revert_config"):
            assert f"`{tool}`" in prompt
    assert "a proposal for a human operator" in propose
    assert "low-risk write is executed at once" in auto


def test_the_prompt_has_every_category_in_the_rubrics_words() -> None:
    for category, definition in CATEGORY_DEFINITIONS.items():
        assert f"- `{category}`: {definition}" in SYSTEM_PROMPT


def test_the_prompt_lists_every_component() -> None:
    line = "Components: " + ", ".join(f"`{component}`" for component in Component) + "."
    assert line in SYSTEM_PROMPT


def test_the_choosing_rules_are_the_rubrics() -> None:
    """The agent is given the rules incident authors follow, in the same words."""
    section = RUBRIC.split("## Choosing the pair")[1].split("\n## ")[0]
    # One string per numbered rule, with Markdown emphasis and line wrapping removed.
    rules = [
        " ".join(rule.replace("**", "").split())
        for rule in re.findall(r"^\d\. (.+?)(?=^\d\. |\Z)", section, flags=re.MULTILINE | re.DOTALL)
    ]
    assert rules == list(CHOOSING_RULES)
    for number, rule in enumerate(CHOOSING_RULES, start=1):
        assert f"{number}. {rule}" in SYSTEM_PROMPT


def test_nothing_the_model_is_given_names_an_incident_or_its_tooling() -> None:
    """The prompt and the tool definitions say nothing about the benchmark.

    Incident IDs, titles and the injector's vocabulary must not appear: the
    agent is an on-call engineer, not a participant in a test.
    """
    given = "".join(
        system_prompt(mode) + str(tool_definitions(mode)) + str(submit_report_definition(mode))
        for mode in PolicyMode
    )
    lowered = given.lower()
    for path in sorted(INCIDENTS.glob("*.yaml")):
        incident = yaml.safe_load(path.read_text())
        assert incident["id"] not in lowered
        assert incident["title"].lower() not in lowered
    for word in ("incident library", "inject", "chaos", "netem", "benchmark", "ground truth"):
        assert word not in lowered, word


# ---- The page ------------------------------------------------------------------------

RULES = {
    "data": {
        "groups": [
            {
                "rules": [
                    {
                        "name": "GatewayLatencyHigh",
                        "query": "histogram_quantile(0.99,\n  sum by (route, le) (x)) > 1",
                        "type": "alerting",
                        "alerts": [
                            {
                                "state": "firing",
                                "labels": {
                                    "alertname": "GatewayLatencyHigh",
                                    "severity": "page",
                                    "route": "POST /api/orders",
                                },
                                "annotations": {"summary": "The p99 of POST /api/orders is high."},
                                "activeAt": "2026-11-10T11:57:36.123Z",
                            },
                            {
                                "state": "pending",
                                "labels": {"alertname": "GatewayLatencyHigh", "route": "GET /x"},
                                "annotations": {"summary": "Not firing yet."},
                                "activeAt": "2026-11-10T11:59:50Z",
                            },
                        ],
                    },
                    {"name": "ConsumerLagHigh", "query": "lag > 100", "alerts": []},
                    {
                        "name": "CacheHitRatioLow",
                        "query": "ratio < 0.5",
                        "alerts": [
                            {
                                "state": "firing",
                                "labels": {"alertname": "CacheHitRatioLow", "severity": "ticket"},
                                "annotations": {"summary": "The cache hit ratio is low."},
                                "activeAt": "2026-11-10T11:55:00Z",
                            }
                        ],
                    },
                ]
            }
        ]
    }
}


def test_only_firing_alerts_are_in_the_page() -> None:
    fetch = FakeFetch(RULES)
    alerts = firing_alerts(fetch)
    assert fetch.requests == [(f"{PROMETHEUS_URL}/api/v1/rules", {"type": "alert"})]
    assert [alert.name for alert in alerts] == ["CacheHitRatioLow", "GatewayLatencyHigh"]
    assert alerts[1].labels == {"route": "POST /api/orders"}
    assert alerts[1].severity == "page"


def test_the_page_text() -> None:
    page = render_page(
        firing_alerts(FakeFetch(RULES)), datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)
    )
    assert page == "\n".join(
        [
            "You have been paged. It is 2026-11-10 12:00:00 UTC.",
            "",
            "Alerts firing in namespace testbed (2):",
            "",
            "1. CacheHitRatioLow (severity: ticket)",
            "   The cache hit ratio is low.",
            "   labels: {}",
            "   condition true since: 11:55:00 UTC",
            "   rule: ratio < 0.5",
            "",
            "2. GatewayLatencyHigh (severity: page)",
            "   The p99 of POST /api/orders is high.",
            '   labels: {route="POST /api/orders"}',
            "   condition true since: 11:57:36 UTC",
            "   rule: histogram_quantile(0.99, sum by (route, le) (x)) > 1",
            "",
            "Investigate, then file the incident report with submit_report.",
        ]
    )


def test_no_alert_firing() -> None:
    assert firing_alerts(FakeFetch({"data": {"groups": []}})) == []


def test_every_alert_rule_describes_a_symptom_and_names_no_cause() -> None:
    """The page is built from these rules. None may say why something is wrong.

    A crude but real check: the rule names and summaries must not contain the
    vocabulary of causes (the fault categories of the rubric).
    """
    rules_file = REPO / "deploy" / "observability" / "config" / "alerts.yml"
    text = Path(rules_file).read_text().lower()
    for category in CATEGORY_DEFINITIONS:
        assert category not in text
        assert category.replace("_", " ") not in text
