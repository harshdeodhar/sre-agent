# Scoring rubric

How an incident report is compared with an incident's ground truth.

This document was written in Phase 2, before any agent existed and before any
model was run against an incident, so that the rules could not be shaped by
results. The scoring code arrives in Phase 5 and implements exactly what is
written here; if a rule has to change after that, the change and its reason are
recorded at the end of this file.

Scoring is done by code, never by a model: the same report always gets the same
score.

## Vocabulary

A root cause is a pair: **the component that is broken** and **the kind of
fault**. Both come from closed lists defined in
[`src/oncall/chaos/taxonomy.py`](../src/oncall/chaos/taxonomy.py). Incident
files use those lists for ground truth, and the agent's report must use them
for its hypothesis, so comparing the two needs no interpretation of free text.

**Components:** `api-gateway`, `order-service`, `inventory-service`,
`notification-worker`, `postgres`, `redis`, `nats`.

**Fault categories.** A category names a cause, never a symptom. "Crash loop"
and "consumer lag" are not categories, because each can have several causes.

| Category | Definition |
|---|---|
| `bad_config_value` | An application setting of the component (environment variable or flag) has a wrong value: unparsable, or valid but harmful. Use a more specific category if one applies. |
| `bad_image` | The component runs an image or version whose code is broken. |
| `memory_limit_too_low` | The container's memory limit is below what the workload needs in normal operation, so it is OOM-killed without leaking. |
| `memory_leak` | The process's memory grows without bound until it is OOM-killed or degrades. |
| `cpu_limit_too_low` | The container's CPU limit throttles it below what the workload needs. |
| `insufficient_replicas` | The component is scaled below what its load needs, including to zero. |
| `network_latency` | Packets to or from the component are delayed. |
| `network_packet_loss` | Packets to or from the component are dropped. |
| `network_partition` | The component cannot reach, or be reached by, a peer at all. |
| `missing_index` | A database query scans a table because an index it relies on is missing. |
| `lock_contention` | Queries wait on locks held by another session or a long-running transaction. |
| `connection_pool_too_small` | The component's connection pool to a dependency is too small for its load, so requests queue for a connection. |
| `cache_memory_too_low` | The cache's memory ceiling is too low for its data set, so entries are evicted or rejected and reads fall through to the backing store. |
| `dependency_unavailable` | The component itself is down or unreachable, and its dependants fail because of it. |
| `disk_full` | The component has run out of disk space. |
| `dns_failure` | Name resolution fails or returns wrong addresses. |
| `expired_credentials` | A credential or certificate the component uses is expired or wrong. |
| `traffic_spike` | Incoming load is far above normal; nothing is misconfigured. |

A test keeps this table identical to the definitions in the code, and the agent
is given the same text.

**The list is longer than the set of faults the library injects, on purpose.**
If every category occurred in some incident, the list would be an answer key
and an agent could score well by guessing among the few that ever occur. A test
fails if fewer than five categories are unused.

## Choosing the pair

These rules apply to incident authors and to the agent alike.

1. **The most specific category wins.** Several categories are, mechanically,
   "a setting was changed": a memory limit, a replica count, a pool size.
   `bad_config_value` is only for application settings that no other category
   covers.
2. **The component is where the fix goes.** A timeout that is too short is a
   fault of the caller that holds the setting, even though the errors mention
   the service being called.
3. **For network faults, the component is the workload whose network path is
   degraded**, not the peers that notice it.
4. **A symptom is not a cause.** A database that is slow because a client
   floods it is not the broken component.

## Matching one cause

A reported cause is compared with a ground-truth cause:

| Component | Category | Credit |
|---|---|---|
| matches | matches | 1 |
| matches | differs | 0.5 |
| differs | matches | 0 |
| differs | differs | 0 |

Half credit for the right component alone, because knowing where the fault is
already narrows an on-call engineer's next step to one workload. No credit for
the right category on the wrong component: "a bad config value somewhere" sends
the engineer to roll back the wrong service.

## Matching a report

Ground truth is a set of causes (one for most incidents, two or more for
multi-cause incidents). A report also gives a set.

1. Each reported cause is paired with at most one ground-truth cause and vice
   versa. Full matches are paired first, then component-only matches among what
   is left.
2. **Recall** is the summed credit divided by the number of ground-truth causes.
   **Precision** is the summed credit divided by the number of reported causes.
   A report with no cause has precision and recall of zero.
3. Per incident, two numbers are reported:
   - **exact**: 1 if every ground-truth cause has a full match and the report
     contains no other cause, else 0;
   - **F1** of precision and recall, which gives partial credit.

Precision is what stops a report from listing every plausible cause: three
guesses of which one is right score a precision of one third.

The benchmark's headline "root-cause accuracy" is the mean of **exact** over
incidents. Mean F1 is reported next to it.

The free-text explanation and the stated confidence are recorded but not
scored.

## Remediation

Each incident lists its `acceptable_remediations`: an action from the agent's
write tools (`rollback_deployment`, `scale_deployment`, `restart_pod`,
`revert_config`) with the component it applies to, or `manual`.

`manual` means that no write tool the agent has can fix the fault, for example
a dropped database index. The correct outcome there is a report that recommends
the fix, and a write action taken anyway cannot be the right one.

Whether the system recovered is not judged from this list. It is measured by
the harness from telemetry, independently of what the agent claims: every
Deployment back at its intended state, no alert pending or firing, and the
incident's evidence queries returning nothing. The list is used to label the
actions the agent took or proposed as acceptable or not. How those labels are
aggregated into benchmark metrics is specified with the harness in Phase 5,
before any model is run.

## What is not scored

An incident's `scenarios` (crash loop, consumer lag, slow query and so on) are
symptom classes used to report what the library covers. They play no part in
scoring, and the agent never sees them or anything else in `incidents/`.

## Changes after Phase 2

None.
