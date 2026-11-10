"""Kubectl.run: an argument list, the repo's kubeconfig, and no shell."""

import subprocess
from pathlib import Path

import pytest

from oncall.chaos.kubectl import CommandError, Kubectl


class Recorder:
    """Stands in for subprocess.run and records how it was called."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.result = (returncode, stdout, stderr)
        self.args: list[str] | None = None
        self.kwargs: dict[str, object] = {}

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.args, self.kwargs = args, kwargs
        returncode, stdout, stderr = self.result
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)


@pytest.fixture
def kubeconfig(tmp_path: Path) -> Path:
    path = tmp_path / "sre-testbed.kubeconfig"
    path.write_text("apiVersion: v1\n")
    return path


def test_runs_an_argument_list_with_the_explicit_kubeconfig(
    kubeconfig: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(stdout="ok\n")
    monkeypatch.setattr(subprocess, "run", recorder)

    out = Kubectl(kubeconfig).run(["get", "pods", "-l", "app=x; rm -rf /"], stdin="{}")

    assert out == "ok\n"
    # A list, so no shell ever parses the values; and never shell=True.
    assert recorder.args == ["kubectl", "--kubeconfig", str(kubeconfig), "get", "pods", "-l"] + [
        "app=x; rm -rf /"
    ]
    assert "shell" not in recorder.kwargs
    assert recorder.kwargs["input"] == "{}"


def test_non_zero_exit_raises_with_stderr(
    kubeconfig: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", Recorder(returncode=1, stderr="not found\n"))
    with pytest.raises(CommandError, match="exited 1: not found"):
        Kubectl(kubeconfig).run(["get", "nothing"])


def test_timeout_raises_command_error(kubeconfig: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def expire(args: list[str], *, timeout: float, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(subprocess, "run", expire)
    with pytest.raises(CommandError, match="timed out after 5 s"):
        Kubectl(kubeconfig).run(["rollout", "status"], timeout=5)


def test_missing_kubeconfig_never_falls_back_to_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    monkeypatch.setattr(subprocess, "run", recorder)
    with pytest.raises(CommandError, match="is the cluster up"):
        Kubectl(tmp_path / "absent.kubeconfig").run(["get", "pods"])
    assert recorder.args is None  # kubectl was not run at all
