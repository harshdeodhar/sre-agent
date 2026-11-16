"""Replay the traces of real runs through the loop.

Each run directory under results/ holds the trace and the report of one live
investigation by a real model. Replaying a trace runs today's loop code
against that model's recorded responses, with no API key and no cluster.

What this proves: the loop still turns those responses into the same
conversation, the same tool calls and the same records as on the day of the
recording. What it does not prove: that the model would answer the same way
again, or that the report is correct. Scoring is the benchmark's job.

Two sets of runs:

- results/phase3-agent/: observe mode, recorded before the write tools
  existed. Observe mode still sends the same prompt and tools, so these must
  still replay record for record. Their report files are in the Phase 3
  format (no write actions) and are not compared with today's rendering.
- results/phase4-guardrails/: propose and auto mode, with write tools. Their
  report files must also be exactly what today's code makes of the trace.

A write tool call replays from the recording, gate decision included: a
replay never reaches a cluster, so the gate itself is not run here. It is
tested in test_gate.py.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from oncall.agent.replay import ReplayMismatchError, replay
from oncall.agent.report import render_markdown
from oncall.agent.runner import REPORT_JSON, REPORT_MARKDOWN, TRACE_FILE, build_report, report_json
from oncall.agent.trace import read_trace

from .conftest import REPO


def _runs(phase: str) -> list[Path]:
    return sorted(path.parent for path in (REPO / "results" / phase).glob(f"*/{TRACE_FILE}"))


OBSERVE_RUNS = _runs("phase3-agent")
WRITE_RUNS = _runs("phase4-guardrails")
RUNS = OBSERVE_RUNS + WRITE_RUNS

# Fields that record when something happened or how long it took on the day.
# The replay has no wall clock, so they are left out of the comparison.
_WALL_CLOCK = ("seq", "at")
# Records the live run wrote that the loop does not: `session` comes from the
# runner before the loop starts, `write_dry_run` from the gate, which a replay
# does not run.
_NOT_FROM_THE_LOOP = ("session", "write_dry_run")


def _comparable(record: dict[str, Any]) -> dict[str, Any]:
    kept = {name: value for name, value in record.items() if name not in _WALL_CLOCK}
    if kept["kind"] == "tool_call":
        del kept["duration_s"]
    return kept


def _name(run: Path) -> str:
    return f"{run.parent.name}/{run.name}"


def test_there_are_recorded_runs_to_replay() -> None:
    # Without these, deleting the traces would turn every test below into a
    # parametrised test with no cases, which passes.
    assert OBSERVE_RUNS, "no trace found under results/phase3-agent/"
    assert WRITE_RUNS, "no trace found under results/phase4-guardrails/"


@pytest.mark.parametrize("run", RUNS, ids=_name)
def test_a_recorded_run_replays_to_the_same_run(run: Path) -> None:
    records = read_trace(run / TRACE_FILE)
    replayed = replay(records)
    assert replayed.differences == []
    assert not replayed.prompt_changed
    # Every record the live loop wrote, the replayed loop wrote too.
    recorded = [_comparable(r) for r in records if r["kind"] not in _NOT_FROM_THE_LOOP]
    assert [_comparable(record) for record in json.loads(json.dumps(replayed.records))] == recorded


@pytest.mark.parametrize("run", WRITE_RUNS, ids=_name)
def test_the_committed_report_is_what_the_trace_produces(run: Path) -> None:
    records = read_trace(run / TRACE_FILE)
    session = next(record for record in records if record["kind"] == "session")
    replayed = replay(records)
    report = build_report(
        replayed.page,
        replayed.config,
        replayed.result,
        datetime.fromisoformat(session["started_at"]),
    )
    assert report_json(report) == (run / REPORT_JSON).read_text(encoding="utf-8")
    assert render_markdown(report) == (run / REPORT_MARKDOWN).read_text(encoding="utf-8")


@pytest.mark.parametrize("run", RUNS, ids=_name)
def test_a_recorded_run_with_a_changed_tool_result_does_not_replay(run: Path) -> None:
    records = read_trace(run / TRACE_FILE)
    first_call = next(record for record in records if record["kind"] == "tool_call")
    first_call["output"] += " (edited)"
    with pytest.raises(ReplayMismatchError, match="the conversation sent to the model differs"):
        replay(records)


@pytest.mark.parametrize("run", WRITE_RUNS, ids=_name)
def test_every_dry_run_was_recorded_before_its_outcome(run: Path) -> None:
    """The gate writes the diff to the trace before it executes or proposes anything."""
    records = read_trace(run / TRACE_FILE)
    for index, record in enumerate(records):
        action = record.get("action") if record["kind"] == "tool_call" else None
        if action is None or action["diff"] is None:
            continue
        earlier = [r for r in records[:index] if r["kind"] == "write_dry_run"]
        assert earlier, f"call {record['call_id']} has a diff but no write_dry_run before it"
        assert earlier[-1]["diff"] == action["diff"]
        assert earlier[-1]["command"] == action["command"]
