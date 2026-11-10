"""The incident file format: one YAML document per incident.

The models below are the single definition of the format. incidents/schema.json
is generated from them (`python -m oncall.chaos schema`) and a test fails if the
two drift apart.

Steps are typed. An incident cannot contain a shell command: each step names an
action from a closed list, and code turns its fields into an argument list (see
steps.py). That keeps every incident reviewable at a glance and makes "what can
the injector do to the cluster?" a question with a short, checkable answer.
"""

import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oncall.chaos.taxonomy import Component, FaultCategory, RemediationAction, Scenario

# The Deployments a step may modify. The data stores are not in the list:
# faults in them are injected through their own interfaces (SQL, redis-cli),
# and restoring their Deployment would wipe their emptyDir data.
ServiceDeployment = Literal[
    "api-gateway", "order-service", "inventory-service", "notification-worker"
]

# Rule 2 of the incident design rules: nothing in the cluster may name the
# fault. Steps cannot set labels or annotations at all; environment variable
# names are the one place a tell-tale could slip in, so they are checked.
#
# Matched against each underscore-separated word of the name, from the start
# of the word: FAULT_MODE and INJECTED_DELAY are caught, DEFAULT_TIMEOUT and
# DEBUG are not.
_TELL_TALE = re.compile(r"(^|_)(FAULT|CHAOS|INJECT|INCIDENT|BROKEN|BUG)")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_QUANTITY = re.compile(r"^[1-9][0-9]*(m|Mi|Gi)?$")
_INCIDENT_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class _Model(BaseModel):
    """Base for every model here: unknown keys are errors, not ignored typos."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---- Injection steps ----------------------------------------------------------


class SetEnv(_Model):
    """Change environment variables of a service. A real rollout: new ReplicaSet."""

    action: Literal["set_env"]
    deployment: ServiceDeployment
    env: dict[str, str] = Field(min_length=1)

    @field_validator("env")
    @classmethod
    def _names_are_ordinary(cls, env: dict[str, str]) -> dict[str, str]:
        for name in env:
            if not _ENV_NAME.match(name):
                raise ValueError(f"{name!r} is not an UPPER_SNAKE_CASE variable name")
            if _TELL_TALE.search(name):
                raise ValueError(f"{name!r} names the fault; see 'No tell-tale names'")
        return env


class SetResources(_Model):
    """Change a service container's resource requests and limits. Also a real rollout.

    Both can be given because Kubernetes rejects a limit below the request:
    lowering a limit under the current request means lowering the request too,
    exactly as someone "right-sizing" a workload would.
    """

    action: Literal["set_resources"]
    deployment: ServiceDeployment
    requests: dict[Literal["cpu", "memory"], str] = {}
    limits: dict[Literal["cpu", "memory"], str] = {}

    @field_validator("requests", "limits")
    @classmethod
    def _quantities_are_plain(cls, quantities: dict[str, str]) -> dict[str, str]:
        for resource, quantity in quantities.items():
            if not _QUANTITY.match(quantity):
                raise ValueError(f"{resource}: {quantity!r} is not a quantity like 250m or 24Mi")
        return quantities

    @model_validator(mode="after")
    def _changes_something(self) -> "SetResources":
        if not self.requests and not self.limits:
            raise ValueError("set_resources needs requests or limits")
        return self


class Scale(_Model):
    """Change a service's replica count. Leaves no entry in rollout history."""

    action: Literal["scale"]
    deployment: ServiceDeployment
    replicas: int = Field(ge=0, le=4)


class PostgresSQL(_Model):
    """Run one SQL statement in the shop database, as its owner."""

    action: Literal["postgres_sql"]
    sql: str = Field(min_length=1)


class RedisCommand(_Model):
    """Run one Redis command, given as its words: ["CONFIG", "SET", ...]."""

    action: Literal["redis_command"]
    args: list[str] = Field(min_length=1)


class Netem(_Model):
    """Delay or drop packets leaving a service's pods (Linux `tc netem`).

    Applied from outside the pod, in its network namespace, by the helper in
    the chaos namespace. Nothing in the pod's spec or status changes.
    """

    action: Literal["netem"]
    deployment: ServiceDeployment
    # "first" is the first Running pod in name order: one degraded replica
    # among healthy ones. "all" degrades every replica.
    pods: Literal["first", "all"]
    delay_ms: int = Field(default=0, ge=0, le=5000)
    jitter_ms: int = Field(default=0, ge=0, le=5000)
    loss_percent: float = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def _does_something(self) -> "Netem":
        if self.delay_ms == 0 and self.loss_percent == 0:
            raise ValueError("netem needs a delay or a loss")
        if self.jitter_ms > self.delay_ms:
            raise ValueError("jitter cannot exceed the delay")
        return self


# ---- Cleanup steps ------------------------------------------------------------


class RestoreDeployment(_Model):
    """Put a service's Deployment back to exactly what the repo's manifest says.

    Cleanup does not undo the injection step by step. It states the wanted end
    state, so it is also correct after an agent has rolled back, scaled or
    restarted things in between, and running it twice does no harm.
    """

    action: Literal["restore_deployment"]
    deployment: ServiceDeployment


class ClearNetem(_Model):
    """Remove netem from every pod of a service. A no-op where there is none."""

    action: Literal["clear_netem"]
    deployment: ServiceDeployment


InjectStep = Annotated[
    SetEnv | SetResources | Scale | PostgresSQL | RedisCommand | Netem,
    Field(discriminator="action"),
]
CleanupStep = Annotated[
    RestoreDeployment | ClearNetem | PostgresSQL | RedisCommand,
    Field(discriminator="action"),
]


# ---- Ground truth, detection, remediation --------------------------------------


class Cause(_Model):
    """One root cause: which component, and what kind of fault."""

    component: Component
    category: FaultCategory
    # For people reading the file. Never scored and never shown to the agent.
    explanation: str = Field(min_length=1)


class GroundTruth(_Model):
    causes: list[Cause] = Field(min_length=1)

    @field_validator("causes")
    @classmethod
    def _causes_are_distinct(cls, causes: list[Cause]) -> list[Cause]:
        pairs = [(c.component, c.category) for c in causes]
        if len(set(pairs)) != len(pairs):
            raise ValueError("two causes have the same component and category")
        return causes


class Evidence(_Model):
    """A telemetry query that returns a result while the fault is active.

    Written like an alert rule: the query filters with a comparison, so "any
    series returned" means "the signal is present". The same queries must
    return nothing again after cleanup; that is how cleanup is verified
    against telemetry and not just assumed.
    """

    description: str = Field(min_length=1)
    source: Literal["prometheus", "loki"]
    query: str = Field(min_length=1)


class Detection(_Model):
    # Alert rules (deploy/observability/config/alerts.yml) expected to fire.
    # The first is the page the agent is started with.
    alerts: list[str] = Field(min_length=1)
    # How long after injection all of them must be firing.
    deadline_seconds: int = Field(ge=30, le=900)
    evidence: list[Evidence] = Field(min_length=1)


class Remediation(_Model):
    action: RemediationAction
    # The component the action is applied to. Optional for manual fixes.
    target: Component | None = None
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def _tool_actions_have_a_target(self) -> "Remediation":
        if self.action is not RemediationAction.MANUAL and self.target is None:
            raise ValueError(f"{self.action.value} needs a target")
        return self


class Incident(_Model):
    id: str
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    scenarios: list[Scenario] = Field(min_length=1)
    inject: list[InjectStep] = Field(min_length=1)
    cleanup: list[CleanupStep] = Field(min_length=1)
    ground_truth: GroundTruth
    # Components whose behaviour degrades (the blast radius). The root-cause
    # component is in ground_truth and need not be listed here.
    affected_services: list[Component] = Field(min_length=1)
    detection: Detection
    acceptable_remediations: list[Remediation] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _id_is_a_slug(cls, value: str) -> str:
        if not _INCIDENT_ID.match(value):
            raise ValueError("id must be lower-case words joined by hyphens")
        return value

    @model_validator(mode="after")
    def _multi_cause_is_labelled(self) -> "Incident":
        is_multi = len(self.ground_truth.causes) > 1
        if is_multi != (Scenario.MULTI_CAUSE in self.scenarios):
            raise ValueError("scenario 'multi_cause' must be set exactly when there are 2+ causes")
        return self

    @model_validator(mode="after")
    def _every_injection_has_a_cleanup(self) -> "Incident":
        """Reject an incident that changes something its cleanup does not restore.

        For Deployments and netem the match is exact. For SQL and Redis the
        parser cannot know the inverse of a statement, so it only insists that
        cleanup touches the same store; the check run against the cluster
        (check.py) is what proves the state really comes back.
        """
        restored = {s.deployment for s in self.cleanup if isinstance(s, RestoreDeployment)}
        cleared = {s.deployment for s in self.cleanup if isinstance(s, ClearNetem)}
        cleanup_kinds = {type(s) for s in self.cleanup}
        for step in self.inject:
            if isinstance(step, SetEnv | SetResources | Scale) and step.deployment not in restored:
                raise ValueError(f"{step.deployment} is changed but has no restore_deployment")
            if isinstance(step, Netem) and step.deployment not in cleared:
                raise ValueError(f"netem on {step.deployment} has no clear_netem")
            if isinstance(step, PostgresSQL | RedisCommand) and type(step) not in cleanup_kinds:
                raise ValueError(f"{step.action} is injected but cleanup has no {step.action} step")
        return self


# ---- Loading -------------------------------------------------------------------


class IncidentFileError(ValueError):
    """An incident file that cannot be used, with the file named in the message."""


def load_incident(path: Path) -> Incident:
    """Parse and validate one incident file."""
    try:
        raw: Any = yaml.safe_load(path.read_text())
        incident = Incident.model_validate(raw)
    except (yaml.YAMLError, ValueError) as err:
        raise IncidentFileError(f"{path}: {err}") from err
    # The file name is how people and commands refer to an incident, so the two
    # must agree.
    if path.stem != incident.id:
        raise IncidentFileError(f"{path}: id {incident.id!r} does not match the file name")
    return incident


def load_library(directory: Path) -> list[Incident]:
    """Load every incident in a directory, in file-name order."""
    return [load_incident(path) for path in sorted(directory.glob("*.yaml"))]


def json_schema() -> dict[str, Any]:
    """The JSON schema of the incident format, as checked in at incidents/schema.json."""
    schema = Incident.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Incident"
    return schema
