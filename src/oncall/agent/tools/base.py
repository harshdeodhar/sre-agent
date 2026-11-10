"""What a tool is, and the rules every tool follows.

A tool is a name, a description, a pydantic model of its parameters, and a
function that takes a validated instance of that model and returns text. The
model on the other end never supplies a command line: it fills in typed
fields, and the tool's own code decides what to run.

Two layers check the parameters. The API is given a strict JSON schema, so the
model's output is constrained to the right shape. Then the same pydantic model
validates the values here, because a schema cannot express everything (ranges,
name patterns) and this code should not trust its caller either way.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError

# The largest tool result handed to the model, in characters (roughly 1,500 to
# 2,000 tokens). The whole conversation is re-read on every model request, so
# one unbounded `kubectl get -o yaml` would be paid for on every later turn.
MAX_OUTPUT_CHARS = 6000


class ToolError(Exception):
    """A tool could not do what was asked. The message is shown to the model."""


class Params(BaseModel):
    """Base class of every tool's parameters.

    `extra="forbid"` is what puts `additionalProperties: false` into the JSON
    schema, which strict tool use requires. Fields have no defaults, so every
    one is required: the model states each choice instead of leaving it out.
    """

    model_config = ConfigDict(extra="forbid")


def between(low: int, high: int) -> AfterValidator:
    """A range check that stays out of the JSON schema.

    Strict tool schemas do not support `minimum` and `maximum`, so a range
    cannot be declared there. It is stated in the field's description for the
    model and enforced here.
    """

    def check(value: int) -> int:
        if not low <= value <= high:
            raise ValueError(f"must be between {low} and {high}")
        return value

    return AfterValidator(check)


@dataclass(frozen=True)
class Tool[P: Params]:
    name: str
    description: str
    params: type[P]
    run: Callable[[P], str]

    def definition(self) -> dict[str, Any]:
        """The tool as the Messages API wants it."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": strict_schema(self.params),
            "strict": True,
        }


@dataclass(frozen=True)
class ToolOutcome:
    text: str
    is_error: bool


class ToolRunner(Protocol):
    """What the loop needs from a set of tools. Replay substitutes a recording."""

    def definitions(self) -> list[dict[str, Any]]: ...

    def call(self, name: str, raw_input: dict[str, Any]) -> ToolOutcome: ...


class Toolbox:
    """The tools of one run, in a fixed order.

    The order matters: tool definitions are the start of the cached prompt
    prefix, and reordering them would invalidate the cache.
    """

    def __init__(self, tools: list[Tool[Any]]) -> None:
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("two tools share a name")

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.definition() for tool in self._tools.values()]

    def call(self, name: str, raw_input: dict[str, Any]) -> ToolOutcome:
        """Validate the parameters, run the tool, bound its output.

        A failure is returned, not raised: the model sees the message and can
        correct the call. Only ToolError is caught. Anything else is a bug in
        this code and should stop the run.
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolOutcome(f"There is no tool named {name!r}.", is_error=True)
        try:
            params = tool.params.model_validate(raw_input)
        except ValidationError as err:
            return ToolOutcome(f"Invalid parameters for {name}:\n{_problems(err)}", is_error=True)
        try:
            text = tool.run(params)
        except ToolError as err:
            return ToolOutcome(bound(str(err)), is_error=True)
        return ToolOutcome(bound(text), is_error=False)


def _problems(err: ValidationError) -> str:
    return "\n".join(
        f"- {'.'.join(str(part) for part in problem['loc']) or 'input'}: {problem['msg']}"
        for problem in err.errors()
    )


def bound(text: str, limit: int = MAX_OUTPUT_CHARS, keep: Literal["head", "tail"] = "head") -> str:
    """Cut `text` to `limit` characters and say how much was cut.

    Nothing is dropped silently: a model that does not know its view is partial
    will reason as if it had seen everything.
    """
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    note = f"[truncated: {omitted} of {len(text)} characters omitted; narrow the query to see more]"
    if keep == "tail":
        return f"{note}\n{text[-limit:]}"
    return f"{text[:limit]}\n{note}"


# ---- JSON schema for strict tool use ----------------------------------------------

# The only keywords the tools' schemas use. Strict mode supports a subset of
# JSON Schema; staying inside a small, known part of it means a schema that
# passes the test in tests/agent/test_tools_base.py is one the API accepts.
ALLOWED_SCHEMA_KEYWORDS = frozenset(
    {"type", "properties", "required", "additionalProperties", "description", "enum", "items"}
)


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The JSON schema of `model`, reduced to what strict tool use accepts.

    pydantic's schema is nearly right. Three things are changed: `$ref`s to
    shared definitions are replaced by the definition (so the schema reads top
    to bottom), `title` is dropped (noise), and every property is listed as
    required.
    """
    schema = model.model_json_schema()
    return _clean(schema, schema.get("$defs", {}))


def _clean(node: Any, defs: dict[str, Any]) -> Any:  # noqa: ANN401 - JSON is untyped
    if isinstance(node, list):
        return [_clean(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = defs[node["$ref"].removeprefix("#/$defs/")]
        # Keep a description written on the field over the one on the type.
        merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
        return _clean(merged, defs)
    cleaned = {}
    for key, value in node.items():
        if key in ("title", "$defs"):
            continue
        # Under "properties" the keys are field names, not schema keywords.
        cleaned[key] = (
            {name: _clean(sub, defs) for name, sub in value.items()}
            if key == "properties"
            else _clean(value, defs)
        )
    if cleaned.get("type") == "object":
        cleaned["required"] = list(cleaned.get("properties", {}))
        cleaned["additionalProperties"] = False
    return cleaned
