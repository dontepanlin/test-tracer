"""
Controller-side daemon manager for in-container long-lived processes.

Why this exists:

On the clab/docker provider, netlab's `linux` containers have **`bash` as PID
1** (no real init). A process started by an Ansible `shell`/`raw` task is in
the docker-exec session's process group/cgroup, and when the task (or the
enclosing `ansible-playbook`) ends, Docker tears that session down and kills
the process — even a `setsid nohup ... &` or double-forked one. This is why a
plain backgrounded `tcpdump` or proxy dies with `rc=-15` / silently.

The reliable fix: launch the daemon with **`docker exec -d`** (a *detached*
exec session) from the controller. A detached exec is a separate session not
tied to any Ansible task's process group, so the daemon survives. Stopping it
is a normal `docker exec ... pkill/kill`.

This is provider-specific to docker/clab (the provider this repo targets). The
container name is the node's `hostname` hostvar (netlab/clab set it to
`clab-<lab>-<node>`); the interface name is resolved from
`host_vars/<node>/topology.json`.
"""

import json
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from .scenario import Capture


class DaemonError(Exception):
    pass


def _host_var_path(lab_dir: Path, node: str) -> Optional[Path]:
    for name in ("topology.json", "topology"):
        p = lab_dir / "host_vars" / node / name
        if p.exists():
            return p
    return None


def _node_hostvars(lab_dir: Path, node: str) -> dict:
    p = _host_var_path(lab_dir, node)
    if p is None:
        return {}
    try:
        with open(p) as f:
            text = f.read()
        data = json.loads(text) if p.name.endswith(".json") else _yaml_load(text)
        return data or {}
    except Exception:  # noqa: BLE001
        return {}


def _yaml_load(text: str):
    import yaml
    return yaml.safe_load(text) or {}


def container_name(lab_dir: Path, node: str) -> str:
    """The docker container name for a netlab/clab node (its hostname hostvar)."""
    hv = _node_hostvars(lab_dir, node)
    name = hv.get("hostname")
    if not name:
        raise DaemonError(
            f"cannot resolve container name for node {node!r} "
            f"(no host_vars/{node}/topology.json or no 'hostname')")
    return str(name)


def interface_name(lab_dir: Path, node: str, link: Optional[int],
                   interface: Optional[str]) -> str:
    """Resolve a capture's interface to a container ifname."""
    if interface is not None:
        return interface
    hv = _node_hostvars(lab_dir, node)
    ifaces = hv.get("interfaces", []) or []
    idx = link or 0
    if not ifaces or idx >= len(ifaces):
        raise DaemonError(
            f"node {node!r} has no interface at index {idx} (for capture link:{link})")
    return str(ifaces[idx].get("ifname", f"eth{idx + 1}"))


def _docker(*args: str, check: bool = True, dry_run: bool = False) -> subprocess.CompletedProcess:
    cmd = ["docker", *args]
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return subprocess.run(cmd, check=check, capture_output=False)


def _capture_paths(cap: Capture):
    out = f"/tmp/{cap.out_name()}"
    return out, f"/tmp/cap.{cap.out_name()}.pid", f"/tmp/cap.{cap.out_name()}.log"


def start_capture(lab_dir: Path, cap: Capture, dry_run: bool = False) -> None:
    """Start a background tcpdump for a capture via `docker exec -d`.

    The launcher writes tcpdump's PID to a pidfile so :func:`stop_capture` can
    kill it precisely (no fragile `pkill` pattern matching, and no nested
    single-quote quoting that breaks `sh -c`). tcpdump is run detached
    (`-d`) so it survives the Ansible session teardown.
    """
    ctn = container_name(lab_dir, cap.node)
    ifname = interface_name(lab_dir, cap.node, cap.link, cap.interface)
    out, pidfile, log = _capture_paths(cap)
    bpf = f'-f "{cap.filter}"' if cap.filter else ""
    # Build the launcher as a single-quoted sh -c body with NO inner single
    # quotes. The tcpdump args are embedded directly (filter already quoted).
    launcher = (
        f"rm -f {out} {pidfile}; "
        f"tcpdump -i {ifname} {bpf} -s 0 -w {out} >{log} 2>&1 & "
        f"echo $! > {pidfile}"
    )
    if dry_run:
        return
    # Ensure tcpdump exists (best-effort safety net).
    _docker("exec", ctn, "sh", "-c",
            "command -v tcpdump >/dev/null 2>&1 || "
            "(apt-get update -qq && apt-get install -y -qq tcpdump) >/tmp/ensure_tcpdump.log 2>&1",
            check=False)
    # Run detached: `sh -c '<launcher>'` backgrounds tcpdump and records its
    # pid. The exec session exits immediately; tcpdump keeps running.
    _docker("exec", "-d", ctn, "sh", "-c", launcher)


def stop_capture(lab_dir: Path, cap: Capture, dry_run: bool = False) -> None:
    """Stop the capture's tcpdump (SIGTERM flushes the pcap to disk)."""
    ctn = container_name(lab_dir, cap.node)
    _out, pidfile, _log = _capture_paths(cap)
    if dry_run:
        return
    # Kill by pidfile. SIGTERM makes tcpdump flush buffered packets to disk
    # before exiting; escalate to SIGKILL if it doesn't die in time.
    _docker("exec", ctn, "sh", "-c",
            f"if [ -f {pidfile} ]; then kill -TERM \"$(cat {pidfile})\" 2>/dev/null; "
            f"n=0; while kill -0 \"$(cat {pidfile} 2>/dev/null)\" 2>/dev/null && [ $n -lt 10 ]; do "
            f"sleep 0.3; n=$((n+1)); done; "
            f"kill -KILL \"$(cat {pidfile})\" 2>/dev/null; fi; "
            f"rm -f {pidfile}; true",
            check=False)


def start_process(lab_dir: Path, node: str, cmd: str,
                  dry_run: bool = False) -> None:
    """Launch a long-lived process in a node's container via `docker exec -d`."""
    ctn = container_name(lab_dir, node)
    if dry_run:
        return
    _docker("exec", "-d", ctn, "sh", "-c", cmd)


def kill_process(lab_dir: Path, node: str, pattern: str,
                 dry_run: bool = False) -> None:
    """Kill processes matching `pattern` in a node's container."""
    ctn = container_name(lab_dir, node)
    if dry_run:
        return
    _docker("exec", ctn, "sh", "-c",
            f"pkill -f {shlex.quote(pattern)} 2>/dev/null; true",
            check=False)
