"""The kubectl command lines each incident step turns into.

A fake runner records the calls, so these tests need no cluster. They pin down
two things: that a step does what its name says, and that every value from an
incident file travels as its own argument, never spliced into a shell string.
"""

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from oncall.chaos.incident import CleanupStep, InjectStep
from oncall.chaos.kubectl import CommandError
from oncall.chaos.steps import Executor

from .conftest import FakeKubectl

REPO = Path("/repo")
UID_A = "11111111-1111-4111-8111-111111111111"
UID_B = "22222222-2222-4222-8222-222222222222"
UID_C = "33333333-3333-4333-8333-333333333333"

_INJECT = TypeAdapter(InjectStep)
_CLEANUP = TypeAdapter(CleanupStep)


def inject_step(**fields: object) -> InjectStep:
    return _INJECT.validate_python(fields)


def cleanup_step(**fields: object) -> CleanupStep:
    return _CLEANUP.validate_python(fields)


def pod(name: str, uid: str, phase: str = "Running", deleting: bool = False) -> dict[str, object]:
    metadata: dict[str, object] = {"name": name, "uid": uid}
    if deleting:
        metadata["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    return {"metadata": metadata, "status": {"phase": phase}}


def pods_answer(*pods: dict[str, object]) -> dict[tuple[str, ...], str]:
    return {("-n", "testbed", "get", "pods"): json.dumps({"items": list(pods)})}


def test_set_env(fake_kubectl: FakeKubectl) -> None:
    step = inject_step(
        action="set_env", deployment="order-service", env={"INVENTORY_TIMEOUT": "1ms", "A": "b c"}
    )
    Executor(fake_kubectl, REPO).run(step)
    assert fake_kubectl.calls == [
        ["-n", "testbed", "set", "env", "deployment/order-service", "-c", "order-service"]
        + ["INVENTORY_TIMEOUT=1ms", "A=b c"]
    ]


def test_set_resources(fake_kubectl: FakeKubectl) -> None:
    step = inject_step(
        action="set_resources", deployment="inventory-service", limits={"memory": "20Mi"}
    )
    Executor(fake_kubectl, REPO).run(step)
    assert fake_kubectl.calls == [
        ["-n", "testbed", "set", "resources", "deployment/inventory-service"]
        + ["-c", "inventory-service", "--limits=memory=20Mi"]
    ]


def test_set_resources_with_requests_and_limits(fake_kubectl: FakeKubectl) -> None:
    step = inject_step(
        action="set_resources",
        deployment="inventory-service",
        requests={"memory": "16Mi"},
        limits={"memory": "20Mi", "cpu": "50m"},
    )
    Executor(fake_kubectl, REPO).run(step)
    assert fake_kubectl.calls == [
        ["-n", "testbed", "set", "resources", "deployment/inventory-service"]
        + ["-c", "inventory-service", "--requests=memory=16Mi", "--limits=memory=20Mi,cpu=50m"]
    ]


def test_scale(fake_kubectl: FakeKubectl) -> None:
    Executor(fake_kubectl, REPO).run(
        inject_step(action="scale", deployment="notification-worker", replicas=0)
    )
    assert fake_kubectl.calls == [
        ["-n", "testbed", "scale", "deployment/notification-worker", "--replicas=0"]
    ]


def test_postgres_sql_is_one_argument(fake_kubectl: FakeKubectl) -> None:
    sql = "DROP INDEX IF EXISTS orders_customer_created_idx; -- $(reboot) `id`"
    Executor(fake_kubectl, REPO).run(inject_step(action="postgres_sql", sql=sql))
    (call,) = fake_kubectl.calls
    assert call[:7] == ["-n", "testbed", "exec", "deploy/postgres", "-c", "postgres", "--"]
    assert call[7:] == [
        "psql",
        "-X",
        "-U",
        "shop",
        "-d",
        "shop",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
    ]


def test_redis_command_words_are_separate_arguments(fake_kubectl: FakeKubectl) -> None:
    step = inject_step(action="redis_command", args=["CONFIG", "SET", "maxmemory", "1mb"])
    Executor(fake_kubectl, REPO).run(step)
    assert fake_kubectl.calls == [
        ["-n", "testbed", "exec", "deploy/redis", "-c", "redis", "--"]
        + ["redis-cli", "-e", "CONFIG", "SET", "maxmemory", "1mb"]
    ]


# ---- netem ---------------------------------------------------------------------------

HELPER = ["-n", "chaos", "exec", "deploy/netem-helper", "--", "pod-netem"]


def netem_calls(kubectl: FakeKubectl) -> list[list[str]]:
    return [call[len(HELPER) :] for call in kubectl.calls if call[: len(HELPER)] == HELPER]


def test_netem_first_targets_the_first_running_pod_by_name() -> None:
    kubectl = FakeKubectl(
        pods_answer(
            pod("inventory-b", UID_B),
            pod("inventory-a", UID_A, deleting=True),  # on its way out
            pod("inventory-0", UID_C, phase="Pending"),  # not started yet
            pod("inventory-c", UID_C),
        )
    )
    step = inject_step(
        action="netem", deployment="inventory-service", pods="first", delay_ms=300, jitter_ms=50
    )
    Executor(kubectl, REPO).run(step)

    assert kubectl.calls[0] == ["-n", "testbed", "get", "pods", "-l", "app=inventory-service"] + [
        "-o",
        "json",
    ]
    assert netem_calls(kubectl) == [[UID_B, "apply", "delay", "300ms", "50ms"]]


def test_netem_all_targets_every_running_pod() -> None:
    kubectl = FakeKubectl(pods_answer(pod("b", UID_B), pod("a", UID_A)))
    step = inject_step(
        action="netem", deployment="inventory-service", pods="all", delay_ms=100, loss_percent=2.5
    )
    Executor(kubectl, REPO).run(step)
    spec = ["apply", "delay", "100ms", "loss", "2.5%"]
    assert netem_calls(kubectl) == [[UID_A, *spec], [UID_B, *spec]]


def test_netem_loss_only() -> None:
    kubectl = FakeKubectl(pods_answer(pod("a", UID_A)))
    step = inject_step(action="netem", deployment="order-service", pods="first", loss_percent=5)
    Executor(kubectl, REPO).run(step)
    assert netem_calls(kubectl) == [[UID_A, "apply", "loss", "5%"]]


def test_netem_without_a_running_pod_fails() -> None:
    kubectl = FakeKubectl(pods_answer(pod("a", UID_A, phase="Pending")))
    step = inject_step(action="netem", deployment="order-service", pods="first", delay_ms=100)
    with pytest.raises(CommandError, match="no running pod"):
        Executor(kubectl, REPO).run(step)


def test_netem_refuses_a_uid_that_is_not_a_uuid() -> None:
    kubectl = FakeKubectl(pods_answer(pod("a", ".* /proc")))
    step = inject_step(action="netem", deployment="order-service", pods="first", delay_ms=100)
    with pytest.raises(CommandError, match="unexpected UID"):
        Executor(kubectl, REPO).run(step)
    assert netem_calls(kubectl) == []


def test_clear_netem_clears_every_running_pod() -> None:
    kubectl = FakeKubectl(pods_answer(pod("b", UID_B), pod("a", UID_A)))
    Executor(kubectl, REPO).run(cleanup_step(action="clear_netem", deployment="inventory-service"))
    assert netem_calls(kubectl) == [[UID_A, "clear"], [UID_B, "clear"]]


# ---- restore_deployment ----------------------------------------------------------------

RENDERED = """\
apiVersion: v1
kind: Service
metadata: {name: order-service, namespace: testbed}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: api-gateway, namespace: testbed}
spec: {replicas: 2}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: order-service, namespace: testbed}
spec: {replicas: 2}
"""


def replicasets(**replicas_by_name: int) -> str:
    items = [
        {"metadata": {"name": n}, "spec": {"replicas": r}} for n, r in replicas_by_name.items()
    ]
    return json.dumps({"items": items})


def test_restore_replaces_with_the_repo_manifest_and_prunes_idle_replicasets() -> None:
    kubectl = FakeKubectl(
        {
            ("kustomize",): RENDERED,
            ("-n", "testbed", "get", "replicasets"): replicasets(new=2, old_bad=0, older=0),
        }
    )
    step = cleanup_step(action="restore_deployment", deployment="order-service")
    Executor(kubectl, REPO).run(step)

    assert kubectl.calls == [
        ["kustomize", "/repo/deploy/testbed/services"],
        ["replace", "--save-config", "-f", "-"],
        ["-n", "testbed", "rollout", "status", "deployment/order-service", "--timeout=300s"],
        ["-n", "testbed", "get", "replicasets", "-l", "app=order-service", "-o", "json"],
        ["-n", "testbed", "delete", "replicaset", "old_bad", "older"],
    ]
    # What is sent to `kubectl replace` is the order-service Deployment, not
    # the Service of the same name and not another Deployment.
    replaced = json.loads(kubectl.stdins[1] or "")
    assert replaced == {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "order-service", "namespace": "testbed"},
        "spec": {"replicas": 2},
    }


def test_restore_deletes_nothing_when_no_replicaset_is_idle() -> None:
    kubectl = FakeKubectl(
        {("kustomize",): RENDERED, ("-n", "testbed", "get", "replicasets"): replicasets(new=2)}
    )
    Executor(kubectl, REPO).run(cleanup_step(action="restore_deployment", deployment="api-gateway"))
    assert not [call for call in kubectl.calls if "delete" in call]


def test_restore_fails_when_the_manifest_has_no_such_deployment() -> None:
    kubectl = FakeKubectl({("kustomize",): RENDERED})
    step = cleanup_step(action="restore_deployment", deployment="inventory-service")
    with pytest.raises(CommandError, match="no Deployment named inventory-service"):
        Executor(kubectl, REPO).run(step)
    assert kubectl.calls == [["kustomize", "/repo/deploy/testbed/services"]]
