"""
Scenario model.

A scenario is a **named-capture + step-timeline** structure that lives in the
``scenario:`` block of a netlab topology file. It defines, in one place:

* **what to capture** and **how** (named :class:`Capture` definitions),
* **when to capture** and **what traffic to generate** (the ordered
  :class:`Step` timeline, where each step declares which captures are active
  during it).

This is the LLM-friendly form: a scenario is a small flat declarative YAML list
of captures and steps. See ``PLAN-refactor.md`` for the design.
"""

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class ScenarioError(Exception):
    """Raised when the scenario definition is invalid."""


_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_VALID_ASSERT_KEYS = {
    "rc_eq", "rc_neq", "contains", "not_contains", "regex", "lines_ge",
}


def _clean(value: str) -> str:
    """Collapse a multi-line value to a single line (for embedding in a name)."""
    return " ".join(str(value).split())


def _safe_id(value: str, what: str) -> str:
    """Validate that a name/id is safe to use in a filename or ansible var."""
    if not isinstance(value, str) or not value:
        raise ScenarioError(f"{what} must be a non-empty string: {value!r}")
    if not _SAFE_NAME.match(value):
        raise ScenarioError(
            f"{what} {value!r} contains invalid characters "
            "(allowed: letters, digits, '.', '_', '-')")
    return value


# ---------------------------------------------------------------------------
# Captures
# ---------------------------------------------------------------------------

@dataclass
class Capture:
    """A named packet-capture definition (the *how*)."""

    id: str
    node: str
    link: Optional[int] = None            # logical link id (resolved at runtime)
    interface: Optional[str] = None       # explicit container ifname
    filter: Optional[str] = None          # BPF filter expression
    output: Optional[str] = None          # local filename (default: <id>.pcap)

    def out_name(self) -> str:
        if self.output:
            return self.output
        return f"{self.id}.pcap"

    @classmethod
    def from_dict(cls, data: Dict[str, Any], index: int) -> "Capture":
        where = f"capture[{index}]"
        if not isinstance(data, dict):
            raise ScenarioError(f"{where} must be a mapping: {data!r}")
        cap_id = data.get("id")
        if cap_id is None:
            raise ScenarioError(f"{where} is missing required key 'id'")
        cap_id = _safe_id(str(cap_id), f"{where}.id")
        if "node" not in data:
            raise ScenarioError(f"{where} ({cap_id}) is missing required key 'node'")
        if "link" in data and "interface" in data:
            raise ScenarioError(
                f"{where} ({cap_id}) cannot have both 'link' and 'interface'")
        if "link" not in data and "interface" not in data:
            raise ScenarioError(
                f"{where} ({cap_id}) needs 'link' (logical) or 'interface' "
                f"(container ifname)")
        return cls(
            id=cap_id,
            node=str(data["node"]),
            link=(int(data["link"]) if "link" in data else None),
            interface=(str(data["interface"]) if "interface" in data else None),
            filter=data.get("filter"),
            output=data.get("output"),
        )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

_ACTION_KEYS = ("playbook", "shell", "raw", "command", "daemon", "daemon_kill")


@dataclass
class Action:
    """A single traffic step inside a :class:`Step`'s ``do:`` list."""

    playbook: Optional[str] = None        # path to an ansible playbook
    shell: Optional[str] = None           # jinja-templated command (run on node)
    raw: Optional[str] = None             # raw command, no remote interpreter
    command: Optional[str] = None         # controller-side command
    daemon: Optional[str] = None          # long-lived process, via `docker exec -d`
    daemon_kill: Optional[str] = None     # pkill pattern for a daemon
    node: Optional[str] = None            # target node for shell/raw/daemon
    label: Optional[str] = None           # optional label (for the report)

    @property
    def kind(self) -> str:
        for k in _ACTION_KEYS:
            if getattr(self, k) is not None:
                return k
        raise ScenarioError(f"action has no known type: {self.__dict__!r}")

    @classmethod
    def from_dict(cls, data: Dict[str, Any], index: int) -> "Action":
        where = f"do[{index}]"
        if not isinstance(data, dict):
            raise ScenarioError(f"{where} must be a mapping: {data!r}")
        kinds = [k for k in _ACTION_KEYS if k in data]
        if len(kinds) != 1:
            raise ScenarioError(
                f"{where} must have exactly one of "
                f"playbook/shell/raw/command/daemon/daemon_kill, "
                f"got {kinds or 'none'}")
        if kinds[0] in ("daemon", "daemon_kill") and "node" not in data:
            raise ScenarioError(f"{where} ({kinds[0]}) requires a 'node'")
        return cls(
            playbook=data.get("playbook"),
            shell=data.get("shell"),
            raw=data.get("raw"),
            command=data.get("command"),
            daemon=data.get("daemon"),
            daemon_kill=data.get("daemon_kill"),
            node=data.get("node"),
            label=data.get("label"),
        )


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

@dataclass
class Step:
    """One test case in the timeline (the *when* + *what*)."""

    name: str
    do: List[Action] = field(default_factory=list)
    capture: List[str] = field(default_factory=list)   # capture ids active during this step
    assert_: Optional[Dict[str, Any]] = None           # declarative pass/fail (YAML key: `assert`)
    log: List[Dict[str, Any]] = field(default_factory=list)   # log entries to harvest

    @property
    def assertion(self) -> Optional[Dict[str, Any]]:
        """The declarative pass/fail condition (YAML key `assert`)."""
        return self.assert_

    @classmethod
    def from_dict(cls, data: Dict[str, Any], index: int) -> "Step":
        where = f"steps[{index}]"
        if not isinstance(data, dict):
            raise ScenarioError(f"{where} must be a mapping: {data!r}")
        name = data.get("name")
        if not name:
            raise ScenarioError(f"{where} is missing required key 'name'")
        name = _safe_id(str(name), f"{where}.name")

        do_raw = data.get("do", [])
        if not isinstance(do_raw, list) or not do_raw:
            raise ScenarioError(f"{where} ({name}) needs a non-empty 'do' list")
        do = [Action.from_dict(a, i) for i, a in enumerate(do_raw)]

        cap = data.get("capture", [])
        if not isinstance(cap, list):
            raise ScenarioError(f"{where} ({name}).capture must be a list")
        cap_ids = [_safe_id(str(c), f"{where} ({name}).capture") for c in cap]

        assert_data = data.get("assert")
        if assert_data is not None:
            if not isinstance(assert_data, dict):
                raise ScenarioError(f"{where} ({name}).assert must be a mapping")
            bad = set(assert_data) - _VALID_ASSERT_KEYS
            if bad:
                raise ScenarioError(
                    f"{where} ({name}).assert has unknown key(s): {sorted(bad)} "
                    f"(valid: {sorted(_VALID_ASSERT_KEYS)})")
            if "regex" in assert_data:
                try:
                    re.compile(assert_data["regex"])
                except re.error as ex:
                    raise ScenarioError(
                        f"{where} ({name}).assert.regex is invalid: {ex}")

        log = data.get("log", [])
        if not isinstance(log, list):
            raise ScenarioError(f"{where} ({name}).log must be a list")
        for li, entry in enumerate(log):
            if not isinstance(entry, dict) or "src" not in entry or "output" not in entry:
                raise ScenarioError(
                    f"{where} ({name}).log[{li}] needs 'src' and 'output': {entry!r}")
            if "node" not in entry:
                entry = dict(entry)
                entry["node"] = None   # resolved to the step's node later

        return cls(
            name=name,
            do=do,
            capture=cap_ids,
            assert_=assert_data,
            log=[dict(e) for e in log],
        )


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    """Parsed contents of the ``scenario:`` block."""

    name: str = "scenario"
    results_dir: str = "results"
    verify_wait: int = 0
    verify_pings: List[Dict[str, str]] = field(default_factory=list)
    captures: List[Capture] = field(default_factory=list)
    steps: List[Step] = field(default_factory=list)
    always_capture: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def capture_map(self) -> Dict[str, Capture]:
        return {c.id: c for c in self.captures}

    def validate_references(self) -> None:
        """Cross-check capture ids referenced by steps against definitions."""
        known = set(self.capture_map())
        for c in self.always_capture:
            if c not in known:
                raise ScenarioError(
                    f"always_capture references unknown capture id {c!r}")
        for step in self.steps:
            for c in step.capture:
                if c not in known:
                    raise ScenarioError(
                        f"step {step.name!r} references unknown capture id {c!r}")
        # duplicate capture ids
        ids = [c.id for c in self.captures]
        dups = {i for i in ids if ids.count(i) > 1}
        if dups:
            raise ScenarioError(f"duplicate capture ids: {sorted(dups)}")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Scenario":
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ScenarioError(f"'scenario' must be a mapping, got {type(data).__name__}")
        spec = cls(raw=copy.deepcopy(data))
        spec.name = _safe_id(str(data.get("name", "scenario")), "scenario.name")

        # verify:
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

        for i, c in enumerate(data.get("captures", []) or []):
            spec.captures.append(Capture.from_dict(c, i))
        for i, s in enumerate(data.get("steps", []) or []):
            spec.steps.append(Step.from_dict(s, i))

        always = data.get("always_capture", [])
        if not isinstance(always, list):
            raise ScenarioError("'scenario.always_capture' must be a list")
        spec.always_capture = [_safe_id(str(c), "always_capture") for c in always]

        if "results" in data:
            spec.results_dir = str(data["results"])
        return spec


def declaration_present(data: Dict[str, Any]) -> bool:
    """True if ``defaults.attributes.global.scenario`` is declared."""
    attrs = (data.get("defaults") or {}).get("attributes", {})
    if not isinstance(attrs, dict):
        return False
    globs = attrs.get("global")
    return isinstance(globs, dict) and "scenario" in globs


@dataclass
class LabTopology:
    """A netlab topology file plus its ``scenario:`` block."""

    path: Path
    scenario: Scenario
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
        if not has_scenario:
            raise ScenarioError(
                f"topology {path} has no 'scenario' block; a scenario is required")
        scenario = Scenario.from_dict(data["scenario"])
        scenario.validate_references()

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
                raise ScenarioError("defaults.attributes must be a mapping")
            globs = attrs.setdefault("global", {})
            if not isinstance(globs, dict):
                raise ScenarioError("defaults.attributes.global must be a mapping")
            globs.setdefault("scenario", {"type": "dict"})
        return data
