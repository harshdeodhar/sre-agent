"""The agent's identities in the cluster.

The agent's kubectl never uses the administrator's credentials. It uses
kubeconfigs that hold short-lived tokens for two ServiceAccounts:

- `oncall-agent-observer` (deploy/rbac/agent-observer.yaml): `get` and `list`
  on a handful of resource kinds in `testbed`, and nothing else. Every read
  tool, and every read the write gate makes, runs as this account.
- `oncall-agent-operator` (deploy/rbac/agent-operator.yaml): only the writes
  the four write actions need, on the targets the policy allows. It is
  created only in propose and auto mode, and only the write gate is given it.

The code already refuses anything outside its rules. These accounts are the
second fence: if the code had a bug, the API server would answer "forbidden".

Minting a token needs the administrator's kubeconfig. That happens here,
before the run starts; the tools are handed only the resulting files.
"""

import json
import os
from pathlib import Path

from oncall.chaos.kubectl import TESTBED, Runner

# The administrator's kubeconfig, written by `make cluster`.
ADMIN_KUBECONFIG = Path("sre-testbed.kubeconfig")
SERVICE_ACCOUNT = "oncall-agent-observer"
OBSERVER_KUBECONFIG = Path("agent-observer.kubeconfig")  # gitignored by *.kubeconfig
OPERATOR_ACCOUNT = "oncall-agent-operator"
OPERATOR_KUBECONFIG = Path("agent-operator.kubeconfig")
# A fresh token is minted for every run, so it only has to outlive one run.
# (Ten minutes is the shortest lifetime the API server grants.)
TOKEN_LIFETIME = "1h"


def write_observer_kubeconfig(admin: Runner, path: Path) -> str:
    """Write a kubeconfig for the read-only ServiceAccount; returns the token.

    The token is returned so the caller can tell the trace writer to refuse
    it. It is never printed.
    """
    return _write_kubeconfig(admin, path, SERVICE_ACCOUNT)


def write_operator_kubeconfig(admin: Runner, path: Path) -> str:
    """Write a kubeconfig for the write ServiceAccount; returns the token."""
    return _write_kubeconfig(admin, path, OPERATOR_ACCOUNT)


def _write_kubeconfig(admin: Runner, path: Path, account: str) -> str:
    # Where the cluster is and which CA signs for it, from the admin kubeconfig.
    view = json.loads(admin.run(["config", "view", "--raw", "--minify", "--output=json"]))
    cluster = view["clusters"][0]["cluster"]
    token = admin.run(
        ["--namespace", TESTBED, "create", "token", account, f"--duration={TOKEN_LIFETIME}"]
    ).strip()
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": "sre-testbed",
                "cluster": {
                    "server": cluster["server"],
                    "certificate-authority-data": cluster["certificate-authority-data"],
                },
            }
        ],
        "users": [{"name": account, "user": {"token": token}}],
        "contexts": [
            {
                "name": account,
                "context": {
                    "cluster": "sre-testbed",
                    "user": account,
                    "namespace": TESTBED,
                },
            }
        ],
        "current-context": account,
    }
    # Created with owner-only permissions from the start, not chmod-ed after
    # writing. (JSON is valid YAML, so kubectl reads this file as it is.)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(config, file, indent=2)
        file.write("\n")
    # O_CREAT's mode does not apply to a file that already existed.
    path.chmod(0o600)
    return token
