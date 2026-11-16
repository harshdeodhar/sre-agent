"""The write gate: the one place where the agent changes anything.

Every write tool call goes through `WriteGate.submit`, and nothing else in the
agent holds the kubectl runner that can write. The checks run in a fixed
order, and the first that fails rejects the write:

1. schema      the parameters have the right types
2. scope       the target exists in testbed and is the right kind of object
3. allow-list  this action may touch this target, with these values
4. budget      the run has writes left; no second write to the same Deployment
               after one was executed; not the same proposal twice
5. policy      the mode allows writes at all (observe does not)
6. dry run     the API server accepts the change (RBAC and admission
               included) and it changes something. The diff is written to the
               trace before anything else happens.
7. decision    auto mode executes a low-risk change; anything else goes to the
               approver, and with no approver it stays a proposal
8. execute     the change is made, then the object is read back to confirm it

The tool-call and time budgets are the loop's: when they are used up the loop
refuses every tool call, writes included, before the gate sees it.

Whatever happens is returned as an `ActionRecord` on the tool outcome. The
loop counts and traces it, and it goes into the report: a rejected write is
never dropped.
"""

import difflib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import yaml
from pydantic import ValidationError

from oncall.agent.guardrails.actions import (
    ACTIONS,
    ClusterView,
    DeletePod,
    Plan,
    Rejected,
    ReplaceField,
    write_definitions,
)
from oncall.agent.guardrails.policy import AUTO_EXECUTES, Policy, PolicyMode
from oncall.agent.guardrails.record import ActionRecord, Decision, Step
from oncall.agent.tools import read_only_definitions
from oncall.agent.tools.base import Toolbox, ToolError, ToolOutcome, bound
from oncall.chaos.kubectl import TESTBED, CommandError, Runner

AUTO_APPROVAL = "auto mode: low risk"


class Trace(Protocol):
    def write(self, kind: str, **fields: object) -> None: ...


@dataclass(frozen=True)
class Approval:
    approved: bool
    by: str
    note: str = ""


class Approver(Protocol):
    """Decides on a proposal. Returns None if nobody is there to decide."""

    def review(self, proposal: ActionRecord) -> Approval | None: ...


class NobodyApproves:
    """No human in the loop: every proposal stays a proposal."""

    def review(self, proposal: ActionRecord) -> Approval | None:
        return None


class WriteGate:
    def __init__(
        self,
        policy: Policy,
        reader: Runner,
        writer: Runner | None,
        approver: Approver,
        trace: Trace,
    ) -> None:
        """`reader` is kubectl as the read-only identity, `writer` as the operator identity.

        In observe mode there is no writer at all: the identity that could
        write is never even created.
        """
        self._policy = policy
        self._view = ClusterView(reader)
        self._reader = reader
        self._writer = writer
        self._approver = approver
        self._trace = trace
        self._dry_runs = 0
        self._executed_on: set[str] = set()
        self._proposed: set[str] = set()

    def handles(self, name: str) -> bool:
        return name in ACTIONS

    def definitions(self) -> list[dict[str, Any]]:
        return write_definitions()

    def submit(self, name: str, raw_input: dict[str, Any]) -> ToolOutcome:
        record = self._decide(name, raw_input)
        is_error = record.decision in (Decision.REJECTED, Decision.DECLINED, Decision.FAILED) or (
            record.verified is False
        )
        return ToolOutcome(bound(_tell_the_model(record)), is_error=is_error, action=record)

    # ---- The steps -----------------------------------------------------------------------

    def _decide(self, name: str, raw_input: dict[str, Any]) -> ActionRecord:
        def rejected(step: Step, reason: str, plan: Plan | None = None) -> ActionRecord:
            return _record(name, raw_input, Decision.REJECTED, reason, plan, step=step)

        # 1. Schema.
        action = ACTIONS[name]
        try:
            params = action.params.model_validate(raw_input)
        except ValidationError as err:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in problem['loc']) or 'input'}: {problem['msg']}"
                for problem in err.errors()
            )
            return rejected(Step.SCHEMA, problems)

        # 2 and 3. Scope and allow-list, which need a read of the cluster.
        try:
            plan = action.plan(params, self._view)
        except Rejected as err:
            return rejected(err.step, err.reason)
        except ToolError as err:
            return _record(name, raw_input, Decision.FAILED, str(err))

        # 4. Budget.
        if self._dry_runs >= self._policy.max_write_actions:
            return rejected(
                Step.BUDGET,
                f"this run has used its {self._policy.max_write_actions} write actions",
                plan,
            )
        if plan.deployment in self._executed_on:
            return rejected(
                Step.BUDGET,
                f"a change to deployment/{plan.deployment} has already been executed in this run; "
                "check its effect before anything else is changed there",
                plan,
            )
        if plan.key() in self._proposed:
            return rejected(Step.BUDGET, "this exact change has already been proposed", plan)

        # 5. Policy mode.
        if self._policy.mode is PolicyMode.OBSERVE or self._writer is None:
            return rejected(
                Step.POLICY, "observe mode: writes are neither executed nor proposed", plan
            )

        # 6. Dry run on the server, and the diff, recorded before anything else.
        self._dry_runs += 1
        command = _command(plan)
        try:
            diff, expected = self._dry_run(plan, command)
        except Rejected as err:
            return rejected(err.step, err.reason, plan)
        self._trace.write(
            "write_dry_run",
            action=name,
            target=plan.target,
            risk=plan.risk.value,
            command=command,
            diff=diff,
        )

        def outcome(decision: Decision, reason: str, **fields: Any) -> ActionRecord:  # noqa: ANN401
            return _record(
                name, raw_input, decision, reason, plan, command=command, diff=diff, **fields
            )

        # 7. Decision.
        if self._policy.mode is PolicyMode.AUTO and plan.risk in AUTO_EXECUTES:
            approved_by = AUTO_APPROVAL
        else:
            proposal = outcome(Decision.PROPOSED, "recorded for a human operator; not executed")
            approval = self._approver.review(proposal)
            if approval is None:
                self._proposed.add(plan.key())
                return proposal
            if not approval.approved:
                self._proposed.add(plan.key())
                return outcome(
                    Decision.DECLINED,
                    approval.note or "declined",
                    approved_by=approval.by,
                )
            approved_by = approval.by

        # 8. Execute, then read the object back.
        try:
            self._writer.run(command, timeout=60)
        except CommandError as err:
            return outcome(Decision.FAILED, str(err), approved_by=approved_by)
        self._executed_on.add(plan.deployment)
        verified, detail = self._verify(plan, expected)
        return outcome(Decision.EXECUTED, detail, approved_by=approved_by, verified=verified)

    def _dry_run(self, plan: Plan, command: list[str]) -> tuple[str, Any]:
        """Ask the API server to check the change without making it.

        Returns the diff, and the value the server says the changed field would
        have: verification compares the object read back afterwards with it.
        """
        assert self._writer is not None  # checked by the policy step
        try:
            answer = self._writer.run([*command, "--dry-run=server"], timeout=60)
        except CommandError as err:
            raise Rejected(
                Step.DRY_RUN, f"the API server refused the change in a dry run: {err}"
            ) from err
        change = plan.change
        if isinstance(change, DeletePod):
            diff = f"- pod/{change.pod}\n  (deployment/{plan.deployment} creates a replacement)"
            return diff, None
        after = _at(json.loads(answer), change.path)
        if after == change.before:
            raise Rejected(Step.DRY_RUN, "the change would leave the Deployment as it is")
        return _diff(change.before, after, change.path), after

    def _verify(self, plan: Plan, expected: object) -> tuple[bool, str]:
        """Read the object back and check the change is there."""
        try:
            return self._read_back(plan, expected)
        except (CommandError, ToolError) as err:
            return False, f"the object could not be read back: {err}"

    def _read_back(self, plan: Plan, expected: object) -> tuple[bool, str]:
        change = plan.change
        if isinstance(change, DeletePod):
            pod = self._view.pod(change.pod)
            if pod is None or pod.metadata.deletion_timestamp:
                return True, (
                    f"pod/{change.pod} is being deleted; deployment/{plan.deployment} is "
                    "replacing it"
                )
            return False, f"pod/{change.pod} is still there and not being deleted"
        deployment = self._reader.run(
            ["--namespace", TESTBED, "get", "deployment", change.deployment, "--output=json"],
            timeout=30,
        )
        if _at(json.loads(deployment), change.path) == expected:
            return True, (
                f"deployment/{change.deployment} now has the new {change.path.rsplit('/', 1)[-1]}"
            )
        return False, f"reading deployment/{change.deployment} back does not show the change"


# ---- The tools the agent sees ----------------------------------------------------------


class GatedTools:
    """The read-only tools plus the write tools, with every write routed to the gate."""

    def __init__(self, reads: Toolbox, gate: WriteGate) -> None:
        self._reads = reads
        self._gate = gate

    def definitions(self) -> list[dict[str, Any]]:
        return [*self._reads.definitions(), *self._gate.definitions()]

    def call(self, name: str, raw_input: dict[str, Any]) -> ToolOutcome:
        if self._gate.handles(name):
            return self._gate.submit(name, raw_input)
        return self._reads.call(name, raw_input)


def tool_definitions(mode: PolicyMode) -> list[dict[str, Any]]:
    """The tools offered in `mode`, without anything behind them (for replays and tests).

    Observe mode offers the read-only tools only, exactly as before the write
    tools existed. The other modes add the four write tools after them.
    """
    if mode is PolicyMode.OBSERVE:
        return read_only_definitions()
    return [*read_only_definitions(), *write_definitions()]


# ---- Helpers ---------------------------------------------------------------------------


def _command(plan: Plan) -> list[str]:
    """The kubectl arguments that make the change."""
    change = plan.change
    if isinstance(change, DeletePod):
        # --wait=false: return once the API server has accepted the deletion,
        # instead of blocking until the pod has gone.
        return ["--namespace", TESTBED, "delete", "pod", change.pod, "--wait=false"]
    assert isinstance(change, ReplaceField)
    return [
        "--namespace",
        TESTBED,
        "patch",
        "deployment",
        change.deployment,
        "--type=json",
        f"--patch={json.dumps(change.json_patch(), separators=(',', ':'))}",
        "--output=json",
    ]


def _at(document: Mapping[str, Any], pointer: str) -> Any:  # noqa: ANN401 - JSON is untyped
    """The value at a JSON pointer such as /spec/template/spec/containers/0."""
    value: Any = document
    for part in pointer.strip("/").split("/"):
        value = value[int(part)] if isinstance(value, list) else value.get(part)
        if value is None:
            return None
    return value


def _diff(before: object, after: object, path: str) -> str:
    """A unified diff of two values as YAML."""

    def lines(value: object) -> list[str]:
        if not isinstance(value, dict | list):
            # A bare scalar (a replica count): YAML would add a "..." line.
            return [json.dumps(value)]
        return yaml.safe_dump(value, sort_keys=True, default_flow_style=False).splitlines()

    return "\n".join(
        difflib.unified_diff(
            lines(before),
            lines(after),
            fromfile=f"{path} (now)",
            tofile=f"{path} (after the change, from a server dry run)",
            lineterm="",
        )
    )


def _record(
    name: str,
    raw_input: dict[str, Any],
    decision: Decision,
    reason: str,
    plan: Plan | None = None,
    **fields: Any,  # noqa: ANN401
) -> ActionRecord:
    planned: dict[str, Any] = {}
    if plan is not None:
        planned = {
            "target": plan.target,
            "summary": plan.summary,
            "risk": plan.risk,
            "risk_reason": plan.risk_reason,
        }
    return ActionRecord(
        action=name, input=raw_input, decision=decision, reason=reason, **planned, **fields
    )


def _tell_the_model(record: ActionRecord) -> str:  # noqa: PLR0911 - one return per decision
    """The tool result: what happened, and what it means for the investigation."""
    match record.decision:
        case Decision.REJECTED:
            return (
                f"Rejected by the guardrails at the {record.step} check: {record.reason}. "
                "Nothing was changed."
            )
        case Decision.FAILED:
            return f"Failed: {record.reason}. Nothing was changed by this call."
        case Decision.PROPOSED:
            return (
                f"Proposed, not executed: {record.summary}. Risk {record.risk}: "
                f"{record.risk_reason}. It is recorded for a human operator, who decides "
                "whether to apply it; nothing has changed yet. Diff from a server dry run:\n"
                f"{record.diff}"
            )
        case Decision.DECLINED:
            return (
                f"Declined by {record.approved_by}: {record.reason}. Nothing was changed: "
                f"{record.summary}."
            )
        case _:  # Decision.EXECUTED
            if record.verified:
                return (
                    f"Executed ({record.approved_by}): {record.summary}. Verified: "
                    f"{record.reason}. The change has been made; whether it fixes the incident "
                    f"is for the telemetry to show. Diff:\n{record.diff}"
                )
            return (
                f"Executed ({record.approved_by}), but not verified: {record.reason}. "
                f"Diff that was applied:\n{record.diff}"
            )
