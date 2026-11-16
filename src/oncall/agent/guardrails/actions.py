"""The four write actions: what each would change, worked out before anything changes.

A write action is a pydantic model of its parameters and a `plan` function.
`plan` reads the cluster through a `ClusterView`, which can only read, and
returns a `Plan`: the target, the exact change, a one-line summary and the
risk. It cannot make the change; nothing here holds a way to write. The gate
(gate.py) is the only code that executes a plan.

The scope and allow-list checks are made here because most of them need a
read of the cluster: does the Deployment exist, does it have an earlier
revision, is this pod the last one serving. A failed check raises `Rejected`
with the gate step it belongs to.

Every change to a Deployment is one JSON-patch `replace` of one field. A
rollback replaces the pod template with the previous revision's, exactly as
`kubectl rollout undo` does; a scale replaces the replica count; a config
revert replaces the one container whose settings are set back. Doing all three
as an explicit patch is what lets the gate dry-run it, show its diff, and pin
it to the version of the Deployment that was checked.
"""

import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from oncall.agent.guardrails.policy import (
    ALLOWED_TARGETS,
    MAX_REPLICAS,
    MAX_SETTINGS,
    MIN_REPLICAS,
    RESTART,
    REVERT,
    REVERTIBLE_RESOURCES,
    ROLLBACK,
    ROLLOUT_RISK,
    SCALE,
    Risk,
    restart_risk,
    scale_risk,
    target_problem,
)
from oncall.agent.guardrails.record import Step
from oncall.agent.tools.base import Params, ToolError, strict_schema
from oncall.agent.tools.kube import OBJECT_NAME, REVISION_ANNOTATION
from oncall.chaos.kubectl import TESTBED, CommandError, Runner
from oncall.chaos.taxonomy import RemediationAction

# The label a Deployment puts on each ReplicaSet's pod template to tell them
# apart. It is not part of the Deployment's own template, so a rollback
# removes it, as `kubectl rollout undo` does.
POD_TEMPLATE_HASH = "pod-template-hash"
# An environment variable name as Kubernetes accepts it (C identifier rules).
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Rejected(Exception):
    """A check failed. Nothing has been dry-run or changed."""

    def __init__(self, step: Step, reason: str) -> None:
        super().__init__(reason)
        self.step = step
        self.reason = reason


# ---- Reading the cluster ---------------------------------------------------------------


class _Kube(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class OwnerReference(_Kube):
    kind: str
    name: str


class Metadata(_Kube):
    name: str
    uid: str = ""
    generation: int = 0
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}
    owner_references: list[OwnerReference] = Field(default=[], alias="ownerReferences")
    deletion_timestamp: str | None = Field(default=None, alias="deletionTimestamp")

    @property
    def revision(self) -> int:
        try:
            return int(self.annotations.get(REVISION_ANNOTATION, "0"))
        except ValueError:
            return 0


class _DeploymentSpec(_Kube):
    replicas: int = 1
    selector: dict[str, Any] = {}
    # Kept as the API returned it: a rollback puts a template back verbatim.
    template: dict[str, Any] = {}


class _DeploymentStatus(_Kube):
    available_replicas: int = Field(default=0, alias="availableReplicas")


class Deployment(_Kube):
    metadata: Metadata
    spec: _DeploymentSpec
    status: _DeploymentStatus = _DeploymentStatus()


class _ReplicaSetSpec(_Kube):
    template: dict[str, Any] = {}


class ReplicaSet(_Kube):
    metadata: Metadata
    spec: _ReplicaSetSpec = _ReplicaSetSpec()


class _ReplicaSetList(_Kube):
    items: list[ReplicaSet] = []


class _Condition(_Kube):
    type: str
    status: str


class _PodStatus(_Kube):
    conditions: list[_Condition] = []


class Pod(_Kube):
    metadata: Metadata
    status: _PodStatus = _PodStatus()

    @property
    def ready(self) -> bool:
        return any(c.type == "Ready" and c.status == "True" for c in self.status.conditions)


class ClusterView:
    """The reads a plan needs, made as the agent's read-only identity."""

    def __init__(self, kubectl: Runner) -> None:
        self._kubectl = kubectl

    def deployment(self, name: str) -> Deployment | None:
        text = self._get(["deployment", name])
        return Deployment.model_validate_json(text) if text else None

    def replicaset(self, name: str) -> ReplicaSet | None:
        text = self._get(["replicaset", name])
        return ReplicaSet.model_validate_json(text) if text else None

    def pod(self, name: str) -> Pod | None:
        text = self._get(["pod", name])
        return Pod.model_validate_json(text) if text else None

    def revisions(self, deployment: Deployment) -> list[ReplicaSet]:
        """The ReplicaSets the Deployment owns, oldest revision first."""
        labels = deployment.spec.selector.get("matchLabels", {})
        selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        text = self._get(["replicasets", f"--selector={selector}"])
        owned = [
            rs
            for rs in _ReplicaSetList.model_validate_json(text or "{}").items
            if any(
                ref.kind == "Deployment" and ref.name == deployment.metadata.name
                for ref in rs.metadata.owner_references
            )
        ]
        return sorted(owned, key=lambda rs: rs.metadata.revision)

    def _get(self, what: list[str]) -> str | None:
        """`kubectl get ... -o json` in testbed; None if the object does not exist."""
        try:
            return self._kubectl.run(
                ["--namespace", TESTBED, "get", *what, "--output=json"], timeout=30
            )
        except CommandError as err:
            if "(NotFound)" in str(err):
                return None
            # Not a rejection: the check could not be made at all.
            raise ToolError(f"could not read {' '.join(what)}: {err}") from err


# ---- What a plan is --------------------------------------------------------------------


@dataclass(frozen=True)
class ReplaceField:
    """Set one field of a Deployment, provided its spec is still the one that was checked.

    Sent as a JSON patch. The `test` operation makes the API server refuse the
    whole patch if the Deployment's generation, which goes up with every change
    to its spec, is no longer the one the plan was made from. So what is
    executed is exactly what was checked and dry-run, or nothing is.
    """

    deployment: str
    generation: int
    path: str  # a JSON pointer, e.g. /spec/replicas
    before: Any
    after: Any

    def json_patch(self) -> list[dict[str, Any]]:
        return [
            {"op": "test", "path": "/metadata/generation", "value": self.generation},
            {"op": "replace", "path": self.path, "value": self.after},
        ]


@dataclass(frozen=True)
class DeletePod:
    pod: str


@dataclass(frozen=True)
class Plan:
    target: str  # "deployment/order-service", "pod/redis-5d9c7-x2x4v"
    deployment: str  # the Deployment the change lands on
    summary: str
    risk: Risk
    risk_reason: str
    change: ReplaceField | DeletePod

    def key(self) -> str:
        """Identifies the change itself, to recognise the same proposal made twice."""
        change = self.change
        if isinstance(change, DeletePod):
            return f"delete pod/{change.pod}"
        return f"{self.target} {change.path} {json.dumps(change.after, sort_keys=True)}"


@dataclass(frozen=True)
class WriteAction[P: Params]:
    name: RemediationAction
    description: str
    params: type[P]
    plan: Callable[[P, ClusterView], Plan]

    def definition(self) -> dict[str, Any]:
        """The tool as the Messages API wants it."""
        return {
            "name": str(self.name),
            "description": self.description,
            "input_schema": strict_schema(self.params),
            "strict": True,
        }


# ---- The four actions ------------------------------------------------------------------

_REASON = "Why this fixes a root cause, with the call IDs of the evidence."


class RollbackDeploymentParams(Params):
    deployment: str = Field(description='The Deployment to roll back, e.g. "order-service".')
    reason: str = Field(description=_REASON)


class ScaleDeploymentParams(Params):
    deployment: str = Field(description='The Deployment to scale, e.g. "notification-worker".')
    replicas: int = Field(description=f"The new replica count, {MIN_REPLICAS} to {MAX_REPLICAS}.")
    reason: str = Field(description=_REASON)


class RestartPodParams(Params):
    pod: str = Field(description="The exact name of the pod, as kube_get shows it.")
    reason: str = Field(description=_REASON)


class RevertConfigParams(Params):
    deployment: str = Field(description='The Deployment, e.g. "order-service".')
    settings: list[str] = Field(
        description="The settings to set back: environment variable names such as "
        f'"INVENTORY_TIMEOUT", or {", ".join(repr(r) for r in REVERTIBLE_RESOURCES)}. '
        f"At most {MAX_SETTINGS}."
    )
    reason: str = Field(description=_REASON)


def plan_rollback(params: RollbackDeploymentParams, view: ClusterView) -> Plan:
    deployment = _deployment_in_scope(view, params.deployment)
    name = deployment.metadata.name
    _check_target(ROLLBACK, name)
    previous = _previous_revision(view, deployment)
    template = _template_of(previous)
    if template == deployment.spec.template:
        raise Rejected(
            Step.ALLOW_LIST,
            f"deployment/{name} already runs the pod template of revision "
            f"{previous.metadata.revision}: a rollback would change nothing",
        )
    risk, why = ROLLOUT_RISK
    return Plan(
        target=f"deployment/{name}",
        deployment=name,
        summary=(
            f"roll deployment/{name} back from revision {deployment.metadata.revision} to the "
            f"pod template of revision {previous.metadata.revision}"
        ),
        risk=risk,
        risk_reason=why,
        change=ReplaceField(
            name,
            deployment.metadata.generation,
            "/spec/template",
            deployment.spec.template,
            template,
        ),
    )


def plan_scale(params: ScaleDeploymentParams, view: ClusterView) -> Plan:
    deployment = _deployment_in_scope(view, params.deployment)
    name = deployment.metadata.name
    _check_target(SCALE, name)
    if not MIN_REPLICAS <= params.replicas <= MAX_REPLICAS:
        raise Rejected(
            Step.ALLOW_LIST,
            f"replicas must be between {MIN_REPLICAS} and {MAX_REPLICAS}, not {params.replicas}",
        )
    current = deployment.spec.replicas
    if params.replicas == current:
        raise Rejected(Step.ALLOW_LIST, f"deployment/{name} already has {current} replicas")
    risk, why = scale_risk(current, params.replicas)
    return Plan(
        target=f"deployment/{name}",
        deployment=name,
        summary=f"scale deployment/{name} from {current} to {params.replicas} replicas",
        risk=risk,
        risk_reason=why,
        change=ReplaceField(
            name, deployment.metadata.generation, "/spec/replicas", current, params.replicas
        ),
    )


def plan_restart(params: RestartPodParams, view: ClusterView) -> Plan:
    name = _in_scope_name(params.pod)
    pod = view.pod(name)
    if pod is None:
        raise Rejected(Step.SCOPE, f"there is no pod named {name} in namespace {TESTBED}")
    deployment = _owning_deployment(view, pod)
    if deployment is None:
        raise Rejected(
            Step.SCOPE,
            f"pod/{name} is not managed by a Deployment, so deleting it would not bring it back",
        )
    owner = deployment.metadata.name
    _check_target(RESTART, owner)
    if pod.metadata.deletion_timestamp:
        raise Rejected(Step.ALLOW_LIST, f"pod/{name} is already being deleted")
    other_available = max(0, deployment.status.available_replicas - (1 if pod.ready else 0))
    risk, why = restart_risk(other_available)
    return Plan(
        target=f"pod/{name}",
        deployment=owner,
        summary=f"delete pod/{name} so that deployment/{owner} replaces it with a new pod",
        risk=risk,
        risk_reason=why,
        change=DeletePod(name),
    )


def plan_revert(params: RevertConfigParams, view: ClusterView) -> Plan:
    deployment = _deployment_in_scope(view, params.deployment)
    name = deployment.metadata.name
    _check_target(REVERT, name)
    settings = params.settings
    if not settings:
        raise Rejected(Step.ALLOW_LIST, "name at least one setting to revert")
    if len(settings) > MAX_SETTINGS:
        raise Rejected(Step.ALLOW_LIST, f"at most {MAX_SETTINGS} settings at a time")
    if len(set(settings)) != len(settings):
        raise Rejected(Step.ALLOW_LIST, "a setting is named more than once")
    for setting in settings:
        if setting not in REVERTIBLE_RESOURCES and not _ENV_NAME.fullmatch(setting):
            raise Rejected(
                Step.ALLOW_LIST,
                f"{setting!r} is not a setting revert_config can change: give an environment "
                f"variable name or one of {', '.join(REVERTIBLE_RESOURCES)}",
            )
    previous = _previous_revision(view, deployment)
    index, current = _container(deployment.spec.template, name)
    _, before = _container(_template_of(previous), name)

    reverted = copy.deepcopy(current)
    unchanged = []
    for setting in settings:
        if setting in REVERTIBLE_RESOURCES:
            changed = _revert_resource(reverted, before, setting)
        else:
            changed = _revert_env(reverted, before, setting)
        if not changed:
            unchanged.append(setting)
    revisions = f"revision {previous.metadata.revision} and {deployment.metadata.revision}"
    if unchanged:
        raise Rejected(
            Step.ALLOW_LIST,
            f"{', '.join(unchanged)}: the same in {revisions}, so the last rollout did not change "
            "it. revert_config only sets back what the last rollout changed.",
        )
    return Plan(
        target=f"deployment/{name}",
        deployment=name,
        summary=(
            f"set {', '.join(settings)} of deployment/{name} back to the value(s) of revision "
            f"{previous.metadata.revision}"
        ),
        risk=ROLLOUT_RISK[0],
        risk_reason=ROLLOUT_RISK[1],
        change=ReplaceField(
            name,
            deployment.metadata.generation,
            f"/spec/template/spec/containers/{index}",
            current,
            reverted,
        ),
    )


def _allowed_on(action: RemediationAction) -> str:
    return ", ".join(sorted(ALLOWED_TARGETS[action]))


ACTIONS: dict[str, WriteAction[Any]] = {
    str(action.name): action
    for action in (
        WriteAction(
            name=ROLLBACK,
            description=(
                "Roll a Deployment back to its previous revision: its pod template (image, "
                "environment, resources, probes) becomes that of the revision before the current "
                "one, as with `kubectl rollout undo`. deploy_history shows what the last rollout "
                f"changed. Allowed on: {_allowed_on(ROLLBACK)}."
            ),
            params=RollbackDeploymentParams,
            plan=plan_rollback,
        ),
        WriteAction(
            name=SCALE,
            description=(
                f"Set the replica count of a Deployment, between {MIN_REPLICAS} and "
                f"{MAX_REPLICAS}. Allowed on: {_allowed_on(SCALE)}."
            ),
            params=ScaleDeploymentParams,
            plan=plan_scale,
        ),
        WriteAction(
            name=RESTART,
            description=(
                "Delete one pod so that its Deployment replaces it with a new one. The new pod "
                "gets a new name. Allowed for pods of: "
                f"{_allowed_on(RESTART)}."
            ),
            params=RestartPodParams,
            plan=plan_restart,
        ),
        WriteAction(
            name=REVERT,
            description=(
                "Set chosen settings of a Deployment's container back to their values in the "
                "previous revision, and leave everything else the last rollout changed as it is. "
                "A setting is an environment variable with a literal value, or one of "
                f"{', '.join(REVERTIBLE_RESOURCES)}. Allowed on: {_allowed_on(REVERT)}."
            ),
            params=RevertConfigParams,
            plan=plan_revert,
        ),
    )
}


def write_definitions() -> list[dict[str, Any]]:
    """The write tools as the Messages API wants them, in a fixed order."""
    return [action.definition() for action in ACTIONS.values()]


# ---- Checks shared by the actions ------------------------------------------------------


def _in_scope_name(name: str) -> str:
    # fullmatch, not match: with match, `$` would accept a trailing newline.
    if not OBJECT_NAME.fullmatch(name):
        raise Rejected(Step.SCOPE, f"{name!r} is not a valid object name")
    return name


def _deployment_in_scope(view: ClusterView, name: str) -> Deployment:
    deployment = view.deployment(_in_scope_name(name))
    if deployment is None:
        raise Rejected(Step.SCOPE, f"there is no Deployment named {name} in namespace {TESTBED}")
    return deployment


def _check_target(action: RemediationAction, deployment: str) -> None:
    problem = target_problem(action, deployment)
    if problem:
        raise Rejected(Step.ALLOW_LIST, problem)


def _owning_deployment(view: ClusterView, pod: Pod) -> Deployment | None:
    """The Deployment behind a pod: pod -> ReplicaSet -> Deployment, by owner references."""
    for ref in pod.metadata.owner_references:
        if ref.kind != "ReplicaSet":
            continue
        replicaset = view.replicaset(ref.name)
        for owner in replicaset.metadata.owner_references if replicaset else []:
            if owner.kind == "Deployment":
                return view.deployment(owner.name)
    return None


def _previous_revision(view: ClusterView, deployment: Deployment) -> ReplicaSet:
    current = deployment.metadata.revision
    earlier = [rs for rs in view.revisions(deployment) if rs.metadata.revision < current]
    if not earlier:
        raise Rejected(
            Step.ALLOW_LIST,
            f"deployment/{deployment.metadata.name} has no revision before {current} to go back to",
        )
    return earlier[-1]


def _template_of(replicaset: ReplicaSet) -> dict[str, Any]:
    """A ReplicaSet's pod template as it would be in the Deployment."""
    template = copy.deepcopy(replicaset.spec.template)
    labels = template.get("metadata", {}).get("labels", {})
    labels.pop(POD_TEMPLATE_HASH, None)
    return template


def _container(template: dict[str, Any], name: str) -> tuple[int, dict[str, Any]]:
    """The container named after the Deployment, and its index. Each service has one."""
    containers = template.get("spec", {}).get("containers", [])
    for index, container in enumerate(containers):
        if container.get("name") == name:
            return index, container
    raise Rejected(Step.SCOPE, f"the pod template of deployment/{name} has no container {name}")


def _revert_env(container: dict[str, Any], previous: dict[str, Any], name: str) -> bool:
    """Set environment variable `name` in `container` as it is in `previous`. False if equal."""
    now = _env_entry(container, name)
    before = _env_entry(previous, name)
    for entry in (now, before):
        if entry is not None and "valueFrom" in entry:
            raise Rejected(
                Step.ALLOW_LIST,
                f"{name} is set from a Secret or ConfigMap reference, not a value; revert_config "
                "does not change references",
            )
    if now == before:
        return False
    env = container.setdefault("env", [])
    if before is None:
        env.remove(now)  # the last rollout added it
    elif now is None:
        env.append(copy.deepcopy(before))  # the last rollout removed it
    else:
        env[env.index(now)] = copy.deepcopy(before)
    if not env:
        del container["env"]
    return True


def _env_entry(container: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((entry for entry in container.get("env", []) if entry.get("name") == name), None)


def _revert_resource(container: dict[str, Any], previous: dict[str, Any], setting: str) -> bool:
    """Set e.g. `limits.memory` in `container` as it is in `previous`. False if equal."""
    kind, resource = setting.split(".")
    now = container.get("resources", {}).get(kind, {}).get(resource)
    before = previous.get("resources", {}).get(kind, {}).get(resource)
    if now == before:
        return False
    resources = container.setdefault("resources", {})
    section = resources.setdefault(kind, {})
    if before is None:
        del section[resource]  # the last rollout added it
        if not section:
            del resources[kind]
    else:
        section[resource] = before
    return True
