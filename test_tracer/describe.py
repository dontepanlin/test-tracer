"""
Render a parsed scenario (and its lab) into a readable, LLM-friendly form.

This is the "LLM-friendly scenario maker" surface:

* ``describe -t <topology>``            -> human-readable timeline (default)
* ``describe -t <topology> --format yaml`` -> canonical round-trippable YAML
* ``describe -t <topology> --format json`` -> JSON (for tooling)

An LLM can **write** a scenario as the ``--format yaml`` output and **read**
the result of a run back through the same renderer. The YAML form is the
canonical scenario description; the text form adds the capture-window
annotation ("active during steps ...") that makes the *when* explicit.
"""

import json
from typing import Any, Dict, List

import yaml

from .scenario import LabTopology, Scenario, ScenarioError


def _capture_windows(scenario: Scenario) -> Dict[str, List[int]]:
    """Map capture id -> the (1-based) step indexes where it is active."""
    windows: Dict[str, List[int]] = {c.id: [] for c in scenario.captures}
    for i, step in enumerate(scenario.steps, 1):
        for cap_id in step.capture:
            windows.setdefault(cap_id, []).append(i)
    # always_capture is active across all steps.
    all_steps = list(range(1, len(scenario.steps) + 1))
    for cap_id in scenario.always_capture:
        windows.setdefault(cap_id, []).extend(all_steps)
    return windows


def _action_text(action) -> str:
    kind = action.kind
    if kind == "playbook":
        return f"playbook: {action.playbook}"
    if kind == "command":
        return f"command: {action.command}"
    if kind == "daemon":
        return f"daemon on {action.node}: {action.daemon}"
    if kind == "daemon_kill":
        return f"daemon_kill on {action.node}: {action.daemon_kill}"
    target = f" on {action.node}" if action.node else ""
    body = (action.shell if kind == "shell" else action.raw or "")
    body = " ".join(body.split())
    if len(body) > 100:
        body = body[:97] + "..."
    return f"{kind}:{target} {body}".strip()


def describe_text(topo: LabTopology) -> str:
    s = topo.scenario
    lines: List[str] = []
    lines.append(f"Scenario: {s.name}")
    lines.append(f"Topology: {topo.path}")
    lines.append("")

    # Lab summary
    nodes = topo.raw.get("nodes", {})
    links = topo.raw.get("links", [])
    lines.append(f"Lab: {len(nodes)} node(s), {len(links)} link(s)")
    for name, node in nodes.items():
        node = node or {}
        dev = node.get("device", "linux")
        img = node.get("image", "")
        suffix = f" ({img})" if img else ""
        lines.append(f"  - {name}: {dev}{suffix}")
    for link in links:
        ifaces = link.get("interfaces", []) if isinstance(link, dict) else link
        tc = link.get("tc") if isinstance(link, dict) else None
        names = []
        for item in ifaces:
            if isinstance(item, dict):
                names.append(item.get("node", "?"))
            else:
                names.append(str(item))
        tcs = f"  tc={tc}" if tc else ""
        lines.append(f"  link: {' - '.join(names)}{tcs}")
    lines.append("")

    # Captures
    windows = _capture_windows(s)
    lines.append("Captures:")
    if not s.captures:
        lines.append("  (none)")
    for cap in s.captures:
        where = f"link {cap.link}" if cap.link is not None else f"interface {cap.interface}"
        filt = f"  filter: {cap.filter}" if cap.filter else ""
        w = windows.get(cap.id, [])
        if cap.id in s.always_capture:
            wtext = "always (whole timeline)"
        elif w:
            wtext = "active during step(s) " + ", ".join(str(x) for x in w)
        else:
            wtext = "not scheduled (defined but never started)"
        lines.append(f"  - {cap.id}: {cap.node} {where} -> {cap.out_name()}{filt}")
        lines.append(f"      {wtext}")
    lines.append("")

    # Timeline
    lines.append("Timeline:")
    for i, step in enumerate(s.steps, 1):
        caps = step.capture
        captext = f"  [capture: {', '.join(caps)}]" if caps else ""
        lines.append(f"  {i}. {step.name}{captext}")
        for a in step.do:
            lines.append(f"       - {_action_text(a)}")
        if step.assert_:
            lines.append(f"       assert: {step.assert_}")
        if step.log:
            logs = ", ".join(l["output"] for l in step.log)
            lines.append(f"       log: {logs}")
    lines.append("")
    return "\n".join(lines)


def describe_yaml(topo: LabTopology) -> str:
    """The canonical, round-trippable scenario YAML (the form an LLM writes)."""
    s = topo.scenario
    doc: Dict[str, Any] = {"name": s.name, "results": s.results_dir}

    if s.verify_wait or s.verify_pings:
        verify: List[Dict[str, Any]] = []
        if s.verify_wait:
            verify.append({"wait": s.verify_wait})
        for p in s.verify_pings:
            ping: Dict[str, Any] = {"node": p["node"], "target": p["target"]}
            if "self" in p:
                ping["self"] = p["self"]
            verify.append({"ping": ping})
        doc["verify"] = verify

    if s.captures:
        doc["captures"] = []
        for cap in s.captures:
            entry: Dict[str, Any] = {"id": cap.id, "node": cap.node}
            if cap.link is not None:
                entry["link"] = cap.link
            else:
                entry["interface"] = cap.interface
            if cap.filter:
                entry["filter"] = cap.filter
            entry["output"] = cap.out_name()
            doc["captures"].append(entry)

    if s.always_capture:
        doc["always_capture"] = list(s.always_capture)

    doc["steps"] = []
    for step in s.steps:
        entry: Dict[str, Any] = {"name": step.name}
        if step.capture:
            entry["capture"] = list(step.capture)
        do = []
        for a in step.do:
            d: Dict[str, Any] = {}
            kind = a.kind
            if kind == "playbook":
                d["playbook"] = a.playbook
            elif kind == "command":
                d["command"] = a.command
            elif kind == "daemon":
                d["node"] = a.node
                d["daemon"] = a.daemon
            elif kind == "daemon_kill":
                d["node"] = a.node
                d["daemon_kill"] = a.daemon_kill
            else:
                if a.node:
                    d["node"] = a.node
                d[kind] = a.shell if kind == "shell" else a.raw
            if a.label:
                d["label"] = a.label
            do.append(d)
        entry["do"] = do
        if step.assert_:
            entry["assert"] = dict(step.assert_)
        if step.log:
            entry["log"] = [dict(l) for l in step.log]
        doc["steps"] = doc.get("steps", [])
        doc["steps"].append(entry)

    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=2000)


def _json_safe(value):
    """Convert a parsed-YAML value into something json.dumps can serialize.

    PyYAML turns a bare scalar like ``HTTP/ 200`` into a Python ``str`` that is
    fine for YAML but the value itself is still a str — the real issue is
    scalars PyYAML may interpret as non-JSON (e.g. a string that YAML would
    re-emit needing quotes). We normalize every leaf to a JSON-friendly type.
    """
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # Anything else (str, bytes, ...) -> str.
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def describe_json(topo: LabTopology) -> str:
    doc = yaml.safe_load(describe_yaml(topo))
    return json.dumps(_json_safe(doc), indent=2)


def describe(topo: LabTopology, fmt: str = "text") -> str:
    if fmt == "text":
        return describe_text(topo)
    if fmt == "yaml":
        return describe_yaml(topo)
    if fmt == "json":
        return describe_json(topo)
    raise ScenarioError(f"unknown describe format: {fmt!r} (text|yaml|json)")
