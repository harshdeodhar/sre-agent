"""The write identity grants what the policy allows, and nothing more.

The gate's allow-list (guardrails/policy.py) is the first fence; the
operator's Role and admission policy (deploy/rbac/agent-operator.yaml) are
the second. These tests read both and fail if they disagree. That the API
server enforces the manifest is checked against the live cluster; its
answers are in results/phase4-guardrails/.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from oncall.agent.guardrails.policy import (
    ALLOWED_TARGETS,
    MAX_REPLICAS,
    MIN_REPLICAS,
    RESTART,
    REVERT,
    ROLLBACK,
    SCALE,
)
from oncall.agent.identity import OPERATOR_ACCOUNT, write_operator_kubeconfig
from tests.agent.conftest import REPO
from tests.agent.test_identity import admin

MANIFEST = REPO / "deploy" / "rbac" / "agent-operator.yaml"
OPERATOR_USER = f"system:serviceaccount:testbed:{OPERATOR_ACCOUNT}"


def objects() -> dict[str, dict[str, Any]]:
    return {document["kind"]: document for document in yaml.safe_load_all(MANIFEST.read_text())}


def test_the_manifest_holds_the_account_its_role_and_the_admission_policy() -> None:
    assert set(objects()) == {
        "ServiceAccount",
        "Role",
        "RoleBinding",
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
    }
    account = objects()["ServiceAccount"]
    assert account["metadata"] == {"name": OPERATOR_ACCOUNT, "namespace": "testbed"}
    assert account["automountServiceAccountToken"] is False


def test_the_role_is_two_rules_one_per_kind_of_write() -> None:
    role = objects()["Role"]
    assert role["metadata"]["namespace"] == "testbed"
    deployments, pods = role["rules"]
    assert deployments["apiGroups"] == ["apps"]
    assert deployments["resources"] == ["deployments"]
    # `get` only because `kubectl patch` reads the object first; see the manifest.
    assert deployments["verbs"] == ["get", "patch"]
    assert pods["apiGroups"] == [""]
    assert pods["resources"] == ["pods"]
    assert pods["verbs"] == ["delete"]
    # No other reads: the gate reads as the observer. No Secrets, no subresources.
    assert "resourceNames" not in pods


def test_only_the_deployments_the_policy_allows_can_be_patched() -> None:
    patchable = set(objects()["Role"]["rules"][0]["resourceNames"])
    # Rollback, scale and revert are all patches of a Deployment.
    assert ALLOWED_TARGETS[ROLLBACK] == ALLOWED_TARGETS[SCALE] == ALLOWED_TARGETS[REVERT]
    assert patchable == set(ALLOWED_TARGETS[ROLLBACK])


def test_the_binding_is_for_the_operator_account_only() -> None:
    binding = objects()["RoleBinding"]
    assert binding["metadata"]["namespace"] == "testbed"
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": objects()["Role"]["metadata"]["name"],
    }
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": OPERATOR_ACCOUNT, "namespace": "testbed"}
    ]


def _policy() -> dict[str, Any]:
    return objects()["ValidatingAdmissionPolicy"]["spec"]


def test_the_admission_policy_applies_to_the_operator_alone() -> None:
    (condition,) = _policy()["matchConditions"]
    assert condition["expression"].strip() == f'request.userInfo.username == "{OPERATOR_USER}"'
    assert _policy()["failurePolicy"] == "Fail"
    binding = objects()["ValidatingAdmissionPolicyBinding"]["spec"]
    assert binding == {"policyName": "oncall-agent-operator", "validationActions": ["Deny"]}


def test_the_admission_policy_allows_deleting_the_pods_the_policy_allows() -> None:
    pods, _ = _policy()["validations"]
    # The list after `in`; `labels["app"]` has brackets too.
    (listed,) = re.findall(r"\bin\s*\[([^\]]*)\]", pods["expression"])
    apps = {name.strip().strip('"') for name in listed.split(",")}
    assert apps == set(ALLOWED_TARGETS[RESTART])


def test_the_admission_policy_bounds_replicas_as_the_policy_does() -> None:
    _, replicas = _policy()["validations"]
    expression = " ".join(replicas["expression"].split())
    assert f"object.spec.replicas >= {MIN_REPLICAS}" in expression
    assert f"object.spec.replicas <= {MAX_REPLICAS}" in expression


@pytest.mark.parametrize("kind", ["Role", "RoleBinding"])
def test_nothing_is_cluster_wide_except_the_admission_policy(kind: str) -> None:
    assert objects()[kind]["metadata"]["namespace"] == "testbed"


def test_the_operator_token_is_minted_for_the_operator_account(tmp_path: Path) -> None:
    kubectl = admin()
    write_operator_kubeconfig(kubectl, tmp_path / "operator.kubeconfig")
    assert kubectl.calls[1] == [
        "--namespace",
        "testbed",
        "create",
        "token",
        OPERATOR_ACCOUNT,
        "--duration=1h",
    ]
    config = yaml.safe_load((tmp_path / "operator.kubeconfig").read_text())
    assert config["users"][0]["name"] == OPERATOR_ACCOUNT
