"""Checks on the incident files that are actually checked in under incidents/."""

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from oncall.chaos.cli import INCIDENTS_DIR, SCHEMA_FILE, library_problems, schema_text
from oncall.chaos.incident import Incident, PostgresSQL, load_library
from oncall.chaos.taxonomy import FaultCategory
from oncall.chaos.telemetry import ALERT_RULES, load_alert_names

REPO = Path(__file__).resolve().parents[2]
LIBRARY = load_library(REPO / INCIDENTS_DIR)


def test_library_is_not_empty() -> None:
    # Guards every parametrized test below against passing vacuously.
    assert len(LIBRARY) >= 8


def test_library_has_no_cross_file_problems() -> None:
    assert library_problems(LIBRARY, REPO) == []


def test_checked_in_schema_matches_the_models() -> None:
    """incidents/schema.json is generated; a stale copy would document a format that is not enforced."""
    assert (REPO / SCHEMA_FILE).read_text() == schema_text()


@pytest.mark.parametrize(
    "path", sorted((REPO / INCIDENTS_DIR).glob("*.yaml")), ids=lambda p: p.stem
)
def test_file_validates_against_the_json_schema(path: Path) -> None:
    """Independent of pydantic: the schema file alone must accept every incident."""
    schema = json.loads((REPO / SCHEMA_FILE).read_text())
    jsonschema.validate(yaml.safe_load(path.read_text()), schema)


def test_json_schema_rejects_an_unknown_key() -> None:
    """The schema has teeth: additionalProperties is false, as in the models."""
    schema = json.loads((REPO / SCHEMA_FILE).read_text())
    document = yaml.safe_load((REPO / INCIDENTS_DIR / f"{LIBRARY[0].id}.yaml").read_text())
    document["hint_for_the_agent"] = "look at redis"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


@pytest.mark.parametrize("incident", LIBRARY, ids=lambda i: i.id)
def test_expected_alerts_are_defined(incident: Incident) -> None:
    known = load_alert_names(REPO / ALERT_RULES)
    assert set(incident.detection.alerts) <= known


def test_taxonomy_is_larger_than_the_set_of_injected_faults() -> None:
    """If every category occurred in the library, the category list would be an answer key."""
    used = {cause.category for incident in LIBRARY for cause in incident.ground_truth.causes}
    unused = set(FaultCategory) - used
    assert len(unused) >= 5, f"only {sorted(unused)} are never the answer"


def test_alert_names_do_not_name_a_cause() -> None:
    """Alerts are symptoms. A rule named after a fault category would hand the agent the answer."""
    for name in load_alert_names(REPO / ALERT_RULES):
        for category in FaultCategory:
            # "MissingIndex" and "missing_index" compare equal this way.
            assert category.value.replace("_", "") not in name.lower(), (name, category)


@pytest.mark.parametrize("incident", LIBRARY, ids=lambda i: i.id)
def test_sql_steps_are_idempotent(incident: Incident) -> None:
    """Cleanup may run twice, or after a failed injection, so DDL must tolerate both."""
    for step in [*incident.inject, *incident.cleanup]:
        if isinstance(step, PostgresSQL) and step.sql.upper().startswith(("DROP", "CREATE")):
            assert "IF EXISTS" in step.sql.upper() or "IF NOT EXISTS" in step.sql.upper()
