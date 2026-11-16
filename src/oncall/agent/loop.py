"""The agent loop: ask the model, run the tools it asks for, repeat.

Hand-written instead of using the SDK's tool runner, because this loop is
where the limits, the trace and (from Phase 4) the write gate live, and those
have to be code that can be read and tested.

How a run goes:

1. The conversation starts with the page (the firing alerts).
2. Each turn sends the whole conversation to the model. The reply is appended
   unchanged, the tools it asked for are run, and their results are appended
   as the next user message. Nothing already in the conversation is ever
   edited: the API caches the prefix, and the model's thinking blocks are only
   valid in the conversation that produced them.
3. The run ends when the model calls `submit_report` with a report that passes
   the checks, or when a limit or an API stop reason ends it. Every way a run
   can end is a value of `Outcome`.

The loop does not know which tools write. A write tool's outcome carries the
gate's decision (guardrails/gate.py); the loop records it with the call, so a
rejected write is traced and counted like any other.

Limits come in two kinds. Soft limits (tool calls, time, conversation size)
stop further investigation but still let the model file a report from what it
has: the tools refuse and say why. Hard limits (model requests, cost) end the
run on the spot.
"""

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from pydantic import ValidationError

from oncall.agent.guardrails.policy import Policy
from oncall.agent.guardrails.record import ActionRecord
from oncall.agent.llm import ModelClient, ModelError, Response
from oncall.agent.pricing import request_cost
from oncall.agent.prompt import system_prompt
from oncall.agent.report import (
    SUBMIT_REPORT,
    Submission,
    check_submission,
    submit_report_definition,
)
from oncall.agent.tools.base import ToolRunner
from oncall.agent.trace import digest

NUDGE = (
    "You ended your turn without calling a tool. The investigation only ends when you call "
    "submit_report. Continue investigating, or file the report now."
)


class Outcome(StrEnum):
    """Every way a run can end."""

    REPORT = "report"  # a report was submitted and accepted
    REPORT_REJECTED = "report_rejected"  # too many submissions failed the checks
    NO_TOOL_CALL = "no_tool_call"  # the model kept answering in prose
    MAX_TOKENS = "max_tokens"  # a reply was cut off at the output limit
    REFUSAL = "refusal"  # the model declined to continue
    CONTEXT_WINDOW = "context_window_exceeded"
    UNEXPECTED_STOP = "unexpected_stop_reason"
    REQUEST_LIMIT = "request_limit"
    COST_LIMIT = "cost_limit"
    MODEL_ERROR = "model_error"  # the API call failed after the SDK's retries


@dataclass(frozen=True)
class Limits:
    # Soft: when reached, tools refuse and the model is told to file its report.
    max_tool_calls: int = 30
    max_seconds: float = 600
    # Claude Haiku 5.5 charges five times as much for a request whose prompt
    # is over 100,000 tokens. Stop investigating before a conversation gets there.
    max_prompt_tokens: int = 90_000
    # Hard: the run ends.
    max_model_requests: int = 45
    max_cost_usd: float = 0.50
    max_report_attempts: int = 3
    max_nudges: int = 2


@dataclass(frozen=True)
class RunConfig:
    model: str
    # Thinking depth. Pinned for the whole run: changing it between requests
    # would invalidate the prompt cache.
    effort: str = "medium"
    max_tokens: int = 8000
    limits: Limits = Limits()
    # What the agent may change. Decides the system prompt and the tools;
    # the gate enforces it.
    policy: Policy = Policy()


@dataclass(frozen=True)
class CallRecord:
    call_id: str
    tool: str
    input: dict[str, Any]
    is_error: bool
    # Set for a write tool call: the gate's decision.
    action: ActionRecord | None = None


@dataclass
class RunResult:
    outcome: Outcome
    detail: str
    submission: Submission | None
    calls: list[CallRecord]
    model_requests: int
    refused_tool_calls: int
    rejected_submissions: int
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    cost_usd: float
    duration_seconds: float

    @property
    def actions(self) -> list[ActionRecord]:
        """Every write the model attempted, in order, whatever the gate decided."""
        return [call.action for call in self.calls if call.action is not None]


class Trace(Protocol):
    def write(self, kind: str, **fields: object) -> None: ...


@dataclass
class _State:
    messages: list[dict[str, Any]]
    calls: list[CallRecord] = field(default_factory=list)
    submission: Submission | None = None
    requests: int = 0
    refused: int = 0
    rejected: int = 0
    nudges: int = 0
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    last_prompt_tokens: int = 0
    elapsed: float = 0.0


class Agent:
    def __init__(
        self,
        config: RunConfig,
        client: ModelClient,
        tools: ToolRunner,
        trace: Trace,
        elapsed: Callable[[], float],
    ) -> None:
        """`elapsed` returns the seconds since the run started.

        It is read once per turn and nowhere else, so a replay can feed back
        the recorded values and take the same decisions.
        """
        self._config = config
        self._limits = config.limits
        self._client = client
        self._tools = tools
        self._trace = trace
        self._elapsed = elapsed
        mode = config.policy.mode
        self._system = system_prompt(mode)
        # Fixed order: investigation tools first, the report tool last.
        self._tool_definitions = [*tools.definitions(), submit_report_definition(mode)]

    def run(self, page: str) -> RunResult:
        state = _State(messages=[{"role": "user", "content": page}])
        self._trace.write(
            "run_start",
            model=self._config.model,
            effort=self._config.effort,
            max_tokens=self._config.max_tokens,
            limits=asdict(self._limits),
            system=self._system,
            tools=self._tool_definitions,
            page=page,
        )
        outcome, detail = self._loop(state)
        result = RunResult(
            outcome=outcome,
            detail=detail,
            submission=state.submission,
            calls=state.calls,
            model_requests=state.requests,
            refused_tool_calls=state.refused,
            rejected_submissions=state.rejected,
            input_tokens=state.input_tokens,
            cache_write_tokens=state.cache_write_tokens,
            cache_read_tokens=state.cache_read_tokens,
            output_tokens=state.output_tokens,
            cost_usd=state.cost_usd,
            duration_seconds=self._elapsed(),
        )
        self._trace.write(
            "run_end",
            outcome=outcome.value,
            detail=detail,
            model_requests=result.model_requests,
            tool_calls=len(result.calls),
            refused_tool_calls=result.refused_tool_calls,
            rejected_submissions=result.rejected_submissions,
            input_tokens=result.input_tokens,
            cache_write_tokens=result.cache_write_tokens,
            cache_read_tokens=result.cache_read_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            duration_s=result.duration_seconds,
        )
        return result

    # ---- One turn after another ---------------------------------------------------

    def _loop(self, state: _State) -> tuple[Outcome, str]:
        limits = self._limits
        while True:
            state.elapsed = self._elapsed()
            if state.requests >= limits.max_model_requests:
                return Outcome.REQUEST_LIMIT, f"{state.requests} model requests made"
            if state.cost_usd >= limits.max_cost_usd:
                return Outcome.COST_LIMIT, f"${state.cost_usd:.4f} spent"

            request = self._request(state.messages)
            self._trace.write(
                "request",
                turn=state.requests + 1,
                elapsed_s=state.elapsed,
                message_count=len(state.messages),
                messages_sha256=digest(state.messages),
                request_sha256=digest(request),
            )
            try:
                reply = self._client.create(request)
            except ModelError as err:
                self._trace.write("model_error", turn=state.requests + 1, error=str(err))
                return Outcome.MODEL_ERROR, str(err)
            state.requests += 1

            response = Response.model_validate(reply.body)
            cost = request_cost(self._config.model, response.usage)
            usage = response.usage
            state.input_tokens += usage.input_tokens
            state.cache_write_tokens += usage.cache_creation_input_tokens
            state.cache_read_tokens += usage.cache_read_input_tokens
            state.output_tokens += usage.output_tokens
            state.cost_usd += cost
            state.last_prompt_tokens = usage.prompt_tokens
            self._trace.write(
                "response",
                turn=state.requests,
                request_id=reply.request_id,
                cost_usd=cost,
                total_cost_usd=state.cost_usd,
                body=reply.body,
            )

            # The reply goes back into the conversation exactly as it arrived,
            # thinking blocks included.
            state.messages.append({"role": "assistant", "content": reply.body["content"]})

            # The stop reason is checked before anything in the reply is acted
            # on: tool calls in a reply that was cut off or refused are not run.
            match response.stop_reason:
                case "tool_use":
                    ended = self._run_tools(state, response)
                    if ended is not None:
                        return ended
                case "end_turn" | "stop_sequence":
                    state.nudges += 1
                    if state.nudges > limits.max_nudges:
                        return (
                            Outcome.NO_TOOL_CALL,
                            f"the model ended its turn without a tool call {state.nudges} times",
                        )
                    self._trace.write("nudge", turn=state.requests)
                    state.messages.append({"role": "user", "content": NUDGE})
                case "max_tokens":
                    return (
                        Outcome.MAX_TOKENS,
                        f"a reply reached the output limit of {self._config.max_tokens} tokens",
                    )
                case "refusal":
                    details = response.stop_details
                    reason = (details.explanation or details.category) if details else None
                    return Outcome.REFUSAL, reason or "the model gave no explanation"
                case "model_context_window_exceeded":
                    return Outcome.CONTEXT_WINDOW, "the conversation outgrew the context window"
                case other:
                    # `pause_turn` belongs to server-side tools, which this
                    # agent does not use; anything else is new to this code.
                    return Outcome.UNEXPECTED_STOP, f"stop reason {other!r}"

    def _request(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """The request body for the next turn.

        Caching: the prompt is rendered as tools, then system, then messages,
        and a cache breakpoint covers everything before it. The breakpoint on
        the system block caches the tools and the system prompt, which never
        change. The top-level `cache_control` puts a second, moving
        breakpoint at the end of the conversation, so each turn reads the
        previous turn's prefix from the cache and writes only what is new.

        No `tool_choice` (the default, `auto`): the larger models reject a
        forced tool call. No `temperature`, `top_p` or `top_k`: they are not
        accepted when thinking is on, and it always is on these models.
        """
        return {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,
            "system": [
                {"type": "text", "text": self._system, "cache_control": {"type": "ephemeral"}}
            ],
            "tools": self._tool_definitions,
            "messages": messages,
            "cache_control": {"type": "ephemeral"},
            # "summarized" returns a summary of the thinking, which makes the
            # trace explain itself. It does not change what is billed.
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": self._config.effort},
        }

    # ---- Tool calls ----------------------------------------------------------------

    def _run_tools(self, state: _State, response: Response) -> tuple[Outcome, str] | None:
        """Run the tool calls of one reply, in order. Returns how the run ended, if it did."""
        results: list[dict[str, Any]] = []
        # A report may cite only calls whose results the model has seen. A call
        # made in this same reply has an ID the model could guess ("c8") but a
        # result it has not read yet.
        seen = {call.call_id for call in state.calls}
        for block in response.content:
            if block.type != "tool_use":
                continue
            if block.name == SUBMIT_REPORT:
                problems = self._submit(state, block.input, seen)
                if not problems:
                    return Outcome.REPORT, "report accepted"
                if state.rejected >= self._limits.max_report_attempts:
                    return (
                        Outcome.REPORT_REJECTED,
                        f"{state.rejected} submissions were rejected; last: {'; '.join(problems)}",
                    )
                text = "Report rejected:\n" + "\n".join(f"- {problem}" for problem in problems)
                results.append(_tool_result(block.id, text, is_error=True))
                continue

            reason = self._wrap_up_reason(state)
            if reason is not None:
                state.refused += 1
                self._trace.write("tool_refused", tool=block.name, input=block.input, reason=reason)
                text = f"Not run. {reason} File the report now with the evidence you have."
                results.append(_tool_result(block.id, text, is_error=True))
                continue

            call_id = f"c{len(state.calls) + 1}"
            started = time.monotonic()
            outcome = self._tools.call(block.name, block.input)
            state.calls.append(
                CallRecord(call_id, block.name, block.input, outcome.is_error, outcome.action)
            )
            # A read tool's record has no `action` field at all, so the records
            # of an observe-mode run are what they were before write tools existed.
            action = {"action": outcome.action.model_dump(mode="json")} if outcome.action else {}
            self._trace.write(
                "tool_call",
                call_id=call_id,
                tool_use_id=block.id,
                tool=block.name,
                input=block.input,
                is_error=outcome.is_error,
                duration_s=round(time.monotonic() - started, 3),
                output=outcome.text,
                **action,
            )
            # The header gives the model the ID to cite and its remaining budget.
            header = (
                f"[call {call_id} | {len(state.calls)} of {self._limits.max_tool_calls} "
                "tool calls used]"
            )
            results.append(_tool_result(block.id, f"{header}\n{outcome.text}", outcome.is_error))

        # Every tool call of the reply is answered in one user message.
        state.messages.append({"role": "user", "content": results})
        return None

    def _wrap_up_reason(self, state: _State) -> str | None:
        limits = self._limits
        if len(state.calls) >= limits.max_tool_calls:
            return f"The budget of {limits.max_tool_calls} tool calls is used up."
        if state.elapsed >= limits.max_seconds:
            return f"The time budget of {limits.max_seconds:g} seconds is used up."
        if state.last_prompt_tokens >= limits.max_prompt_tokens:
            return "The conversation has reached its size budget."
        return None

    def _submit(self, state: _State, raw: dict[str, Any], citable: set[str]) -> list[str]:
        """Check a submitted report. Returns the problems; none means it was accepted."""
        try:
            submission = Submission.model_validate(raw)
        except ValidationError as err:
            problems = [
                f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}"
                for problem in err.errors()
            ]
        else:
            problems = check_submission(submission, citable)
        if problems:
            state.rejected += 1
            self._trace.write("report_rejected", input=raw, problems=problems)
            return problems
        state.submission = submission
        self._trace.write("report_accepted", submission=submission.model_dump(mode="json"))
        return []


def _tool_result(tool_use_id: str, text: str, is_error: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
    if is_error:
        result["is_error"] = True
    return result
