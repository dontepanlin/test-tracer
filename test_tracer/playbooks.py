"""
Generates the Ansible playbooks that implement the capture, step, and harvest
phases of the scenario timeline.

netlab already generates ``hosts.yml`` and ``ansible.cfg`` in the lab
directory; the generated playbooks are written to ``<lab-dir>/scenario-<name>/``
and are run from the lab directory (so ``ansible.cfg`` and the inventory are
picked up automatically).

Generated playbooks:

* ``scenario_capture.yml`` -- a single play over all capture nodes with a
  ``start <id>`` and a ``stop <id>`` task per capture. Each task is gated by a
  ``when:`` on the ``capture_start`` / ``capture_stop`` extra-var list. Stop is
  **path-specific** (kills only the tcpdump writing to ``/tmp/<out>.pcap``) so
  multiple captures on one node coexist, and stale captures are cleaned before
  a start.
* ``scenario_step_<i>_<name>.yml`` -- one play per step: bootstrap python3 on
  the step's primary node, run each action (shell/raw delegate to their node;
  playbook/command run on the controller), register each action's output, then
  apply the step's declarative assert as an ``ansible.builtin.assert`` task.
* ``scenario_fetch.yml`` -- fetch each capture's PCAP and each step's ``log:``
  entries into ``results/``.
* ``scenario_verify.yml`` -- reachability pings (when the scenario defines
  ``verify.ping``).

Interface resolution for ``link:`` captures is done **at Ansible runtime**
from ``hostvars[<node>].interfaces`` (netlab exposes the per-node interface
list in ``host_vars/<node>/topology``), so it works uniformly for data and
mgmt interfaces.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .scenario import Capture, Step


def _dump(doc) -> str:
    """A playbook must be a *list* of plays."""
    if isinstance(doc, dict):
        doc = [doc]
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=2000)


def _bootstrap_python_task() -> dict:
    """Install python3 if missing (raw runs without a remote interpreter)."""
    return {
        "name": "bootstrap python3 (required by Ansible modules)",
        "ansible.builtin.raw": (
            "(apt-get update -qq && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3) "
            ">/tmp/apt_bootstrap.log 2>&1"
        ),
        "ignore_errors": True,
    }


def _ensure_tcpdump_task() -> dict:
    return {
        "name": "ensure tcpdump is installed",
        "ansible.builtin.shell": (
            "command -v tcpdump >/dev/null 2>&1 && echo present || "
            "{ apt-get update -qq && apt-get install -y -qq tcpdump; } "
            ">/tmp/ensure_tcpdump.log 2>&1"
        ),
        "ignore_errors": True,
    }


def _clean(text: str) -> str:
    return " ".join(str(text).split())


def _q(value) -> str:
    """Escape a value for embedding inside a single-quoted Jinja/Ansible string."""
    return str(value).replace("'", "'\\''")


# ---------------------------------------------------------------------------
# Capture playbook
# ---------------------------------------------------------------------------

def _capture_start_tasks(captures: List[Capture]) -> List[dict]:
    tasks = [_bootstrap_python_task(), _ensure_tcpdump_task()]
    for cap in captures:
        out = cap.out_name()
        bpf = f' -f "{_clean(cap.filter)}"' if cap.filter else ""
        if cap.interface is not None:
            ifexpr = f"'{cap.interface}'"
        else:
            ifexpr = f"{{{{ hostvars['{cap.node}'].interfaces[{cap.link}].ifname }}}}"
        pid = f"/tmp/cap.{out}.pid"
        # Stop any prior capture for this pcap (by pidfile), remove the stale
        # pcap, and start a fresh tcpdump. The start uses a DOUBLE-FORK
        # (`sh -c 'setsid nohup ... & exit 0'`) so tcpdump is reparented to
        # PID 1 and survives Ansible's process-group teardown on task exit
        # (a plain `setsid nohup ... &` gets SIGTERM'd, rc=-15). The pcap is
        # flushed to disk when the capture is stopped. `failed_when: false`
        # keeps a launch hiccup from failing the play; harvest verifies the
        # pcap is non-empty.
        cmd = (
            f"[ -f {pid} ] && kill \"$(cat {pid})\" 2>/dev/null; "
            f"rm -f /tmp/{out} {pid}; "
            f"sh -c 'setsid nohup tcpdump -i {ifexpr}{bpf} -s 0 -w /tmp/{out} "
            f">/tmp/cap.{out}.log 2>&1 </dev/null & echo $! > {pid}; exit 0'; "
            f"sleep 0.3"
        )
        tasks.append({
            "name": f"start capture {cap.id} ({cap.node})",
            "delegate_to": cap.node,
            "when": "capture_start is defined and '" + cap.id + "' in capture_start",
            "ansible.builtin.shell": cmd,
            "failed_when": False,
            "register": f"capstart_{cap.id}",
        })
    return tasks


def _capture_stop_tasks(captures: List[Capture]) -> List[dict]:
    tasks = []
    for cap in captures:
        out = cap.out_name()
        pid = f"/tmp/cap.{out}.pid"
        # Stop by pidfile. Killing tcpdump (SIGTERM) makes it flush the pcap
        # to disk before exiting, so the harvested file contains the packets.
        cmd = (
            f"[ -f {pid} ] && kill \"$(cat {pid})\" 2>/dev/null; "
            f"sleep 1; rm -f {pid}"
        )
        tasks.append({
            "name": f"stop capture {cap.id} ({cap.node})",
            "delegate_to": cap.node,
            "when": "capture_stop is defined and '" + cap.id + "' in capture_stop",
            "ansible.builtin.shell": cmd,
            "failed_when": False,
        })
    return tasks


def build_capture_playbook(captures: List[Capture]) -> str:
    if not captures:
        return _dump([{"hosts": "all", "gather_facts": False, "tasks": []}])
    tasks = _capture_start_tasks(captures) + _capture_stop_tasks(captures)
    playbook = {
        "hosts": sorted({c.node for c in captures}),
        "gather_facts": False,
        "tasks": tasks,
    }
    return _dump(playbook)


# ---------------------------------------------------------------------------
# Step playbook
# ---------------------------------------------------------------------------

def _action_task(index: int, action, lab_dir: Path) -> dict:
    """Translate one action into an ansible task that registers its output."""
    var = f"action_{index}"
    label = action.label or action.kind
    kind = action.kind
    if kind == "shell" or kind == "raw":
        module = "ansible.builtin.shell" if kind == "shell" else "ansible.builtin.raw"
        # For shell actions, merge stderr into stdout (`2>&1`) so text asserts
        # (contains/regex) see diagnostic output — e.g. `curl --verbose`
        # writes the "HTTP/ 200" status line to stderr. We do this in the
        # command rather than via the shell module's `stderr` param, because
        # on some Ansible setups the module resolves to `command`, which
        # rejects `stderr` as an unsupported parameter.
        cmd = action.shell if kind == "shell" else action.raw
        if kind == "shell":
            cmd = f"({cmd}) 2>&1"
        task = {
            "name": f"[{label}] {kind}",
            "delegate_to": action.node,
            module: cmd,
            "register": var,
            "failed_when": False,
        }
        return task
    if kind == "playbook":
        # Run the user playbook via the controller. `delegate_to: localhost`
        # tasks do NOT inherit the outer ansible-playbook's cwd, so cd into
        # the lab dir explicitly so `hosts.yml` and the playbook path resolve.
        return {
            "name": f"[{label}] playbook {action.playbook}",
            "delegate_to": "localhost",
            "ansible.builtin.shell": (
                f"cd {lab_dir} && ansible-playbook -i hosts.yml {action.playbook}"
            ),
            "register": var,
            "failed_when": False,
        }
    # command (controller-side)
    return {
        "name": f"[{label}] command (controller)",
        "delegate_to": "localhost",
        "ansible.builtin.shell": f"cd {lab_dir} && {action.command}",
        "register": var,
        "failed_when": False,
    }


def _assert_conditions(assertion: Dict[str, Any], n_actions: int) -> List[str]:
    """Build the list of failure conditions (any true => step failed)."""
    conds = []
    for key, val in assertion.items():
        if key == "rc_eq":
            conds.append(f"action_{n_actions - 1}.rc != {int(val)}")
        elif key == "rc_neq":
            conds.append(f"action_{n_actions - 1}.rc == {int(val)}")
        elif key == "contains":
            conds.append(
                f"action_out is not search('{_q(val)}')"
            )
        elif key == "not_contains":
            conds.append(f"action_out is search('{_q(val)}')")
        elif key == "regex":
            conds.append(f"action_out is not search('{_q(val)}')")
        elif key == "lines_ge":
            conds.append(f"action_out_lines | length < {int(val)}")
    return conds


def _step_out_dir(step: Step) -> str:
    """A per-step temp dir where each action's stdout is persisted."""
    return f"/tmp/stepout_{step.name}"


def build_action_playbook(step: Step, index: int, lab_dir: Path) -> str:
    """A one-action ansible playbook.

    Runs the single action (shell/raw/playbook/command), registers its output,
    and appends it to a per-step output file so a later, *separate* assert
    playbook (a distinct ansible invocation) can read the combined output.
    Controller-side daemon/daemon_kill actions are NOT generated here — the
    runner executes those directly.
    """
    action = step.do[index]
    primary = None
    for a in step.do:
        if a.kind in ("shell", "raw"):
            primary = a.node
            break
    hosts = [primary] if primary else ["localhost"]

    out_dir = _step_out_dir(step)
    out_file = f"{out_dir}/{index:02d}.out"
    task = _action_task(index, action, lab_dir)
    var = f"action_{index}"
    tasks = [_bootstrap_python_task()]
    tasks.append(task)
    # Persist this action's stdout (and a last-rc marker) to disk so the assert
    # playbook, run as a separate ansible invocation, can read it. Use the
    # `copy` module with `content` (not a shell printf) because the output can
    # contain arbitrary characters (HTML quotes, etc.) that would break shell
    # quoting. Ansible serializes `content` safely as JSON.
    tasks.append({
        "name": "persist action output dir",
        "delegate_to": "localhost",
        "ansible.builtin.file": {"path": out_dir, "state": "directory", "mode": "0755"},
    })
    tasks.append({
        "name": "persist action output",
        "delegate_to": "localhost",
        "ansible.builtin.copy": {
            "dest": out_file,
            "content": f"{{{{ {var}.stdout | default('') }}}}\n",
        },
    })
    tasks.append({
        "name": "persist action rc",
        "delegate_to": "localhost",
        "ansible.builtin.copy": {
            "dest": f"{out_file}.rc",
            "content": f"{{{{ {var}.rc | default('') }}}}\n",
        },
    })
    playbook = {"hosts": hosts, "gather_facts": False, "tasks": tasks}
    return _dump(playbook)


def build_assert_playbook(step: Step) -> str:
    """A standalone assert playbook.

    Reads every ``<out_dir>/<i>.out`` file (written by the per-action
    playbooks, which run as separate ansible invocations) into a combined
    ``action_out`` fact, then applies the declarative assert.
    """
    out_dir = _step_out_dir(step)
    n = len(step.do)
    # Build a shell expression that concatenates the per-action output files.
    files = " ".join(f"{out_dir}/{i:02d}.out" for i in range(n))
    tasks = [{
        "name": "read combined step output",
        "delegate_to": "localhost",
        "ansible.builtin.shell": (
            f"cat {files} 2>/dev/null; "
            f"echo \"---rc---\"; "
            f"cat {files} 2>/dev/null | tail -1  # noqa"
        ),
        "register": "stepread",
        "failed_when": False,
    }]
    # Set action_out from the read result; last rc = rc of the last action file.
    tasks.append({
        "name": "collect step output",
        "ansible.builtin.set_fact": {
            "action_out": "{{ stepread.stdout | default('') }}",
        },
    })
    tasks.append({
        "name": "collect step output lines",
        "ansible.builtin.set_fact": {
            "action_out_lines": "{{ action_out.splitlines() }}",
        },
    })
    # rc assertions use the last action's rc file.
    last_rc_file = f"{out_dir}/{n - 1:02d}.out.rc"
    # The last action's rc is persisted to <last_rc_file>. Read it defensively:
    # lookup with errors='' returns '' when the file is missing, and
    # `| default('-1') | int` coerces '' to -1 so a missing file never breaks
    # the expression.
    rc_expr = (
        f"lookup('file', '{last_rc_file}', errors='') | default('-1') | int"
    )
    conds = []
    for key, val in step.assert_.items():
        if key == "rc_eq":
            conds.append(f"({rc_expr}) != {int(val)}")
        elif key == "rc_neq":
            conds.append(f"({rc_expr}) == {int(val)}")
        elif key == "contains":
            conds.append(f"action_out is not search('{_q(val)}')")
        elif key == "not_contains":
            conds.append(f"action_out is search('{_q(val)}')")
        elif key == "regex":
            conds.append(f"action_out is not search('{_q(val)}')")
        elif key == "lines_ge":
            conds.append(f"action_out_lines | length < {int(val)}")
    if conds:
        tasks.append({
            "name": f"assert step {step.name!r} passed",
            "ansible.builtin.assert": {
                "that": [f"not ({c})" for c in conds],
                "fail_msg": f"step {step.name!r} failed its declarative assert: {step.assert_}",
                "success_msg": f"step {step.name!r} passed",
            },
        })
    playbook = {"hosts": "localhost", "gather_facts": False, "tasks": tasks}
    return _dump(playbook)


# ---------------------------------------------------------------------------
# Verify + fetch
# ---------------------------------------------------------------------------

def build_verify_playbook(pings: List[Dict[str, str]]) -> str:
    """Reachability-check playbook (one play per ping source node)."""
    plays = []
    for p in pings:
        tasks = [{
            "name": "ensure iproute2 and iputils-ping are installed",
            "ansible.builtin.raw": (
                "sh -c '(command -v ip >/dev/null 2>&1 && command -v ping >/dev/null 2>&1) || "
                "{ apt-get update -qq && "
                "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iproute2 iputils-ping; }' "
                ">/tmp/nettools_install.log 2>&1"
            ),
            "ignore_errors": True,
        }]
        if p.get("self"):
            tasks.append({
                "name": f"ensure {p['node']} has its LAN address",
                "ansible.builtin.raw": (
                    f"ip addr replace {p['self']} dev eth1 && ip link set dev eth1 up"
                ),
            })
        tasks.append({
            "name": f"ping {p['target']} (3 tries)",
            "ansible.builtin.raw": f"ping -c 3 -W 2 {p['target']}",
            "register": "pingr",
            "failed_when": "pingr.rc != 0",
        })
        plays.append({"hosts": p["node"], "gather_facts": False, "tasks": tasks})
    return _dump(plays)


def build_fetch_playbook(
    captures: List[Capture],
    step_logs: List[Dict[str, Any]],
    results_dir: Path,
) -> str:
    """Fetch each capture's PCAP and each step's log entries into results/."""
    tasks = []
    for cap in captures:
        out = cap.out_name()
        tasks.append({
            "name": f"fetch capture {cap.id} ({out})",
            "delegate_to": cap.node,
            "ansible.builtin.fetch": {
                "src": f"/tmp/{out}",
                "dest": f"{results_dir}/{out}",
                "flat": True,
            },
            "ignore_errors": True,
        })
    for log in step_logs:
        node = log.get("node") or "localhost"
        tasks.append({
            "name": f"fetch log {log['output']} from {node}",
            "delegate_to": node,
            "ansible.builtin.fetch": {
                "src": str(log["src"]),
                "dest": f"{results_dir}/{log['output']}",
                "flat": True,
            },
            "ignore_errors": True,
        })
    if not tasks:
        return _dump([{"hosts": "all", "gather_facts": False, "tasks": []}])
    hosts = set()
    for cap in captures:
        hosts.add(cap.node)
    for log in step_logs:
        hosts.add(log.get("node") or "localhost")
    playbook = {"hosts": sorted(hosts), "gather_facts": False, "tasks": tasks}
    return _dump(playbook)


# ---------------------------------------------------------------------------
# Top-level generator
# ---------------------------------------------------------------------------

def generate_playbooks(
    work_dir: Path,
    scenario_name: str,
    scenario,
    results_dir: Path,
) -> Dict[str, Path]:
    """Write all generated playbooks into work_dir; return filename -> path."""
    work_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    captures = scenario.captures
    files: Dict[str, str] = {}

    if captures:
        files["scenario_capture.yml"] = build_capture_playbook(captures)

    # Per-action playbooks (shell/raw/playbook/command) + a per-step assert
    # playbook. daemon/daemon_kill actions are NOT generated — the runner runs
    # them controller-side via the daemon manager.
    for i, step in enumerate(scenario.steps):
        for j, action in enumerate(step.do):
            if action.kind in ("daemon", "daemon_kill"):
                continue
            files[f"scenario_step_{i}_{step.name}_action_{j}.yml"] = (
                build_action_playbook(step, j, work_dir.parent)
            )
        if step.assert_:
            files[f"scenario_step_{i}_{step.name}_assert.yml"] = (
                build_assert_playbook(step)
            )

    # Collect step log entries (resolve node=None -> step's primary node).
    step_logs: List[Dict[str, Any]] = []
    for step in scenario.steps:
        primary = None
        for action in step.do:
            if action.kind in ("shell", "raw"):
                primary = action.node
                break
        for log in step.log:
            entry = dict(log)
            if not entry.get("node"):
                entry["node"] = primary
            step_logs.append(entry)
    files["scenario_fetch.yml"] = build_fetch_playbook(captures, step_logs, results_dir)

    if scenario.verify_pings:
        files["scenario_verify.yml"] = build_verify_playbook(scenario.verify_pings)

    out: Dict[str, Path] = {}
    for fname, content in files.items():
        p = work_dir / fname
        p.write_text(content)
        out[fname] = p
    return out
