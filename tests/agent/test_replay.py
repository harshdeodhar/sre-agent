"""Replay the traces of real runs through the loop.

Each directory under results/phase3-agent/ holds the trace and the report of
one live investigation by a real model. Replaying a trace runs today's loop
code against that model's recorded responses, with no API key and no cluster.

What this proves: the loop still turns those responses into the same
conversation, the same tool calls and the same report as on the day of the
recording. What it does not prove: that the model would answer the same way
again, or that the report is correct. Scoring is the benchmark's job.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from oncall.agent.replay import ReplayMismatchError, replay
from oncall.agent.report import render_markdown
from oncall.agent.runner import REPORT_JSON, REPORT_MARKDOWN, TRACE_FILE, build_report, report_json
from oncall.agent.tools import read_only_definitions
from oncall.agent.trace import read_trace

from .conftest import REPO

RUNS = sorted(path.parent for path in (REPO / "results" / "phase3-agent").glob(f"*/{TRACE_FILE}"))

# Fields that record when something happened or how long it took on the day.
# The replay has no wall clock, so they are left out of the comparison.
_WALL_CLOCK = ("seq", "at")


def _comparable(record: dict[str, Any]) -> dict[str, Any]:
    kept = {name: value for name, value in record.items() if name not in _WALL_CLOCK}
    if kept["kind"] == "tool_call":
        del kept["duration_s"]
    return kept


def test_there_are_recorded_runs_to_replay() -> None:
    # Without this, deleting the traces would turn every test below into a
    # parametrised test with no cases, which passes.
    assert RUNS, "no trace found under results/phase3-agent/"


@pytest.mark.parametrize("run", RUNS, ids=lambda run: run.name)
def test_a_recorded_run_replays_to_the_same_run(run: Path) -> None:
    records = read_trace(run / TRACE_FILE)
    replayed = replay(records, read_only_definitions())
    assert replayed.differences == []
    # Every record the live loop wrote, the replayed loop wrote too. The
    # `session` record is written by the runner, before the loop starts.
    recorded = [_comparable(record) for record in records if record["kind"] != "session"]
    assert [_comparable(record) for record in json.loads(json.dumps(replayed.records))] == recorded


@pytest.mark.parametrize("run", RUNS, ids=lambda run: run.name)
def test_the_committed_report_is_what_the_trace_produces(run: Path) -> None:
    records = read_trace(run / TRACE_FILE)
    session = next(record for record in records if record["kind"] == "session")
    replayed = replay(records, read_only_definitions())
    report = build_report(
        replayed.page,
        replayed.config,
        replayed.result,
        datetime.fromisoformat(session["started_at"]),
    )
    assert report_json(report) == (run / REPORT_JSON).read_text(encoding="utf-8")
    assert render_markdown(report) == (run / REPORT_MARKDOWN).read_text(encoding="utf-8")


@pytest.mark.parametrize("run", RUNS, ids=lambda run: run.name)
def test_a_recorded_run_with_a_changed_tool_result_does_not_replay(run: Path) -> None:
    records = read_trace(run / TRACE_FILE)
    first_call = next(record for record in records if record["kind"] == "tool_call")
    first_call["output"] += " (edited)"
    with pytest.raises(ReplayMismatchError, match="the conversation sent to the model differs"):
        replay(records, read_only_definitions())
