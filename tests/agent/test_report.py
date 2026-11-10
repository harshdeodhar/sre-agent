"""The incident report: the checks on a submission, and the files written from it."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from oncall.agent.loop import CallRecord, Outcome, RunConfig, RunResult
from oncall.agent.pricing import PRICE_RETRIEVED, PRICE_SOURCE
from oncall.agent.replay import ReplayMismatchError, replay
from oncall.agent.report import Submission, check_submission, render_markdown
from oncall.agent.runner import build_report, report_json
from oncall.agent.tools import read_only_definitions
from oncall.agent.trace import read_trace

from .conftest import REPO

STARTED = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)

# A live run from before `check_submission` looked for tool-call markup. Its
# report was accepted then; results/phase3-agent/README.md tells the story.
BEFORE_MARKUP_CHECK = (
    REPO
    / "results"
    / "phase3-agent"
    / "before-markup-check"
    / "orders-inventory-timeout-too-short"
    / "trace.jsonl"
)


def cause(component: str = "redis", category: str = "cache_memory_too_low") -> dict[str, Any]:
    return {
        "component": component,
        "category": category,
        "explanation": "maxmemory is 1mb | far below the data set",
        "confidence": "high",
    }


def submission(*call_ids: str, **overrides: object) -> Submission:
    document = {
        "summary": "Redis evicts entries because its memory ceiling is too low.",
        "root_causes": [cause()],
        "evidence": [{"call_id": call_id, "finding": "evictions"} for call_id in call_ids],
        "timeline": [{"time": "11:57:30", "event": "Evictions begin."}],
        "recommended_actions": [
            {"action": "revert_config", "component": "redis", "rationale": "Raise maxmemory."}
        ],
        **overrides,
    }
    return Submission.model_validate(document)


# ---- check_submission ------------------------------------------------------------------


def test_a_report_citing_calls_that_were_made_is_accepted() -> None:
    assert check_submission(submission("c1", "c3"), {"c1", "c2", "c3"}) == []


def test_evidence_must_cite_calls_that_were_made() -> None:
    (problem,) = check_submission(submission("c1", "c7", "c12"), {"c1", "c2"})
    assert "no tool result you have received: c12, c7." in problem


def test_a_root_cause_needs_evidence() -> None:
    assert check_submission(submission(), {"c1"}) == ["a root cause is given without any evidence"]


def test_no_cause_and_no_evidence_is_an_acceptable_report() -> None:
    """An agent that could not establish a cause may say so. Scoring will count it as a miss."""
    assert check_submission(submission(root_causes=[]), set()) == []


def test_the_same_root_cause_twice_is_rejected() -> None:
    twice = submission("c1", root_causes=[cause(), cause(), cause("postgres", "missing_index")])
    assert check_submission(twice, {"c1"}) == [
        "root cause (redis, cache_memory_too_low) is listed more than once"
    ]


def test_a_text_field_holding_another_parameter_is_rejected() -> None:
    merged = submission(
        "c1",
        summary='Orders fail.</summary>\n<parameter name="root_causes">[{"component": "redis"}]',
        root_causes=[],
    )
    (problem,) = check_submission(merged, {"c1"})
    assert problem.startswith("the text of `summary` contains tool-call markup")
    assert "Submit the report again" in problem


def test_markup_is_found_in_nested_fields_too() -> None:
    nested = submission(
        "c1",
        root_causes=[{**cause(), "explanation": 'too low<parameter name="evidence">[]'}],
        timeline=[
            {"time": "11:57:30", "event": "Evictions begin."},
            {"time": "11:58:00", "event": '<parameter name="x">'},
        ],
    )
    problems = check_submission(nested, {"c1"})
    assert [problem.split("`")[1] for problem in problems] == [
        "root_causes[0].explanation",
        "timeline[1].event",
    ]


def test_angle_brackets_alone_are_not_markup() -> None:
    """PromQL comparisons and log lines contain `<` and `>`; only the marker is refused."""
    plain = submission("c1", summary="p99 > 1 s while pool size < 5; see <order-service> logs")
    assert check_submission(plain, {"c1"}) == []


def test_the_report_recorded_before_the_markup_check_is_now_rejected() -> None:
    """The real `submit_report` call that made the check necessary (see report.py)."""
    records = read_trace(BEFORE_MARKUP_CHECK)
    last_reply = [record for record in records if record["kind"] == "response"][-1]
    (call,) = [block for block in last_reply["body"]["content"] if block["type"] == "tool_use"]
    assert call["name"] == "submit_report"
    recorded = Submission.model_validate(call["input"])
    # What went wrong: the diagnosis is in the summary, and the field is empty.
    assert recorded.root_causes == []
    assert '"category": "bad_config_value"' in recorded.summary
    seen = {record["call_id"] for record in records if record["kind"] == "tool_call"}
    (problem,) = check_submission(recorded, seen)
    assert problem.startswith("the text of `summary` contains tool-call markup")


def test_a_trace_recorded_before_the_markup_check_no_longer_replays() -> None:
    """Replay detects that the loop now behaves differently; it does not paper over it.

    Today's loop rejects that report and asks the model again, and the trace
    has no further response to give.
    """
    with pytest.raises(ReplayMismatchError, match="a model request the trace does not contain"):
        replay(read_trace(BEFORE_MARKUP_CHECK), read_only_definitions())


@pytest.mark.parametrize(
    "overrides",
    [
        {"root_causes": [cause(component="the-network")]},
        {"root_causes": [cause(category="crash_loop")]},  # a symptom, not a category
        {"root_causes": [{**cause(), "confidence": "certain"}]},
        {"recommended_actions": [{"action": "delete_pod", "component": "redis", "rationale": "x"}]},
        {"unexpected_field": 1},
    ],
)
def test_values_outside_the_closed_lists_are_not_a_submission(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        submission("c1", **overrides)


# ---- report.json and report.md ---------------------------------------------------------


def result(report: Submission | None, outcome: Outcome = Outcome.REPORT) -> RunResult:
    return RunResult(
        outcome=outcome,
        detail="report accepted" if report else "a reply reached the output limit",
        submission=report,
        calls=[
            CallRecord("c1", "query_metrics", {"promql": "up", "minutes": 0}, is_error=False),
            CallRecord("c2", "kube_get", {"kind": "pods"}, is_error=True),
            CallRecord("c3", "search_logs", {"logql": '{app="redis"}'}, is_error=False),
        ],
        model_requests=4,
        refused_tool_calls=1,
        rejected_submissions=0,
        input_tokens=1200,
        cache_write_tokens=3400,
        cache_read_tokens=5600,
        output_tokens=780,
        cost_usd=0.012345,
        duration_seconds=41.6,
    )


CONFIG = RunConfig(model="claude-haiku-5-5")


def test_the_report_joins_the_submission_with_facts_from_the_run() -> None:
    report = build_report("the page", CONFIG, result(submission("c3", "c1")), STARTED)
    # Only the cited calls, in the order they were made.
    assert [(call.call_id, call.tool) for call in report.cited_calls] == [
        ("c1", "query_metrics"),
        ("c3", "search_logs"),
    ]
    run = report.run
    assert (run.model, run.effort, run.policy_mode) == ("claude-haiku-5-5", "medium", "observe")
    assert (run.tool_calls, run.tool_errors, run.refused_tool_calls) == (3, 1, 1)
    assert (run.outcome, run.cost_usd) == ("report", 0.012345)
    assert (run.price_source, run.price_retrieved) == (PRICE_SOURCE, PRICE_RETRIEVED)


def test_report_json_round_trips() -> None:
    report = build_report("the page", CONFIG, result(submission("c1")), STARTED)
    document = json.loads(report_json(report))
    assert document["submission"]["root_causes"][0]["category"] == "cache_memory_too_low"
    assert document["run"]["started_at"] == "2026-11-10T12:00:00Z"
    assert type(report).model_validate(document) == report


def test_the_markdown_report() -> None:
    report = build_report("the page\n", CONFIG, result(submission("c3", "c1")), STARTED)
    lines = render_markdown(report).splitlines()
    assert lines[:6] == ["# Incident report", "", "## Page", "", "```", "the page"]
    # A '|' in the model's text must not break the table.
    assert (
        "| `redis` | `cache_memory_too_low` | high | maxmemory is 1mb \\| far below the data set |"
    ) in lines
    # Evidence is shown with the call it cites, so a reader can re-run the query.
    assert "- **c3** `search_logs` `logql='{app=\"redis\"}'`: evictions" in lines
    assert "- **c1** `query_metrics` `promql='up'` `minutes=0`: evictions" in lines
    assert "- 11:57:30: Evictions begin." in lines
    assert "- `revert_config` on `redis`: Raise maxmemory." in lines
    assert "| Model | `claude-haiku-5-5` (effort `medium`) |" in lines
    assert "| Started | 2026-11-10 12:00:00 UTC |" in lines
    assert "| Tool calls | 3 (1 returned an error, 1 refused) |" in lines
    assert "| Tokens | 1200 input, 3400 cache write, 5600 cache read, 780 output |" in lines
    assert f"| Cost | $0.0123 (prices from {PRICE_SOURCE}, retrieved {PRICE_RETRIEVED}) |" in lines


def test_the_markdown_of_a_run_without_a_report_says_so() -> None:
    report = build_report("the page", CONFIG, result(None, Outcome.MAX_TOKENS), STARTED)
    assert report.cited_calls == []
    markdown = render_markdown(report)
    assert "## No report was submitted" in markdown
    assert "outcome `max_tokens`: a reply reached the output limit" in markdown
    assert "## Root cause" not in markdown


def test_the_markdown_of_a_report_without_a_cause() -> None:
    report = build_report("the page", CONFIG, result(submission(root_causes=[])), STARTED)
    assert "No root cause was established." in render_markdown(report)
