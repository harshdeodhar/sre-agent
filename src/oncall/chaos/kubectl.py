"""The one place this package starts a subprocess.

Every command is an argument list handed straight to the kernel. There is no
shell in between, so nothing in an argument is ever interpreted: a value like
`1ms; rm -rf /` would be passed to kubectl as that literal string.
"""

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# The only namespace the injector changes workloads in.
TESTBED = "testbed"
# Where the netem helper runs. The agent has no access to it.
CHAOS = "chaos"


class CommandError(RuntimeError):
    """A command exited non-zero. Carries its output for the error report."""


class Runner(Protocol):
    """What steps.py needs from kubectl. Tests substitute a recording fake."""

    def run(self, args: Sequence[str], *, stdin: str | None = None, timeout: float = 120) -> str:
        """Run `kubectl <args>` and return its stdout."""
        ...


@dataclass(frozen=True)
class Kubectl:
    """Runs kubectl against the repo-local kubeconfig and nothing else.

    The kubeconfig is passed explicitly on every call. ~/.kube/config and the
    KUBECONFIG environment variable are never consulted, so this code cannot
    inject a fault into some other cluster the user happens to be logged in to.
    """

    kubeconfig: Path

    def run(self, args: Sequence[str], *, stdin: str | None = None, timeout: float = 120) -> str:
        if not self.kubeconfig.is_file():
            raise CommandError(f"{self.kubeconfig} not found; is the cluster up? (make up)")
        argv = ["kubectl", "--kubeconfig", str(self.kubeconfig), *args]
        try:
            result = subprocess.run(
                argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as err:
            raise CommandError(f"kubectl {' '.join(args)} timed out after {timeout:g} s") from err
        if result.returncode != 0:
            raise CommandError(
                f"kubectl {' '.join(args)} exited {result.returncode}: {result.stderr.strip()}"
            )
        return result.stdout
