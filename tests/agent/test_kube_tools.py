"""The Kubernetes tools: the exact kubectl argument list each one builds, and what they refuse."""

import json
from collections.abc import Sequence
from typing import Any

import pytest

from oncall.agent.tools import read_only_toolbox
from oncall.agent.tools.base import Toolbox
from oncall.agent.tools.kube import Kind
from oncall.chaos.kubectl import CommandError
from tests.agent.conftest import FakeFetch, now
from tests.chaos.conftest import FakeKubectl

NAMESPACE = ["--namespace", "testbed"]


def toolbox(kubectl: FakeKubectl) -> Toolbox:
    return read_only_toolbox(FakeFetch(), kubectl, now)


def get(**overrides: object) -> dict[str, Any]:
    return {"kind": "pods", "name": "", "label_selector": "", "output": "wide", **overrides}


@pytest.mark.parametrize(
    ("tool", "params", "argv"),
    [
        ("kube_get", get(), ["get", "pods", "--output", "wide"]),
        (
            "kube_get",
            get(kind="deployments", name="order-service", output="yaml"),
            ["get", "deployments", "order-service", "--output", "yaml"],
        ),
        (
            "kube_get",
            get(label_selector="app=redis,tier!=db"),
            ["get", "pods", "--selector=app=redis,tier!=db", "--output", "wide"],
        ),
        (
            "kube_describe",
            {"kind": "pods", "name": "redis-7d9d88cc-lnhpm"},
            ["describe", "pods", "redis-7d9d88cc-lnhpm"],
        ),
        (
            "kube_events",
            {"object_name": "", "warnings_only": False},
            ["get", "events", "--sort-by=.lastTimestamp"],
        ),
        (
            "kube_events",
            {"object_name": "redis-7d9d88cc-lnhpm", "warnings_only": True},
            [
                "get",
                "events",
                "--sort-by=.lastTimestamp",
                "--field-selector=involvedObject.name=redis-7d9d88cc-lnhpm,type=Warning",
            ],
        ),
    ],
)
def test_the_argument_list_each_tool_builds(
    kubectl: FakeKubectl, tool: str, params: dict[str, Any], argv: list[str]
) -> None:
    outcome = toolbox(kubectl).call(tool, params)
    assert not outcome.is_error
    assert kubectl.calls == [[*NAMESPACE, *argv]]


def test_every_call_is_a_read_in_the_testbed_namespace(kubectl: FakeKubectl) -> None:
    """Whatever the parameters, kubectl is given `--namespace testbed` and a read verb."""
    kubectl.answers = {(*NAMESPACE, "get", "replicasets"): _replicasets(1)}
    box = toolbox(kubectl)
    for kind in Kind:
        box.call("kube_get", get(kind=kind.value))
        box.call("kube_describe", {"kind": kind.value, "name": "x"})
    box.call("kube_events", {"object_name": "", "warnings_only": True})
    box.call("deploy_history", {"deployment": "order-service"})
    assert len(kubectl.calls) == 2 * len(Kind) + 1 + 2
    for call in kubectl.calls:
        assert call[:2] == NAMESPACE
        assert call[2] in ("get", "describe") or call[2:4] == ["rollout", "history"]
        assert call.count("--namespace") == 1
    assert all(stdin is None for stdin in kubectl.stdins)


@pytest.mark.parametrize("kind", ["secrets", "secret", "nodes", "serviceaccounts", "pods/exec"])
def test_kinds_outside_the_list_are_refused(kubectl: FakeKubectl, kind: str) -> None:
    outcome = toolbox(kubectl).call("kube_get", get(kind=kind))
    assert outcome.is_error
    assert kubectl.calls == []


@pytest.mark.parametrize(
    "name",
    [
        "--all-namespaces",
        "-A",
        "pod --namespace=chaos",
        "x; rm -rf /",
        "Redis",
        "pods/redis",
        "$(id)",
        "a\nb",
        "redis\n",
    ],
)
def test_a_name_that_is_not_an_object_name_never_reaches_kubectl(
    kubectl: FakeKubectl, name: str
) -> None:
    box = toolbox(kubectl)
    outcomes = [
        box.call("kube_get", get(name=name)),
        box.call("kube_describe", {"kind": "pods", "name": name}),
        box.call("kube_events", {"object_name": name, "warnings_only": False}),
        box.call("deploy_history", {"deployment": name}),
    ]
    assert all(outcome.is_error for outcome in outcomes)
    assert kubectl.calls == []


@pytest.mark.parametrize(
    "selector",
    ["app=redis --all-namespaces", "--all-namespaces", "app in (a,b)", "app=redis;id", "=x"],
)
def test_a_selector_that_is_not_key_value_never_reaches_kubectl(
    kubectl: FakeKubectl, selector: str
) -> None:
    outcome = toolbox(kubectl).call("kube_get", get(label_selector=selector))
    assert outcome.is_error
    assert kubectl.calls == []


def test_describe_needs_a_name(kubectl: FakeKubectl) -> None:
    assert toolbox(kubectl).call("kube_describe", {"kind": "pods", "name": ""}).is_error
    assert kubectl.calls == []


def test_an_empty_listing_is_said_in_words(kubectl: FakeKubectl) -> None:
    assert toolbox(kubectl).call("kube_get", get()).text == "No pods found."


def test_a_failed_command_is_shown_to_the_model() -> None:
    class Forbidden:
        def run(
            self, args: Sequence[str], *, stdin: str | None = None, timeout: float = 120
        ) -> str:
            raise CommandError('kubectl get pods exited 1: pods "x" not found')

    outcome = read_only_toolbox(FakeFetch(), Forbidden(), now).call("kube_get", get(name="x"))
    assert outcome.is_error
    assert 'pods "x" not found' in outcome.text


def test_events_keep_the_newest_and_count_the_rest(kubectl: FakeKubectl) -> None:
    table = "\n".join(["LAST SEEN   TYPE   REASON", *[f"event {i}" for i in range(40)]])
    kubectl.answers = {(*NAMESPACE, "get", "events"): table}
    lines = (
        toolbox(kubectl)
        .call("kube_events", {"object_name": "", "warnings_only": False})
        .text.splitlines()
    )
    assert lines[0] == "LAST SEEN   TYPE   REASON"
    assert lines[1] == "event 10"
    assert lines[-2:] == ["event 39", "[10 older events not shown]"]


def test_no_events(kubectl: FakeKubectl) -> None:
    outcome = toolbox(kubectl).call("kube_events", {"object_name": "", "warnings_only": False})
    assert outcome.text == "No events found."


def _replicasets(count: int) -> str:
    items = [
        {
            "metadata": {
                "name": f"order-service-rs{revision}",
                "creationTimestamp": f"2026-11-10T0{revision}:00:00Z",
                "annotations": {"deployment.kubernetes.io/revision": str(revision)},
            },
            "spec": {"replicas": 2 if revision == count else 0},
            "status": {"readyReplicas": 2} if revision == count else {},
        }
        # Listed newest first, to show that the tool sorts by revision.
        for revision in range(count, 0, -1)
    ]
    return json.dumps({"items": items})


def test_deploy_history_lists_every_revision_and_details_the_last_three(
    kubectl: FakeKubectl,
) -> None:
    kubectl.answers = {
        (*NAMESPACE, "get", "replicasets"): _replicasets(5),
        (*NAMESPACE, "rollout", "history"): "<pod template>",
    }
    outcome = toolbox(kubectl).call("deploy_history", {"deployment": "order-service"})
    assert kubectl.calls == [
        [*NAMESPACE, "get", "replicasets", "--selector=app=order-service", "--output", "json"],
        [*NAMESPACE, "rollout", "history", "deployment/order-service", "--revision=3"],
        [*NAMESPACE, "rollout", "history", "deployment/order-service", "--revision=4"],
        [*NAMESPACE, "rollout", "history", "deployment/order-service", "--revision=5"],
    ]
    lines = outcome.text.splitlines()
    assert lines[1] == "REVISION  REPLICASET  REPLICASET_CREATED(UTC)  DESIRED  READY"
    assert lines[2:7] == [
        "1  order-service-rs1  2026-11-10T01:00:00Z  0  0",
        "2  order-service-rs2  2026-11-10T02:00:00Z  0  0",
        "3  order-service-rs3  2026-11-10T03:00:00Z  0  0",
        "4  order-service-rs4  2026-11-10T04:00:00Z  0  0",
        "5  order-service-rs5  2026-11-10T05:00:00Z  2  2",
    ]
    assert outcome.text.count("<pod template>") == 3


def test_deploy_history_of_something_that_is_not_a_deployment(kubectl: FakeKubectl) -> None:
    kubectl.answers = {(*NAMESPACE, "get", "replicasets"): json.dumps({"items": []})}
    outcome = toolbox(kubectl).call("deploy_history", {"deployment": "nope"})
    assert outcome.is_error
    assert "no ReplicaSet with label app=nope" in outcome.text
