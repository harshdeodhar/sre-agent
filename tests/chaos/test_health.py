"""What "healthy" means, and the hold that turns one clean reading into "steady"."""

from collections.abc import Callable

import pytest

from oncall.chaos.health import Deployment, alert_problems, deployment_problems, wait_until_stable
from oncall.chaos.telemetry import Alert

EXPECTED = {"api-gateway": 2}


def deployment(
    *,
    spec: int = 2,
    generation: int = 3,
    observed: int = 3,
    replicas: int = 2,
    updated: int = 2,
    available: int = 2,
) -> Deployment:
    return Deployment.model_validate(
        {
            "metadata": {"name": "api-gateway", "generation": generation},
            "spec": {"replicas": spec},
            "status": {
                "observedGeneration": observed,
                "replicas": replicas,
                "updatedReplicas": updated,
                "availableReplicas": available,
            },
        }
    )


def test_deployment_in_its_intended_state_has_no_problems() -> None:
    assert deployment_problems([deployment()], EXPECTED) == []


@pytest.mark.parametrize(
    ("live", "expected"),
    [
        pytest.param(
            deployment(spec=0, replicas=0, updated=0, available=0), "scaled to 0", id="scaled down"
        ),
        pytest.param(
            deployment(spec=3, replicas=3, updated=3, available=3), "scaled to 3", id="scaled up"
        ),
        pytest.param(deployment(observed=2), "has not acted on", id="change not yet observed"),
        pytest.param(
            deployment(replicas=3, updated=1), "3 exist, 1 updated", id="rollout in progress"
        ),
        pytest.param(deployment(available=1), "1 available", id="replica not ready"),
    ],
)
def test_deployment_problems(live: Deployment, expected: str) -> None:
    (problem,) = deployment_problems([live], EXPECTED)
    assert expected in problem


def test_missing_deployment_is_a_problem() -> None:
    assert deployment_problems([], EXPECTED) == ["deployment api-gateway does not exist"]


def test_status_fields_omitted_by_the_api_count_as_zero() -> None:
    """The API leaves out zero-valued status fields, e.g. while no replica is available."""
    live = Deployment.model_validate(
        {
            "metadata": {"name": "api-gateway", "generation": 1},
            "spec": {"replicas": 2},
            "status": {},
        }
    )
    assert len(deployment_problems([live], EXPECTED)) == 1


def test_pending_alerts_count_as_problems() -> None:
    alerts = [
        Alert(name="GatewayLatencyHigh", state="pending", labels={"route": "a"}),
        Alert(name="GatewayLatencyHigh", state="pending", labels={"route": "b"}),
        Alert(name="ConsumerLagHigh", state="firing", labels={}),
    ]
    assert alert_problems(alerts) == [
        "alert ConsumerLagHigh is firing",
        "alert GatewayLatencyHigh is pending",
    ]


class FakeTime:
    """A clock that only advances when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def probe_from(readings: list[list[str]]) -> Callable[[], list[str]]:
    """A probe that returns the readings in turn, then the last one for ever."""
    remaining = list(readings)

    def probe() -> list[str]:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return probe


def test_stable_once_clean_for_the_whole_hold() -> None:
    time = FakeTime()
    probe = probe_from([[]])
    stable, waited, problems = wait_until_stable(
        probe,
        hold_seconds=30,
        timeout_seconds=600,
        clock=time.clock,
        sleep=time.sleep,
    )
    assert (stable, waited, problems) == (True, 30, [])


def test_a_problem_restarts_the_hold() -> None:
    time = FakeTime()
    # Clean at 0, 5, 10 s; a problem at 15 s; clean from 20 s on.
    probe = probe_from([[], [], [], ["alert X is pending"], []])
    stable, waited, _ = wait_until_stable(
        probe,
        hold_seconds=30,
        timeout_seconds=600,
        clock=time.clock,
        sleep=time.sleep,
    )
    assert stable
    assert waited == 50  # 20 s + a full 30 s hold, not 30 s from the start


def test_gives_up_at_the_timeout_and_reports_the_last_problems() -> None:
    time = FakeTime()
    probe = probe_from([["deployment x is scaled to 0, not 2"]])
    stable, waited, problems = wait_until_stable(
        probe,
        hold_seconds=30,
        timeout_seconds=60,
        clock=time.clock,
        sleep=time.sleep,
    )
    assert (stable, waited) == (False, 60)
    assert problems == ["deployment x is scaled to 0, not 2"]
