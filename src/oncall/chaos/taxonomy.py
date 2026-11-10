"""The fixed vocabulary that ground truth and incident reports are written in.

A root cause is a pair: the component that is broken, and the kind of fault.
Scoring compares pairs, so both sides must draw from the same closed lists.

The category list is deliberately longer than the set of faults the incident
library injects. If the two were the same, the list would be an answer key: an
agent could score well by guessing among the few categories that ever occur.
tests/chaos/test_incident_library.py asserts that unused categories exist.
"""

from enum import StrEnum


class Component(StrEnum):
    """Everything in the testbed namespace that can be the site of a fault."""

    API_GATEWAY = "api-gateway"
    ORDER_SERVICE = "order-service"
    INVENTORY_SERVICE = "inventory-service"
    NOTIFICATION_WORKER = "notification-worker"
    POSTGRES = "postgres"
    REDIS = "redis"
    NATS = "nats"


class FaultCategory(StrEnum):
    """What kind of defect has to be fixed.

    Categories describe causes, never symptoms: "crash loop" and "consumer lag"
    are not here, because each can have several different causes.
    """

    BAD_CONFIG_VALUE = "bad_config_value"
    BAD_IMAGE = "bad_image"
    MEMORY_LIMIT_TOO_LOW = "memory_limit_too_low"
    MEMORY_LEAK = "memory_leak"
    CPU_LIMIT_TOO_LOW = "cpu_limit_too_low"
    INSUFFICIENT_REPLICAS = "insufficient_replicas"
    NETWORK_LATENCY = "network_latency"
    NETWORK_PACKET_LOSS = "network_packet_loss"
    NETWORK_PARTITION = "network_partition"
    MISSING_INDEX = "missing_index"
    LOCK_CONTENTION = "lock_contention"
    CONNECTION_POOL_TOO_SMALL = "connection_pool_too_small"
    CACHE_MEMORY_TOO_LOW = "cache_memory_too_low"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    DISK_FULL = "disk_full"
    DNS_FAILURE = "dns_failure"
    EXPIRED_CREDENTIALS = "expired_credentials"
    TRAFFIC_SPIKE = "traffic_spike"


# One definition per category. docs/RUBRIC.md prints this table, and the agent
# is given the same text (Phase 3), so a category means the same thing to the
# incident author, the agent and the scorer.
#
# Several categories are, mechanically, "a setting was changed". The rule that
# keeps them apart: pick the most specific category that applies.
# BAD_CONFIG_VALUE is for application settings that no other category covers.
CATEGORY_DEFINITIONS: dict[FaultCategory, str] = {
    FaultCategory.BAD_CONFIG_VALUE: (
        "An application setting of the component (environment variable or flag) has a wrong "
        "value: unparsable, or valid but harmful. Use a more specific category if one applies."
    ),
    FaultCategory.BAD_IMAGE: "The component runs an image or version whose code is broken.",
    FaultCategory.MEMORY_LIMIT_TOO_LOW: (
        "The container's memory limit is below what the workload needs in normal operation, "
        "so it is OOM-killed without leaking."
    ),
    FaultCategory.MEMORY_LEAK: (
        "The process's memory grows without bound until it is OOM-killed or degrades."
    ),
    FaultCategory.CPU_LIMIT_TOO_LOW: (
        "The container's CPU limit throttles it below what the workload needs."
    ),
    FaultCategory.INSUFFICIENT_REPLICAS: (
        "The component is scaled below what its load needs, including to zero."
    ),
    FaultCategory.NETWORK_LATENCY: "Packets to or from the component are delayed.",
    FaultCategory.NETWORK_PACKET_LOSS: "Packets to or from the component are dropped.",
    FaultCategory.NETWORK_PARTITION: "The component cannot reach, or be reached by, a peer at all.",
    FaultCategory.MISSING_INDEX: (
        "A database query scans a table because an index it relies on is missing."
    ),
    FaultCategory.LOCK_CONTENTION: (
        "Queries wait on locks held by another session or a long-running transaction."
    ),
    FaultCategory.CONNECTION_POOL_TOO_SMALL: (
        "The component's connection pool to a dependency is too small for its load, so requests "
        "queue for a connection."
    ),
    FaultCategory.CACHE_MEMORY_TOO_LOW: (
        "The cache's memory ceiling is too low for its data set, so entries are evicted or "
        "rejected and reads fall through to the backing store."
    ),
    FaultCategory.DEPENDENCY_UNAVAILABLE: (
        "The component itself is down or unreachable, and its dependants fail because of it."
    ),
    FaultCategory.DISK_FULL: "The component has run out of disk space.",
    FaultCategory.DNS_FAILURE: "Name resolution fails or returns wrong addresses.",
    FaultCategory.EXPIRED_CREDENTIALS: (
        "A credential or certificate the component uses is expired or wrong."
    ),
    FaultCategory.TRAFFIC_SPIKE: "Incoming load is far above normal; nothing is misconfigured.",
}


class Scenario(StrEnum):
    """The incident classes the project brief asks the library to cover.

    These are symptom patterns, used only to report coverage. They play no part
    in scoring.
    """

    CRASH_LOOP = "crash_loop"
    OOM = "oom"
    CONSUMER_LAG = "consumer_lag"
    NETWORK = "network"
    BAD_CONFIG_PUSH = "bad_config_push"
    POOL_EXHAUSTION = "pool_exhaustion"
    REDIS_EVICTION = "redis_eviction"
    SLOW_QUERY = "slow_query"
    CASCADING_FAILURE = "cascading_failure"
    MULTI_CAUSE = "multi_cause"


class RemediationAction(StrEnum):
    """What can fix an incident: the agent's four write tools, or a human.

    MANUAL marks a fix none of the agent's tools can perform (recreating a
    database index, for example). The right outcome there is a correct
    recommendation, not a write action.
    """

    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    SCALE_DEPLOYMENT = "scale_deployment"
    RESTART_POD = "restart_pod"
    REVERT_CONFIG = "revert_config"
    MANUAL = "manual"
