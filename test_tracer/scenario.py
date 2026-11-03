"""
Scenario topology model.

Reads the `scenario:` block from the netlab topology file and turns it into a
structured object that the orchestrator can drive.

Netlab validates topology attributes strictly, so a custom top-level
`scenario:` block must be declared in `defaults.attributes.global` (see
netlab's "Custom Attributes" documentation). :class:`LabTopology` reports
whether that declaration is present so the orchestrator can materialize a
normalized copy of the topology before invoking netlab.
"""

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class ScenarioError(Exception):
    """Raised when the scenario definition is invalid."""


@dataclass
class CaptureSpec:
    """A single packet-capture request."""

    node: str
    interface: str = "eth1"
    filter: Optional[str] = None          # BPF filter expression
    output: Optional[str] = None          # local filename (default: <node>_<intf>.pcap)
    seconds: Optional[int] = None         # capture duration (default: until harvest phase)
    count: Optional[int] = None           # max number of packets
    background: bool = True

    def out_name(self) -> str:
        if self.output:
            return self.output
        return f"{self.node}_{self.interface}.pcap".replace("/", "_").replace(" ", "_")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CaptureSpec":
        if not isinstance(data, dict) or "node" not in data:
            raise ScenarioError(f"capture entry must be a mapping with a 'node' key: {data!r}")
        return cls(
            node=str(data["node"]),
            interface=str(data.get("interface", "eth1")),
            filter=data.get("filter"),
            output=data.get("output"),
            seconds=data.get("seconds"),
            count=data.get("count"),
            background=bool(data.get("background", True)),
        )


@dataclass
class ScriptSpec:
    """A traffic-generation step: either an Ansible playbook or a raw command."""

    playbook: Optional[str] = None        # path to an Ansible playbook (relative to lab dir)
    command: Optional[str] = None         # raw command executed on the controller
    nodes: Optional[str] = None           # optional netlab node subset for the playbook
    wait_after: Optional[int] = None      # seconds to sleep after the step

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScriptSpec":
        if not isinstance(data, dict):
            raise ScenarioError(f"scripts entry must be a mapping: {data!r}")
        if "playbook" in data and "command" in data:
            raise ScenarioError("scripts entry cannot have both 'playbook' and 'command'")
        if "playbook" not in data and "command" not in data:
            raise ScenarioError(f"scripts entry needs a 'playbook' or 'command': {data!r}")
        return cls(
            playbook=data.get("playbook"),
            command=data.get("command"),
            nodes=data.get("nodes"),
            wait_after=data.get("wait_after"),
        )


@dataclass
class ScenarioSpec:
    """Parsed contents of the `scenario:` block."""

    name: str = "scenario"
    results_dir: str = "results"          # where PCAPs are harvested (relative to lab dir)
    verify_wait: int = 0                  # seconds to wait for routing convergence
    verify: List[Dict[str, Any]] = field(default_factory=list)
    verify_pings: List[Dict[str, str]] = field(default_factory=list)
    captures: List[CaptureSpec] = field(default_factory=list)
    scripts: List[ScriptSpec] = field(default_factory=list)
    teardown: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScenarioSpec":
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ScenarioError(f"'scenario' must be a mapping, got {type(data).__name__}")
        spec = cls(raw=copy.deepcopy(data))
        spec.name = str(data.get("name", "scenario"))

        verify = data.get("verify", [])
        if not isinstance(verify, list):
            raise ScenarioError("'scenario.verify' must be a list")
        for entry in verify:
            if not isinstance(entry, dict):
                raise ScenarioError(f"'scenario.verify' entries must be mappings: {entry!r}")
            if "wait" in entry:
                spec.verify_wait += int(entry["wait"])
            if "ping" in entry:
                ping = entry["ping"]
                if not isinstance(ping, dict) or "node" not in ping or "target" not in ping:
                    raise ScenarioError(
                        f"verify.ping must be a mapping with 'node' and 'target': {ping!r}")
                item = {"node": str(ping["node"]), "target": str(ping["target"])}
                if "self" in ping:
                    item["self"] = str(ping["self"])
                spec.verify_pings.append(item)
            spec.verify.append(entry)

        for c in data.get("capture", []) or []:
            spec.captures.append(CaptureSpec.from_dict(c))
        for s in data.get("scripts", []) or []:
            spec.scripts.append(ScriptSpec.from_dict(s))

        if "results" in data:
            spec.results_dir = str(data["results"])
        if "teardown" in data:
            spec.teardown = data["teardown"] or {}
        return spec


def declaration_present(data: Dict[str, Any]) -> bool:
    """True if `defaults.attributes.global.scenario` is declared."""
    attrs = (data.get("defaults") or {}).get("attributes", {})
    if not isinstance(attrs, dict):
        return False
    globs = attrs.get("global")
    return isinstance(globs, dict) and "scenario" in globs


@dataclass
class LabTopology:
    """A netlab topology file plus its `scenario:` block."""

    path: Path
    scenario: ScenarioSpec
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)
    declaration_present: bool = False

    @property
    def lab_dir(self) -> Path:
        """Directory where netlab artifacts live (directory of the topology file)."""
        return self.path.resolve().parent

    @classmethod
    def load(cls, path: Path) -> "LabTopology":
        path = Path(path)
        if not path.exists():
            raise ScenarioError(f"topology file not found: {path}")
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ScenarioError(f"topology file {path} must contain a YAML mapping")
        if not data.get("nodes"):
            raise ScenarioError(f"topology {path} has no 'nodes' section")

        has_scenario = "scenario" in data
        scenario = ScenarioSpec.from_dict(data.get("scenario") if has_scenario else {})
        if not has_scenario:
            # A topology without a scenario block is valid, but not very useful;
            # keep going so `--phase create` etc. still works, and warn in CLI.
            pass

        return cls(
            path=path,
            scenario=scenario,
            raw=copy.deepcopy(data),
            declaration_present=declaration_present(data),
        )

    def normalized(self) -> Dict[str, Any]:
        """Return the full topology with the scenario attribute declaration ensured."""
        data = copy.deepcopy(self.raw)
        if "scenario" in data:
            attrs = data.setdefault("defaults", {}).setdefault("attributes", {})
            if not isinstance(attrs, dict):
                raise ScenarioError("defaults.attributes.global must be a mapping")
            globs = attrs.setdefault("global", {})
            if not isinstance(globs, dict):
                raise ScenarioError("defaults.attributes.global must be a mapping")
            globs.setdefault("scenario", {"type": "dict"})
        return data
