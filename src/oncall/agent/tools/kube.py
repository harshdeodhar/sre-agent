"""Kubernetes tools: get, describe, events, and rollout history.

Each tool builds a kubectl argument list from typed parameters. The namespace
is not a parameter: it is always `testbed`. Resource kinds come from a closed
list that does not contain Secrets. Names and selectors are checked against a
pattern before they are used, so a value cannot be read by kubectl as a flag
(a "name" of `--all-namespaces`, say).

These checks are the first of two layers. The second is the identity kubectl
runs as: a ServiceAccount whose Role grants `get` and `list` on these kinds in
`testbed` and nothing else (deploy/rbac). If a check here were wrong, the API
server would still refuse.
"""

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from oncall.agent.tools.base import MAX_OUTPUT_CHARS, Params, Tool, ToolError, bound
from oncall.chaos.kubectl import TESTBED, CommandError, Runner

# How many of a Deployment's most recent revisions deploy_history prints in full.
SHOWN_REVISIONS = 3
MAX_EVENTS = 30
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"

# A Kubernetes object name: lower-case letters, digits, '-' and '.', starting
# and ending with a letter or digit. Cannot start with '-', so it is never a flag.
_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
# Equality-based label selectors only: app=redis, tier!=db, a=b,c=d.
_SELECTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*(=|==|!=)[A-Za-z0-9_.-]*$")


class Kind(StrEnum):
    """The resource kinds the agent can read. Secrets are deliberately absent."""

    PODS = "pods"
    DEPLOYMENTS = "deployments"
    REPLICASETS = "replicasets"
    SERVICES = "services"
    ENDPOINTS = "endpoints"
    CONFIGMAPS = "configmaps"


class KubeGet(Params):
    kind: Kind = Field(description="The kind of resource.")
    name: str = Field(description='The name of one object, or "" for all objects of the kind.')
    label_selector: str = Field(
        description='An equality-based label selector such as "app=order-service", or "" for none.'
    )
    output: Literal["wide", "yaml"] = Field(
        description='"wide" for a table with one line per object, "yaml" for the full object.'
    )


class KubeDescribe(Params):
    kind: Kind = Field(description="The kind of resource.")
    name: str = Field(description="The name of the object.")


class KubeEvents(Params):
    object_name: str = Field(
        description='Only events about the object with this exact name (e.g. a pod), or "" for all.'
    )
    warnings_only: bool = Field(description="Leave out events of type Normal.")


class DeployHistory(Params):
    deployment: str = Field(description='The name of a Deployment, e.g. "order-service".')


class _Kube(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Metadata(_Kube):
    name: str
    creationTimestamp: str = ""
    annotations: dict[str, str] = {}


class _ReplicaSetSpec(_Kube):
    replicas: int = 0


class _ReplicaSetStatus(_Kube):
    readyReplicas: int = 0


class _ReplicaSet(_Kube):
    metadata: _Metadata
    spec: _ReplicaSetSpec = _ReplicaSetSpec()
    status: _ReplicaSetStatus = _ReplicaSetStatus()

    @property
    def revision(self) -> int:
        try:
            return int(self.metadata.annotations.get(REVISION_ANNOTATION, "0"))
        except ValueError:
            return 0


class _ReplicaSetList(_Kube):
    items: list[_ReplicaSet] = []


def kube_tools(kubectl: Runner) -> list[Tool[Any]]:
    """The Kubernetes tools, run through `kubectl` (the agent's own identity)."""

    def run(args: list[str]) -> str:
        try:
            return kubectl.run(["--namespace", TESTBED, *args], timeout=30)
        except CommandError as err:
            raise ToolError(str(err)) from err

    def kube_get(params: KubeGet) -> str:
        args = ["get", params.kind.value]
        if params.name:
            args.append(_checked_name(params.name))
        if params.label_selector:
            # One argument, `--selector=value`: the value cannot be taken for a flag.
            args.append(f"--selector={_checked_selector(params.label_selector)}")
        args += ["--output", params.output]
        return run(args) or f"No {params.kind.value} found."

    def kube_describe(params: KubeDescribe) -> str:
        return run(["describe", params.kind.value, _checked_name(params.name)])

    def kube_events(params: KubeEvents) -> str:
        fields = []
        if params.object_name:
            fields.append(f"involvedObject.name={_checked_name(params.object_name)}")
        if params.warnings_only:
            fields.append("type=Warning")
        args = ["get", "events", "--sort-by=.lastTimestamp"]
        if fields:
            args.append(f"--field-selector={','.join(fields)}")
        return _recent_events(run(args))

    def deploy_history(params: DeployHistory) -> str:
        name = _checked_name(params.deployment)
        try:
            listing = _ReplicaSetList.model_validate_json(
                run(["get", "replicasets", f"--selector=app={name}", "--output", "json"])
            )
        except ValidationError as err:
            raise ToolError(f"unexpected output from kubectl: {err}") from err
        revisions = sorted(listing.items, key=lambda rs: rs.revision)
        if not revisions:
            raise ToolError(f"no ReplicaSet with label app={name}; is {name} a Deployment?")
        lines = [
            f"Revisions of deployment/{name}, oldest first. A revision is one version of the "
            "pod template (image, environment, resources, probes); the Deployment creates a "
            "ReplicaSet for it. Changing only the replica count does not create a revision.",
            "REVISION  REPLICASET  REPLICASET_CREATED(UTC)  DESIRED  READY",
        ]
        lines += [
            f"{rs.revision}  {rs.metadata.name}  {rs.metadata.creationTimestamp}  "
            f"{rs.spec.replicas}  {rs.status.readyReplicas}"
            for rs in revisions
        ]
        for rs in revisions[-SHOWN_REVISIONS:]:
            lines.append("")
            lines.append(
                run(["rollout", "history", f"deployment/{name}", f"--revision={rs.revision}"])
            )
        return "\n".join(lines)

    return [
        Tool(
            name="kube_get",
            description=(
                "kubectl get in the testbed namespace: list objects of a kind, or show one. "
                'Use output "wide" for status at a glance (ready counts, restarts, age, pod '
                'IPs) and "yaml" for the full specification and status of an object.'
            ),
            params=KubeGet,
            run=kube_get,
        ),
        Tool(
            name="kube_describe",
            description=(
                "kubectl describe for one object in the testbed namespace. For a pod this "
                "shows each container's state, last termination reason and exit code, "
                "restart count, resource limits, environment, and the object's recent events."
            ),
            params=KubeDescribe,
            run=kube_describe,
        ),
        Tool(
            name="kube_events",
            description=(
                "Recent Kubernetes events in the testbed namespace, oldest first: "
                "scheduling, image pulls, probe failures, OOM kills, back-off, scaling. "
                "Kubernetes keeps events for about an hour."
            ),
            params=KubeEvents,
            run=kube_events,
        ),
        Tool(
            name="deploy_history",
            description=(
                "The rollout history of a Deployment: its revisions, when each was "
                f"created, and the full pod template of the {SHOWN_REVISIONS} most recent "
                "ones. Compare revisions to see what a rollout changed."
            ),
            params=DeployHistory,
            run=deploy_history,
        ),
    ]


def _recent_events(table: str) -> str:
    """Keep the header and the newest events.

    The table is sorted oldest first, so when it is too long it is the old end
    that goes: the recent events are the ones an investigation needs.
    """
    lines = table.splitlines()
    if not lines:
        return "No events found."
    header, events = lines[0], lines[1:]
    kept = events[-MAX_EVENTS:]
    # Event messages vary in length, so the character budget is applied too.
    body = bound("\n".join(kept), MAX_OUTPUT_CHARS - 300, keep="tail")
    omitted = len(events) - len(kept)
    note = f"\n[{omitted} older events not shown]" if omitted else ""
    return f"{header}\n{body}{note}"


def _checked_name(name: str) -> str:
    # fullmatch, not match: with match, `$` would accept a trailing newline.
    if not _NAME.fullmatch(name):
        raise ToolError(
            f"{name!r} is not a valid object name (lower-case letters, digits, '-' and '.')"
        )
    return name


def _checked_selector(selector: str) -> str:
    for term in selector.split(","):
        if not _SELECTOR.fullmatch(term):
            raise ToolError(
                f"{selector!r} is not a supported label selector; use key=value[,key=value]"
            )
    return selector
