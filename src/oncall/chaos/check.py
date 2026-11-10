"""Prove on the live cluster that an incident works as its file claims.

For one incident: wait for a steady state, inject, watch until every expected
alert fires and every evidence query returns a result, clean up, then wait
until the cluster is healthy again and the evidence is gone. What was observed,
and when, is written to a JSON record. Those records are the evidence for
"each incident shows up in telemetry and cleans up fully".

No number in a record is computed from the incident file: every value is read
from Prometheus, Loki, the Kubernetes API or the clock during the run.
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from oncall.chaos.health import ClusterHealth, wait_until_stable
from oncall.chaos.incident import Evidence, Incident
from oncall.chaos.kubectl import TESTBED, CommandError, Runner
from oncall.chaos.steps import Executor
from oncall.chaos.telemetry import Loki, Prometheus, Sample, TelemetryError

# Before injection: no problem for a full minute, or the run does not start.
STEADY_HOLD_SECONDS = 60
STEADY_TIMEOUT_SECONDS = 600
# After cleanup: healthy, and every evidence query empty, for 30 s in a row.
RECOVERY_HOLD_SECONDS = 30
RECOVERY_TIMEOUT_SECONDS = 600
POLL_SECONDS = 5

_GATEWAY = 'job="api-gateway"'
# How the gateway looked just before injection. Recorded so a reader can judge
# whether the run started from a quiet system: on this laptop the host itself
# can cause errors and latency (docs/ARCHITECTURE.md, "Known limits").
BASELINE_QUERIES = {
    # `or vector(0)`: when there is no 5xx series at all the share is zero,
    # not "no result". A missing value in a record then always means the query
    # itself failed.
    "gateway_5xx_share_2m": (
        f'(sum(rate(http_server_requests_total{{{_GATEWAY},code=~"5.."}}[2m])) or vector(0))'
        f" / sum(rate(http_server_requests_total{{{_GATEWAY}}}[2m]))"
    ),
    "gateway_p99_seconds_2m": (
        "histogram_quantile(0.99, sum by (le) "
        f"(rate(http_server_request_duration_seconds_bucket{{{_GATEWAY}}}[2m])))"
    ),
}


class Signal(BaseModel):
    """One thing the incident file says should be observable, and what was seen."""

    kind: Literal["alert", "evidence"]
    name: str
    source: str | None = None
    query: str | None = None
    # Seconds from injection until first observed. None: never observed.
    seen_after_seconds: float | None = None
    value_when_seen: list[Sample] = []
    # Evidence only. Seconds from the start of cleanup until the query first
    # returned nothing again.
    cleared_after_seconds: float | None = None


class CheckRecord(BaseModel):
    incident: str
    # not_run: the cluster never reached a steady state, so nothing was injected.
    outcome: Literal["passed", "failed", "not_run"]
    failures: list[str] = []
    started_at: str
    host_load_average_1m: float
    steady_state_wait_seconds: float
    baseline: dict[str, float | None] = {}
    injected_at: str | None = None
    # Seconds from injection until every signal had been observed.
    detected_after_seconds: float | None = None
    signals: list[Signal] = []
    # Alerts that fired during the incident but are not in its `detection.alerts`.
    other_alerts: list[str] = []
    cleanup_started_at: str | None = None
    cleanup_steps_seconds: float | None = None
    # Seconds from the start of cleanup until the recovery hold was satisfied.
    recovered_after_seconds: float | None = None
    # kubectl output captured at detection time and after recovery.
    snapshots: dict[str, str] = {}


@dataclass
class Environment:
    """Everything a check talks to. Tests replace the pieces with fakes."""

    kubectl: Runner
    executor: Executor
    health: ClusterHealth
    prometheus: Prometheus
    loki: Loki
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = print
    _noted: set[str] = field(default_factory=set)

    def evidence_now(self, evidence: Evidence) -> list[Sample]:
        """Run an evidence query. A failed query counts as "nothing seen"."""
        client = self.prometheus if evidence.source == "prometheus" else self.loki
        try:
            return client.query(evidence.query)
        except TelemetryError as err:
            # Report each distinct failure once; a typo in a query should be
            # visible, not look like an absent signal.
            if str(err) not in self._noted:
                self._noted.add(str(err))
                self.log(f"  query failed: {err}")
            return []

    def firing_alerts(self) -> set[str]:
        try:
            return {a.name for a in self.prometheus.alerts() if a.state == "firing"}
        except TelemetryError:
            return set()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def inject(incident: Incident, executor: Executor) -> None:
    for step in incident.inject:
        executor.run(step)


def cleanup(incident: Incident, executor: Executor) -> None:
    for step in incident.cleanup:
        executor.run(step)


def run_check(incident: Incident, env: Environment) -> CheckRecord:
    """Run the full cycle for one incident and return what was observed."""
    record = CheckRecord(
        incident=incident.id,
        outcome="failed",
        started_at=_utc_now(),
        host_load_average_1m=round(os.getloadavg()[0], 2),
        steady_state_wait_seconds=0,
    )

    env.log(f"[{incident.id}] waiting for a steady state")
    steady, waited, problems = wait_until_stable(
        env.health.problems,
        hold_seconds=STEADY_HOLD_SECONDS,
        timeout_seconds=STEADY_TIMEOUT_SECONDS,
        interval_seconds=POLL_SECONDS,
        clock=env.clock,
        sleep=env.sleep,
    )
    record.steady_state_wait_seconds = round(waited, 1)
    if not steady:
        record.outcome = "not_run"
        record.failures = [f"no steady state before injection: {p}" for p in problems]
        return record
    record.baseline = _baseline(env)

    # A signal that is already there cannot be attributed to the injection, so
    # "seen after N seconds" would mean nothing. Do not inject in that case.
    already = [e.description for e in incident.detection.evidence if env.evidence_now(e)]
    if already:
        record.outcome = "not_run"
        record.failures = [f"evidence present before injection: {d}" for d in already]
        return record

    record.signals = [Signal(kind="alert", name=name) for name in incident.detection.alerts] + [
        Signal(kind="evidence", name=e.description, source=e.source, query=e.query)
        for e in incident.detection.evidence
    ]
    failures: list[str] = []
    try:
        env.log(f"[{incident.id}] injecting")
        record.injected_at = _utc_now()
        injected = env.clock()
        inject(incident, env.executor)
        record.other_alerts = _watch(incident, env, record.signals, injected)
        unseen = [s for s in record.signals if s.seen_after_seconds is None]
        if unseen:
            failures += [f"{s.kind} not observed before the deadline: {s.name}" for s in unseen]
        else:
            record.detected_after_seconds = max(s.seen_after_seconds or 0 for s in record.signals)
        record.snapshots.update(_snapshots(env, "at_detection"))
    except CommandError as err:
        failures.append(f"injection failed: {err}")
    finally:
        # Cleanup runs whatever happened above, including a failed injection
        # that may have applied only some of its steps.
        env.log(f"[{incident.id}] cleaning up")
        record.cleanup_started_at = _utc_now()
        cleanup_started = env.clock()
        try:
            cleanup(incident, env.executor)
        except CommandError as err:
            failures.append(f"cleanup failed: {err}")
        record.cleanup_steps_seconds = round(env.clock() - cleanup_started, 1)

    recovered, _, problems = wait_until_stable(
        lambda: _recovery_problems(incident, env, record.signals, cleanup_started),
        hold_seconds=RECOVERY_HOLD_SECONDS,
        timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
        interval_seconds=POLL_SECONDS,
        clock=env.clock,
        sleep=env.sleep,
    )
    if recovered:
        record.recovered_after_seconds = round(env.clock() - cleanup_started, 1)
    else:
        failures += [f"not healthy after cleanup: {p}" for p in problems]
    record.snapshots.update(_snapshots(env, "after_recovery"))

    record.failures = failures
    record.outcome = "failed" if failures else "passed"
    env.log(f"[{incident.id}] {record.outcome}" + "".join(f"\n  {f}" for f in failures))
    return record


def _baseline(env: Environment) -> dict[str, float | None]:
    baseline: dict[str, float | None] = {}
    for name, query in BASELINE_QUERIES.items():
        try:
            samples = env.prometheus.query(query)
        except TelemetryError:
            samples = []
        # None: not measured (the query failed). Never replaced by a default.
        baseline[name] = round(samples[0].value, 4) if samples else None
    return baseline


def _watch(
    incident: Incident, env: Environment, signals: list[Signal], injected: float
) -> list[str]:
    """Poll until every signal has been seen or the deadline passes.

    Returns the names of alerts that fired but were not expected.
    """
    expected = set(incident.detection.alerts)
    evidence = {e.description: e for e in incident.detection.evidence}
    others: set[str] = set()
    while True:
        elapsed = env.clock() - injected
        firing = env.firing_alerts()
        others |= firing - expected
        for signal in signals:
            if signal.seen_after_seconds is not None:
                continue
            if signal.kind == "alert":
                seen = signal.name in firing
            else:
                signal.value_when_seen = env.evidence_now(evidence[signal.name])
                seen = bool(signal.value_when_seen)
            if seen:
                signal.seen_after_seconds = round(elapsed, 1)
                env.log(f"  +{elapsed:4.0f}s {signal.kind}: {signal.name}")
        if all(s.seen_after_seconds is not None for s in signals):
            return sorted(others)
        if elapsed >= incident.detection.deadline_seconds:
            return sorted(others)
        env.sleep(POLL_SECONDS)


def _recovery_problems(
    incident: Incident, env: Environment, signals: list[Signal], cleanup_started: float
) -> list[str]:
    """Health problems, plus any evidence of the fault that is still visible."""
    problems = env.health.problems()
    by_name = {s.name: s for s in signals if s.kind == "evidence"}
    for item in incident.detection.evidence:
        if env.evidence_now(item):
            problems.append(f"evidence still present: {item.description}")
        elif by_name[item.description].cleared_after_seconds is None:
            by_name[item.description].cleared_after_seconds = round(
                env.clock() - cleanup_started, 1
            )
    return problems


def _snapshots(env: Environment, label: str) -> dict[str, str]:
    """What `kubectl get` shows, kept verbatim as human-readable evidence."""
    commands = {
        "pods": ["-n", TESTBED, "get", "pods", "-o", "wide"],
        "deployments": ["-n", TESTBED, "get", "deployments", "-o", "wide"],
    }
    captured = {}
    for name, args in commands.items():
        try:
            captured[f"{name}_{label}"] = env.kubectl.run(args)
        except CommandError as err:
            captured[f"{name}_{label}"] = f"kubectl failed: {err}"
    return captured


def write_record(record: CheckRecord, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record.incident}.json"
    path.write_text(record.model_dump_json(indent=2) + "\n")
    return path


def summary_table(records: list[CheckRecord]) -> str:
    """A Markdown table of check records. Every cell comes from a record."""

    def seconds(value: float | None) -> str:
        return "never" if value is None else f"{value:.0f} s"

    lines = [
        "| Incident | Outcome | Alerts (time to fire) | Evidence seen | All signals seen after | Healthy again after | 5xx / p99 before | Host load |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in records:
        alerts = ", ".join(
            f"{s.name} ({seconds(s.seen_after_seconds)})" for s in r.signals if s.kind == "alert"
        )
        evidence = [s for s in r.signals if s.kind == "evidence"]
        seen = sum(1 for s in evidence if s.seen_after_seconds is not None)
        share = r.baseline.get("gateway_5xx_share_2m")
        p99 = r.baseline.get("gateway_p99_seconds_2m")
        before = " / ".join(
            [
                "not measured" if share is None else f"{share * 100:.1f}%",
                "not measured" if p99 is None else f"{p99 * 1000:.0f} ms",
            ]
        )
        lines.append(
            f"| `{r.incident}` | {r.outcome} | {alerts} | {seen} of {len(evidence)} "
            f"| {seconds(r.detected_after_seconds)} | {seconds(r.recovered_after_seconds)} "
            f"| {before} | {r.host_load_average_1m} |"
        )
    return "\n".join(lines) + "\n"
