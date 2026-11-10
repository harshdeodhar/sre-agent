"""Shared fixtures for the agent tests. No cluster, no API key, no network."""

from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.chaos.conftest import FakeKubectl

REPO = Path(__file__).resolve().parents[2]

# The time the fake clock shows: 2026-11-10 12:00:00 UTC.
NOW = 1_794_312_000.0


class FakeFetch:
    """Stands in for the HTTP GET: records each request, returns a canned body."""

    def __init__(self, body: object = None) -> None:
        self.body = body
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, params: Mapping[str, str]) -> object:
        self.requests.append((url, dict(params)))
        return self.body


@pytest.fixture
def fetch() -> FakeFetch:
    return FakeFetch()


@pytest.fixture
def kubectl() -> FakeKubectl:
    return FakeKubectl()


def now() -> float:
    return NOW
