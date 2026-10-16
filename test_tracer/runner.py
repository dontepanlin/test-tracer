"""
Scenario lifecycle orchestrator.

Implements the plan's lifecycle:

    Create -> (TC via netlab up) -> Verify -> Capture + Scripts -> Harvest -> Teardown

Design: this project treats *netlab* as the **Topology Provider** (driving its
public CLI: ``netlab up``/``down``/``validate``) and *Ansible* as the **Test
Engine** (running playbooks against the ``hosts.yml`` netlab generates). This
keeps the two loosely coupled and version-agnostic.
"""

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from . import console as c
from .scenario import LabTopology, ScenarioError
from .playbooks import generate_playbooks


def resolve_netlab() -> str:
    """Locate the ``netlab`` executable."""
    found = shutil.which("netlab")
    if found:
        return found
    # Fall back to the interpreter's bin dir (e.g. the project venv).
    candidate = Path(sys.executable).parent / "netlab"
    if candidate.exists():
        return str(candidate)
    raise ScenarioError("netlab executable not found; install it with `uv add networklab`")


@dataclass
class ScenarioConfig:
    """Runtime configuration for a scenario run."""

    topology: LabTopology
    provider: Optional[str] = None
    dry_run: bool = False
    keep_lab: bool = False          # skip teardown
    no_validate: bool = False
    extra_up_args: List[str] = field(default_factory=list)
    netlab_bin: str = field(default_factory=resolve_netlab)

    @property
    def lab_dir(self) -> Path:
        return self.topology.lab_dir

    @property
    def name(self) -> str:
        return self.topology.scenario.name

    @property
    def work_dir(self) -> Path:
        return self.lab_dir / f"scenario-{self.name}"

    @property
    def results_dir(self) -> Path:
        return self.lab_dir / self.topology.scenario.results_dir

    @property
    def normalized_topo_path(self) -> Path:
        return self.lab_dir / "netlab.scenario.topology.yml"


class ScenarioRunError(Exception):
    """Raised when a phase fails."""


@dataclass
class PhaseResult:
    phase: str
    ok: bool
    detail: str = ""


class ScenarioRunner:
    """Drives the scenario lifecycle."""

    def __init__(self, cfg: ScenarioConfig):
        self.cfg = cfg
        self.results: List[PhaseResult] = []
        self._netlab_bin = cfg.netlab_bin

    # ------------------------------------------------------------------ utils

    def _run(self, cmd: List[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
        """Run a command, streaming output to the console.

        In dry-run mode the command is only printed.
        """
        cwd = cwd or self.cfg.lab_dir
        shown = " ".join(str(x) for x in cmd)
        if self.cfg.dry_run:
            c.info(c.dim(f"[dry-run] $ {shown}"))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        c.info("$ " + shown)
        proc = subprocess.run([str(x) for x in cmd], cwd=str(cwd))
        if check and proc.returncode != 0:
            raise ScenarioRunError(f"command failed ({proc.returncode}): {shown}")
        return proc

    def _netlab(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return self._run([self._netlab_bin, *args], check=check)

    def _ansible(self, playbook: str, extra: Optional[List[str]] = None,
                 hosts: Optional[str] = None, check: bool = True) -> subprocess.CompletedProcess:
        """Run an Ansible playbook against the netlab-generated inventory.

        Runs from the lab directory so netlab's ``ansible.cfg`` and
        ``hosts.yml`` are picked up automatically.
        """
        cmd = ["ansible-playbook", "-i", "hosts.yml", playbook]
        if hosts:
            cmd += ["--limit", hosts]
        if extra:
            cmd += extra
        return self._run(cmd, check=check)

    def _record(self, phase: str, ok: bool, detail: str = "") -> None:
        self.results.append(PhaseResult(phase, ok, detail))

    def _resolve_playbook(self, name: str) -> Path:
        """Resolve a playbook path relative to the lab dir (or its playbooks/)."""
        lab = self.cfg.lab_dir
        for cand in (lab / name, lab / "playbooks" / name):
            if cand.exists():
                return cand
        raise ScenarioRunError(
            f"playbook not found: {name} (looked in {lab} and {lab}/playbooks)")

    # ----------------------------------------------------------------- phases

    def phase_create(self) -> None:
        """Materialize a normalized topology and start the lab (netlab up)."""
        c.section("1/5", "CREATE — spin up lab & apply traffic control")
        cfg = self.cfg
        topo: LabTopology = cfg.topology

        if not topo.declaration_present and "scenario" in topo.raw:
            c.warn(
                "topology does not declare `defaults.attributes.global.scenario`; "
                "materializing a normalized copy for netlab")

        # Write a normalized topology (guarantees the scenario attribute
        # declaration) so the user's file stays untouched.
        cfg.normalized_topo_path.write_text(
            yaml.safe_dump(topo.normalized(), sort_keys=False, default_flow_style=False, width=1000))
        c.info(f"normalized topology -> {cfg.normalized_topo_path}")

        up_args = ["up", str(cfg.normalized_topo_path)]
        if cfg.provider:
            up_args += ["-p", cfg.provider]
        up_args += cfg.extra_up_args
        if cfg.dry_run:
            up_args += ["--dry-run"]
        self._netlab(*up_args)
        c.ok("lab started (netlab up also applies native link tc)")
        self._record("create", True)

    def phase_verify(self) -> None:
        """Validate the lab and wait for convergence."""
        c.section("2/5", "VERIFY — check connectivity")
        cfg = self.cfg
        if cfg.no_validate:
            c.info(c.dim("validate skipped (--no-validate)"))
        else:
            # netlab validate: exit 1 = failure, exit 3 = warning, else OK
            proc = self._netlab("validate", check=False)
            if proc.returncode == 1:
                c.err("netlab validate reported failures")
                self._record("verify", False, "netlab validate exit=1")
                raise ScenarioRunError("validation failed")
            if proc.returncode == 3:
                c.warn("netlab validate produced a warning")
        wait = cfg.topology.scenario.verify_wait
        if wait and not cfg.dry_run:
            c.info(f"waiting {wait}s for routing convergence...")
            time.sleep(wait)
        c.ok("lab verified")
        self._record("verify", True)

    def phase_capture(self) -> None:
        """Start captures, run traffic scripts, stop captures."""
        c.section("3/5", "CAPTURE + SCRIPTS")
        cfg = self.cfg
        captures = cfg.topology.scenario.captures
        scripts = cfg.topology.scenario.scripts

        # Generate helper playbooks (capture start/stop, fetch).
        if captures:
            files = generate_playbooks(cfg.work_dir, cfg.name, captures)
            c.info(f"generated playbooks in {cfg.work_dir}")
            self._ansible(str(files["scenario_capture_start.yml"]))
            c.ok(f"started {len(captures)} capture(s)")

        # Run user-supplied traffic generators.
        for i, script in enumerate(scripts, 1):
            if script.playbook:
                pb = self._resolve_playbook(script.playbook)
                c.info(f"[script {i}/{len(scripts)}] ansible-playbook {pb.name}")
                self._ansible(str(pb), hosts=script.nodes)
            elif script.command:
                c.info(f"[script {i}/{len(scripts)}] $ {script.command}")
                self._run(["bash", "-lc", script.command])
            if script.wait_after and not cfg.dry_run:
                c.info(f"waiting {script.wait_after}s...")
                time.sleep(script.wait_after)

        if captures:
            stop = cfg.work_dir / "scenario_capture_stop.yml"
            if stop.exists():
                self._ansible(str(stop))
                c.ok("captures stopped")
        self._record("capture", True)

    def phase_harvest(self) -> None:
        """Fetch PCAP files from the nodes into results/."""
        c.section("4/5", "HARVEST — fetch PCAPs")
        cfg = self.cfg
        if not cfg.topology.scenario.captures:
            c.info(c.dim("no captures defined; nothing to harvest"))
            self._record("harvest", True, "no captures")
            return
        if not cfg.dry_run:
            cfg.results_dir.mkdir(parents=True, exist_ok=True)
        fetch = cfg.work_dir / "scenario_fetch.yml"
        if fetch.exists():
            self._ansible(str(fetch), check=False)
            pcaps = list(cfg.results_dir.glob("*.pcap"))
            c.ok(f"fetched {len(pcaps)} pcap(s) into {cfg.results_dir}")
            self._record("harvest", True, f"{len(pcaps)} pcap(s)")
        else:
            c.warn("fetch playbook missing; skipping harvest")
            self._record("harvest", False, "no fetch playbook")

    def phase_teardown(self) -> None:
        """Tear down the lab."""
        c.section("5/5", "TEARDOWN")
        cfg = self.cfg
        if cfg.keep_lab:
            c.info(c.dim("lab kept (--keep-lab); run `netlab down --cleanup` to destroy"))
            self._record("teardown", True, "kept lab")
            return
        down_args = ["down", "--cleanup"]
        if cfg.dry_run:
            down_args += ["--dry-run"]
        self._netlab(*down_args, check=False)
        # Remove the materialized topology copy.
        if cfg.normalized_topo_path.exists() and not cfg.dry_run:
            cfg.normalized_topo_path.unlink()
        c.ok("lab torn down")
        self._record("teardown", True)

    # -------------------------------------------------------------------- api

    def run(self, phases: Optional[List[str]] = None) -> List[PhaseResult]:
        all_phases = ["create", "verify", "capture", "harvest", "teardown"]
        phases = phases or all_phases
        for p in phases:
            if p not in all_phases:
                raise ScenarioError(f"unknown phase: {p} (valid: {', '.join(all_phases)})")

        c.section("RUN", f"scenario '{self.cfg.name}'")
        c.info(f"topology: {self.cfg.topology.path}")
        c.info(f"lab dir:  {self.cfg.lab_dir}")
        if self.cfg.provider:
            c.info(f"provider: {self.cfg.provider}")

        failed = False
        last_phase = "create"
        try:
            for p in phases:
                last_phase = p
                getattr(self, f"phase_{p}")()
        except ScenarioRunError as ex:
            c.err(str(ex))
            failed = True
        except Exception as ex:  # noqa: BLE001 - report & continue to teardown
            c.err(f"unexpected error in phase '{last_phase}': {ex}")
            failed = True

        # Best-effort teardown on failure: only when this run started the lab
        # itself (create phase requested). If the user targeted a running lab
        # (--phase capture,harvest etc.) we must never destroy it.
        if (
            failed
            and "create" in phases
            and "teardown" not in phases
            and not self.cfg.keep_lab
        ):
            c.warn("attempting best-effort teardown after failure")
            try:
                self.phase_teardown()
            except Exception:  # noqa: BLE001
                pass

        self._summary(failed)
        return self.results

    def _summary(self, failed: bool) -> None:
        print()
        c.section("SUMMARY", "scenario run")
        for r in self.results:
            mark = "ok " if r.ok else "ERR"
            line = f"  {mark:<3}  {r.phase:<8}"
            if r.detail:
                line += f"  {r.detail}"
            print(line)
        if self.cfg.dry_run:
            c.info(c.dim("(dry-run: no commands were actually executed)"))
        print()
