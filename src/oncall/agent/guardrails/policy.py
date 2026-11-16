"""What the agent may change, and how risky each change is.

Every name and number that limits a write action is in this file. The Role
and the admission policy in deploy/rbac/agent-operator.yaml repeat some of
them for the API server; tests/agent/test_operator_rbac.py keeps the two
in step.

Policy modes:

- `observe`: the agent has no write tools. A write that reached the gate
  anyway would be rejected.
- `propose`: a write that passes every check is dry-run on the server and
  recorded, with its diff, as a proposal. A human approves or declines it; if
  nobody is asked, it stays a proposal and nothing changes.
- `auto`: as `propose`, except that a low-risk write is executed without
  asking anyone.
"""

from dataclasses import dataclass
from enum import StrEnum

from oncall.chaos.taxonomy import Component, RemediationAction


class PolicyMode(StrEnum):
    OBSERVE = "observe"
    PROPOSE = "propose"
    AUTO = "auto"


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class Policy:
    mode: PolicyMode = PolicyMode.OBSERVE
    # Writes that get as far as a dry run, per investigation: executed,
    # proposed and declined ones all count, and so does one the API server
    # refused in the dry run. An investigation that needs more than this is
    # one a human should be running.
    max_write_actions: int = 3


# The write tools are named after the remediation actions of the taxonomy, so
# a report's recommendation and a tool call use the same word.
ROLLBACK = RemediationAction.ROLLBACK_DEPLOYMENT
SCALE = RemediationAction.SCALE_DEPLOYMENT
RESTART = RemediationAction.RESTART_POD
REVERT = RemediationAction.REVERT_CONFIG
WRITE_ACTIONS = (ROLLBACK, SCALE, RESTART, REVERT)

# The four Go services hold no data: a replaced pod loses nothing, and every
# one of them can run more than one replica.
SERVICES = frozenset(
    {
        Component.API_GATEWAY,
        Component.ORDER_SERVICE,
        Component.INVENTORY_SERVICE,
        Component.NOTIFICATION_WORKER,
    }
)

# The Deployments each action may touch.
#
# The stores are left out wherever the change would replace or duplicate a
# store's pod. Postgres and NATS keep their data on an emptyDir volume, so a
# new pod starts with an empty database or an empty stream; and none of the
# three stores is built to run as two replicas. The one exception is
# restarting Redis: it is a cache, its data can be lost at any time by design,
# and a restart is how it gets its configured settings back.
ALLOWED_TARGETS: dict[RemediationAction, frozenset[str]] = {
    ROLLBACK: SERVICES,
    REVERT: SERVICES,
    SCALE: SERVICES,
    RESTART: SERVICES | {Component.REDIS},
}

WHY_NOT: dict[str, str] = {
    Component.POSTGRES: "its data is on an emptyDir volume, so a new pod starts with an empty "
    "database",
    Component.NATS: "its stream is on an emptyDir volume, so a new pod starts with no messages",
    Component.REDIS: "it runs as a single instance; only restarting its pod is allowed",
}

# Replica counts scale_deployment may set. Zero would be an outage. Three is
# one more than any service runs normally: each replica has a 96 MiB memory
# limit, and the whole cluster has to fit in a 4 GB VM.
MIN_REPLICAS = 1
MAX_REPLICAS = 3

# The settings revert_config can set back, besides environment variables with
# a literal value. The CPU and memory requests and limits of the container.
REVERTIBLE_RESOURCES = ("requests.cpu", "requests.memory", "limits.cpu", "limits.memory")
MAX_SETTINGS = 5

# What auto mode executes without asking. Everything else is a proposal.
AUTO_EXECUTES = frozenset({Risk.LOW})


def target_problem(action: RemediationAction, deployment: str) -> str | None:
    """Why `action` may not touch `deployment`; None if it may."""
    if deployment in ALLOWED_TARGETS[action]:
        return None
    allowed = ", ".join(sorted(ALLOWED_TARGETS[action]))
    why = WHY_NOT.get(deployment)
    reason = f"{action} may only act on {allowed}"
    return f"{reason}; not on {deployment}: {why}" if why else f"{reason}."


# ---- Risk ------------------------------------------------------------------------------
#
# Low risk means: the change keeps every replica's code and configuration as
# it is, does not reduce the capacity that is left, and Kubernetes undoes or
# absorbs it on its own. Those are the only changes auto mode makes alone.


def scale_risk(current: int, wanted: int) -> tuple[Risk, str]:
    if wanted > current:
        return Risk.LOW, "adds replicas; the pods that are running are not touched"
    return Risk.HIGH, "removes replicas, and with them capacity, during an incident"


def restart_risk(other_available: int) -> tuple[Risk, str]:
    if other_available > 0:
        return Risk.LOW, (
            f"{other_available} other available replica(s) keep serving while the "
            "Deployment replaces this pod"
        )
    return Risk.HIGH, (
        "this is the only available replica: the component is down until its replacement is ready"
    )


ROLLOUT_RISK = (
    Risk.MEDIUM,
    "replaces every pod of the Deployment with a different pod template, which helps only if "
    "the diagnosis is right",
)
