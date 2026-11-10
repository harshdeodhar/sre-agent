"""The incident report: what the model submits, how it is checked, how it is rendered.

The model ends a run by calling `submit_report`. That tool's parameters are
the `Submission` model below, so the report arrives as structured data: the
root cause is a (component, category) pair from the same closed lists the
benchmark's ground truth uses (docs/RUBRIC.md), and scoring it needs no
reading of free text.

`check_submission` then applies the rules a schema cannot express. The
important one: every piece of evidence must cite a tool call that really
happened in this run. A report cannot quote telemetry the agent never fetched.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from oncall.agent.tools.base import strict_schema
from oncall.chaos.taxonomy import Component, FaultCategory, RemediationAction

SUBMIT_REPORT = "submit_report"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class _Part(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RootCause(_Part):
    component: Component = Field(description="The component where the fix has to be made.")
    category: FaultCategory = Field(description="The kind of fault, from the fixed list.")
    explanation: str = Field(
        description="What exactly is wrong (which setting, limit, index, link) and how it "
        "produces the symptoms."
    )
    confidence: Confidence = Field(description="How well the evidence supports this cause.")


class Evidence(_Part):
    call_id: str = Field(
        description='The ID of the tool call that produced this evidence, e.g. "c7". It is '
        "printed at the top of every tool result."
    )
    finding: str = Field(description="What that result showed, with the numbers that matter.")


class TimelineEntry(_Part):
    time: str = Field(description="When it happened, as HH:MM:SS UTC.")
    event: str = Field(description="What happened.")


class RecommendedAction(_Part):
    action: RemediationAction = Field(
        description="rollback_deployment, scale_deployment, restart_pod or revert_config if "
        'that would fix it; "manual" for anything else (describe it in the rationale).'
    )
    component: Component = Field(description="The component the action applies to.")
    rationale: str = Field(description="What to do exactly, and why it fixes the root cause.")


class Submission(_Part):
    """What the model passes to `submit_report`."""

    summary: str = Field(description="Two or three sentences: what broke, why, and the impact.")
    root_causes: list[RootCause] = Field(
        description="Each independent root cause, once. Not symptoms, not consequences, not "
        "alternatives that were ruled out. Empty only if no cause could be established."
    )
    evidence: list[Evidence] = Field(description="The observations that support the root causes.")
    timeline: list[TimelineEntry] = Field(description="The key events in order.")
    recommended_actions: list[RecommendedAction] = Field(
        description="What should be done to resolve the incident. Nothing has been changed yet."
    )


def submit_report_definition() -> dict[str, Any]:
    """The `submit_report` tool as the Messages API wants it."""
    return {
        "name": SUBMIT_REPORT,
        "description": (
            "File the incident report and end the investigation. Call this exactly once, "
            "when you have established the root cause or have used your budget. If the "
            "report is rejected, the result says why; correct it and submit again."
        ),
        "input_schema": strict_schema(Submission),
        "strict": True,
    }


def check_submission(submission: Submission, call_ids: set[str]) -> list[str]:
    """The reasons to reject a submission; empty if it is acceptable.

    `call_ids` are the IDs of the tool calls whose results the model has been
    shown. That leaves out calls made in the same reply as the submission:
    their IDs can be guessed, but their results have not been read.
    """
    problems = []
    unknown = sorted({item.call_id for item in submission.evidence} - call_ids)
    if unknown:
        problems.append(
            f"evidence cites call IDs that match no tool result you have received: "
            f"{', '.join(unknown)}. Cite the call ID printed at the top of a tool result."
        )
    if submission.root_causes and not submission.evidence:
        problems.append("a root cause is given without any evidence")
    pairs = [(cause.component, cause.category) for cause in submission.root_causes]
    for pair in sorted({pair for pair in pairs if pairs.count(pair) > 1}):
        problems.append(f"root cause ({pair[0]}, {pair[1]}) is listed more than once")
    problems += [
        f"the text of `{field}` contains tool-call markup ({_CALL_MARKUP!r}): the value of "
        f"another parameter has run into it. Submit the report again with every parameter "
        f"in its own field."
        for field in _fields_with_markup(submission.model_dump(mode="json"))
    ]
    return problems


# The start of a parameter in the model's own tool-call syntax. The first live
# run (claude-haiku-5-5, 2026-11-10) ended with a `submit_report` whose
# `summary` held the summary, then `</summary>`, then this marker and the whole
# root-cause list as text, while the real `root_causes` field was empty. The
# call satisfied the strict schema, so the API accepted it, and a correct
# diagnosis was filed as "no root cause". A schema constrains the shape of a
# call, not which field the model writes its answer into. The recorded call is
# kept in results/phase3-agent/ and tests/agent/test_report.py checks it is
# rejected.
_CALL_MARKUP = "<parameter name="


def _fields_with_markup(value: object, path: str = "") -> list[str]:
    """The paths of the text fields in `value` that contain tool-call markup."""
    if isinstance(value, str):
        return [path] if _CALL_MARKUP in value else []
    if isinstance(value, dict):
        return [
            found
            for name, item in value.items()
            for found in _fields_with_markup(item, f"{path}.{name}" if path else name)
        ]
    if isinstance(value, list):
        return [
            found
            for index, item in enumerate(value)
            for found in _fields_with_markup(item, f"{path}[{index}]")
        ]
    return []


# ---- The report as written to disk -----------------------------------------------------


class CitedCall(BaseModel):
    """A tool call the report cites: enough to re-run the query by hand."""

    call_id: str
    tool: str
    input: dict[str, Any]


class RunStats(BaseModel):
    """Facts about the run, filled in by code. None of it is the model's claim."""

    model: str
    effort: str
    policy_mode: str
    started_at: datetime
    duration_seconds: float
    outcome: str
    outcome_detail: str
    model_requests: int
    tool_calls: int
    tool_errors: int
    refused_tool_calls: int
    rejected_submissions: int
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    cost_usd: float
    price_source: str
    price_retrieved: str


class IncidentReport(BaseModel):
    """`report.json`: the page, the model's submission, and the run's statistics."""

    page: str
    submission: Submission | None
    cited_calls: list[CitedCall]
    run: RunStats


def render_markdown(report: IncidentReport) -> str:
    """`report.md`, generated from `report.json` and nothing else."""
    run = report.run
    out = ["# Incident report", "", "## Page", "", "```", report.page.strip(), "```", ""]
    submission = report.submission
    if submission is None:
        out += [
            "## No report was submitted",
            "",
            f"The run ended with outcome `{run.outcome}`: {run.outcome_detail}",
            "",
        ]
    else:
        out += ["## Summary", "", submission.summary, "", "## Root cause", ""]
        if submission.root_causes:
            out += ["| Component | Category | Confidence | Explanation |", "|---|---|---|---|"]
            out += [
                f"| `{cause.component}` | `{cause.category}` | {cause.confidence} | "
                f"{_cell(cause.explanation)} |"
                for cause in submission.root_causes
            ]
        else:
            out.append("No root cause was established.")
        calls = {call.call_id: call for call in report.cited_calls}
        out += ["", "## Evidence", ""]
        for item in submission.evidence:
            call = calls[item.call_id]
            out.append(f"- **{item.call_id}** `{call.tool}` {_inline(call.input)}: {item.finding}")
        out += ["", "## Timeline", ""]
        out += [f"- {entry.time}: {entry.event}" for entry in submission.timeline]
        out += ["", "## Recommended actions", ""]
        out += [
            f"- `{action.action}` on `{action.component}`: {action.rationale}"
            for action in submission.recommended_actions
        ]
        out += ["", "Policy mode was `observe`: nothing in the cluster was changed.", ""]
    out += [
        "## Run",
        "",
        "| | |",
        "|---|---|",
        f"| Model | `{run.model}` (effort `{run.effort}`) |",
        f"| Outcome | `{run.outcome}` |",
        f"| Started | {run.started_at:%Y-%m-%d %H:%M:%S} UTC |",
        f"| Duration | {run.duration_seconds:.0f} s |",
        f"| Model requests | {run.model_requests} |",
        f"| Tool calls | {run.tool_calls} ({run.tool_errors} returned an error, "
        f"{run.refused_tool_calls} refused) |",
        f"| Rejected submissions | {run.rejected_submissions} |",
        f"| Tokens | {run.input_tokens} input, {run.cache_write_tokens} cache write, "
        f"{run.cache_read_tokens} cache read, {run.output_tokens} output |",
        f"| Cost | ${run.cost_usd:.4f} (prices from {run.price_source}, "
        f"retrieved {run.price_retrieved}) |",
        "",
    ]
    return "\n".join(out)


def _cell(text: str) -> str:
    """Text that is safe inside a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def _inline(tool_input: dict[str, Any]) -> str:
    return " ".join(f"`{name}={value!r}`" for name, value in tool_input.items())
