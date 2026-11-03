"""
Command-line interface for the scenario runner.

Usage examples::

    scenario -t topology.yml                     # full lifecycle
    scenario -t topology.yml --phase create      # just start the lab
    scenario -t topology.yml --dry-run           # show what would run
    scenario -t topology.yml --keep-lab          # skip teardown
    scenario -t topology.yml --phase steps,harvest   # subset of phases

    scenario describe -t topology.yml            # LLM-readable timeline
    scenario describe -t topology.yml --format yaml
    scenario describe -t topology.yml --format json
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__, console as c
from .runner import ScenarioConfig, ScenarioRunner, ScenarioRunError
from .scenario import LabTopology, ScenarioError
from .describe import describe as render_describe

PHASES = ["create", "verify", "steps", "harvest", "teardown"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scenario",
        description=(
            "Run a network scenario: netlab builds the topology (with native "
            "tc), Ansible drives the step timeline (captures + traffic + "
            "asserts), PCAPs are harvested. "
            "Lifecycle: create -> verify -> steps -> harvest -> teardown"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    # --- run (default) ---
    run_p = sub.add_parser("run", help="Run the scenario lifecycle (default)")
    _add_run_args(run_p)

    # --- describe ---
    desc_p = sub.add_parser(
        "describe",
        help="Print the parsed scenario as a readable, LLM-friendly timeline",
    )
    desc_p.add_argument(
        "-t", "--topology",
        default="topology.yml",
        help="Path to the netlab topology file (default: topology.yml)",
    )
    desc_p.add_argument(
        "--format",
        choices=["text", "yaml", "json"],
        default="text",
        help="Output format (default: text)",
    )

    # --- top-level (so `scenario -t ...` works without a subcommand) ---
    _add_run_args(parser)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _add_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-t", "--topology",
        default="topology.yml",
        help="Path to the netlab topology file (default: topology.yml)",
    )
    p.add_argument(
        "-p", "--provider",
        default=None,
        help="Virtualization provider override (e.g. clab, libvirt, vagrant)",
    )
    p.add_argument(
        "--phase",
        default=None,
        help=(
            "Comma-separated phases to run (default: all). "
            f"Valid: {', '.join(PHASES)}"
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned timeline and commands, but do not execute them",
    )
    p.add_argument(
        "--keep-lab",
        action="store_true",
        help="Skip the teardown phase (leave the lab running)",
    )
    p.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip the `netlab validate` connectivity check",
    )
    p.add_argument(
        "--up-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="Extra argument passed through to `netlab up` (repeatable)",
    )


def parse_phases(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    phases = [p.strip() for p in value.split(",") if p.strip()]
    unknown = [p for p in phases if p not in PHASES]
    if unknown:
        raise ScenarioError(
            f"unknown phase(s): {', '.join(unknown)} (valid: {', '.join(PHASES)})")
    return phases


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # No subcommand and no -t => default to run.
    command = args.command
    if command == "describe":
        return _cmd_describe(args)
    # 'run' subcommand or top-level (no subcommand): run the lifecycle.
    return _cmd_run(args)


def _cmd_describe(args) -> int:
    try:
        topo = LabTopology.load(Path(args.topology))
    except ScenarioError as ex:
        c.fail(str(ex))
        return 2
    try:
        print(render_describe(topo, args.format), end="")
    except ScenarioError as ex:
        c.fail(str(ex))
        return 2
    return 0


def _cmd_run(args) -> int:
    try:
        phases = parse_phases(getattr(args, "phase", None))
        topo = LabTopology.load(Path(args.topology))
    except ScenarioError as ex:
        c.fail(str(ex))
        return 2

    try:
        cfg = ScenarioConfig(
            topology=topo,
            provider=args.provider,
            dry_run=args.dry_run,
            keep_lab=args.keep_lab,
            no_validate=args.no_validate,
            extra_up_args=list(args.up_arg),
        )
        runner = ScenarioRunner(cfg)
        runner.run(phases)
    except ScenarioRunError as ex:
        c.fail(str(ex))
        return 1
    except ScenarioError as ex:
        c.fail(str(ex))
        return 2
    except KeyboardInterrupt:
        c.fail("interrupted by user")
        return 130

    return 0 if all(r.ok for r in runner.results) else 1


if __name__ == "__main__":
    sys.exit(main())
