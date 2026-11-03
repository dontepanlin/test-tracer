# test-tracer scenario schema

The complete `scenario:` block schema, plus the netlab topology conventions it
sits inside. This is the source of truth for authoring a lab.

## Topology file layout

A lab is one YAML file. The network part is plain netlab; the `scenario:` block
is the test.

```yaml
name: <lab-name>            # netlab lab id
provider: clab              # clab | libvirt | vagrant | ...
defaults:
  device: linux             # default node device
  group: test-group
  mgmt_base: 192.0.2
  mgmt_prefix: 24
  mgmt_mode: static
  addressing:
    v4_pool: 10.1.0.0/24    # optional addressing pool
  multilab.id: <unique>     # optional: avoid collisions with other labs
  attributes:
    global:
      scenario: { type: dict }   # REQUIRED so netlab accepts the block

nodes:
  <node-a>:
    image: debian:13        # optional (per-node image)
  <node-b>: { device: linux }
  <router>: { device: eos } # optional: a real routing device

links:
  - interfaces:
      - node: <node-a>
        ipv4: 10.10.0.10/24
      - node: <node-b>
        ipv4: 10.10.0.11/24
    prefix: 10.10.0.0/24
    tc: { delay: 50, loss: 2 }   # optional native tc impairment (ms, %)
  - interfaces: [ <node-b>, <router> ]

scenario:
  ...
```

Notes:

- **`tc:`** on a link is applied by `netlab up` automatically (native netem).
  Units: `delay` in ms, `loss` in percent. For complex netem, put `tc qdisc`
  commands in a setup playbook and run them on the node.
- **Static IPs** (per-interface `ipv4:`) are the reliable way to address nodes
  in a plain L2 lab. Without them, `addressing.v4_pool` is used.
- The runner auto-materializes a normalized copy that includes the
  `attributes.global.scenario` declaration if you omit it, but declare it
  explicitly so `netlab` itself also accepts the file.

## `scenario:` block

```yaml
scenario:
  name: <snake_case_scenario_id>   # used in scenario-<name>/ work dir
  results: results                 # where PCAPs + logs are harvested (rel. to lab dir)

  verify:                          # optional connectivity check
    - wait: 30                     # seconds to wait for convergence (sums)
    - ping:                        # optional direct reachability check
        node: <source-node>
        target: <dest-ip>
        self: <ip/prefix>          # optional: (re)address source's LAN intf first

  captures:                        # NAMED capture definitions (the "how")
    - id: <capture_id>
      node: <node>
      link: 0                      # XOR interface:  logical link id (0 = first data link)
      # interface: eth0            # XOR link:  explicit container ifname
      filter: "<BPF filter>"       # optional
      output: <name>.pcap          # optional (default <capture_id>.pcap)

  always_capture: [<capture_id>]   # optional: active for the WHOLE timeline

  steps:                           # the TIMELINE (the "when" + "what")
    - name: <step_name>
      capture: [<capture_id>, ...]  # captures active DURING this step
      do:                          # list of actions, in order (>=1 required)
        - <action>
      assert: { <assert_key>: <val> }   # optional declarative pass/fail
      log:                         # optional log entries to harvest
        - node: <node>
          src: /path/on/node
          output: <local>.log
```

### Captures

| key | required | meaning |
| --- | --- | --- |
| `id` | yes | unique name; referenced by steps' `capture:`; used in default output |
| `node` | yes | netlab node to capture on |
| `link` | xor | logical link index. `link: 0` = the node's first data interface, resolved at runtime to the real container ifname via `hostvars[<node>].interfaces[0].ifname`. |
| `interface` | xor | explicit container interface name (e.g. `eth0` for the mgmt/egress interface). Use when the capture is not on the first data link. |
| `filter` | no | BPF filter expression |
| `output` | no | local filename, default `<id>.pcap` |

A capture uses **either** `link` **or** `interface`, never both.

### Steps

A step is a self-contained test case.

| key | required | meaning |
| --- | --- | --- |
| `name` | yes | unique step id |
| `do` | yes | non-empty list of actions, run in order |
| `capture` | no | list of capture ids active during this step |
| `assert` | no | declarative pass/fail (see below) |
| `log` | no | log files to fetch into `results/` |

### Actions (each `do:` entry has exactly one of these)

| key | runs on | meaning |
| --- | --- | --- |
| `playbook: <path>` | controller | run an Ansible playbook against `hosts.yml` (path relative to lab dir, or under `playbooks/`). Typical for SUT setup (install + config). |
| `shell: <cmd>` | `node` | Jinja-templated shell command on the node. Use `node:` to target a node (defaults to the step's primary node). |
| `raw: <cmd>` | `node` | raw command, no remote interpreter needed. Good for one-shot checks/commands. |
| `command: <cmd>` | controller | controller-side shell command (e.g. `sleep`). |
| `daemon: <cmd>` | `node` | start a **long-lived** process in the node's container, detached (`docker exec -d`). Survives the task. Use for SUT servers/proxies. |
| `daemon_kill: <pattern>` | `node` | kill the node's daemon(s) matching `<pattern>` (pkill -f). Use to stop a `daemon`. |

`daemon`/`daemon_kill` **require** `node:`. Any action may carry a `label:` (for
the report).

**Why `daemon` exists:** a process backgrounded *inside* an Ansible `shell`/
`raw` task (e.g. `nohup ... &`) is killed when that task's docker-exec session
tears down — there's no init in these containers to adopt it. So **never start
a long-lived process from a `shell`/`raw`/`playbook` action.** Start it with a
`daemon:` action (or let the runner manage it for captures) and stop it with
`daemon_kill:`. A typical SUT bring-up is: `playbook:` (install + config) →
`daemon:` (start the server) → `shell:` (verify the port is listening) →
`assert: { contains: "<UP marker>" }`.

`shell`/`raw` support Jinja templating, so you can reference other nodes:
`hostvars['<node>'].interfaces[0].ipv4` gives that node's first data-link IPv4.

### Asserts (declarative, AND-combined)

Evaluated against the step's **combined stdout** (all actions) and the **last
action's exit code**.

| key | meaning |
| --- | --- |
| `rc_eq: N` | last action's exit code == N |
| `rc_neq: N` | last action's exit code != N |
| `contains: <s>` | combined output contains `<s>` |
| `not_contains: <s>` | combined output does not contain `<s>` |
| `regex: <r>` | combined output matches regex `<r>` |
| `lines_ge: N` | combined output has at least N lines |

Write the assert so the **correct** system passes and a **broken** one fails.
For a negative test (the request *must* fail), assert `not_contains` on the
success marker (or `rc_neq: 0`).

### Capture window semantics

- A capture starts right before the first step that lists it and stops right
  after the last **consecutive** step that lists it. Consecutive steps sharing
  a capture = one continuous capture (no restart gap).
- `always_capture` runs for the whole timeline.
- If you capture in step 2 but not step 3, the step-3 traffic is **not** in the
  PCAP. This is the mechanism that scopes a PCAP to a single test case.

## Validation (what `scenario describe` checks)

- every `capture` has `id`, `node`, and exactly one of `link`/`interface`.
- capture ids are unique.
- every step has a `name` and a non-empty `do`.
- every `capture:` id referenced by a step exists.
- every `always_capture` id exists.
- every `assert` key is one of the valid keys; `regex` compiles.
- every `log` entry has `src` and `output`.

The runner fails fast with a `[FATAL] <message>` on any of these — use
`describe` to validate before a real run.

## Running

```bash
uv run scenario describe -t <topology.yml>                 # validate + readable timeline
uv run scenario describe -t <topology.yml> --format yaml   # round-trippable YAML
uv run scenario -t <topology.yml> --dry-run                # plan, execute nothing
uv run scenario -t <topology.yml> --phase steps,harvest    # re-run timeline on a live lab
uv run scenario -t <topology.yml> --keep-lab               # skip teardown
uv run scenario -t <topology.yml>                          # full lifecycle (host has passwordless sudo ip)
```

**Host without passwordless `sudo ip`** (common on desktops): netlab's initial
config uses `sudo ip netns exec ...`, which will fail. Run with:

```bash
uv run scenario -t <topology.yml> --no-validate --up-arg=--no-config
```

- `--no-validate` is a **scenario** flag (skip `netlab validate`).
- `--up-arg=--no-config` is **forwarded to `netlab up`** (skip the privileged
  initial config). Use the `=` form — a bare `--up-arg --no-config` is eaten by
  argparse. Multiple `--up-arg=` flags may be repeated.
- With `--no-config`, netlab does not address the data links, so the scenario
  must address them itself (an early `address-links` step using
  `hostvars[<node>].interfaces[0]`) and install any missing tools
  (`iproute2`, `iputils-ping`, `curl`, `tcpdump`) since the base image lacks
  them. The socks lab is the reference for this pattern.

The user must be in the `docker` group (the runner drives containers with
`docker exec`); no sudo is otherwise required.
