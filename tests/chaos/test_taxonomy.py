"""docs/RUBRIC.md and the taxonomy in code must say the same thing."""

import re
from pathlib import Path

from oncall.chaos.taxonomy import CATEGORY_DEFINITIONS, Component, FaultCategory

RUBRIC = (Path(__file__).resolve().parents[2] / "docs" / "RUBRIC.md").read_text()


def test_every_category_has_a_definition() -> None:
    assert set(CATEGORY_DEFINITIONS) == set(FaultCategory)


def test_rubric_category_table_matches_the_code() -> None:
    """Same categories, same order, same wording as CATEGORY_DEFINITIONS."""
    rows = re.findall(r"^\| `([a-z_]+)` \| (.+) \|$", RUBRIC, flags=re.MULTILINE)
    assert rows == [(category.value, text) for category, text in CATEGORY_DEFINITIONS.items()]


def test_rubric_lists_every_component() -> None:
    (line,) = re.findall(r"\*\*Components:\*\* (.+?)\.\n\n", RUBRIC, flags=re.DOTALL)
    assert re.findall(r"`([a-z-]+)`", line) == [component.value for component in Component]
