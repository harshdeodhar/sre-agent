"""Command line for the incident library: python -m oncall.chaos <command>."""

import argparse
import json
import sys
from pathlib import Path

from oncall.chaos import check
from oncall.chaos.health import ClusterHealth
from oncall.chaos.incident import Incident, IncidentFileError, json_schema, load_library
from oncall.chaos.kubectl import CommandError, Kubectl
from oncall.chaos.steps import Executor
from oncall.chaos.telemetry import ALERT_RULES, Loki, Prometheus, load_alert_names

INCIDENTS_DIR = Path("incidents")
SCHEMA_FILE = INCIDENTS_DIR / "schema.json"
KUBECONFIG = Path("sre-testbed.kubeconfig")


def schema_text() -> str:
    """incidents/schema.json exactly as it should be on disk."""
    return json.dumps(json_schema(), indent=2, sort_keys=True) + "\n"


def library_problems(incidents: list[Incident], repo: Path) -> list[str]:
    """Checks that span files: alerts that no rule defines, and a stale schema."""
    problems = []
    known_alerts = load_alert_names(repo / ALERT_RULES)
    for incident in incidents:
        for alert in incident.detection.alerts:
            if alert not in known_alerts:
                problems.append(f"{incident.id}: alert {alert!r} is not defined in {ALERT_RULES}")
    schema_file = repo / SCHEMA_FILE
    if not schema_file.is_file() or schema_file.read_text() != schema_text():
        problems.append(f"{SCHEMA_FILE} is out of date; run: python -m oncall.chaos schema --write")
    return problems


def _find(incidents: list[Incident], incident_id: str) -> Incident:
    for incident in incidents:
        if incident.id == incident_id:
            return incident
    known = ", ".join(i.id for i in incidents)
    raise SystemExit(f"unknown incident {incident_id!r}; known: {known}")


def _environment(repo: Path) -> check.Environment:
    kubectl = Kubectl(repo / KUBECONFIG)
    prometheus = Prometheus()
    return check.Environment(
        kubectl=kubectl,
        executor=Executor(kubectl, repo),
        health=ClusterHealth(kubectl, prometheus, repo),
        prometheus=prometheus,
        loki=Loki(),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m oncall.chaos", description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="repository root (default: .)"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="list the incidents in the library")
    commands.add_parser("validate", help="parse every incident and cross-check the library")
    schema = commands.add_parser("schema", help="print the JSON schema of the incident format")
    schema.add_argument("--write", action="store_true", help=f"write it to {SCHEMA_FILE}")
    commands.add_parser("status", help="report whether the cluster is healthy right now")

    for name, text in (("inject", "inject one incident"), ("cleanup", "clean up one incident")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("incident")

    run = commands.add_parser(
        "check", help="inject, confirm it shows in telemetry, clean up, confirm recovery"
    )
    run.add_argument("incidents", nargs="*", help="incident ids (default with --all: every one)")
    run.add_argument("--all", action="store_true", help="check every incident in the library")
    run.add_argument("--out", type=Path, required=True, help="directory for the JSON records")

    summary = commands.add_parser("summary", help="print a Markdown table of check records")
    summary.add_argument("directory", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo: Path = args.repo.resolve()

    if args.command == "schema":
        if args.write:
            (repo / SCHEMA_FILE).write_text(schema_text())
        else:
            sys.stdout.write(schema_text())
        return 0

    if args.command == "summary":
        records = [
            check.CheckRecord.model_validate_json(path.read_text())
            for path in sorted(args.directory.glob("*.json"))
        ]
        sys.stdout.write(check.summary_table(records))
        return 0

    try:
        incidents = load_library(repo / INCIDENTS_DIR)
    except IncidentFileError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    if args.command == "list":
        for incident in incidents:
            causes = ", ".join(
                f"{c.component.value}/{c.category.value}" for c in incident.ground_truth.causes
            )
            print(f"{incident.id:45s} {causes}")
        return 0

    if args.command == "validate":
        problems = library_problems(incidents, repo)
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        if not problems:
            print(f"{len(incidents)} incidents valid")
        return 1 if problems else 0

    env = _environment(repo)
    try:
        if args.command == "status":
            problems = env.health.problems()
            print("\n".join(problems) if problems else "healthy")
            return 1 if problems else 0
        if args.command == "inject":
            check.inject(_find(incidents, args.incident), env.executor)
            print(f"injected {args.incident}")
            return 0
        if args.command == "cleanup":
            check.cleanup(_find(incidents, args.incident), env.executor)
            print(f"cleaned up {args.incident}")
            return 0
    except CommandError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    # check
    if args.all == bool(args.incidents):
        raise SystemExit("check: give incident ids, or --all (not both)")
    selected = incidents if args.all else [_find(incidents, i) for i in args.incidents]
    outcomes = []
    for incident in selected:
        record = check.run_check(incident, env)
        path = check.write_record(record, args.out)
        print(f"[{incident.id}] record written to {path}")
        outcomes.append(record.outcome)
    return 0 if all(outcome == "passed" for outcome in outcomes) else 1
