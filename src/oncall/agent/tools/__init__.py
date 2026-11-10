"""The agent's read-only tools.

`read_only_toolbox` is the one place that decides which tools exist and in
what order. The order is fixed because the tool definitions are the first part
of the cached prompt.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from oncall.agent.tools.base import Toolbox
from oncall.agent.tools.http import Fetch
from oncall.agent.tools.kube import kube_tools
from oncall.agent.tools.logs import logs_tools
from oncall.agent.tools.metrics import metrics_tools
from oncall.agent.tools.traces import traces_tools
from oncall.chaos.kubectl import Runner


def read_only_toolbox(fetch: Fetch, kubectl: Runner, now: Callable[[], float]) -> Toolbox:
    """Every tool available in `observe` mode. None of them can change anything.

    `fetch` reaches Prometheus, Loki and Tempo; `kubectl` runs as the agent's
    read-only identity; `now` returns the current Unix time.
    """
    return Toolbox(
        [
            *metrics_tools(fetch, now),
            *logs_tools(fetch, now),
            *traces_tools(fetch, now),
            *kube_tools(kubectl),
        ]
    )


def read_only_definitions() -> list[dict[str, Any]]:
    """The tools' definitions, without anything behind them.

    For replays and tests, which need to know what the model is told about
    the tools but must not reach a cluster.
    """
    return read_only_toolbox(_no_fetch, _NoKubectl(), lambda: 0.0).definitions()


def _no_fetch(url: str, params: Mapping[str, str]) -> object:
    raise RuntimeError("definitions only: this toolbox cannot make requests")


class _NoKubectl:
    def run(self, args: Sequence[str], *, stdin: str | None = None, timeout: float = 120) -> str:
        raise RuntimeError("definitions only: this toolbox cannot run kubectl")
