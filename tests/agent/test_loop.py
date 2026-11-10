"""The agent loop, driven by scripted model replies.

The replies in this file are written by hand to put the loop into each of its
branches: they are NOT recordings of a model, and say nothing about how a
model behaves. A real recorded run is replayed in test_replay.py.
"""

import copy
import json
from dataclasses import dataclass
from typing import Any

import pytest

from oncall.agent.llm import ModelError, Reply
from oncall.agent.loop import NUDGE, Agent, Limits, Outcome, RunConfig, RunResult
from oncall.agent.prompt import SYSTEM_PROMPT
from oncall.agent.replay import ReplayMismatchError, replay
from oncall.agent.report import submit_report_definition
from oncall.agent.tools.base import ToolOutcome
from oncall.agent.trace import MemoryTrace

MODEL = "claude-haiku-5-5"
PAGE = "You have been paged."
# 1,000 input and 100 output tokens of Claude Haiku 5.5: $0.0001 + $0.00005.
USAGE = {"input_tokens": 1000, "output_tokens": 100}
COST = 0.00015


# ---- Stand-ins for the model, the tools and the clock ----------------------------------


class ScriptedClient:
    """Returns the given replies in order. An exception in the list is raised instead."""

    def __init__(self, replies: list[Reply | Exception]) -> None:
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def create(self, request: dict[str, Any]) -> Reply:
        # A copy: the loop keeps appending to the same message list.
        self.requests.append(copy.deepcopy(request))
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeTools:
    """One tool, `look`, which reports what it was asked to look at."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "look",
                "description": "Look at something.",
                "input_schema": {
                    "type": "object",
                    "properties": {"at": {"type": "string"}},
                    "required": ["at"],
                    "additionalProperties": False,
                },
                "strict": True,
            }
        ]

    def call(self, name: str, raw_input: dict[str, Any]) -> ToolOutcome:
        self.calls.append((name, raw_input))
        if raw_input["at"] == "nothing":
            return ToolOutcome("there is no such thing", is_error=True)
        return ToolOutcome(f"saw {raw_input['at']}", is_error=False)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Run:
    result: RunResult
    client: ScriptedClient
    tools: FakeTools
    trace: MemoryTrace

    def kinds(self) -> list[str]:
        return [record["kind"] for record in self.trace.records]

    def only(self, kind: str) -> dict[str, Any]:
        (record,) = [record for record in self.trace.records if record["kind"] == kind]
        return record

    def results_sent(self, request: int) -> list[dict[str, Any]]:
        """The tool results in the last message of the given request (0-based)."""
        return self.client.requests[request]["messages"][-1]["content"]


def run(
    replies: list[Reply | Exception], limits: Limits | None = None, clock: Clock | None = None
) -> Run:
    client, tools, trace = ScriptedClient(replies), FakeTools(), MemoryTrace()
    config = RunConfig(model=MODEL, limits=limits or Limits())
    result = Agent(config, client, tools, trace, clock or Clock()).run(PAGE)
    return Run(result, client, tools, trace)


def reply(
    *blocks: dict[str, Any],
    stop_reason: str = "tool_use",
    usage: dict[str, int] | None = None,
    **extra: object,
) -> Reply:
    body = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": list(blocks),
        "stop_reason": stop_reason,
        "usage": usage or USAGE,
        **extra,
    }
    return Reply(body=body, request_id="req_test")


def look(tool_use_id: str, at: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_use_id, "name": "look", "input": {"at": at}}


def submit(tool_use_id: str, *call_ids: str, **overrides: object) -> dict[str, Any]:
    """A `submit_report` call whose evidence cites `call_ids`."""
    report = {
        "summary": "Redis evicts entries because its memory ceiling is too low.",
        "root_causes": [
            {
                "component": "redis",
                "category": "cache_memory_too_low",
                "explanation": "maxmemory is 1mb.",
                "confidence": "high",
            }
        ],
        "evidence": [{"call_id": call_id, "finding": "evictions"} for call_id in call_ids],
        "timeline": [{"time": "12:00:00", "event": "Evictions begin."}],
        "recommended_actions": [
            {"action": "revert_config", "component": "redis", "rationale": "Raise maxmemory."}
        ],
        **overrides,
    }
    return {"type": "tool_use", "id": tool_use_id, "name": "submit_report", "input": report}


def text(words: str) -> dict[str, Any]:
    return {"type": "text", "text": words}


THINKING = {"type": "thinking", "thinking": "Start with the cache.", "signature": "c2lnbmF0dXJl"}


# ---- A run that ends in a report -------------------------------------------------------


def test_a_run_from_the_page_to_an_accepted_report() -> None:
    first = reply(THINKING, text("Looking."), look("tu_1", "redis"), look("tu_2", "nothing"))
    done = run([first, reply(submit("tu_3", "c1", "c2"))])

    result = done.result
    assert (result.outcome, result.detail) == (Outcome.REPORT, "report accepted")
    assert result.submission is not None
    assert [item.call_id for item in result.submission.evidence] == ["c1", "c2"]
    assert [(c.call_id, c.tool, c.input, c.is_error) for c in result.calls] == [
        ("c1", "look", {"at": "redis"}, False),
        ("c2", "look", {"at": "nothing"}, True),
    ]
    assert done.tools.calls == [("look", {"at": "redis"}), ("look", {"at": "nothing"})]
    assert (result.model_requests, result.refused_tool_calls, result.rejected_submissions) == (
        2,
        0,
        0,
    )
    assert (result.input_tokens, result.output_tokens) == (2000, 200)
    assert result.cost_usd == pytest.approx(2 * COST)


def test_the_conversation_is_append_only_and_replies_go_back_unchanged() -> None:
    first = reply(THINKING, text("Looking."), look("tu_1", "redis"), look("tu_2", "nothing"))
    done = run([first, reply(submit("tu_3", "c1"))])

    opening, second = (request["messages"] for request in done.client.requests)
    assert opening == [{"role": "user", "content": PAGE}]
    assert second[:1] == opening
    # The assistant turn is the reply's content, thinking block and all.
    assert second[1] == {"role": "assistant", "content": first.body["content"]}
    # Both tool calls are answered in one user message, in order, each with
    # the ID to cite and the budget used.
    assert second[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu_1",
                "content": "[call c1 | 1 of 30 tool calls used]\nsaw redis",
            },
            {
                "type": "tool_result",
                "tool_use_id": "tu_2",
                "content": "[call c2 | 2 of 30 tool calls used]\nthere is no such thing",
                "is_error": True,
            },
        ],
    }
    assert len(second) == 3


def test_the_request_body() -> None:
    done = run([reply(look("tu_1", "redis")), reply(submit("tu_2", "c1"))])
    request = done.client.requests[0]
    # No tool_choice (forced tool use is rejected by the larger models) and no
    # sampling parameters (not accepted with thinking on).
    assert set(request) == {
        "model",
        "max_tokens",
        "system",
        "tools",
        "messages",
        "cache_control",
        "thinking",
        "output_config",
    }
    assert request["model"] == MODEL
    assert request["max_tokens"] == 8000
    assert request["system"] == [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    assert request["tools"] == [*done.tools.definitions(), submit_report_definition()]
    assert request["cache_control"] == {"type": "ephemeral"}
    assert request["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert request["output_config"] == {"effort": "medium"}
    # Everything but the messages is the same on every turn, or the cache would miss.
    later = done.client.requests[1]
    assert {k: v for k, v in later.items() if k != "messages"} == {
        k: v for k, v in request.items() if k != "messages"
    }


def test_the_trace_of_a_run() -> None:
    done = run([reply(look("tu_1", "redis")), reply(submit("tu_2", "c1"))])
    assert done.kinds() == [
        "run_start",
        "request",
        "response",
        "tool_call",
        "request",
        "response",
        "report_accepted",
        "run_end",
    ]
    start = done.only("run_start")
    assert (start["model"], start["page"], start["system"]) == (MODEL, PAGE, SYSTEM_PROMPT)
    assert start["limits"]["max_tool_calls"] == 30
    call = done.only("tool_call")
    assert (call["call_id"], call["tool_use_id"], call["tool"], call["input"]) == (
        "c1",
        "tu_1",
        "look",
        {"at": "redis"},
    )
    assert (call["is_error"], call["output"]) == (False, "saw redis")
    response = done.trace.records[2]
    assert response["body"]["content"] == [look("tu_1", "redis")]
    assert response["request_id"] == "req_test"
    assert response["cost_usd"] == pytest.approx(COST)
    end = done.only("run_end")
    assert (end["outcome"], end["model_requests"], end["tool_calls"]) == ("report", 2, 1)
    assert end["cost_usd"] == pytest.approx(2 * COST)


# ---- Stop reasons ----------------------------------------------------------------------


def test_a_reply_without_a_tool_call_is_nudged_and_the_run_goes_on() -> None:
    done = run(
        [
            reply(text("I believe it is Redis."), stop_reason="end_turn"),
            reply(look("tu_1", "redis")),
            reply(submit("tu_2", "c1")),
        ]
    )
    assert done.result.outcome == Outcome.REPORT
    assert done.client.requests[1]["messages"][-1] == {"role": "user", "content": NUDGE}
    assert done.kinds().count("nudge") == 1


def test_a_model_that_keeps_answering_in_prose_ends_the_run() -> None:
    prose = reply(text("It is Redis."), stop_reason="end_turn")
    done = run([prose, prose, prose])
    assert done.result.outcome == Outcome.NO_TOOL_CALL
    assert done.result.submission is None
    assert done.result.model_requests == 3
    assert done.kinds().count("nudge") == 2


def test_tool_calls_in_a_reply_cut_off_at_the_output_limit_are_not_run() -> None:
    done = run([reply(look("tu_1", "redis"), stop_reason="max_tokens")])
    assert done.result.outcome == Outcome.MAX_TOKENS
    assert "8000" in done.result.detail
    assert done.tools.calls == []


def test_a_refusal_ends_the_run_with_the_models_explanation() -> None:
    details = {"type": "refusal", "category": "cyber", "explanation": "I cannot help with this."}
    done = run([reply(look("tu_1", "redis"), stop_reason="refusal", stop_details=details)])
    assert (done.result.outcome, done.result.detail) == (
        Outcome.REFUSAL,
        "I cannot help with this.",
    )
    assert done.tools.calls == []


def test_a_refusal_without_details() -> None:
    done = run([reply(stop_reason="refusal", stop_details=None)])
    assert (done.result.outcome, done.result.detail) == (
        Outcome.REFUSAL,
        "the model gave no explanation",
    )


@pytest.mark.parametrize(
    ("stop_reason", "outcome"),
    [
        ("model_context_window_exceeded", Outcome.CONTEXT_WINDOW),
        ("pause_turn", Outcome.UNEXPECTED_STOP),
        ("something_new", Outcome.UNEXPECTED_STOP),
    ],
)
def test_other_stop_reasons_end_the_run(stop_reason: str, outcome: Outcome) -> None:
    done = run([reply(look("tu_1", "redis"), stop_reason=stop_reason)])
    assert done.result.outcome == outcome
    assert done.tools.calls == []
    assert done.only("run_end")["outcome"] == outcome.value


# ---- Hard limits -----------------------------------------------------------------------


def test_the_request_limit_ends_the_run() -> None:
    replies = [reply(look(f"tu_{n}", "redis")) for n in range(5)]
    done = run(replies, Limits(max_model_requests=2))
    assert done.result.outcome == Outcome.REQUEST_LIMIT
    assert len(done.client.requests) == 2


def test_the_cost_limit_ends_the_run() -> None:
    replies = [reply(look(f"tu_{n}", "redis")) for n in range(5)]
    # Each request costs $0.00015: the limit is passed after the second.
    done = run(replies, Limits(max_cost_usd=0.0002))
    assert done.result.outcome == Outcome.COST_LIMIT
    assert len(done.client.requests) == 2
    assert done.result.cost_usd == pytest.approx(2 * COST)


def test_a_failed_api_call_ends_the_run_and_is_recorded() -> None:
    done = run([reply(look("tu_1", "redis")), ModelError("APIConnectionError: no route")])
    assert (done.result.outcome, done.result.detail) == (
        Outcome.MODEL_ERROR,
        "APIConnectionError: no route",
    )
    # The failed request is not counted as made, and costs nothing.
    assert done.result.model_requests == 1
    assert done.only("model_error") == {
        "kind": "model_error",
        "turn": 2,
        "error": "APIConnectionError: no route",
    }


# ---- Soft limits: tools refuse, the report is still accepted ----------------------------


def test_tool_calls_beyond_the_budget_are_refused_and_counted() -> None:
    first = reply(look("tu_1", "a"), look("tu_2", "b"), look("tu_3", "c"))
    done = run([first, reply(submit("tu_4", "c1", "c2"))], Limits(max_tool_calls=2))
    assert done.tools.calls == [("look", {"at": "a"}), ("look", {"at": "b"})]
    assert done.results_sent(1)[2] == {
        "type": "tool_result",
        "tool_use_id": "tu_3",
        "content": (
            "Not run. The budget of 2 tool calls is used up. "
            "File the report now with the evidence you have."
        ),
        "is_error": True,
    }
    assert done.only("tool_refused")["input"] == {"at": "c"}
    assert done.result.refused_tool_calls == 1
    assert done.only("run_end")["refused_tool_calls"] == 1
    # The report is still accepted.
    assert done.result.outcome == Outcome.REPORT
    assert len(done.result.calls) == 2


def test_tools_are_refused_once_the_time_budget_is_used() -> None:
    clock = Clock()
    clock.now = 601.0
    done = run([reply(look("tu_1", "a")), reply(submit("tu_2", root_causes=[]))], clock=clock)
    assert done.tools.calls == []
    assert done.results_sent(1)[0]["content"].startswith(
        "Not run. The time budget of 600 seconds is used up."
    )
    assert done.result.outcome == Outcome.REPORT
    assert done.result.duration_seconds == 601.0


def test_tools_are_refused_once_the_conversation_is_too_large() -> None:
    # The prompt is everything sent: uncached, cache-written and cache-read tokens.
    big = {
        "input_tokens": 10,
        "cache_creation_input_tokens": 5_000,
        "cache_read_input_tokens": 85_000,
        "output_tokens": 100,
    }
    done = run([reply(look("tu_1", "a"), usage=big), reply(submit("tu_2", root_causes=[]))])
    assert done.tools.calls == []
    assert done.results_sent(1)[0]["content"].startswith(
        "Not run. The conversation has reached its size budget."
    )
    assert done.result.outcome == Outcome.REPORT
    assert (done.result.cache_write_tokens, done.result.cache_read_tokens) == (5_000, 85_000)


# ---- Report checks ---------------------------------------------------------------------


def test_a_report_that_fails_validation_is_rejected_then_corrected() -> None:
    bad_cause = {
        "component": "redis",
        "category": "gremlins",
        "explanation": "?",
        "confidence": "high",
    }
    done = run(
        [
            reply(look("tu_1", "redis")),
            reply(submit("tu_2", "c1", root_causes=[bad_cause])),
            reply(submit("tu_3", "c1")),
        ]
    )
    (rejection,) = done.results_sent(2)
    assert rejection["is_error"] is True
    assert rejection["content"].startswith("Report rejected:\n- root_causes.0.category: ")
    assert done.result.outcome == Outcome.REPORT
    assert done.result.rejected_submissions == 1
    assert done.only("report_rejected")["input"]["root_causes"] == [bad_cause]


def test_a_report_citing_a_call_that_was_never_made_is_rejected() -> None:
    done = run(
        [
            reply(look("tu_1", "redis")),
            reply(submit("tu_2", "c1", "c9")),
            reply(submit("tu_3", "c1")),
        ]
    )
    (rejection,) = done.results_sent(2)
    assert "no tool result you have received: c9" in rejection["content"]
    assert done.result.outcome == Outcome.REPORT


def test_a_report_cannot_cite_a_call_made_in_the_same_reply() -> None:
    """The model has not seen that call's result, so it cannot be evidence yet."""
    done = run(
        [
            reply(look("tu_1", "redis"), submit("tu_2", "c1")),
            reply(submit("tu_3", "c1")),
        ]
    )
    ran, rejection = done.results_sent(1)
    assert ran["content"] == "[call c1 | 1 of 30 tool calls used]\nsaw redis"
    assert "no tool result you have received: c1" in rejection["content"]
    # Once the result has been shown, the same citation is accepted.
    assert done.result.outcome == Outcome.REPORT
    assert done.result.rejected_submissions == 1


def test_too_many_rejected_reports_end_the_run() -> None:
    bad = reply(submit("tu_1", "c9"))
    done = run([bad, bad, bad, bad])
    assert done.result.outcome == Outcome.REPORT_REJECTED
    assert done.result.submission is None
    assert done.result.rejected_submissions == 3
    assert len(done.client.requests) == 3
    assert "c9" in done.result.detail


def test_nothing_after_an_accepted_report_is_run() -> None:
    done = run(
        [
            reply(look("tu_1", "redis")),
            reply(submit("tu_2", "c1"), look("tu_3", "postgres")),
        ]
    )
    assert done.result.outcome == Outcome.REPORT
    assert done.tools.calls == [("look", {"at": "redis"})]


# ---- Replaying what the loop recorded ---------------------------------------------------


def scripted_trace() -> list[dict[str, Any]]:
    """The trace of a scripted run, as it would be read back from a file."""
    done = run(
        [
            reply(text("It is Redis."), stop_reason="end_turn"),
            reply(look("tu_1", "redis"), look("tu_2", "nothing"), submit("tu_3", "c1")),
            reply(submit("tu_4", "c1", "c2")),
        ]
    )
    return json.loads(json.dumps(done.trace.records))


def test_a_trace_replays_to_the_same_run() -> None:
    records = scripted_trace()
    replayed = replay(records, FakeTools().definitions())
    assert replayed.differences == []
    assert not replayed.prompt_changed
    assert replayed.page == PAGE
    assert replayed.result.outcome == Outcome.REPORT
    assert replayed.result.rejected_submissions == 1
    # The replayed loop wrote the same records as the recorded one.
    assert json.loads(json.dumps(replayed.records)) == [
        # The tool-call duration is measured with the real clock.
        {**record, "duration_s": replayed.records[n]["duration_s"]}
        if record["kind"] == "tool_call"
        else record
        for n, record in enumerate(records)
    ]


def test_replay_notices_a_changed_tool_result() -> None:
    records = scripted_trace()
    next(r for r in records if r["kind"] == "tool_call")["output"] = "saw nothing wrong"
    with pytest.raises(ReplayMismatchError, match="turn 3"):
        replay(records, FakeTools().definitions())


def test_replay_notices_a_changed_response() -> None:
    records = scripted_trace()
    second = [r for r in records if r["kind"] == "response"][1]
    second["body"]["content"][0]["input"] = {"at": "postgres"}
    with pytest.raises(ReplayMismatchError, match="call c1"):
        replay(records, FakeTools().definitions())


def test_replay_reports_a_run_that_ends_differently() -> None:
    records = scripted_trace()
    next(r for r in records if r["kind"] == "run_end")["outcome"] = "refusal"
    replayed = replay(records, FakeTools().definitions())
    assert replayed.differences == ["outcome: recorded 'refusal', replayed 'report'"]


def test_replay_says_when_the_prompt_has_changed_since_the_recording() -> None:
    records = scripted_trace()
    next(r for r in records if r["kind"] == "run_start")["system"] = "An older prompt."
    replayed = replay(records, FakeTools().definitions())
    assert replayed.prompt_changed
    assert replayed.differences == []


def test_replay_needs_a_complete_trace() -> None:
    records = [r for r in scripted_trace() if r["kind"] != "run_end"]
    with pytest.raises(ReplayMismatchError, match="exactly one run_end"):
        replay(records, FakeTools().definitions())
