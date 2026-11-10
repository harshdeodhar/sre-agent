"""Turn typed incident steps into kubectl calls.

One method per step kind. Each builds an argument list from validated fields;
none of them builds a command string.
"""

import json
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from oncall.chaos.incident import (
    CleanupStep,
    ClearNetem,
    InjectStep,
    Netem,
    PostgresSQL,
    RedisCommand,
    RestoreDeployment,
    Scale,
    SetEnv,
    SetResources,
)
from oncall.chaos.kubectl import CHAOS, TESTBED, CommandError, Runner

# Where the service Deployments are defined, relative to the repo root.
SERVICES_KUSTOMIZATION = Path("deploy/testbed/services")
ROLLOUT_TIMEOUT_SECONDS = 300

_UUID = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


# ---- The few fields read back from kubectl's JSON output ------------------------


class _Kube(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class _Metadata(_Kube):
    name: str
    uid: str = ""
    deletion_timestamp: str | None = Field(default=None, alias="deletionTimestamp")


class _PodStatus(_Kube):
    phase: str = ""


class _Pod(_Kube):
    metadata: _Metadata
    status: _PodStatus = _PodStatus()


class _PodList(_Kube):
    items: list[_Pod]


class _ReplicaSetSpec(_Kube):
    replicas: int = 0


class _ReplicaSet(_Kube):
    metadata: _Metadata
    spec: _ReplicaSetSpec = _ReplicaSetSpec()


class _ReplicaSetList(_Kube):
    items: list[_ReplicaSet]


# ---- Execution -------------------------------------------------------------------


class Executor:
    """Applies incident steps to the cluster."""

    def __init__(self, kubectl: Runner, repo_root: Path) -> None:
        self._kubectl = kubectl
        self._repo_root = repo_root

    def run(self, step: InjectStep | CleanupStep) -> None:
        """Apply one step. Raises CommandError if the cluster refuses it."""
        match step:
            case SetEnv():
                self._set_env(step)
            case SetResources():
                self._set_resources(step)
            case Scale():
                self._scale(step)
            case PostgresSQL():
                self._postgres_sql(step)
            case RedisCommand():
                self._redis_command(step)
            case Netem():
                self._netem(step)
            case RestoreDeployment():
                self._restore_deployment(step)
            case ClearNetem():
                self._clear_netem(step)

    # The service container has the same name as its Deployment.

    def _set_env(self, step: SetEnv) -> None:
        assignments = [f"{name}={value}" for name, value in step.env.items()]
        target = f"deployment/{step.deployment}"
        self._kubectl.run(
            ["-n", TESTBED, "set", "env", target, "-c", step.deployment, *assignments]
        )

    def _set_resources(self, step: SetResources) -> None:
        flags = []
        for flag, quantities in (("--requests", step.requests), ("--limits", step.limits)):
            if quantities:
                pairs = ",".join(
                    f"{resource}={quantity}" for resource, quantity in quantities.items()
                )
                flags.append(f"{flag}={pairs}")
        target = f"deployment/{step.deployment}"
        self._kubectl.run(
            ["-n", TESTBED, "set", "resources", target, "-c", step.deployment, *flags]
        )

    def _scale(self, step: Scale) -> None:
        target = f"deployment/{step.deployment}"
        self._kubectl.run(["-n", TESTBED, "scale", target, f"--replicas={step.replicas}"])

    def _postgres_sql(self, step: PostgresSQL) -> None:
        # psql runs inside the Postgres pod over its local socket, so no
        # password leaves the cluster. ON_ERROR_STOP makes a failed statement a
        # non-zero exit instead of a message on an otherwise "successful" run.
        psql = ["psql", "-X", "-U", "shop", "-d", "shop", "-v", "ON_ERROR_STOP=1", "-c", step.sql]
        self._kubectl.run(["-n", TESTBED, "exec", "deploy/postgres", "-c", "postgres", "--", *psql])

    def _redis_command(self, step: RedisCommand) -> None:
        # -e: exit non-zero when Redis answers with an error. Without it
        # redis-cli prints the error and exits 0.
        redis_cli = ["redis-cli", "-e", *step.args]
        self._kubectl.run(["-n", TESTBED, "exec", "deploy/redis", "-c", "redis", "--", *redis_cli])

    def _netem(self, step: Netem) -> None:
        pods = self._running_pods(step.deployment)
        if not pods:
            raise CommandError(f"no running pod of {step.deployment} to apply netem to")
        targets = pods[:1] if step.pods == "first" else pods
        spec: list[str] = []
        if step.delay_ms:
            spec += ["delay", f"{step.delay_ms}ms"]
            if step.jitter_ms:
                spec += [f"{step.jitter_ms}ms"]
        if step.loss_percent:
            spec += ["loss", f"{step.loss_percent:g}%"]
        for pod in targets:
            self._pod_netem(pod, "apply", spec)

    def _clear_netem(self, step: ClearNetem) -> None:
        for pod in self._running_pods(step.deployment):
            self._pod_netem(pod, "clear", [])

    def _pod_netem(self, pod: _Pod, verb: str, spec: list[str]) -> None:
        # The UID ends up in a `grep` pattern inside the helper. It comes from
        # the API server, not from an incident file, but it is checked anyway.
        if not _UUID.match(pod.metadata.uid):
            raise CommandError(
                f"pod {pod.metadata.name} has an unexpected UID {pod.metadata.uid!r}"
            )
        helper = ["pod-netem", pod.metadata.uid, verb, *spec]
        self._kubectl.run(["-n", CHAOS, "exec", "deploy/netem-helper", "--", *helper])

    def _running_pods(self, deployment: str) -> list[_Pod]:
        """Running, non-terminating pods of a service, in name order."""
        out = self._kubectl.run(
            ["-n", TESTBED, "get", "pods", "-l", f"app={deployment}", "-o", "json"]
        )
        pods = _PodList.model_validate_json(out).items
        live = [
            p for p in pods if p.status.phase == "Running" and p.metadata.deletion_timestamp is None
        ]
        return sorted(live, key=lambda p: p.metadata.name)

    def _restore_deployment(self, step: RestoreDeployment) -> None:
        """Replace the live Deployment with the repo's manifest, then tidy its history.

        `kubectl replace`, not `apply`: apply merges, so it would put a changed
        value back but leave a field that the incident (or the agent) added,
        such as a CPU limit. Replace makes the spec exactly the manifest's.

        The ReplicaSets left at zero replicas are then deleted. Otherwise the
        faulty revision of one incident would sit in the rollout history
        during the next one: a red herring at best, a hint at worst. What
        remains is one revision, the good one, which is also the revision a
        rollback during the next incident returns to.
        """
        manifest = self._deployment_manifest(step.deployment)
        self._kubectl.run(["replace", "--save-config", "-f", "-"], stdin=json.dumps(manifest))
        self._kubectl.run(
            [
                "-n",
                TESTBED,
                "rollout",
                "status",
                f"deployment/{step.deployment}",
                f"--timeout={ROLLOUT_TIMEOUT_SECONDS}s",
            ],
            timeout=ROLLOUT_TIMEOUT_SECONDS + 30,
        )
        out = self._kubectl.run(
            ["-n", TESTBED, "get", "replicasets", "-l", f"app={step.deployment}", "-o", "json"]
        )
        idle = [
            rs.metadata.name
            for rs in _ReplicaSetList.model_validate_json(out).items
            if rs.spec.replicas == 0
        ]
        if idle:
            self._kubectl.run(["-n", TESTBED, "delete", "replicaset", *sorted(idle)])

    def _deployment_manifest(self, name: str) -> dict[str, Any]:
        """The Deployment called `name`, as `make deploy` would apply it."""
        rendered = self._kubectl.run(["kustomize", str(self._repo_root / SERVICES_KUSTOMIZATION)])
        for document in yaml.safe_load_all(rendered):
            if (
                isinstance(document, dict)
                and document.get("kind") == "Deployment"
                and document.get("metadata", {}).get("name") == name
            ):
                return document
        raise CommandError(f"no Deployment named {name} in {SERVICES_KUSTOMIZATION}")
