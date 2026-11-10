"""The trace of a run: one JSON object per line, appended as things happen.

Everything needed to reconstruct the run is here: the system prompt and tool
definitions (once, at the start), every model response exactly as the API
returned it, every tool call with its parameters and result, and every
decision the loop made. The conversation is append-only, so the request sent
on turn N is the start record plus everything before it; each request record
carries a hash of what was actually sent, which is how a replay proves it
rebuilt the same request.

Lines are flushed as they are written. A run that crashes or is interrupted
leaves a trace up to that point.

The API key is never passed to this module. As a second line of defence the
writer is told the secret values that exist in the process and refuses to
write a line that contains one.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self


class SecretInTraceError(RuntimeError):
    """A record about to be written contained a secret. Nothing was written."""


class TraceWriter:
    def __init__(self, path: Path, secrets: tuple[str, ...] = ()) -> None:
        # An empty string would "match" every line.
        self._secrets = tuple(secret for secret in secrets if secret)
        self._seq = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("x", encoding="utf-8")  # never overwrite a trace

    def write(self, kind: str, **fields: object) -> None:
        self._seq += 1
        record = {
            "seq": self._seq,
            "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "kind": kind,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False)
        if any(secret in line for secret in self._secrets):
            raise SecretInTraceError(f"refusing to write a {kind!r} record: it contains a secret")
        self._file.write(line + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class MemoryTrace:
    """A trace kept in memory, for replays and tests."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, kind: str, **fields: object) -> None:
        self.records.append({"kind": kind, **fields})


def read_trace(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def digest(value: object) -> str:
    """SHA-256 of a JSON value in a canonical form (sorted keys, no spaces)."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
