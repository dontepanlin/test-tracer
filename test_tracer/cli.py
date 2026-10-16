"""
Command-line interface for the scenario runner.

Usage examples::

    scenario -t topology.yml                     # full lifecycle
    scenario -t topology.yml --phase create      # just start the lab
    scenario -t topology.yml --dry-run           # show what would run
    scenario -t topology.yml --keep-lab          # skip teardown
    scenario -t topology.yml --phase capture,harvest   # subset of phases
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__, console as c
from .runner import ScenarioConfig, ScenarioRunner, ScenarioRunError
from .scenario import LabTopology, ScenarioError

PHASES = ["create", "verify", "capture", "harvest", "teardown"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scenario",
        description=(
            "Run a network scenario: netlab builds the topology (with native "
            "tc), Ansible drives captures and traffic, PCAPs are harvested. "
            "Lifecycle: create -> tc -> verify -> capture/scripts -> harvest "
            "-> teardown"),
    )
    parser.add_argument(
        "-t", "--topology",
        default="topology.yml",
        help="Path to the netlab topology file (default: topology.yml)",
    )
    parser.add_argument(
        "-p", "--provider",
        default=None,
        help="Virtualization provider override (e.g. clab, libvirt, vagrant)",
    )
    parser.add_argument(
        "--phase",
        default=None,
        help=(
            "Comma-separated phases to run (default: all). "
            f"Valid: {', '.join(PHASES)}"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the commands that would be executed, but do not execute them",
    )
    parser.add_argument(
        "--keep-lab",
        action="store_true",
        help="Skip the teardown phase (leave the lab running)",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip the `netlab validate` connectivity check",
    )
    parser.add_argument(
        "--up-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="Extra argument passed through to `netlab up` (repeatable)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


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

    try:
        phases = parse_phases(args.phase)
        topology = LabTopology.load(Path(args.topology))
        if "scenario" not in topology.raw:
            c.warn("no `scenario:` block found in the topology; running netlab-only lifecycle")
    except ScenarioError as ex:
        c.fail(str(ex))
        return 2

    try:
        cfg = ScenarioConfig(
            topology=topology,
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

    # Non-zero exit if any recorded phase failed.
    return 0 if all(r.ok for r in runner.results) else 1


if __name__ == "__main__":
    sys.exit(main())
