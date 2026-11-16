"""The write gate: what is allowed, what is blocked, and what each policy mode does.

Every test runs the real gate and the real planners against a fake API server
(fake_cluster.py) that holds the objects as JSON and answers the gate's
kubectl commands. Nothing here needs a cluster.

For each write action there are tests for the allowed case and for each way
it is blocked (CLAUDE.md: "a new write action needs an allow-list entry, a
risk level, and tests for both the allowed and the blocked case"). A blocked
write must leave the cluster untouched: the tests check that the writer ran
no command at all, or only a dry run.
"""

import json
from typing import Any

import pytest

from oncall.agent.guardrails.actions import ACTIONS
from oncall.agent.guardrails.gate import (
    AUTO_APPROVAL,
    Approval,
    GatedTools,
    NobodyApproves,
    WriteGate,
    tool_definitions,
)
from oncall.agent.guardrails.policy import (
    ALLOWED_TARGETS,
    MAX_REPLICAS,
    WRITE_ACTIONS,
    Policy,
    PolicyMode,
    Risk,
)
from oncall.agent.guardrails.record import ActionRecord, Decision, Step
from oncall.agent.tools import read_only_definitions
from oncall.agent.tools.base import Toolbox, ToolOutcome
from oncall.chaos.taxonomy import Component

from .fake_cluster import FakeCluster, Mutator, make_testbed

REASON = "c4 shows INVENTORY_TIMEOUT went from 1s to 1ms in revision 2."


class Approver:
    """A scripted human: approves or declines, and can change the cluster while deciding."""

    def __init__(
        self, approve: bool, cluster: FakeCluster | None = None, meanwhile: Mutator | None = None
    ) -> None:
        self.approve = approve
        self.cluster = cluster
        self.meanwhile = meanwhile
        self.seen: list[ActionRecord] = []

    def review(self, proposal: ActionRecord) -> Approval:
        self.seen.append(proposal)
        if self.meanwhile and self.cluster:
            self.meanwhile(self.cluster)
        return Approval(self.approve, by="the test's approver", note="" if self.approve else "no")


def make_gate(
    cluster: FakeCluster,
    mode: PolicyMode = PolicyMode.AUTO,
    approver: Any = None,  # noqa: ANN401
    max_writes: int = 3,
) -> WriteGate:
    return WriteGate(
        Policy(mode=mode, max_write_actions=max_writes),
        cluster.reader,
        None if mode is PolicyMode.OBSERVE else cluster.writer,
        approver or NobodyApproves(),
        cluster.trace,
    )


def submit(gate: WriteGate, action: str, **params: Any) -> ToolOutcome:  # noqa: ANN401
    outcome = gate.submit(action, {**params, "reason": REASON})
    assert outcome.action is not None
    return outcome


def blocked(outcome: ToolOutcome, step: Step, *words: str) -> None:
    """The write was rejected at `step`, with all of `words` in the reason."""
    action = outcome.action
    assert action is not None
    assert (action.decision, action.step) == (Decision.REJECTED, step), action.reason
    for word in words:
        assert word in action.reason
    assert outcome.is_error
    assert "Nothing was changed" in outcome.text


def container(cluster: FakeCluster, deployment: str) -> dict[str, Any]:
    return cluster.deployments[deployment]["spec"]["template"]["spec"]["containers"][0]


def env(cluster: FakeCluster, deployment: str) -> dict[str, str]:
    return {e["name"]: e.get("value", "<ref>") for e in container(cluster, deployment)["env"]}


@pytest.fixture
def cluster() -> FakeCluster:
    return make_testbed()


# ---- rollback_deployment ---------------------------------------------------------------


def test_rollback_restores_the_previous_pod_template(cluster: FakeCluster) -> None:
    approver = Approver(True)
    outcome = submit(
        make_gate(cluster, PolicyMode.PROPOSE, approver), "rollback_deployment",
        deployment="order-service",
    )  # fmt: skip
    action = outcome.action
    assert action is not None
    assert (action.decision, action.verified, action.risk) == (
        Decision.EXECUTED,
        True,
        Risk.MEDIUM,
    )
    assert env(cluster, "order-service")["INVENTORY_TIMEOUT"] == "1s"
    # The template is the revision-1 ReplicaSet's, without its pod-template-hash label.
    assert cluster.deployments["order-service"]["spec"]["template"]["metadata"]["labels"] == {
        "app": "order-service"
    }
    assert "\n-      value: 1ms\n+      value: 1s\n" in (action.diff or "")
    assert not outcome.is_error


def test_rollback_of_a_store_is_blocked(cluster: FakeCluster) -> None:
    outcome = submit(make_gate(cluster), "rollback_deployment", deployment="postgres")
    blocked(outcome, Step.ALLOW_LIST, "postgres", "emptyDir")
    assert cluster.dry_runs() == cluster.writes() == []


def test_rollback_needs_an_earlier_revision(cluster: FakeCluster) -> None:
    outcome = submit(make_gate(cluster), "rollback_deployment", deployment="api-gateway")
    blocked(outcome, Step.ALLOW_LIST, "no revision before 1")


def test_rollback_to_the_template_already_running_is_blocked(cluster: FakeCluster) -> None:
    order = cluster.deployments["order-service"]
    order["spec"]["template"] = cluster.replicasets["order-service-hash1"]["spec"]["template"]
    del order["spec"]["template"]["metadata"]["labels"]["pod-template-hash"]
    outcome = submit(make_gate(cluster), "rollback_deployment", deployment="order-service")
    blocked(outcome, Step.ALLOW_LIST, "would change nothing")


@pytest.mark.parametrize(
    ("name", "words"),
    [
        ("checkout-service", ["no Deployment named checkout-service"]),
        ("--all", ["not a valid object name"]),
        ("kube-system/coredns", ["not a valid object name"]),
        ("order-service\n", ["not a valid object name"]),
    ],
)
def test_rollback_of_something_outside_testbed_is_blocked(
    cluster: FakeCluster, name: str, words: list[str]
) -> None:
    outcome = submit(make_gate(cluster), "rollback_deployment", deployment=name)
    blocked(outcome, Step.SCOPE, *words)
    assert cluster.dry_runs() == []


# ---- scale_deployment ------------------------------------------------------------------


def test_scaling_up_is_low_risk_and_auto_mode_executes_it(cluster: FakeCluster) -> None:
    outcome = submit(
        make_gate(cluster), "scale_deployment", deployment="notification-worker", replicas=2
    )
    action = outcome.action
    assert action is not None
    assert (action.decision, action.risk, action.approved_by, action.verified) == (
        Decision.EXECUTED,
        Risk.LOW,
        AUTO_APPROVAL,
        True,
    )
    assert cluster.deployments["notification-worker"]["spec"]["replicas"] == 2


def test_scaling_down_is_high_risk_and_auto_mode_only_proposes_it(cluster: FakeCluster) -> None:
    outcome = submit(make_gate(cluster), "scale_deployment", deployment="order-service", replicas=1)
    action = outcome.action
    assert action is not None
    assert (action.decision, action.risk) == (Decision.PROPOSED, Risk.HIGH)
    assert cluster.deployments["order-service"]["spec"]["replicas"] == 2
    assert cluster.writes() == []
    assert len(cluster.dry_runs()) == 1


@pytest.mark.parametrize("replicas", [0, -1, MAX_REPLICAS + 1, 50])
def test_scaling_outside_the_allowed_range_is_blocked(cluster: FakeCluster, replicas: int) -> None:
    outcome = submit(
        make_gate(cluster), "scale_deployment", deployment="order-service", replicas=replicas
    )
    blocked(outcome, Step.ALLOW_LIST, "between 1 and 3")
    assert cluster.dry_runs() == []


@pytest.mark.parametrize("store", ["postgres", "redis", "nats"])
def test_scaling_a_store_is_blocked(cluster: FakeCluster, store: str) -> None:
    outcome = submit(make_gate(cluster), "scale_deployment", deployment=store, replicas=2)
    blocked(outcome, Step.ALLOW_LIST, store)


def test_scaling_to_the_current_count_is_blocked(cluster: FakeCluster) -> None:
    outcome = submit(make_gate(cluster), "scale_deployment", deployment="order-service", replicas=2)
    blocked(outcome, Step.ALLOW_LIST, "already has 2 replicas")


# ---- restart_pod -----------------------------------------------------------------------


def test_restarting_one_of_two_replicas_is_low_risk_and_executed(cluster: FakeCluster) -> None:
    outcome = submit(make_gate(cluster), "restart_pod", pod="order-service-hash2-pod0")
    action = outcome.action
    assert action is not None
    assert (action.decision, action.risk, action.verified) == (Decision.EXECUTED, Risk.LOW, True)
    assert "order-service-hash2-pod0" not in cluster.pods
    assert "order-service-hash2-pod1" in cluster.pods
    assert cluster.writes() == [
        ["--namespace", "testbed", "delete", "pod", "order-service-hash2-pod0", "--wait=false"]
    ]


def test_restarting_the_only_replica_is_high_risk_and_only_proposed(cluster: FakeCluster) -> None:
    outcome = submit(make_gate(cluster), "restart_pod", pod="redis-hash1-pod0")
    action = outcome.action
    assert action is not None
    assert (action.decision, action.risk) == (Decision.PROPOSED, Risk.HIGH)
    assert "only available replica" in (action.risk_reason or "")
    assert "redis-hash1-pod0" in cluster.pods


def test_restarting_a_replica_when_the_other_is_not_available_is_high_risk(
    cluster: FakeCluster,
) -> None:
    cluster.deployments["order-service"]["status"]["availableReplicas"] = 1
    outcome = submit(make_gate(cluster), "restart_pod", pod="order-service-hash2-pod0")
    assert outcome.action is not None
    assert (outcome.action.decision, outcome.action.risk) == (Decision.PROPOSED, Risk.HIGH)


@pytest.mark.parametrize("store", ["postgres", "nats"])
def test_restarting_a_store_that_holds_data_is_blocked(cluster: FakeCluster, store: str) -> None:
    outcome = submit(make_gate(cluster), "restart_pod", pod=f"{store}-hash1-pod0")
    blocked(outcome, Step.ALLOW_LIST, store, "emptyDir")
    assert f"{store}-hash1-pod0" in cluster.pods
    assert cluster.dry_runs() == []


def test_restarting_a_pod_that_is_not_there_is_blocked(cluster: FakeCluster) -> None:
    outcome = submit(make_gate(cluster), "restart_pod", pod="order-service-hash2-pod9")
    blocked(outcome, Step.SCOPE, "no pod named")


def test_restarting_a_pod_without_a_deployment_is_blocked(cluster: FakeCluster) -> None:
    cluster.add_pod("debug-shell", app="debug", owner=None)
    outcome = submit(make_gate(cluster), "restart_pod", pod="debug-shell")
    blocked(outcome, Step.SCOPE, "not managed by a Deployment")
    assert "debug-shell" in cluster.pods


def test_restarting_a_pod_that_is_already_going_is_blocked(cluster: FakeCluster) -> None:
    cluster.add_pod("api-gateway-hash1-pod9", app="api-gateway", owner="api-gateway-hash1",
                    deleting=True)  # fmt: skip
    outcome = submit(make_gate(cluster), "restart_pod", pod="api-gateway-hash1-pod9")
    blocked(outcome, Step.ALLOW_LIST, "already being deleted")


# ---- revert_config ---------------------------------------------------------------------


def test_revert_sets_one_variable_back_and_leaves_the_rest(cluster: FakeCluster) -> None:
    before = env(cluster, "order-service")
    outcome = submit(
        make_gate(cluster, PolicyMode.PROPOSE, Approver(True)), "revert_config",
        deployment="order-service", settings=["INVENTORY_TIMEOUT"],
    )  # fmt: skip
    action = outcome.action
    assert action is not None
    assert (action.decision, action.verified, action.risk) == (
        Decision.EXECUTED,
        True,
        Risk.MEDIUM,
    )
    assert env(cluster, "order-service") == {**before, "INVENTORY_TIMEOUT": "1s"}
    # Only the container is replaced, pinned to the generation that was checked.
    (command,) = cluster.writes()
    patch = json.loads(command[6].removeprefix("--patch="))
    assert patch[0] == {"op": "test", "path": "/metadata/generation", "value": 3}
    assert patch[1]["path"] == "/spec/template/spec/containers/0"
    diff_lines = [line for line in (action.diff or "").splitlines() if line[:1] in "+-"]
    assert diff_lines[2:] == ["-  value: 1ms", "+  value: 1s"]


def test_revert_sets_memory_back(cluster: FakeCluster) -> None:
    outcome = submit(
        make_gate(cluster, PolicyMode.PROPOSE, Approver(True)), "revert_config",
        deployment="inventory-service", settings=["limits.memory", "requests.memory"],
    )  # fmt: skip
    assert outcome.action is not None
    assert outcome.action.decision == Decision.EXECUTED
    assert container(cluster, "inventory-service")["resources"] == {
        "requests": {"cpu": "25m", "memory": "32Mi"},
        "limits": {"memory": "96Mi"},
    }


def test_revert_removes_a_variable_the_last_rollout_added(cluster: FakeCluster) -> None:
    order = cluster.deployments["order-service"]["spec"]["template"]["spec"]["containers"][0]
    order["env"].append({"name": "RETRY_BUDGET", "value": "0"})
    outcome = submit(
        make_gate(cluster, PolicyMode.PROPOSE, Approver(True)), "revert_config",
        deployment="order-service", settings=["RETRY_BUDGET"],
    )  # fmt: skip
    assert outcome.action is not None
    assert outcome.action.decision == Decision.EXECUTED
    assert "RETRY_BUDGET" not in env(cluster, "order-service")


def test_revert_of_a_setting_the_last_rollout_did_not_change_is_blocked(
    cluster: FakeCluster,
) -> None:
    outcome = submit(
        make_gate(cluster), "revert_config", deployment="order-service", settings=["DB_MAX_CONNS"]
    )
    blocked(outcome, Step.ALLOW_LIST, "DB_MAX_CONNS", "did not change")


def test_revert_never_touches_a_secret_reference(cluster: FakeCluster) -> None:
    outcome = submit(
        make_gate(cluster), "revert_config", deployment="order-service", settings=["DB_PASSWORD"]
    )
    blocked(outcome, Step.ALLOW_LIST, "Secret or ConfigMap reference")


@pytest.mark.parametrize(
    ("settings", "words"),
    [
        ([], ["at least one setting"]),
        (["image"], []),  # a valid variable name that is not set: "did not change"
        (["spec.containers[0].image"], ["not a setting revert_config can change"]),
        (["limits.storage"], ["not a setting revert_config can change"]),
        (["INVENTORY_TIMEOUT", "INVENTORY_TIMEOUT"], ["more than once"]),
        ([f"VAR_{n}" for n in range(6)], ["at most 5"]),
    ],
)
def test_revert_of_something_that_is_not_a_setting_is_blocked(
    cluster: FakeCluster, settings: list[str], words: list[str]
) -> None:
    outcome = submit(
        make_gate(cluster), "revert_config", deployment="order-service", settings=settings
    )
    blocked(outcome, Step.ALLOW_LIST, *words)
    assert cluster.dry_runs() == []


def test_revert_of_a_store_is_blocked(cluster: FakeCluster) -> None:
    outcome = submit(make_gate(cluster), "revert_config", deployment="redis", settings=["X"])
    blocked(outcome, Step.ALLOW_LIST, "redis")


# ---- The steps of the gate -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {"deployment": "order-service"},  # no reason
        {"deployment": "order-service", "reason": "x", "replicas": 2},  # an extra field
        {"deployment": 7, "reason": "x"},
    ],
)
def test_parameters_of_the_wrong_shape_are_blocked_before_anything_is_read(
    cluster: FakeCluster, raw: dict[str, Any]
) -> None:
    outcome = make_gate(cluster).submit("rollback_deployment", raw)
    blocked(outcome, Step.SCHEMA)
    assert cluster.log.events == []


def test_observe_mode_rejects_every_write_and_has_no_writer(cluster: FakeCluster) -> None:
    gate = make_gate(cluster, PolicyMode.OBSERVE)
    outcomes = [
        submit(gate, "scale_deployment", deployment="notification-worker", replicas=2),
        submit(gate, "restart_pod", pod="order-service-hash2-pod0"),
        submit(gate, "rollback_deployment", deployment="order-service"),
        submit(gate, "revert_config", deployment="order-service", settings=["INVENTORY_TIMEOUT"]),
    ]
    for outcome in outcomes:
        blocked(outcome, Step.POLICY, "observe mode")
    assert [kind for kind in cluster.log.kinds() if kind != "kubectl reader"] == []


def test_propose_mode_without_an_approver_only_records(cluster: FakeCluster) -> None:
    gate = make_gate(cluster, PolicyMode.PROPOSE)
    outcome = submit(gate, "scale_deployment", deployment="notification-worker", replicas=2)
    action = outcome.action
    assert action is not None
    # Low risk, but propose mode executes nothing without a human.
    assert (action.decision, action.risk, action.approved_by) == (
        Decision.PROPOSED,
        Risk.LOW,
        None,
    )
    assert cluster.writes() == []
    assert cluster.deployments["notification-worker"]["spec"]["replicas"] == 1
    assert not outcome.is_error
    assert "Proposed, not executed" in outcome.text


def test_a_declined_proposal_changes_nothing(cluster: FakeCluster) -> None:
    approver = Approver(False)
    gate = make_gate(cluster, PolicyMode.PROPOSE, approver)
    outcome = submit(gate, "rollback_deployment", deployment="order-service")
    assert outcome.action is not None
    assert outcome.action.decision == Decision.DECLINED
    assert outcome.is_error
    assert cluster.writes() == []
    # The approver saw the proposal with its diff and risk.
    (seen,) = approver.seen
    assert (seen.decision, seen.risk) == (Decision.PROPOSED, Risk.MEDIUM)
    assert seen.diff == outcome.action.diff


def test_auto_mode_asks_the_approver_for_anything_above_low_risk(cluster: FakeCluster) -> None:
    approver = Approver(True)
    gate = make_gate(cluster, PolicyMode.AUTO, approver)
    low = submit(gate, "scale_deployment", deployment="notification-worker", replicas=2)
    medium = submit(gate, "rollback_deployment", deployment="order-service")
    assert [a.action.decision for a in (low, medium) if a.action] == [Decision.EXECUTED] * 2
    assert [a.action.approved_by for a in (low, medium) if a.action] == [
        AUTO_APPROVAL,
        "the test's approver",
    ]
    assert [proposal.target for proposal in approver.seen] == ["deployment/order-service"]


def test_the_api_server_refusing_the_dry_run_blocks_the_write(cluster: FakeCluster) -> None:
    cluster.refuse_dry_run = "admission webhook denied the request"
    outcome = submit(make_gate(cluster), "scale_deployment", deployment="api-gateway", replicas=3)
    blocked(outcome, Step.DRY_RUN, "refused the change in a dry run", "admission")
    assert cluster.writes() == []


def test_the_diff_is_in_the_trace_before_the_change_is_made(cluster: FakeCluster) -> None:
    submit(make_gate(cluster), "scale_deployment", deployment="notification-worker", replicas=2)
    kinds = cluster.log.kinds()
    dry_run = next(i for i, (k, args) in enumerate(cluster.log.events) if k == "kubectl writer")
    recorded = kinds.index("trace write_dry_run")
    executed = len(kinds) - 1 - kinds[::-1].index("kubectl writer")
    assert dry_run < recorded < executed
    record = cluster.trace.records[0]
    assert record["command"][2:4] == ["patch", "deployment"]
    assert "-  1" in record["diff"] or "-1" in record["diff"]


def test_a_change_made_while_waiting_for_approval_is_not_overwritten(
    cluster: FakeCluster,
) -> None:
    """The executed patch is pinned to the generation that was checked and dry-run."""
    approver = Approver(
        True, cluster, meanwhile=lambda c: c.change_spec_behind_the_agents_back("order-service")
    )
    outcome = submit(
        make_gate(cluster, PolicyMode.PROPOSE, approver), "rollback_deployment",
        deployment="order-service",
    )  # fmt: skip
    assert outcome.action is not None
    assert outcome.action.decision == Decision.FAILED
    assert "test operation failed" in outcome.action.reason
    assert env(cluster, "order-service")["INVENTORY_TIMEOUT"] == "1ms"
    assert outcome.is_error


def test_a_change_that_does_not_show_when_read_back_is_not_verified(cluster: FakeCluster) -> None:
    cluster.lose_changes = True
    outcome = submit(
        make_gate(cluster), "scale_deployment", deployment="notification-worker", replicas=2
    )
    assert outcome.action is not None
    assert (outcome.action.decision, outcome.action.verified) == (Decision.EXECUTED, False)
    assert outcome.is_error
    assert "not verified" in outcome.text


def test_an_unreachable_cluster_fails_the_write_without_changing_anything(
    cluster: FakeCluster,
) -> None:
    cluster.unreachable = True
    outcome = submit(make_gate(cluster), "rollback_deployment", deployment="order-service")
    assert outcome.action is not None
    assert outcome.action.decision == Decision.FAILED
    assert "connection refused" in outcome.action.reason
    assert outcome.is_error


# ---- Budget ----------------------------------------------------------------------------


def test_the_run_has_a_budget_of_write_actions(cluster: FakeCluster) -> None:
    gate = make_gate(cluster, max_writes=2)
    first = submit(gate, "scale_deployment", deployment="notification-worker", replicas=2)
    second = submit(gate, "restart_pod", pod="redis-hash1-pod0")  # proposed: still counts
    third = submit(gate, "scale_deployment", deployment="api-gateway", replicas=3)
    assert first.action and second.action
    assert (first.action.decision, second.action.decision) == (
        Decision.EXECUTED,
        Decision.PROPOSED,
    )
    blocked(third, Step.BUDGET, "used its 2 write actions")
    assert cluster.deployments["api-gateway"]["spec"]["replicas"] == 2


def test_a_dry_run_the_server_refused_counts_against_the_budget(cluster: FakeCluster) -> None:
    gate = make_gate(cluster, max_writes=1)
    cluster.refuse_dry_run = "denied"
    submit(gate, "scale_deployment", deployment="api-gateway", replicas=3)
    cluster.refuse_dry_run = None
    later = submit(gate, "scale_deployment", deployment="notification-worker", replicas=2)
    blocked(later, Step.BUDGET)


def test_no_second_change_to_a_deployment_after_one_was_executed(cluster: FakeCluster) -> None:
    """Its effect has to be seen first. (In a real cluster a second rollback would
    also flip the Deployment back to the broken revision.)"""
    gate = make_gate(cluster, PolicyMode.PROPOSE, Approver(True))
    submit(gate, "rollback_deployment", deployment="order-service")
    scale = submit(gate, "scale_deployment", deployment="order-service", replicas=3)
    blocked(scale, Step.BUDGET, "already been executed")
    pod = submit(gate, "restart_pod", pod=next(p for p in cluster.pods if "order" in p))
    blocked(pod, Step.BUDGET, "deployment/order-service")
    assert len(cluster.writes()) == 1


def test_the_same_proposal_is_not_made_twice(cluster: FakeCluster) -> None:
    gate = make_gate(cluster, PolicyMode.PROPOSE)
    submit(gate, "rollback_deployment", deployment="order-service")
    again = submit(gate, "rollback_deployment", deployment="order-service")
    blocked(again, Step.BUDGET, "already been proposed")
    assert len(cluster.dry_runs()) == 1


# ---- Who reads, who writes -------------------------------------------------------------


def test_the_gate_reads_as_the_observer_and_only_writes_as_the_operator(
    cluster: FakeCluster,
) -> None:
    gate = make_gate(cluster, PolicyMode.PROPOSE, Approver(True))
    submit(gate, "rollback_deployment", deployment="order-service")
    submit(gate, "restart_pod", pod="api-gateway-hash1-pod0")
    for kind, args in cluster.log.events:
        if kind == "kubectl reader":
            assert args[2] == "get", args
        elif kind == "kubectl writer":
            assert args[2] in ("patch", "delete"), args


def test_gated_tools_send_writes_to_the_gate_and_reads_to_the_toolbox(
    cluster: FakeCluster,
) -> None:
    gate = make_gate(cluster, PolicyMode.PROPOSE)
    tools = GatedTools(Toolbox([]), gate)
    assert [d["name"] for d in tools.definitions()] == [str(a) for a in WRITE_ACTIONS]
    outcome = tools.call("rollback_deployment", {"deployment": "order-service", "reason": "x"})
    assert outcome.action is not None
    assert tools.call("kube_get", {}).is_error  # not in this (empty) read toolbox


def test_observe_mode_offers_the_read_only_tools_only() -> None:
    assert tool_definitions(PolicyMode.OBSERVE) == read_only_definitions()
    for mode in (PolicyMode.PROPOSE, PolicyMode.AUTO):
        names = [definition["name"] for definition in tool_definitions(mode)]
        assert names[-4:] == [str(action) for action in WRITE_ACTIONS]


def test_every_write_action_has_an_allow_list_entry_and_a_planner() -> None:
    assert set(ACTIONS) == {str(action) for action in WRITE_ACTIONS} == set(ALLOWED_TARGETS)
    for action in WRITE_ACTIONS:
        # The tool description tells the model what the allow-list says.
        description = ACTIONS[action].description
        for target in ALLOWED_TARGETS[action]:
            assert target in description
    # No action may touch the stores that hold data.
    for targets in ALLOWED_TARGETS.values():
        assert Component.POSTGRES not in targets
        assert Component.NATS not in targets
