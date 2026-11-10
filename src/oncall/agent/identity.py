"""The agent's identity in the cluster.

The agent's kubectl does not use the administrator's credentials. It uses a
kubeconfig that holds a short-lived token for a ServiceAccount whose Role
(deploy/rbac/agent-observer.yaml) allows `get` and `list` on a handful of
resource kinds in the `testbed` namespace, and nothing else: no writes, no
Secrets, no other namespace.

The tools already refuse anything but those reads. This is the second fence:
if a tool had a bug that let a write or another namespace through, the API
server would answer "forbidden".

Minting the token needs the administrator's kubeconfig. That happens here,
before the run starts; the tools are handed only the resulting file.
"""

import json
import os
from pathlib import Path

from oncall.chaos.kubectl import TESTBED, Runner

# The administrator's kubeconfig, written by `make cluster`.
ADMIN_KUBECONFIG = Path("sre-testbed.kubeconfig")
SERVICE_ACCOUNT = "oncall-agent-observer"
OBSERVER_KUBECONFIG = Path("agent-observer.kubeconfig")  # gitignored by *.kubeconfig
# A fresh token is minted for every run, so it only has to outlive one run.
# (Ten minutes is the shortest lifetime the API server grants.)
TOKEN_LIFETIME = "1h"


def write_observer_kubeconfig(admin: Runner, path: Path) -> str:
    """Write a kubeconfig for the read-only ServiceAccount; returns the token.

    The token is returned so the caller can tell the trace writer to refuse
    it. It is never printed.
    """
    # Where the cluster is and which CA signs for it, from the admin kubeconfig.
    view = json.loads(admin.run(["config", "view", "--raw", "--minify", "--output=json"]))
    cluster = view["clusters"][0]["cluster"]
    token = admin.run(
        ["--namespace", TESTBED, "create", "token", SERVICE_ACCOUNT, f"--duration={TOKEN_LIFETIME}"]
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
        "users": [{"name": SERVICE_ACCOUNT, "user": {"token": token}}],
        "contexts": [
            {
                "name": "observer",
                "context": {
                    "cluster": "sre-testbed",
                    "user": SERVICE_ACCOUNT,
                    "namespace": TESTBED,
                },
            }
        ],
        "current-context": "observer",
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
