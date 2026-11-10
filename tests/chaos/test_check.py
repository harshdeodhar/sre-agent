"""run_check against a scripted cluster: what counts as passed, failed and not run.

The cluster, Prometheus and Loki are replaced by one small simulation, `World`,
in which a fault becomes visible some time after injection and disappears some
time after cleanup. Time only advances when the code under test sleeps.
"""

from pathlib import Path

import pytest

from oncall.chaos import check
from oncall.chaos.incident import CleanupStep, Incident, InjectStep
from oncall.chaos.kubectl import CommandError
from oncall.chaos.telemetry import Alert, Sample, TelemetryError

from .conftest import VALID, FakeKubectl

INCIDENT = Incident.model_validate(VALID)
ALERT = "GatewayErrorRateHigh"  # the alert VALID expects
EVIDENCE_QUERY = "up == 0"  # the evidence query in VALID


class World:
    """A cluster that reacts to inject and cleanup steps after fixed delays."""

    def __init__(self) -> None:
        self.now = 0.0
        self.injected_at: float | None = None
        self.cleaned_at: float | None = None
        self.steps: list[str] = []
        # Knobs the tests turn.
        self.alert_delay = 90.0  # seconds from injection until the alert fires
        self.evidence_delay = 20.0  # ... until the evidence query returns something
        self.clear_delay = 40.0  # seconds from cleanup until both are gone
        self.unhealthy_until = 0.0  # health problems before this time
        self.evidence_before_injection = False
        self.fail_inject = False
        self.fail_queries = False
        self.extra_alert: str | None = None

    # -- clock --
    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    # -- executor --
    def run(self, step: InjectStep | CleanupStep) -> None:
        self.steps.append(step.action)
        if step.action == "restore_deployment":
            self.cleaned_at = self.now
            return
        if self.fail_inject:
            raise CommandError("kubectl set env exited 1: forbidden")
        self.injected_at = self.now

    def _visible(self, delay: float) -> bool:
        """Is a signal that appears `delay` after injection visible now?"""
        if self.injected_at is None or self.now < self.injected_at + delay:
            return False
        return self.cleaned_at is None or self.now < self.cleaned_at + self.clear_delay

    # -- health --
    def problems(self) -> list[str]:
        found = ["deployment x is rolling"] if self.now < self.unhealthy_until else []
        return found + [f"alert {a.name} is {a.state}" for a in self.alerts()]

    # -- Prometheus and Loki --
    def alerts(self) -> list[Alert]:
        names = []
        if self._visible(self.alert_delay):
            names.append(ALERT)
            if self.extra_alert:
                names.append(self.extra_alert)
        return [Alert(name=n, state="firing", labels={}) for n in names]

    def query(self, expr: str) -> list[Sample]:
        if self.fail_queries:
            raise TelemetryError("connection refused")
        if expr != EVIDENCE_QUERY:  # a baseline query
            return [Sample(metric={}, value=0.25)]
        if self.evidence_before_injection or self._visible(self.evidence_delay):
            return [Sample(metric={"job": "x"}, value=1)]
        return []


@pytest.fixture
def world() -> World:
    return World()


def run(world: World) -> check.CheckRecord:
    env = check.Environment(
        kubectl=FakeKubectl({("-n", "testbed", "get", "pods"): "NAME READY\n"}),
        executor=world,  # type: ignore[arg-type]
        health=world,  # type: ignore[arg-type]
        prometheus=world,  # type: ignore[arg-type]
        loki=world,  # type: ignore[arg-type]
        clock=world.clock,
        sleep=world.sleep,
        log=lambda _line: None,
    )
    return check.run_check(INCIDENT, env)


def test_passes_when_signals_appear_and_then_clear(world: World) -> None:
    record = run(world)

    assert record.outcome == "passed"
    assert record.failures == []
    assert world.steps == ["set_env", "restore_deployment"]

    alert, evidence = record.signals
    # Injection happens after the 60 s steady-state hold; times are relative to it.
    assert record.steady_state_wait_seconds == 60
    assert (alert.kind, alert.name, alert.seen_after_seconds) == ("alert", ALERT, 90)
    assert (evidence.kind, evidence.seen_after_seconds) == ("evidence", 20)
    assert evidence.value_when_seen == [Sample(metric={"job": "x"}, value=1)]
    assert record.detected_after_seconds == 90
    # Gone 40 s after cleanup, then a 30 s hold.
    assert evidence.cleared_after_seconds == 40
    assert record.recovered_after_seconds == 70
    assert record.baseline == {"gateway_5xx_share_2m": 0.25, "gateway_p99_seconds_2m": 0.25}
    assert record.snapshots["pods_at_detection"] == "NAME READY\n"
    assert "pods_after_recovery" in record.snapshots


def test_not_run_when_the_cluster_never_settles(world: World) -> None:
    world.unhealthy_until = 10_000
    record = run(world)

    assert record.outcome == "not_run"
    assert record.failures == ["no steady state before injection: deployment x is rolling"]
    assert world.steps == []  # nothing was injected
    assert record.injected_at is None
    assert record.baseline == {}


def test_waits_for_a_steady_state_before_injecting(world: World) -> None:
    world.unhealthy_until = 100
    record = run(world)

    assert record.outcome == "passed"
    assert record.steady_state_wait_seconds == 160  # settled at 100 s, then held 60 s


def test_not_run_when_the_evidence_is_already_present(world: World) -> None:
    """A signal that predates the injection cannot be attributed to it."""
    world.evidence_before_injection = True
    record = run(world)

    assert record.outcome == "not_run"
    assert record.failures == ["evidence present before injection: errors"]
    assert world.steps == []


def test_fails_when_an_alert_does_not_fire_before_the_deadline(world: World) -> None:
    world.alert_delay = INCIDENT.detection.deadline_seconds + 60
    record = run(world)

    assert record.outcome == "failed"
    assert f"alert not observed before the deadline: {ALERT}" in record.failures
    assert record.detected_after_seconds is None
    assert record.signals[0].seen_after_seconds is None
    assert record.signals[1].seen_after_seconds == 20  # the evidence was still recorded
    assert world.steps[-1] == "restore_deployment"  # and cleanup still ran


def test_fails_when_the_fault_is_still_visible_after_cleanup(world: World) -> None:
    world.clear_delay = 10_000
    record = run(world)

    assert record.outcome == "failed"
    assert "not healthy after cleanup: evidence still present: errors" in record.failures
    assert f"not healthy after cleanup: alert {ALERT} is firing" in record.failures
    assert record.recovered_after_seconds is None
    assert record.signals[1].cleared_after_seconds is None


def test_cleanup_runs_even_when_injection_fails(world: World) -> None:
    world.fail_inject = True
    record = run(world)

    assert record.outcome == "failed"
    assert record.failures == ["injection failed: kubectl set env exited 1: forbidden"]
    assert world.steps == ["set_env", "restore_deployment"]


def test_unexpected_alerts_are_recorded_not_hidden(world: World) -> None:
    world.extra_alert = "GatewayLatencyHigh"
    record = run(world)

    assert record.outcome == "passed"
    assert record.other_alerts == ["GatewayLatencyHigh"]


def test_a_failing_query_is_not_mistaken_for_a_signal(world: World) -> None:
    world.fail_queries = True
    record = run(world)

    assert record.outcome == "failed"
    assert "evidence not observed before the deadline: errors" in record.failures
    # The baseline could not be measured either, and says so.
    assert record.baseline == {"gateway_5xx_share_2m": None, "gateway_p99_seconds_2m": None}


def test_record_round_trips_through_its_file(world: World, tmp_path: Path) -> None:
    record = run(world)
    path = check.write_record(record, tmp_path / "out")

    assert path == tmp_path / "out" / "example-incident.json"
    assert check.CheckRecord.model_validate_json(path.read_text()) == record


def test_summary_table_prints_only_what_the_records_hold(world: World) -> None:
    passed = run(world)
    unsettled = World()
    unsettled.unhealthy_until = 10_000
    not_run = run(unsettled)

    header, rule, first, second = check.summary_table([passed, not_run]).splitlines()

    assert header.count("|") == rule.count("|") == first.count("|") == second.count("|")
    assert "| passed |" in first
    assert f"{ALERT} (90 s)" in first
    assert "| 1 of 1 | 90 s | 70 s | 25.0% / 250 ms |" in first
    # A run that never injected has no timings and no baseline; the table
    # must not invent any.
    assert "| not_run |" in second
    assert "| never | never | not measured / not measured |" in second
