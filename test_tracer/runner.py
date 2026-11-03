"""
Scenario lifecycle orchestrator.

Treats *netlab* as the **Topology Provider** (``netlab up``/``down``/
``validate``) and *Ansible* as the **Test Engine** (playbooks against the
``hosts.yml`` netlab generates).

Lifecycle:

    create -> verify -> steps -> harvest -> teardown

The **steps** phase is the heart of the timeline: for each step the runner
starts the captures that step declares, runs the step's actions (with its
declarative assert), and stops captures that the next step no longer needs.
Ambient captures (``always_capture``) run for the whole timeline.
"""

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set

import yaml

from . import console as c, daemons
from .scenario import LabTopology, ScenarioError
from .playbooks import generate_playbooks


def resolve_netlab() -> str:
    """Locate the ``netlab`` executable."""
    found = shutil.which("netlab")
    if found:
        return found
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
    keep_lab: bool = False
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


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""


class ScenarioRunner:
    """Drives the scenario lifecycle."""

    def __init__(self, cfg: ScenarioConfig):
        self.cfg = cfg
        self.results: List[PhaseResult] = []
        self.step_results: List[StepResult] = []
        self._netlab_bin = cfg.netlab_bin
        self._generated: dict = {}   # filename -> Path
        self._active_captures: Set[str] = set()

    # ------------------------------------------------------------------ utils

    def _run(self, cmd: List[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
        cwd = cwd or self.cfg.lab_dir
        shown = " ".join(str(x) for x in cmd)
        if self.cfg.dry_run:
            c.info(c.dim(f"[dry-run] $ {shown}"))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        c.info("$ " + shown)
        # Capture output so the caller can inspect it (e.g. to tell a real
        # `netlab validate` failure apart from "No validation tests defined").
        proc = subprocess.run([str(x) for x in cmd], cwd=str(cwd),
                              capture_output=True, text=True)
        # Echo the captured output so the user still sees netlab/ansible logs.
        if proc.stdout:
            sys.stderr.write(proc.stdout if proc.stdout.endswith("\n") else proc.stdout + "\n")
        if proc.stderr:
            sys.stderr.write(proc.stderr if proc.stderr.endswith("\n") else proc.stderr + "\n")
        if check and proc.returncode != 0:
            raise ScenarioRunError(f"command failed ({proc.returncode}): {shown}")
        return proc

    def _netlab(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return self._run([self._netlab_bin, *args], check=check)

    def _ansible(self, playbook: str, extra_vars: Optional[List[str]] = None,
                 check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["ansible-playbook", "-i", "hosts.yml", playbook]
        if extra_vars:
            cmd += extra_vars
        return self._run(cmd, check=check)

    def _record(self, phase: str, ok: bool, detail: str = "") -> None:
        self.results.append(PhaseResult(phase, ok, detail))

    def _ensure_playbooks(self) -> None:
        """Generate (or regenerate) all playbooks for the scenario."""
        cfg = self.cfg
        spec = cfg.topology.scenario
        if "scenario_capture.yml" not in self._generated:
            self._generated = generate_playbooks(
                cfg.work_dir, spec.name, spec, cfg.results_dir)
            c.info(f"generated playbooks in {cfg.work_dir}")

    # ------------------------------------------------------------- capture ctl

    def _capture_file(self) -> Optional[Path]:
        return self._generated.get("scenario_capture.yml")

    def _start_captures(self, ids: Set[str]) -> None:
        ids = {i for i in ids if i not in self._active_captures}
        if not ids:
            return
        cmap = self.cfg.topology.scenario.capture_map()
        for cap_id in sorted(ids):
            cap = cmap[cap_id]
            if self.cfg.dry_run:
                c.info(c.dim(f"[dry-run] docker exec -d {cap.node} tcpdump -i "
                             f"{cap.interface or ('link:'+str(cap.link))} -> {cap.out_name()}"))
                continue
            daemons.start_capture(self.cfg.lab_dir, cap)
            c.ok(f"started capture {cap_id} on {cap.node} ({cap.out_name()})")
        self._active_captures |= ids

    def _stop_captures(self, ids: Set[str]) -> None:
        ids = {i for i in ids if i in self._active_captures}
        if not ids:
            return
        cmap = self.cfg.topology.scenario.capture_map()
        for cap_id in sorted(ids):
            cap = cmap[cap_id]
            if not self.cfg.dry_run:
                daemons.stop_capture(self.cfg.lab_dir, cap)
            c.ok(f"stopped capture {cap_id} on {cap.node}")
        self._active_captures -= ids

    def _stop_all_captures(self) -> None:
        if self._active_captures:
            self._stop_captures(set(self._active_captures))

    # ----------------------------------------------------------------- phases

    def phase_create(self) -> None:
        c.section("1/5", "CREATE — spin up lab & apply traffic control")
        cfg = self.cfg
        topo: LabTopology = cfg.topology

        if not topo.declaration_present and "scenario" in topo.raw:
            c.warn(
                "topology does not declare `defaults.attributes.global.scenario`; "
                "materializing a normalized copy for netlab")

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
        c.section("2/5", "VERIFY — check connectivity")
        cfg = self.cfg
        spec = cfg.topology.scenario

        wait = spec.verify_wait
        if wait and not cfg.dry_run:
            c.info(f"waiting {wait}s before validation...")
            time.sleep(wait)

        if spec.verify_pings:
            self._ensure_playbooks()
            vfile = self._generated.get("scenario_verify.yml")
            if vfile is None:
                raise ScenarioRunError("verify playbook not generated")
            self._ansible(str(vfile))
            c.ok(f"reachability verified ({len(spec.verify_pings)} ping check(s))")
            self._record("verify", True)
            return

        if cfg.no_validate:
            c.info(c.dim("validate skipped (--no-validate)"))
        else:
            proc = self._netlab("validate", check=False)
            output = (proc.stdout or "") + (proc.stderr or "")
            # A plain L2 lab (no routing module) has no `validate` tests, so
            # netlab exits 1 with "No validation tests defined". That is NOT a
            # failure — there was simply nothing to validate. Treat it as ok.
            if "No validation tests defined" in output:
                c.info(c.dim("netlab validate: no validation tests defined "
                             "(plain lab) — nothing to check"))
            elif proc.returncode == 1:
                c.err("netlab validate reported failures")
                self._record("verify", False, "netlab validate exit=1")
                raise ScenarioRunError("validation failed")
            elif proc.returncode == 3:
                c.warn("netlab validate produced a warning")
        c.ok("lab verified")
        self._record("verify", True)

    def phase_steps(self) -> None:
        c.section("3/5", "STEPS — run the scenario timeline")
        cfg = self.cfg
        spec = cfg.topology.scenario
        always = set(spec.always_capture)

        # Pre-generate all playbooks (capture, per-step, fetch, verify).
        self._ensure_playbooks()

        # Ambient captures run for the whole timeline.
        if always:
            self._start_captures(always)

        steps = spec.steps
        for idx, step in enumerate(steps):
            needed = set(step.capture)
            # Start captures this step needs that are not already active.
            self._start_captures(needed)

            c.section(f"step {idx+1}/{len(steps)}", step.name)
            if cfg.dry_run:
                self._describe_step(step, idx, needed)

            try:
                self._run_step(step, idx)
                self.step_results.append(StepResult(step.name, True))
                c.ok(f"step {step.name!r} passed")
            except ScenarioRunError as ex:
                self.step_results.append(StepResult(step.name, False, str(ex)))
                c.err(f"step {step.name!r} failed: {ex}")

            # Stop captures no longer needed by the next step (keep ambient).
            if idx + 1 < len(steps):
                next_needed = set(steps[idx + 1].capture)
                to_stop = (needed | always) - next_needed - always
                self._stop_captures(to_stop)
            else:
                # Last step: stop everything that is not ambient.
                self._stop_captures((needed | always) - always)

        # Safety: stop any still-active non-ambient captures.
        self._stop_captures(self._active_captures - always)
        # Ambient captures are stopped at the very end (before harvest).
        self._stop_all_captures()

        failed = [s.name for s in self.step_results if not s.ok]
        self._record(
            "steps", not failed,
            f"{len(self.step_results) - len(failed)}/{len(self.step_results)} passed"
            if self.step_results else "no steps",
        )

    def _run_step(self, step, idx: int) -> None:
        """Execute a step's actions in order, then its assert.

        `shell`/`raw`/`playbook`/`command` actions run via a generated
        per-action ansible playbook (so their stdout is persisted for the
        assert). `daemon` / `daemon_kill` actions run controller-side via the
        daemon manager (`docker exec -d`), the only reliable way to keep an
        in-container process alive (see daemons.py).
        """
        cfg = self.cfg
        for i, action in enumerate(step.do):
            kind = action.kind
            label = action.label or action.kind
            if kind in ("daemon", "daemon_kill"):
                if cfg.dry_run:
                    c.info(c.dim(f"  [dry-run] docker exec -d {action.node} {action.daemon or action.daemon_kill}"))
                elif kind == "daemon":
                    daemons.start_process(cfg.lab_dir, action.node, action.daemon)
                    c.ok(f"started daemon on {action.node}: {action.daemon[:60]}")
                else:
                    daemons.kill_process(cfg.lab_dir, action.node, action.daemon_kill)
                    c.ok(f"killed daemon on {action.node}: {action.daemon_kill}")
                continue
            # shell / raw / playbook / command -> per-action ansible playbook.
            pb = self._generated.get(f"scenario_step_{idx}_{step.name}_action_{i}.yml")
            if pb is None:
                raise ScenarioRunError(f"action playbook not generated for {step.name}[{i}]")
            if cfg.dry_run:
                c.info(c.dim(f"  [dry-run] $ ansible-playbook -i hosts.yml {pb.name}  ({label})"))
            else:
                self._ansible(str(pb))
        # Assert (if any) — runs against the combined stdout of the ansible
        # actions (persisted to per-action output files by the action plays).
        if step.assert_:
            assert_pb = self._generated.get(f"scenario_step_{idx}_{step.name}_assert.yml")
            if assert_pb is None:
                raise ScenarioRunError(f"assert playbook not generated for {step.name}")
            if cfg.dry_run:
                c.info(c.dim(f"  [dry-run] $ ansible-playbook -i hosts.yml {assert_pb.name}  (assert: {step.assert_})"))
            else:
                self._ansible(str(assert_pb))

    def _describe_step(self, step, idx: int, needed: Set[str]) -> None:
        if needed:
            c.info(c.dim(f"  capture: {', '.join(sorted(needed))} (active during this step)"))

    def phase_harvest(self) -> None:
        c.section("4/5", "HARVEST — fetch PCAPs and logs")
        cfg = self.cfg
        spec = cfg.topology.scenario
        if not spec.captures and not any(s.log for s in spec.steps):
            c.info(c.dim("nothing to harvest"))
            self._record("harvest", True, "nothing to fetch")
            return
        if not cfg.dry_run:
            cfg.results_dir.mkdir(parents=True, exist_ok=True)
        fetch = self._generated.get("scenario_fetch.yml")
        if fetch is None:
            # Regenerate in case the steps phase was skipped (--phase harvest).
            self._ensure_playbooks()
            fetch = self._generated.get("scenario_fetch.yml")
        if fetch is None:
            c.warn("fetch playbook missing; skipping harvest")
            self._record("harvest", False, "no fetch playbook")
            return
        self._ansible(str(fetch), check=False)
        pcaps = list(cfg.results_dir.glob("*.pcap")) if cfg.results_dir.exists() else []
        c.ok(f"fetched {len(pcaps)} pcap(s) into {cfg.results_dir}")
        self._record("harvest", True, f"{len(pcaps)} pcap(s)")

    def phase_teardown(self) -> None:
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
        if cfg.normalized_topo_path.exists() and not cfg.dry_run:
            cfg.normalized_topo_path.unlink()
        c.ok("lab torn down")
        self._record("teardown", True)

    # -------------------------------------------------------------------- api

    def run(self, phases: Optional[List[str]] = None) -> List[PhaseResult]:
        all_phases = ["create", "verify", "steps", "harvest", "teardown"]
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
        except Exception as ex:  # noqa: BLE001
            c.err(f"unexpected error in phase '{last_phase}': {ex}")
            failed = True

        # A failed step is a failure even if no phase raised.
        if any(not s.ok for s in self.step_results):
            failed = True

        # Best-effort teardown on failure: only when this run started the lab.
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
        if self.step_results:
            print("  steps:")
            for s in self.step_results:
                mark = "ok " if s.ok else "ERR"
                line = f"    {mark:<3}  {s.name}"
                if s.detail:
                    line += f"  {s.detail}"
                print(line)
        if self.cfg.dry_run:
            c.info(c.dim("(dry-run: no commands were actually executed)"))
        print()
