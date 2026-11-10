"""Command line for the agent: python -m oncall.agent <command>."""

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from oncall.agent.identity import ADMIN_KUBECONFIG, OBSERVER_KUBECONFIG, write_observer_kubeconfig
from oncall.agent.llm import AnthropicClient, ConfigurationError, api_key_from_environment
from oncall.agent.loop import Limits, RunConfig
from oncall.agent.page import FiringAlert, firing_alerts, render_page
from oncall.agent.pricing import UnknownPriceError, price_of
from oncall.agent.replay import ReplayMismatchError, replay
from oncall.agent.report import IncidentReport, render_markdown
from oncall.agent.runner import REPORT_MARKDOWN, TRACE_FILE, build_report, investigate
from oncall.agent.tools import read_only_definitions, read_only_toolbox
from oncall.agent.tools.base import ToolError
from oncall.agent.tools.http import fetch_json
from oncall.agent.trace import read_trace
from oncall.chaos.kubectl import CommandError, Kubectl

DEFAULT_MODEL = "claude-haiku-5-5"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
RUNS_DIR = Path("runs") / "agent"
# Prometheus evaluates its rules every 10 seconds.
ALERT_POLL_SECONDS = 10


def _parser() -> argparse.ArgumentParser:
    defaults = Limits()
    parser = argparse.ArgumentParser(prog="python -m oncall.agent", description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="repository root (default: .)"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="investigate the alerts that are firing now")
    run.add_argument("--model", default=DEFAULT_MODEL, help=f"model ID (default: {DEFAULT_MODEL})")
    run.add_argument("--effort", choices=EFFORTS, default="medium", help="thinking effort")
    run.add_argument("--max-tool-calls", type=int, default=defaults.max_tool_calls)
    run.add_argument("--max-seconds", type=float, default=defaults.max_seconds)
    run.add_argument(
        "--max-cost", type=float, default=defaults.max_cost_usd, help="US dollars, for this run"
    )
    run.add_argument(
        "--wait",
        type=float,
        default=0,
        metavar="SECONDS",
        help="wait up to this long for an alert to fire (default: do not wait)",
    )
    run.add_argument("--out", type=Path, help="run directory (default: runs/agent/<time>-<model>)")

    again = commands.add_parser("replay", help="re-run the loop from a recorded trace, offline")
    again.add_argument("trace", type=Path, help="a trace.jsonl file")
    again.add_argument("--report", action="store_true", help="print the re-derived report")

    commands.add_parser(
        "kubeconfig", help=f"write {OBSERVER_KUBECONFIG}, the agent's read-only cluster identity"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo: Path = args.repo.resolve()
    try:
        if args.command == "run":
            return _run(args, repo)
        if args.command == "replay":
            return _replay(args)
        write_observer_kubeconfig(Kubectl(repo / ADMIN_KUBECONFIG), repo / OBSERVER_KUBECONFIG)
        print(f"wrote {OBSERVER_KUBECONFIG}")
        return 0
    except (ConfigurationError, UnknownPriceError, CommandError, ToolError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace, repo: Path) -> int:
    # Everything that can be wrong with the setup is checked before the first
    # paid request: the key, the price table, the model ID.
    key = api_key_from_environment()
    price_of(args.model)
    client = AnthropicClient(key)
    client.check_model(args.model)

    alerts = _wait_for_alerts(args.wait)
    if not alerts:
        print("No alert is firing: there is nothing to investigate.")
        return 1

    # The tools get the read-only identity, never the administrator's.
    token = write_observer_kubeconfig(Kubectl(repo / ADMIN_KUBECONFIG), repo / OBSERVER_KUBECONFIG)
    tools = read_only_toolbox(fetch_json, Kubectl(repo / OBSERVER_KUBECONFIG), time.time)

    now = datetime.now(UTC)
    config = RunConfig(
        model=args.model,
        effort=args.effort,
        limits=Limits(
            max_tool_calls=args.max_tool_calls,
            max_seconds=args.max_seconds,
            max_cost_usd=args.max_cost,
        ),
    )
    directory = args.out or repo / RUNS_DIR / f"{now:%Y%m%dT%H%M%SZ}-{args.model}"
    print(f"Paged for: {', '.join(alert.name for alert in alerts)}")
    print(f"Investigating with {args.model}; trace in {directory / TRACE_FILE}")
    report = investigate(
        config, client, tools, render_page(alerts, now), directory, secrets=(key, token)
    )
    _print_outcome(report)
    print(f"Report: {directory / REPORT_MARKDOWN}")
    return 0 if report.submission else 1


def _wait_for_alerts(wait_seconds: float) -> list[FiringAlert]:
    deadline = time.monotonic() + wait_seconds
    while True:
        alerts = firing_alerts(fetch_json)
        if alerts or time.monotonic() >= deadline:
            return alerts
        time.sleep(ALERT_POLL_SECONDS)


def _replay(args: argparse.Namespace) -> int:
    records = read_trace(args.trace)
    try:
        replayed = replay(records, read_only_definitions())
    except ReplayMismatchError as err:
        print(f"replay failed: {err}", file=sys.stderr)
        return 1
    session = next(record for record in records if record["kind"] == "session")
    report = build_report(
        replayed.page,
        replayed.config,
        replayed.result,
        datetime.fromisoformat(session["started_at"]),
    )
    if args.report:
        sys.stdout.write(render_markdown(report))
    else:
        _print_outcome(report)
    if replayed.prompt_changed:
        print(
            "note: the system prompt or a tool definition has changed since this trace was "
            "recorded",
            file=sys.stderr,
        )
    for difference in replayed.differences:
        print(f"differs from the recording: {difference}", file=sys.stderr)
    if not replayed.differences:
        print("Replay matches the recording.", file=sys.stderr)
    return 1 if replayed.differences else 0


def _print_outcome(report: IncidentReport) -> None:
    run = report.run
    print(f"Outcome: {run.outcome} ({run.outcome_detail})")
    if report.submission:
        for cause in report.submission.root_causes:
            print(f"Root cause: {cause.component} / {cause.category} ({cause.confidence})")
        if not report.submission.root_causes:
            print("Root cause: none established")
    print(
        f"{run.tool_calls} tool calls, {run.model_requests} model requests, "
        f"{run.duration_seconds:.0f} s"
    )
    print(
        f"Tokens: {run.input_tokens} input, {run.cache_write_tokens} cache write, "
        f"{run.cache_read_tokens} cache read, {run.output_tokens} output"
    )
    print(f"Cost: ${run.cost_usd:.4f}")
