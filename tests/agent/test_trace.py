"""The trace file: append-only JSON lines, and the refusal to write a secret."""

import json
from pathlib import Path

import pytest

from oncall.agent.trace import SecretInTraceError, TraceWriter, digest, read_trace

SECRET = "sk-ant-not-a-real-key-0123456789"


def test_records_are_numbered_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "run" / "trace.jsonl"  # the directory is created
    with TraceWriter(path) as trace:
        trace.write("run_start", model="m", page="Paged: ünïcode")
        trace.write("tool_call", call_id="c1", input={"minutes": 5})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert [(r["seq"], r["kind"]) for r in records] == [(1, "run_start"), (2, "tool_call")]
    assert records[0]["page"] == "Paged: ünïcode"
    assert records[1]["input"] == {"minutes": 5}
    assert all(r["at"].endswith("+00:00") for r in records)
    assert read_trace(path) == records


def test_each_record_is_on_disk_as_soon_as_it_is_written(tmp_path: Path) -> None:
    """A run that crashes leaves its trace up to that point."""
    path = tmp_path / "trace.jsonl"
    trace = TraceWriter(path)
    trace.write("run_start", model="m")
    assert len(read_trace(path)) == 1  # read before the writer is closed
    trace.close()


def test_an_existing_trace_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text('{"kind": "run_start"}\n')
    with pytest.raises(FileExistsError):
        TraceWriter(path)
    assert path.read_text() == '{"kind": "run_start"}\n'


def test_a_record_containing_a_secret_is_not_written(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    with TraceWriter(path, secrets=(SECRET, "")) as trace:
        trace.write("run_start", model="m")
        with pytest.raises(SecretInTraceError) as raised:
            # Nested inside a tool result, as a leak would be.
            trace.write("tool_call", output={"env": [f"ANTHROPIC_API_KEY={SECRET}"]})
        # An empty "secret" must not block every line.
        trace.write("run_end", outcome="report")
    assert SECRET not in str(raised.value)
    assert SECRET not in path.read_text()
    assert [r["kind"] for r in read_trace(path)] == ["run_start", "run_end"]


def test_the_digest_does_not_depend_on_key_order() -> None:
    assert digest({"a": 1, "b": [1, {"c": 2, "d": 3}]}) == digest(
        {"b": [1, {"d": 3, "c": 2}], "a": 1}
    )


def test_the_digest_changes_with_the_value() -> None:
    assert digest([{"role": "user", "content": "a"}]) != digest([{"role": "user", "content": "b"}])
    # List order is part of a conversation.
    assert digest([1, 2]) != digest([2, 1])
    assert len(digest("x")) == 64
