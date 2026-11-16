"""Replay a recorded trace through the loop, without the API or the cluster.

A trace holds every model response and every tool result of a run. Replaying
feeds them back to the same `Agent` code in the same order: the model client
returns the recorded responses, the tools return the recorded results, and
the clock returns the recorded readings. If the loop still behaves as it did
when the trace was recorded, it rebuilds the same conversation turn by turn
and ends the same way.

That is checked, not assumed. Every recorded request carries a hash of the
conversation that was sent, and the replay compares it with the conversation
it built. A mismatch means the loop's behaviour has changed.

This is how the loop is unit-tested with a real model's behaviour, free of
charge, and it lets anyone re-derive a report in `results/` from its trace.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from oncall.agent.guardrails.gate import tool_definitions
from oncall.agent.guardrails.policy import Policy, PolicyMode
from oncall.agent.guardrails.record import ActionRecord
from oncall.agent.llm import ModelError, Reply
from oncall.agent.loop import Agent, Limits, RunConfig, RunResult
from oncall.agent.prompt import system_prompt
from oncall.agent.report import submit_report_definition
from oncall.agent.tools.base import ToolOutcome
from oncall.agent.trace import MemoryTrace, digest


class ReplayMismatchError(RuntimeError):
    """The loop did something different from what the trace recorded."""


class ReplayClient:
    """Answers each request with the response recorded for that turn."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._events = [
            record for record in records if record["kind"] in ("request", "response", "model_error")
        ]
        self._next = 0

    def create(self, request: dict[str, Any]) -> Reply:
        recorded = self._take("request")
        turn = recorded["turn"]
        if digest(request["messages"]) != recorded["messages_sha256"]:
            raise ReplayMismatchError(
                f"turn {turn}: the conversation sent to the model differs from the recorded one"
            )
        answer = self._take("response", "model_error")
        if answer["kind"] == "model_error":
            raise ModelError(answer["error"])
        return Reply(body=answer["body"], request_id=answer.get("request_id"))

    def _take(self, *kinds: str) -> dict[str, Any]:
        if self._next >= len(self._events):
            raise ReplayMismatchError("the loop made a model request the trace does not contain")
        event = self._events[self._next]
        if event["kind"] not in kinds:
            raise ReplayMismatchError(
                f"expected a {' or '.join(kinds)} record, found {event['kind']}"
            )
        self._next += 1
        return event


class ReplayToolbox:
    """Answers each tool call with the recorded result, after checking it is the same call.

    A write tool call is answered from the recording too, gate decision
    included: a replay never reaches a cluster, so it never runs the gate.
    The gate itself is tested in tests/agent/test_gate.py.
    """

    def __init__(self, records: list[dict[str, Any]], definitions: list[dict[str, Any]]) -> None:
        self._calls = [record for record in records if record["kind"] == "tool_call"]
        self._definitions = definitions
        self._next = 0

    def definitions(self) -> list[dict[str, Any]]:
        return self._definitions

    def call(self, name: str, raw_input: dict[str, Any]) -> ToolOutcome:
        if self._next >= len(self._calls):
            raise ReplayMismatchError(f"the loop called {name}, which the trace does not contain")
        recorded = self._calls[self._next]
        self._next += 1
        if (recorded["tool"], recorded["input"]) != (name, raw_input):
            raise ReplayMismatchError(
                f"call {recorded['call_id']}: the trace has {recorded['tool']} {recorded['input']}, "
                f"the loop called {name} {raw_input}"
            )
        action = recorded.get("action")
        return ToolOutcome(
            recorded["output"],
            recorded["is_error"],
            ActionRecord.model_validate(action) if action is not None else None,
        )


def replay_clock(records: list[dict[str, Any]]) -> Callable[[], float]:
    """The recorded clock readings, in the order the loop took them.

    The loop reads the clock once at the top of each turn (recorded with the
    request) and once at the end (the run's duration). A run that a hard limit
    ended read it once more than it made requests; that reading is not in the
    trace and nothing depended on it, so the last value is repeated.
    """
    readings = [record["elapsed_s"] for record in records if record["kind"] == "request"]
    readings += [record["duration_s"] for record in records if record["kind"] == "run_end"]
    remaining = iter(readings)
    last = 0.0

    def read() -> float:
        nonlocal last
        last = next(remaining, last)
        return last

    return read


@dataclass
class Replay:
    config: RunConfig
    page: str
    result: RunResult
    # What the replayed loop wrote to its trace, for comparison with the recording.
    records: list[dict[str, Any]]
    # Ways the replayed run differs from the recorded one. Empty means the loop
    # reproduced the run.
    differences: list[str]
    # True if the system prompt or a tool definition has changed since the
    # recording. Not a failure of the loop: the recorded responses answered a
    # different prompt than today's code would send.
    prompt_changed: bool


def replay(
    records: list[dict[str, Any]], definitions: list[dict[str, Any]] | None = None
) -> Replay:
    """Run the loop against a recorded trace.

    `definitions` are the tools the run is checked against, to tell whether
    the prompt has changed since the recording. By default they are today's
    tools for the run's policy mode; the loop's own tests pass their fake tools.
    """
    start = _only(records, "run_start")
    end = _only(records, "run_end")
    policy = policy_of(records)
    config = RunConfig(
        model=start["model"],
        effort=start["effort"],
        max_tokens=start["max_tokens"],
        limits=Limits(**start["limits"]),
        policy=policy,
    )
    if definitions is None:
        definitions = tool_definitions(policy.mode)
    trace = MemoryTrace()
    agent = Agent(
        config,
        ReplayClient(records),
        ReplayToolbox(records, definitions),
        trace,
        replay_clock(records),
    )
    result = agent.run(start["page"])

    replayed_end = trace.records[-1]
    differences = [
        f"{name}: recorded {end[name]!r}, replayed {replayed_end.get(name)!r}"
        for name in end
        if name not in ("seq", "at", "kind") and end[name] != replayed_end.get(name)
    ]
    recorded_prompt = (start["system"], start["tools"])
    current_prompt = (
        system_prompt(policy.mode),
        [*definitions, submit_report_definition(policy.mode)],
    )
    return Replay(
        config=config,
        page=start["page"],
        result=result,
        records=trace.records,
        differences=differences,
        prompt_changed=recorded_prompt != current_prompt,
    )


def policy_of(records: list[dict[str, Any]]) -> Policy:
    """The policy a run was made under, from its `session` record.

    The runner writes that record. A trace written by the loop alone, as in
    the loop's tests, has none, and was made with the default: observe mode.
    """
    sessions = [record for record in records if record["kind"] == "session"]
    if not sessions:
        return Policy()
    session = _only(records, "session")
    return Policy(
        mode=PolicyMode(session["policy_mode"]),
        # Traces from before Phase 4 had no write tools and do not record it.
        max_write_actions=session.get("max_write_actions", Policy().max_write_actions),
    )


def _only(records: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    found = [record for record in records if record["kind"] == kind]
    if len(found) != 1:
        raise ReplayMismatchError(
            f"a complete trace has exactly one {kind} record; this one has {len(found)}"
        )
    return found[0]
