"""One investigation from page to files on disk.

`investigate` is what the command line calls today and what the benchmark
harness will call for each incident: it runs the loop with a trace file open,
then writes the report. A run directory holds three files:

- `trace.jsonl`: everything that happened (trace.py)
- `report.json`: the page, the model's submission, and statistics from code
- `report.md`: the same report for reading, rendered from `report.json`
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from oncall.agent.llm import ModelClient
from oncall.agent.loop import Agent, RunConfig, RunResult, Trace
from oncall.agent.pricing import PRICE_RETRIEVED, PRICE_SOURCE
from oncall.agent.report import CitedCall, IncidentReport, RunStats, WriteCounts, render_markdown
from oncall.agent.tools.base import ToolRunner
from oncall.agent.trace import TraceWriter

TRACE_FILE = "trace.jsonl"
REPORT_JSON = "report.json"
REPORT_MARKDOWN = "report.md"


def build_report(
    page: str, config: RunConfig, result: RunResult, started_at: datetime
) -> IncidentReport:
    """Join the model's submission with the facts the code knows about the run."""
    cited = {item.call_id for item in result.submission.evidence} if result.submission else set()
    return IncidentReport(
        page=page,
        submission=result.submission,
        cited_calls=[
            CitedCall(call_id=call.call_id, tool=call.tool, input=call.input)
            for call in result.calls
            if call.call_id in cited
        ],
        actions=result.actions,
        run=RunStats(
            model=config.model,
            effort=config.effort,
            policy_mode=config.policy.mode,
            started_at=started_at,
            duration_seconds=result.duration_seconds,
            outcome=result.outcome.value,
            outcome_detail=result.detail,
            model_requests=result.model_requests,
            tool_calls=len(result.calls),
            tool_errors=sum(call.is_error for call in result.calls),
            refused_tool_calls=result.refused_tool_calls,
            rejected_submissions=result.rejected_submissions,
            write_actions=WriteCounts.of(result.actions),
            input_tokens=result.input_tokens,
            cache_write_tokens=result.cache_write_tokens,
            cache_read_tokens=result.cache_read_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            price_source=PRICE_SOURCE,
            price_retrieved=PRICE_RETRIEVED,
        ),
    )


def report_json(report: IncidentReport) -> str:
    """`report.json` exactly as it is written to disk."""
    return report.model_dump_json(indent=2) + "\n"


def investigate(
    config: RunConfig,
    client: ModelClient,
    make_tools: Callable[[Trace], ToolRunner],
    page: str,
    directory: Path,
    secrets: tuple[str, ...],
) -> IncidentReport:
    """Run the agent on `page` and write the trace and the report into `directory`.

    `make_tools` builds the tools once the trace is open: the write gate
    records each dry run in the trace before it executes anything.

    `secrets` are values that must never reach the trace (the API key, the
    cluster tokens). Nothing passes them to the trace; the writer refuses any
    line that contains one, in case something ever does.
    """
    started_at = datetime.now(UTC)
    started = time.monotonic()
    with TraceWriter(directory / TRACE_FILE, secrets) as trace:
        trace.write(
            "session",
            started_at=started_at.isoformat(),
            policy_mode=config.policy.mode.value,
            max_write_actions=config.policy.max_write_actions,
            anthropic_sdk=version("anthropic"),
        )
        tools = make_tools(trace)
        agent = Agent(config, client, tools, trace, lambda: time.monotonic() - started)
        result = agent.run(page)
    report = build_report(page, config, result, started_at)
    (directory / REPORT_JSON).write_text(report_json(report), encoding="utf-8")
    (directory / REPORT_MARKDOWN).write_text(render_markdown(report), encoding="utf-8")
    return report
