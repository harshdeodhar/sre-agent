"""The agent's cluster identity and where its credentials come from.

No cluster here: these tests check the kubectl calls made and the file
written. That the ServiceAccount really cannot write is a property of the
live API server; `kubectl auth can-i` output is in results/phase3-agent.
"""

import json
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from oncall.agent.identity import SERVICE_ACCOUNT, write_observer_kubeconfig
from oncall.agent.llm import API_KEY_VARIABLE, ConfigurationError, api_key_from_environment
from oncall.agent.tools.kube import Kind
from tests.agent.conftest import REPO
from tests.chaos.conftest import FakeKubectl

TOKEN = "eyJhbGciOi.not-a-real-token.c2ln"
ADMIN_VIEW = {
    "clusters": [
        {
            "name": "k3d-sre-testbed",
            "cluster": {
                "server": "https://0.0.0.0:6550",
                "certificate-authority-data": "Q0EgREFUQQ==",
            },
        }
    ],
    "users": [{"name": "admin@k3d-sre-testbed", "user": {"client-key-data": "QURNSU4gS0VZ"}}],
}


def admin() -> FakeKubectl:
    return FakeKubectl(
        {
            ("config", "view"): json.dumps(ADMIN_VIEW),
            ("--namespace", "testbed", "create", "token"): TOKEN + "\n",
        }
    )


def test_the_token_is_minted_for_the_observer_account_with_a_short_life(tmp_path: Path) -> None:
    kubectl = admin()
    token = write_observer_kubeconfig(kubectl, tmp_path / "observer.kubeconfig")
    assert token == TOKEN
    assert kubectl.calls == [
        ["config", "view", "--raw", "--minify", "--output=json"],
        ["--namespace", "testbed", "create", "token", "oncall-agent-observer", "--duration=1h"],
    ]


def test_the_kubeconfig_holds_the_token_and_nothing_of_the_administrators(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "observer.kubeconfig"
    write_observer_kubeconfig(admin(), path)
    text = path.read_text()
    config = yaml.safe_load(text)  # kubectl reads it as YAML
    assert config["clusters"][0]["cluster"] == ADMIN_VIEW["clusters"][0]["cluster"]
    assert config["users"] == [{"name": SERVICE_ACCOUNT, "user": {"token": TOKEN}}]
    assert config["contexts"][0]["context"]["namespace"] == "testbed"
    assert config["current-context"] == config["contexts"][0]["name"]
    assert "QURNSU4gS0VZ" not in text
    # The token is written to the file and returned, never printed.
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_the_kubeconfig_is_readable_by_its_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "observer.kubeconfig"
    write_observer_kubeconfig(admin(), path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_an_older_world_readable_kubeconfig_is_tightened(tmp_path: Path) -> None:
    path = tmp_path / "observer.kubeconfig"
    path.write_text("old")
    path.chmod(0o644)
    write_observer_kubeconfig(admin(), path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "old" not in path.read_text()


# ---- The Role the token is bound to ----------------------------------------------------

READ_VERBS = {"get", "list"}


def rbac() -> dict[str, dict[str, Any]]:
    documents = yaml.safe_load_all((REPO / "deploy" / "rbac" / "agent-observer.yaml").read_text())
    return {document["kind"]: document for document in documents}


def test_the_role_grants_reads_only() -> None:
    role = rbac()["Role"]
    assert role["metadata"]["namespace"] == "testbed"
    granted = set()
    for rule in role["rules"]:
        assert set(rule["verbs"]) <= READ_VERBS, rule
        assert "resourceNames" not in rule
        granted |= set(rule["resources"])
    assert "*" not in granted
    assert "secrets" not in granted
    # No subresources: not pod logs (those come from Loki), not exec.
    assert not any("/" in resource for resource in granted)


def test_the_role_is_namespaced_and_bound_to_the_observer_account_only() -> None:
    objects = rbac()
    # A Role and a RoleBinding, not their Cluster-wide counterparts.
    assert set(objects) == {"ServiceAccount", "Role", "RoleBinding"}
    binding = objects["RoleBinding"]
    assert binding["metadata"]["namespace"] == "testbed"
    assert binding["roleRef"]["kind"] == "Role"
    assert binding["roleRef"]["name"] == objects["Role"]["metadata"]["name"]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": SERVICE_ACCOUNT, "namespace": "testbed"}
    ]
    account = objects["ServiceAccount"]
    assert account["metadata"]["name"] == SERVICE_ACCOUNT
    assert account["automountServiceAccountToken"] is False


def test_every_kind_the_tools_can_read_is_granted_and_no_more_than_needed() -> None:
    """The tool list and the Role say the same thing."""
    granted = {resource for rule in rbac()["Role"]["rules"] for resource in rule["resources"]}
    assert {kind.value for kind in Kind} <= granted
    # Beyond the kinds: events (kube_events, describe) and endpoint slices (describe service).
    assert granted - {kind.value for kind in Kind} == {"events", "endpointslices"}


# ---- The API key -----------------------------------------------------------------------


def test_the_api_key_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_VARIABLE, "sk-ant-not-a-real-key")
    assert api_key_from_environment() == "sk-ant-not-a-real-key"


@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_api_key_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(API_KEY_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(API_KEY_VARIABLE, value)
    with pytest.raises(ConfigurationError, match=API_KEY_VARIABLE):
        api_key_from_environment()
