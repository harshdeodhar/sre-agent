"""What is recorded about every write the agent attempts, whatever happened to it.

One `ActionRecord` per write tool call. It goes into the trace with the tool
call, into the run's result, and into report.json, so a rejected or declined
write is counted exactly like an executed one: nothing is dropped.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from oncall.agent.guardrails.policy import Risk


class Step(StrEnum):
    """The gate's checks, in the order they run. A write is rejected at the first that fails."""

    SCHEMA = "schema"  # the parameters have the right types
    SCOPE = "scope"  # the target is a real object in testbed, of the right kind
    ALLOW_LIST = "allow_list"  # this action may touch this target, with these values
    BUDGET = "budget"  # the run has writes left, and has not acted on this target already
    POLICY = "policy"  # the policy mode allows writes at all
    DRY_RUN = "dry_run"  # the API server accepts the change, and it changes something


class Decision(StrEnum):
    REJECTED = "rejected"  # a check failed; nothing was dry-run or changed
    PROPOSED = "proposed"  # dry-run and recorded for a human; nothing changed
    DECLINED = "declined"  # a human said no; nothing changed
    EXECUTED = "executed"  # the change was made (see `verified`)
    FAILED = "failed"  # the cluster could not be read, or the change itself failed


class ActionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    input: dict[str, Any]
    decision: Decision
    # Why: the failed check, the approver's note, or the error.
    reason: str
    # The step that rejected it, for a rejection.
    step: Step | None = None
    # Known once the action has been planned (after the allow-list).
    target: str | None = None
    summary: str | None = None
    risk: Risk | None = None
    risk_reason: str | None = None
    # Known once it has been dry-run: the kubectl arguments that make (or
    # would make) the change, and what the server said it would change.
    command: list[str] | None = None
    diff: str | None = None
    # Who allowed the execution: "auto mode, low risk" or the approver.
    approved_by: str | None = None
    # For an executed change: whether re-reading the object showed it.
    verified: bool | None = None
