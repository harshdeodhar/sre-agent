"""A stand-in for the API server, for the write gate's tests. No cluster needed.

It holds Deployments, ReplicaSets and pods as JSON and answers exactly the
kubectl commands the gate and its planners make. Any other command fails the
test, so code that starts running something new is noticed.

Two runners share it: `reader` behaves like the observer identity (it can
only `get`) and `writer` like the operator identity (it can only patch and
delete). Every command is appended to `log`, as is every trace record when
the test passes `cluster.trace` to the gate, so a test can check what
happened in which order.
"""

import copy
import json
from collections.abc import Callable, Sequence
from typing import Any

from oncall.chaos.kubectl import CommandError

NAMESPACE = "testbed"
SECRET_ENV = {
    "name": "DB_PASSWORD",
    "valueFrom": {"secretKeyRef": {"name": "postgres-credentials", "key": "password"}},
}


def template(
    app: str, env: dict[str, str], memory_request: str = "32Mi", memory_limit: str = "96Mi"
) -> dict[str, Any]:
    """A pod template shaped like the testbed's: one container named after the app."""
    return {
        "metadata": {"labels": {"app": app}, "annotations": {"prometheus.io/scrape": "true"}},
        "spec": {
            "containers": [
                {
                    "name": app,
                    "image": "sre-testbed/services:dev",
                    "env": [{"name": k, "value": v} for k, v in env.items()] + [SECRET_ENV],
                    "resources": {
                        "requests": {"cpu": "25m", "memory": memory_request},
                        "limits": {"memory": memory_limit},
                    },
                }
            ]
        },
    }


class Log:
    """What happened, in order: kubectl commands and trace records."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]


class LogTrace:
    """A trace that writes into the cluster's log."""

    def __init__(self, log: Log) -> None:
        self._log = log
        self.records: list[dict[str, Any]] = []

    def write(self, kind: str, **fields: object) -> None:
        record = {"kind": kind, **fields}
        self.records.append(record)
        self._log.events.append(("trace " + kind, record))


class FakeCluster:
    def __init__(self) -> None:
        self.deployments: dict[str, dict[str, Any]] = {}
        self.replicasets: dict[str, dict[str, Any]] = {}
        self.pods: dict[str, dict[str, Any]] = {}
        self.log = Log()
        self.trace = LogTrace(self.log)
        # Set to make a dry run (or the real change) fail, as admission would.
        self.refuse_dry_run: str | None = None
        self.refuse_change: str | None = None
        # Set to make a real change report success but not happen.
        self.lose_changes = False
        # Set to make every read fail, as an unreachable API server would.
        self.unreachable = False
        self.reader = _Runner(self, "reader")
        self.writer = _Runner(self, "writer")

    # ---- Building a cluster ----------------------------------------------------------------

    def add_deployment(
        self,
        name: str,
        revisions: list[dict[str, Any]],
        replicas: int = 2,
        available: int | None = None,
        pods: int | None = None,
    ) -> None:
        """A Deployment whose last template in `revisions` is the current one.

        Revisions are numbered from 1; one ReplicaSet per revision.
        """
        current = len(revisions)
        uid = f"uid-{name}"
        self.deployments[name] = {
            "metadata": {
                "name": name,
                "namespace": NAMESPACE,
                "uid": uid,
                "generation": current + 1,
                "labels": {"app": name},
                "annotations": {"deployment.kubernetes.io/revision": str(current)},
            },
            "spec": {
                "replicas": replicas,
                "selector": {"matchLabels": {"app": name}},
                "template": copy.deepcopy(revisions[-1]),
            },
            "status": {"availableReplicas": replicas if available is None else available},
        }
        for number, pod_template in enumerate(revisions, start=1):
            rs_template = copy.deepcopy(pod_template)
            rs_template["metadata"]["labels"]["pod-template-hash"] = f"hash{number}"
            rs_name = f"{name}-hash{number}"
            self.replicasets[rs_name] = {
                "metadata": {
                    "name": rs_name,
                    "labels": {"app": name, "pod-template-hash": f"hash{number}"},
                    "annotations": {"deployment.kubernetes.io/revision": str(number)},
                    "ownerReferences": [{"kind": "Deployment", "name": name, "uid": uid}],
                },
                "spec": {
                    "replicas": replicas if number == current else 0,
                    "template": rs_template,
                },
            }
        current_rs = f"{name}-hash{current}"
        for index in range(replicas if pods is None else pods):
            self.add_pod(f"{name}-hash{current}-pod{index}", app=name, owner=current_rs)

    def add_pod(
        self, name: str, app: str, owner: str | None, ready: bool = True, deleting: bool = False
    ) -> None:
        metadata: dict[str, Any] = {"name": name, "labels": {"app": app}}
        if owner:
            metadata["ownerReferences"] = [{"kind": "ReplicaSet", "name": owner}]
        if deleting:
            metadata["deletionTimestamp"] = "2026-11-10T12:00:00Z"
        self.pods[name] = {
            "metadata": metadata,
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
        }

    def change_spec_behind_the_agents_back(self, deployment: str) -> None:
        """Someone else edits the Deployment: its generation goes up."""
        self.deployments[deployment]["metadata"]["generation"] += 1

    # ---- Answering kubectl -----------------------------------------------------------------

    def answer(self, identity: str, args: Sequence[str]) -> str:
        args = list(args)
        self.log.events.append((f"kubectl {identity}", args))
        if args[:2] != ["--namespace", NAMESPACE]:
            raise AssertionError(f"command outside the testbed namespace: {args}")
        verb, rest = args[2], args[3:]
        allowed = {"reader": {"get"}, "writer": {"patch", "delete"}}[identity]
        if verb not in allowed:
            raise CommandError(f"kubectl {' '.join(args)} exited 1: Error from server (Forbidden)")
        if self.unreachable:
            raise CommandError(f"kubectl {' '.join(args)} exited 1: connection refused")
        match verb:
            case "get":
                return self._get(rest)
            case "patch":
                return self._patch(rest)
            case _:
                return self._delete(rest)

    def _get(self, rest: list[str]) -> str:
        assert rest[-1] == "--output=json", rest
        kind = rest[0]
        if kind == "replicasets":
            selector = rest[1].removeprefix("--selector=")
            key, value = selector.split("=")
            items = [
                rs for rs in self.replicasets.values() if rs["metadata"]["labels"].get(key) == value
            ]
            return json.dumps({"items": items})
        store = {"deployment": self.deployments, "replicaset": self.replicasets, "pod": self.pods}
        found = store[kind].get(rest[1])
        if found is None:
            raise CommandError(
                f"kubectl get {kind} {rest[1]} exited 1: Error from server (NotFound): "
                f'{kind}s "{rest[1]}" not found'
            )
        return json.dumps(found)

    def _patch(self, rest: list[str]) -> str:
        kind, name, patch_type, patch_arg, output, *flags = rest
        assert (kind, patch_type, output) == ("deployment", "--type=json", "--output=json"), rest
        dry_run = flags == ["--dry-run=server"]
        assert flags in ([], ["--dry-run=server"]), flags
        self._refuse(dry_run)
        deployment = copy.deepcopy(self.deployments[name])
        for op in json.loads(patch_arg.removeprefix("--patch=")):
            if op["op"] == "test":
                if _at(deployment, op["path"]) != op["value"]:
                    raise CommandError(
                        "kubectl patch exited 1: The request is invalid: the server rejected our "
                        "request due to an error in our request (test operation failed)"
                    )
            else:
                assert op["op"] == "replace", op
                _set(deployment, op["path"], op["value"])
        if deployment["spec"] != self.deployments[name]["spec"]:
            deployment["metadata"]["generation"] += 1
        if not dry_run and not self.lose_changes:
            self.deployments[name] = deployment
        return json.dumps(deployment)

    def _delete(self, rest: list[str]) -> str:
        kind, name, wait, *flags = rest
        assert (kind, wait) == ("pod", "--wait=false"), rest
        dry_run = flags == ["--dry-run=server"]
        self._refuse(dry_run)
        if name not in self.pods:
            raise CommandError(f'kubectl delete exited 1: Error from server (NotFound): "{name}"')
        if not dry_run and not self.lose_changes:
            del self.pods[name]
        return f'pod "{name}" deleted' + (" (server dry run)" if dry_run else "")

    def _refuse(self, dry_run: bool) -> None:
        message = self.refuse_dry_run if dry_run else self.refuse_change
        if message:
            raise CommandError(f"kubectl exited 1: {message}")

    def writes(self) -> list[list[str]]:
        """Every command the writer ran that was not a dry run."""
        return [
            args
            for kind, args in self.log.events
            if kind == "kubectl writer" and "--dry-run=server" not in args
        ]

    def dry_runs(self) -> list[list[str]]:
        return [
            args
            for kind, args in self.log.events
            if kind == "kubectl writer" and "--dry-run=server" in args
        ]


class _Runner:
    def __init__(self, cluster: FakeCluster, identity: str) -> None:
        self._cluster = cluster
        self._identity = identity

    def run(self, args: Sequence[str], *, stdin: str | None = None, timeout: float = 120) -> str:
        return self._cluster.answer(self._identity, args)


def _at(document: Any, pointer: str) -> Any:  # noqa: ANN401
    for part in pointer.strip("/").split("/"):
        document = document[int(part)] if isinstance(document, list) else document[part]
    return document


def _set(document: Any, pointer: str, value: Any) -> None:  # noqa: ANN401
    *parents, last = pointer.strip("/").split("/")
    for part in parents:
        document = document[int(part)] if isinstance(document, list) else document[part]
    if isinstance(document, list):
        document[int(last)] = value
    else:
        document[last] = value


# ---- The cluster most tests start from -------------------------------------------------

ORDER_ENV = {"INVENTORY_TIMEOUT": "1s", "PUBLISH_TIMEOUT": "2s", "DB_MAX_CONNS": "10"}


def make_testbed() -> FakeCluster:
    """The testbed after a bad config push to order-service (revision 2), otherwise at rest.

    - order-service: revision 1 had INVENTORY_TIMEOUT=1s, revision 2 has 1ms.
    - inventory-service: revision 2 lowered the memory request and limit.
    - api-gateway: one revision only. notification-worker: one replica.
    - redis, postgres, nats: one replica each.
    """
    cluster = FakeCluster()
    bad = {**ORDER_ENV, "INVENTORY_TIMEOUT": "1ms"}
    cluster.add_deployment(
        "order-service", [template("order-service", ORDER_ENV), template("order-service", bad)]
    )
    inventory = {"CACHE_TTL": "30s"}
    cluster.add_deployment(
        "inventory-service",
        [
            template("inventory-service", inventory),
            template("inventory-service", inventory, memory_request="8Mi", memory_limit="12Mi"),
        ],
    )
    cluster.add_deployment("api-gateway", [template("api-gateway", {"UPSTREAM_TIMEOUT": "2s"})])
    cluster.add_deployment(
        "notification-worker", [template("notification-worker", {"SEND_DELAY": "25ms"})], replicas=1
    )
    for store in ("redis", "postgres", "nats"):
        cluster.add_deployment(store, [template(store, {})], replicas=1)
    return cluster


Mutator = Callable[[FakeCluster], None]
