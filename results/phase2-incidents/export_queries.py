"""Export the result of PromQL range queries to a JSON file.

Usage: export_queries.py START END OUT_FILE QUERY [QUERY ...]   (RFC 3339, UTC)

Prometheus keeps its data on an emptyDir, so a measurement that a document
quotes is exported here to outlive the pod.
"""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PROMETHEUS = "http://127.0.0.1:9090"
STEP_SECONDS = 5


def query_range(expr: str, start: str, end: str) -> list[dict[str, object]]:
    params = {"query": expr, "start": start, "end": end, "step": str(STEP_SECONDS)}
    url = f"{PROMETHEUS}/api/v1/query_range?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)["data"]["result"]


def main() -> None:
    start, end, out, queries = sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4:]
    export = {
        "window": {"start": start, "end": end, "step_seconds": STEP_SECONDS},
        "queries": [{"query": q, "result": query_range(q, start, end)} for q in queries],
    }
    out.write_text(json.dumps(export, indent=1) + "\n")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
