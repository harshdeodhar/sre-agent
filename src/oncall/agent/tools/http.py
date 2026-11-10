"""HTTP GET returning JSON, for the Prometheus, Loki and Tempo query APIs.

The standard library is enough for this: three local endpoints, GET only.
"""

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping

from oncall.agent.tools.base import ToolError

# Where the cluster's telemetry is published on this machine (see
# deploy/cluster). Bound to 127.0.0.1 only.
PROMETHEUS_URL = "http://127.0.0.1:9090"
LOKI_URL = "http://127.0.0.1:3100"
TEMPO_URL = "http://127.0.0.1:3200"

TIMEOUT_SECONDS = 30

# (url, query parameters) -> the decoded JSON body. Tests pass a fake.
Fetch = Callable[[str, Mapping[str, str]], object]


def fetch_json(url: str, params: Mapping[str, str]) -> object:
    """GET `url` and decode the JSON body.

    A rejected query is the common failure (a PromQL syntax error is a 400),
    and the server's explanation is in the response body, so the body is put
    into the error for the model to read.
    """
    full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    try:
        with urllib.request.urlopen(full_url, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace").strip()
        raise ToolError(f"HTTP {err.code} from {url}: {detail}") from err
    except (OSError, http.client.HTTPException) as err:
        # Refused, reset, timed out (all OSError), or a connection closed
        # before a response arrived (HTTPException). The model is told, and
        # can retry or use another source; the run does not crash.
        raise ToolError(f"could not reach {url}: {err}") from err
    try:
        return json.loads(body)
    except json.JSONDecodeError as err:
        raise ToolError(f"{url} did not return JSON: {body[:200]!r}") from err
