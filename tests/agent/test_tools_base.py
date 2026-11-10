"""The rules every tool follows: typed parameters, strict schemas, bounded output."""

from collections.abc import Callable
from typing import Annotated, Any

import pytest
from pydantic import Field

from oncall.agent.report import submit_report_definition
from oncall.agent.tools import read_only_definitions
from oncall.agent.tools.base import (
    ALLOWED_SCHEMA_KEYWORDS,
    MAX_OUTPUT_CHARS,
    Params,
    Tool,
    Toolbox,
    ToolError,
    between,
    bound,
)

ALL_DEFINITIONS = [*read_only_definitions(), submit_report_definition()]


class Echo(Params):
    text: str = Field(description="What to say.")
    times: Annotated[int, between(1, 3)] = Field(description="How often (1 to 3).")


def echo_box(run: Callable[[Echo], str] | None = None) -> Toolbox:
    def default(params: Echo) -> str:
        return " ".join([params.text] * params.times)

    return Toolbox([Tool("echo", "Repeats text.", Echo, run or default)])


def test_the_tool_list_is_what_the_architecture_says() -> None:
    assert [definition["name"] for definition in ALL_DEFINITIONS] == [
        "query_metrics",
        "list_metrics",
        "search_logs",
        "search_traces",
        "get_trace",
        "kube_get",
        "kube_describe",
        "kube_events",
        "deploy_history",
        "submit_report",
    ]


def _objects(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Every object schema in `schema`, at any depth."""
    found = [schema] if schema.get("type") == "object" else []
    for sub in schema.get("properties", {}).values():
        found += _objects(sub)
    if "items" in schema:
        found += _objects(schema["items"])
    return found


def _keywords(schema: dict[str, Any]) -> set[str]:
    used = set(schema)
    for sub in schema.get("properties", {}).values():
        used |= _keywords(sub)
    if "items" in schema:
        used |= _keywords(schema["items"])
    return used


@pytest.mark.parametrize("definition", ALL_DEFINITIONS, ids=lambda d: d["name"])
def test_every_tool_schema_is_strict(definition: dict[str, Any]) -> None:
    """What strict tool use requires of a schema, checked for every tool."""
    assert definition["strict"] is True
    schema = definition["input_schema"]
    assert schema["type"] == "object"
    for node in _objects(schema):
        assert node["additionalProperties"] is False
        # No optional parameters: the model states every choice.
        assert node["required"] == list(node["properties"])
    assert _keywords(schema) <= ALLOWED_SCHEMA_KEYWORDS


@pytest.mark.parametrize("definition", ALL_DEFINITIONS, ids=lambda d: d["name"])
def test_every_tool_and_parameter_is_described(definition: dict[str, Any]) -> None:
    assert len(definition["description"]) > 40
    for node in _objects(definition["input_schema"]):
        for name, parameter in node["properties"].items():
            assert parameter.get("description"), f"{definition['name']}.{name} has no description"


def test_no_tool_takes_a_command_or_a_namespace() -> None:
    """The model fills in typed fields. It never chooses the namespace or passes a command line."""
    for definition in read_only_definitions():
        names = set(definition["input_schema"]["properties"])
        assert not names & {"namespace", "command", "args", "argv", "shell", "flags"}


def test_definitions_are_identical_every_time() -> None:
    """They are the start of the cached prompt: any difference would cost a cache miss."""
    assert [*read_only_definitions(), submit_report_definition()] == ALL_DEFINITIONS


def test_a_valid_call_runs_the_tool() -> None:
    outcome = echo_box().call("echo", {"text": "hi", "times": 2})
    assert (outcome.text, outcome.is_error) == ("hi hi", False)


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ({"text": "hi"}, "times: Field required"),
        ({"text": "hi", "times": 2, "extra": 1}, "extra: Extra inputs are not permitted"),
        ({"text": "hi", "times": 9}, "times: Value error, must be between 1 and 3"),
        ({"text": ["hi"], "times": 1}, "text: Input should be a valid string"),
    ],
)
def test_invalid_parameters_are_reported_and_the_tool_does_not_run(
    raw: dict[str, Any], problem: str
) -> None:
    def must_not_run(params: Echo) -> str:
        raise AssertionError("the tool ran with invalid parameters")

    outcome = echo_box(must_not_run).call("echo", raw)
    assert outcome.is_error
    assert problem in outcome.text


def test_an_unknown_tool_is_an_error_result() -> None:
    outcome = echo_box().call("kubectl", {"command": "delete pods --all"})
    assert outcome.is_error
    assert "no tool named 'kubectl'" in outcome.text


def test_a_tool_error_is_shown_to_the_model() -> None:
    def fails(params: Echo) -> str:
        raise ToolError("the query was rejected: parse error")

    outcome = echo_box(fails).call("echo", {"text": "hi", "times": 1})
    assert (outcome.text, outcome.is_error) == ("the query was rejected: parse error", True)


def test_any_other_exception_is_a_bug_and_propagates() -> None:
    def broken(params: Echo) -> str:
        raise KeyError("oops")

    with pytest.raises(KeyError):
        echo_box(broken).call("echo", {"text": "hi", "times": 1})


def test_two_tools_cannot_share_a_name() -> None:
    tool = Tool("echo", "Repeats text.", Echo, lambda params: params.text)
    with pytest.raises(ValueError, match="share a name"):
        Toolbox([tool, tool])


def test_long_output_is_cut_and_says_so() -> None:
    def verbose(params: Echo) -> str:
        return "x" * (MAX_OUTPUT_CHARS + 500)

    outcome = echo_box(verbose).call("echo", {"text": "hi", "times": 1})
    assert outcome.text.startswith("x" * MAX_OUTPUT_CHARS + "\n[truncated: 500 of 6500 characters")


def test_bound_leaves_short_text_alone() -> None:
    assert bound("short", limit=10) == "short"
    assert bound("x" * 10, limit=10) == "x" * 10


def test_bound_can_keep_the_end() -> None:
    cut = bound("old " * 5 + "new", limit=3, keep="tail")
    assert cut.endswith("\nnew")
    assert cut.startswith("[truncated: 20 of 23 characters omitted")
