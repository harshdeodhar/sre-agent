"""What "the cluster is healthy" means, as code.

Used twice: before injection (an incident is only meaningful if it starts from
a steady state) and after cleanup (an incident that does not return the cluster
to healthy is a bug in the incident). Phase 5's harness reuses the same
conditions to score recovery.

Healthy means all of:
  1. every testbed Deployment has the replica count its manifest asks for, all
     of them updated and available, and the controller has seen the latest spec;
  2. Prometheus holds no pending or firing alert.

Both must hold continuously for a while, not just at one instant: see
wait_until_stable.
"""

import time
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from oncall.chaos.kubectl import TESTBED, CommandError, Runner
from oncall.chaos.telemetry import Alert, Prometheus, TelemetryError

TESTBED_KUSTOMIZATION = Path("deploy/testbed")


class _Kube(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class _Metadata(_Kube):
    name: str
    generation: int = 0


class _Spec(_Kube):
    replicas: int = 1


class _Status(_Kube):
    # The API omits these fields when they are zero.
    observed_generation: int = Field(default=0, alias="observedGeneration")
    replicas: int = 0
    updated_replicas: int = Field(default=0, alias="updatedReplicas")
    available_replicas: int = Field(default=0, alias="availableReplicas")


class Deployment(_Kube):
    metadata: _Metadata
    spec: _Spec = _Spec()
    status: _Status = _Status()


class _DeploymentList(_Kube):
    items: list[Deployment]


def deployment_problems(live: list[Deployment], expected_replicas: dict[str, int]) -> list[str]:
    """Why the Deployments are not in their intended state. Empty means they are."""
    problems = []
    by_name = {d.metadata.name: d for d in live}
    for name, want in sorted(expected_replicas.items()):
        deployment = by_name.get(name)
        if deployment is None:
            problems.append(f"deployment {name} does not exist")
            continue
        status = deployment.status
        if deployment.spec.replicas != want:
            problems.append(
                f"deployment {name} is scaled to {deployment.spec.replicas}, not {want}"
            )
        elif status.observed_generation != deployment.metadata.generation:
            problems.append(f"deployment {name} has a change its controller has not acted on yet")
        elif not (status.replicas == status.updated_replicas == status.available_replicas == want):
            problems.append(
                f"deployment {name} wants {want} replicas: {status.replicas} exist, "
                f"{status.updated_replicas} updated, {status.available_replicas} available"
            )
    return problems


def alert_problems(alerts: list[Alert]) -> list[str]:
    """A pending alert counts too: its condition is already true."""
    return sorted({f"alert {a.name} is {a.state}" for a in alerts})


class ClusterHealth:
    """Reads the live state and reports what, if anything, is wrong."""

    def __init__(self, kubectl: Runner, prometheus: Prometheus, repo_root: Path) -> None:
        self._kubectl = kubectl
        self._prometheus = prometheus
        self._expected = self._expected_replicas(repo_root)

    def _expected_replicas(self, repo_root: Path) -> dict[str, int]:
        """Replica counts from the repo's manifests: the definition of "intended"."""
        rendered = self._kubectl.run(["kustomize", str(repo_root / TESTBED_KUSTOMIZATION)])
        return {
            doc["metadata"]["name"]: doc["spec"].get("replicas", 1)
            for doc in yaml.safe_load_all(rendered)
            if isinstance(doc, dict) and doc.get("kind") == "Deployment"
        }

    def problems(self) -> list[str]:
        """Everything currently wrong. An unreachable API counts as a problem."""
        try:
            out = self._kubectl.run(["-n", TESTBED, "get", "deployments", "-o", "json"])
            live = _DeploymentList.model_validate_json(out).items
            found = deployment_problems(live, self._expected)
        except CommandError as err:
            found = [f"kubectl failed: {err}"]
        try:
            found += alert_problems(self._prometheus.alerts())
        except TelemetryError as err:
            found.append(f"prometheus unreachable: {err}")
        return found


def wait_until_stable(
    probe: Callable[[], list[str]],
    *,
    hold_seconds: float,
    timeout_seconds: float,
    interval_seconds: float = 5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, float, list[str]]:
    """Poll until `probe` has reported no problem for `hold_seconds` in a row.

    A single clean reading proves little on a machine where latency spikes come
    and go; the hold is what makes "healthy" mean "steady".

    Returns (became stable, seconds waited, problems at the last reading).
    """
    start = clock()
    clean_since: float | None = None
    while True:
        problems = probe()
        now = clock()
        if problems:
            clean_since = None
        else:
            clean_since = now if clean_since is None else clean_since
            if now - clean_since >= hold_seconds:
                return True, now - start, []
        if now - start >= timeout_seconds:
            return False, now - start, problems
        sleep(interval_seconds)
