"""The agent cannot see the answer key, and cannot run a shell.

CLAUDE.md, "Never tilt the benchmark": the agent must never read `incidents/`.
These tests check the agent's source for the ways that could happen: importing
the code that parses incidents, or naming the directory.
"""

import ast
from pathlib import Path

import pytest

from tests.agent.conftest import REPO

AGENT = REPO / "src" / "oncall" / "agent"
SOURCES = sorted(AGENT.rglob("*.py"))

# The two modules of the chaos package the agent may use: the kubectl wrapper
# and the vocabulary of components and fault categories. Neither reads an
# incident. Everything else there (incident parsing, injection, checks) is
# the benchmark's side.
ALLOWED_CHAOS_MODULES = {"oncall.chaos.kubectl", "oncall.chaos.taxonomy"}
# Only these start processes or open sockets, and each is a thin, typed wrapper.
ALLOWED_TO_IMPORT = {
    "subprocess": set(),  # kubectl runs through oncall.chaos.kubectl
    "urllib.request": {"tools/http.py"},
    "anthropic": {"llm.py"},
}


def imports(path: Path) -> set[str]:
    """Every module `path` imports, as dotted names."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            # `from oncall.chaos import incident` names the module in the alias.
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


def test_there_are_sources_to_check() -> None:
    assert len(SOURCES) > 10


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(AGENT)))
def test_the_agent_imports_nothing_that_reads_incidents(path: Path) -> None:
    for module in imports(path):
        if module == "oncall.chaos" or module.startswith("oncall.chaos."):
            # `from oncall.chaos.kubectl import Runner` also yields
            # "oncall.chaos.kubectl.Runner": compare the module part.
            assert any(
                module == allowed or module.startswith(allowed + ".")
                for allowed in ALLOWED_CHAOS_MODULES
            ), f"{path.name} imports {module}"
        assert not module.startswith("oncall.bench"), f"{path.name} imports {module}"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(AGENT)))
def test_the_agent_source_never_names_the_incident_library(path: Path) -> None:
    """No string the code uses is the incident directory or a path into it.

    Docstrings are left out: the package's own docstring says, in words, that
    the agent must not read `incidents/`.
    """
    tree = ast.parse(path.read_text())
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            assert node.value != "incidents", path.name
            assert "incidents/" not in node.value, path.name
            assert "ground_truth" not in node.value, path.name
        # Nor an attribute or variable that reaches for the answer.
        if isinstance(node, ast.Attribute):
            assert node.attr != "ground_truth", path.name


def test_the_modules_the_agent_borrows_do_not_read_incidents_either() -> None:
    for name in ALLOWED_CHAOS_MODULES:
        path = REPO / "src" / Path(*name.split(".")).with_suffix(".py")
        assert not any(module.startswith("oncall") for module in imports(path)), name
        assert "incidents" not in path.read_text()


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(AGENT)))
def test_processes_and_sockets_are_opened_in_one_place_each(path: Path) -> None:
    relative = str(path.relative_to(AGENT))
    imported = imports(path)
    for module, allowed_in in ALLOWED_TO_IMPORT.items():
        if relative not in allowed_in:
            assert not any(name == module or name.startswith(module + ".") for name in imported), (
                f"{relative} imports {module}"
            )
    text = path.read_text()
    assert "os.system" not in text
    assert "os.popen" not in text
    assert "shell=" not in text


def test_kubectl_is_never_run_through_a_shell() -> None:
    assert "shell=" not in (REPO / "src" / "oncall" / "chaos" / "kubectl.py").read_text()
