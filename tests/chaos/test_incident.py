"""Incident parsing: what a file must contain, and what is rejected."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from oncall.chaos.incident import (
    Incident,
    IncidentFileError,
    Netem,
    RestoreDeployment,
    SetEnv,
    load_incident,
    load_library,
)
from oncall.chaos.taxonomy import Component, FaultCategory, RemediationAction, Scenario

Document = dict[str, Any]
Writer = Callable[..., Path]


def test_valid_file_parses_into_typed_objects(valid: Document, write_incident: Writer) -> None:
    incident = load_incident(write_incident(valid))

    assert incident.id == "example-incident"
    assert incident.scenarios == [Scenario.BAD_CONFIG_PUSH]
    assert incident.inject == [
        SetEnv(action="set_env", deployment="order-service", env={"INVENTORY_TIMEOUT": "1ms"})
    ]
    assert incident.cleanup == [
        RestoreDeployment(action="restore_deployment", deployment="order-service")
    ]
    cause = incident.ground_truth.causes[0]
    assert (cause.component, cause.category) == (
        Component.ORDER_SERVICE,
        FaultCategory.BAD_CONFIG_VALUE,
    )
    assert incident.acceptable_remediations[0].action is RemediationAction.ROLLBACK_DEPLOYMENT


def test_incident_is_immutable(valid: Document) -> None:
    incident = Incident.model_validate(valid)
    with pytest.raises(ValidationError):
        incident.title = "changed"  # type: ignore[misc]


def _set(document: Document, path: list[str | int], value: object) -> Document:
    """Set document[path[0]][path[1]]... = value."""
    target: Any = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return document


# (what is wrong, where in the document, the bad value, text expected in the error)
REJECTED: list[tuple[str, list[str | int], object, str]] = [
    ("unknown top-level key", ["severity"], "high", "severity"),
    ("unknown key in a step", ["inject", 0, "namespace"], "kube-system", "namespace"),
    ("unknown action", ["inject", 0, "action"], "run_shell", "action"),
    ("deployment outside the services", ["inject", 0, "deployment"], "postgres", "deployment"),
    ("shell string where a step is expected", ["inject", 0], "kubectl delete ns testbed", "inject"),
    ("unknown component", ["ground_truth", "causes", 0, "component"], "kafka", "component"),
    ("unknown category", ["ground_truth", "causes", 0, "category"], "gremlins", "category"),
    ("unknown scenario", ["scenarios", 0], "meteor_strike", "scenarios"),
    ("unknown remediation", ["acceptable_remediations", 0, "action"], "delete_ns", "action"),
    ("unknown evidence source", ["detection", "evidence", 0, "source"], "tempo", "source"),
    ("id that is not a slug", ["id"], "Example Incident", "lower-case"),
    ("no causes", ["ground_truth", "causes"], [], "causes"),
    ("no inject steps", ["inject"], [], "inject"),
    ("no cleanup steps", ["cleanup"], [], "cleanup"),
    ("no alerts", ["detection", "alerts"], [], "alerts"),
    ("no evidence", ["detection", "evidence"], [], "evidence"),
    ("deadline too long", ["detection", "deadline_seconds"], 3600, "deadline_seconds"),
    (
        "env name that names the fault",
        ["inject", 0, "env"],
        {"FAULT_MODE": "on"},
        "names the fault",
    ),
    ("env name in lower case", ["inject", 0, "env"], {"timeout": "1s"}, "UPPER_SNAKE_CASE"),
    ("empty env", ["inject", 0, "env"], {}, "env"),
    (
        "set_resources that sets nothing",
        ["inject", 0],
        {"action": "set_resources", "deployment": "order-service"},
        "needs requests or limits",
    ),
    (
        "remediation without a target",
        ["acceptable_remediations", 0],
        {"action": "restart_pod", "description": "Restart it."},
        "needs a target",
    ),
]


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [pytest.param(path, value, expected, id=name) for name, path, value, expected in REJECTED],
)
def test_invalid_documents_are_rejected(
    valid: Document, path: list[str | int], value: object, expected: str
) -> None:
    with pytest.raises(ValidationError, match=expected):
        Incident.model_validate(_set(valid, path, value))


@pytest.mark.parametrize(
    "name",
    ["FAULT_MODE", "CHAOS_DELAY", "INJECTED_LATENCY", "INCIDENT_ID", "CACHE_BROKEN", "BUGGY_PATH"],
)
def test_tell_tale_env_names_are_rejected(valid: Document, name: str) -> None:
    """Nothing in the cluster may name the fault (CLAUDE.md, rule 2)."""
    valid["inject"][0]["env"] = {name: "1"}
    with pytest.raises(ValidationError, match="names the fault"):
        Incident.model_validate(valid)


@pytest.mark.parametrize("name", ["DEFAULT_TIMEOUT", "DEBUG", "SEND_DELAY", "DB_MAX_CONNS"])
def test_ordinary_env_names_are_accepted(valid: Document, name: str) -> None:
    """The tell-tale check matches whole words, not letters inside other words."""
    valid["inject"][0]["env"] = {name: "1"}
    Incident.model_validate(valid)


def test_manual_remediation_needs_no_target(valid: Document) -> None:
    valid["acceptable_remediations"] = [{"action": "manual", "description": "Recreate the index."}]
    Incident.model_validate(valid)


# ---- Every injection must have a matching cleanup ---------------------------------

CLEANUP_MISSING: list[tuple[str, Document, Document, str]] = [
    (
        "set_env restored on another deployment",
        {"action": "set_env", "deployment": "api-gateway", "env": {"UPSTREAM_TIMEOUT": "2"}},
        {"action": "restore_deployment", "deployment": "order-service"},
        "api-gateway is changed but has no restore_deployment",
    ),
    (
        "set_resources without a restore",
        {"action": "set_resources", "deployment": "api-gateway", "limits": {"memory": "20Mi"}},
        {"action": "clear_netem", "deployment": "api-gateway"},
        "no restore_deployment",
    ),
    (
        "scale without a restore",
        {"action": "scale", "deployment": "notification-worker", "replicas": 0},
        {"action": "restore_deployment", "deployment": "order-service"},
        "no restore_deployment",
    ),
    (
        "netem without clear_netem",
        {"action": "netem", "deployment": "inventory-service", "pods": "first", "delay_ms": 300},
        {"action": "restore_deployment", "deployment": "inventory-service"},
        "no clear_netem",
    ),
    (
        "SQL without a SQL cleanup",
        {"action": "postgres_sql", "sql": "DROP INDEX x"},
        {"action": "restore_deployment", "deployment": "order-service"},
        "cleanup has no postgres_sql step",
    ),
    (
        "Redis command without a Redis cleanup",
        {"action": "redis_command", "args": ["CONFIG", "SET", "maxmemory", "1mb"]},
        {"action": "postgres_sql", "sql": "SELECT 1"},
        "cleanup has no redis_command step",
    ),
]


@pytest.mark.parametrize(
    ("inject", "cleanup", "expected"),
    [pytest.param(i, c, expected, id=name) for name, i, c, expected in CLEANUP_MISSING],
)
def test_injection_without_matching_cleanup_is_rejected(
    valid: Document, inject: Document, cleanup: Document, expected: str
) -> None:
    valid["inject"] = [inject]
    valid["cleanup"] = [cleanup]
    with pytest.raises(ValidationError, match=expected):
        Incident.model_validate(valid)


# ---- Multi-cause labelling ---------------------------------------------------------

SECOND_CAUSE = {"component": "postgres", "category": "missing_index", "explanation": "No index."}


def test_two_causes_require_the_multi_cause_scenario(valid: Document) -> None:
    valid["ground_truth"]["causes"].append(SECOND_CAUSE)
    with pytest.raises(ValidationError, match="multi_cause"):
        Incident.model_validate(valid)

    valid["scenarios"].append("multi_cause")
    assert len(Incident.model_validate(valid).ground_truth.causes) == 2


def test_multi_cause_scenario_requires_two_causes(valid: Document) -> None:
    valid["scenarios"].append("multi_cause")
    with pytest.raises(ValidationError, match="multi_cause"):
        Incident.model_validate(valid)


def test_duplicate_causes_are_rejected(valid: Document) -> None:
    valid["scenarios"].append("multi_cause")
    valid["ground_truth"]["causes"].append(dict(valid["ground_truth"]["causes"][0]))
    with pytest.raises(ValidationError, match="same component and category"):
        Incident.model_validate(valid)


# ---- Step details ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param({}, "needs a delay or a loss", id="does nothing"),
        pytest.param({"delay_ms": 100, "jitter_ms": 200}, "jitter", id="jitter above delay"),
        pytest.param({"delay_ms": 60000}, "delay_ms", id="delay out of range"),
        pytest.param({"loss_percent": 150}, "loss_percent", id="loss above 100"),
    ],
)
def test_netem_parameters_are_checked(fields: Document, expected: str) -> None:
    with pytest.raises(ValidationError, match=expected):
        Netem(action="netem", deployment="inventory-service", pods="first", **fields)


def _with_limit(valid: Document, resource: str, quantity: str) -> Document:
    valid["inject"] = [
        {"action": "set_resources", "deployment": "order-service", "limits": {resource: quantity}}
    ]
    return valid


@pytest.mark.parametrize("quantity", ["-5Mi", "0Mi", "20MB", "1.5Gi", "lots", "20Mi; rm -rf /"])
def test_malformed_resource_quantities_are_rejected(valid: Document, quantity: str) -> None:
    with pytest.raises(ValidationError, match="not a quantity"):
        Incident.model_validate(_with_limit(valid, "memory", quantity))


@pytest.mark.parametrize(
    ("resource", "quantity"), [("memory", "20Mi"), ("cpu", "50m"), ("cpu", "1")]
)
def test_plain_resource_quantities_are_accepted(
    valid: Document, resource: str, quantity: str
) -> None:
    Incident.model_validate(_with_limit(valid, resource, quantity))


def test_only_cpu_and_memory_limits_can_be_set(valid: Document) -> None:
    with pytest.raises(ValidationError, match="limits"):
        Incident.model_validate(_with_limit(valid, "ephemeral-storage", "1Gi"))


def test_requests_are_checked_like_limits(valid: Document) -> None:
    valid["inject"] = [
        {"action": "set_resources", "deployment": "order-service", "requests": {"memory": "lots"}}
    ]
    with pytest.raises(ValidationError, match="not a quantity"):
        Incident.model_validate(valid)


# ---- Files -------------------------------------------------------------------------


def test_file_name_must_match_id(valid: Document, write_incident: Writer) -> None:
    with pytest.raises(IncidentFileError, match="does not match the file name"):
        load_incident(write_incident(valid, name="something-else"))


def test_errors_name_the_file(valid: Document, write_incident: Writer) -> None:
    del valid["cleanup"]
    path = write_incident(valid)
    with pytest.raises(IncidentFileError, match=str(path)):
        load_incident(path)


def test_file_that_is_not_yaml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("id: [unclosed")
    with pytest.raises(IncidentFileError, match="broken.yaml"):
        load_incident(path)


def test_file_that_is_not_a_mapping_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- just\n- a list\n")
    with pytest.raises(IncidentFileError, match="list.yaml"):
        load_incident(path)


def test_library_loads_in_file_name_order(valid: Document, write_incident: Writer) -> None:
    for incident_id in ("b-second", "a-first"):
        valid["id"] = incident_id
        directory = write_incident(valid).parent
    assert [i.id for i in load_library(directory)] == ["a-first", "b-second"]
