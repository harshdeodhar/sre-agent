"""Shared fixtures for the incident-library tests. No cluster, no API key."""

import copy
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]

# The smallest incident that passes validation. Tests copy it and break one
# thing at a time, so each test shows exactly which rule it exercises.
VALID: dict[str, Any] = {
    "id": "example-incident",
    "title": "An example",
    "description": "Used by the parser tests.",
    "scenarios": ["bad_config_push"],
    "inject": [
        {"action": "set_env", "deployment": "order-service", "env": {"INVENTORY_TIMEOUT": "1ms"}}
    ],
    "cleanup": [{"action": "restore_deployment", "deployment": "order-service"}],
    "ground_truth": {
        "causes": [
            {
                "component": "order-service",
                "category": "bad_config_value",
                "explanation": "The timeout is too short.",
            }
        ]
    },
    "affected_services": ["order-service"],
    "detection": {
        "alerts": ["GatewayErrorRateHigh"],
        "deadline_seconds": 300,
        "evidence": [{"description": "errors", "source": "prometheus", "query": "up == 0"}],
    },
    "acceptable_remediations": [
        {"action": "rollback_deployment", "target": "order-service", "description": "Roll back."}
    ],
}


@pytest.fixture
def repo() -> Path:
    return REPO


@pytest.fixture
def valid() -> dict[str, Any]:
    """A fresh copy of a valid incident document, safe to modify."""
    return copy.deepcopy(VALID)


@pytest.fixture
def write_incident(tmp_path: Path) -> Callable[..., Path]:
    """Write a document to <tmp>/<name>.yaml and return the path."""

    def write(document: dict[str, Any], name: str | None = None) -> Path:
        path = tmp_path / f"{name or document['id']}.yaml"
        path.write_text(yaml.safe_dump(document))
        return path

    return write


class FakeKubectl:
    """Records every kubectl call and answers from a table of canned outputs.

    `answers` maps a tuple of leading arguments to the stdout to return; the
    longest matching prefix wins. Unmatched calls return an empty string.
    """

    def __init__(self, answers: dict[tuple[str, ...], str] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []
        self.stdins: list[str | None] = []

    def run(self, args: Sequence[str], *, stdin: str | None = None, timeout: float = 120) -> str:
        self.calls.append(list(args))
        self.stdins.append(stdin)
        best = ""
        best_length = -1
        for prefix, output in self.answers.items():
            if tuple(args[: len(prefix)]) == prefix and len(prefix) > best_length:
                best, best_length = output, len(prefix)
        return best


@pytest.fixture
def fake_kubectl() -> FakeKubectl:
    return FakeKubectl()
