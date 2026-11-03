# test-tracer

netlab-based **network trace scenario runner**.

It treats **netlab** as the *Topology Provider* (builds multi-node topologies
and applies native `tc` link impairment) and **Ansible** as the *Test Engine*
(drives a **step timeline** of captures, traffic, and declarative asserts
against the inventory netlab generates), then harvests PCAPs and tears the lab
down.

The point of the project is to be an **LLM-friendly test-trace scenario maker**:
a scenario is a small, flat, declarative YAML that an LLM can author, review,
and edit. The `scenario describe` command renders any scenario back into a
readable timeline (or round-trippable YAML/JSON) — the form an LLM reads and
writes.

Lifecycle: **Create → Verify → Steps → Harvest → Teardown**.

## Work without sudo

1. Add to visudo to sudoers access to net utils

sudo visudo -f /etc/sudoers.d/netns-admin
```
user ALL=(root) NOPASSWD: /usr/sbin/ip netns *, /usr/sbin/tc *
```

2. Add user to docker group

```
sudo usermod -aG docker 
```

3. Setup clab to work without sudo

```
sudo chmod u+s `which containerlab`
sudo groupadd -r clab_admins
sudo usermod -aG clab_admins "$USER"
```

## Why not Molecule?

Molecule tests Ansible roles against *single* containers; it has no topology
abstraction. Here netlab builds the whole network (routers, switches, proxy
hosts, `tc`-impaired links) and produces a standard Ansible inventory
(`hosts.yml`). The scenario runner orchestrates: start lab → verify → run the
step timeline (captures + traffic + asserts) → fetch PCAPs → destroy lab.

## Setup

```bash
uv sync                 # installs networklab, ansible-core, pyyaml
which netlab            # provided by the networklab dependency
```

You also need a working virtualization provider on the host. The default
provider for this repo's examples is **containerlab (clab)** — see
`netlab show providers`.

## Usage

```bash
# Full lifecycle on the bundled SOCKS5 example
scenario -t socks/topology.yml

# Preview the planned timeline (no containers, no ansible)
scenario -t socks/topology.yml --dry-run

# Inspect the parsed scenario (LLM-readable timeline)
scenario describe -t socks/topology.yml
scenario describe -t socks/topology.yml --format yaml   # round-trippable
scenario describe -t socks/topology.yml --format json

# Only bring the lab up (leave it running)
scenario -t socks/topology.yml --phase create --keep-lab

# Re-run just the timeline + harvest against a running lab
scenario -t socks/topology.yml --phase steps,harvest
```

`scenario` is the console command installed into the venv;
`python main.py <same args>` works too.

### Run options

| flag | description |
| --- | --- |
| `-t, --topology FILE` | netlab topology file (default `topology.yml`) |
| `-p, --provider NAME` | provider override (`clab`, `libvirt`, `vagrant`, …) |
| `--phase LIST` | comma-separated subset: `create,verify,steps,harvest,teardown` |
| `--dry-run` | print the planned timeline + commands, execute nothing |
| `--keep-lab` | skip teardown |
| `--no-validate` | skip `netlab validate` |
| `--up-arg ARG` | extra arg passed to `netlab up` (repeatable) |

### describe options

| flag | description |
| --- | --- |
| `-t, --topology FILE` | the topology to describe |
| `--format` | `text` (default), `yaml`, or `json` |

## The scenario model (one place: what + how + when)

A `scenario:` block defines everything in **one place**: *what* to capture,
*how* to capture it, and *when* to capture it. It has two parts:

* **`captures:`** — **named** capture definitions (the *how*).
* **`steps:`** — the **timeline** (the *when* + *what traffic*).

A capture is **active only during the steps that list it in `capture:`**.
Consecutive steps sharing a capture form one continuous capture. This is the
fix for the old design, where "where to capture" and "when to capture" lived in
two disconnected sections and the only capture window was *everything*.

```yaml
scenario:
  name: socks5_proxy_test
  results: results

  # HOW to capture — named definitions
  captures:
    - id: socks_handshake
      node: socks-client
      link: 0                       # logical link id (resolved at runtime)
      filter: "host 10.10.0.10 and tcp port 1080"
      output: socks5_client.pcap
    - id: proxy_outbound
      node: socks-proxy
      interface: eth0               # explicit container ifname (mgmt/egress)
      filter: "tcp port 443"
      output: socks5_proxy_outbound.pcap

  # WHEN + WHAT — the timeline. Each step is a self-contained test case.
  steps:
    - name: bring-up
      do:
        - playbook: playbooks/setup_proxy.yml

    - name: happy-path
      capture: [socks_handshake, proxy_outbound]   # active during this step
      do:
        - node: socks-client
          shell: "curl --proxy socks5h://10.10.0.10:1080 https://example.com/ -sS"
      assert: { contains: "HTTP/ 200" }

    - name: negative
      do:
        - node: socks-proxy
          raw: "pkill -x go-socks5-proxy || true"
        - node: socks-client
          shell: "curl --proxy socks5h://10.10.0.10:1080 https://example.com/ -sS || true"
      assert: { not_contains: "HTTP/ 200" }

  # Optional: captures active for the whole timeline (ambient).
  # always_capture: [some_capture_id]
```

### Capture definitions

| key | meaning |
| --- | --- |
| `id` | unique name, referenced by steps' `capture:` and used in filenames |
| `node` | netlab node to capture on |
| `link: N` **or** `interface: X` | which interface. `link: 0` = the node's first data link (resolved at runtime via `hostvars[<node>].interfaces[0].ifname`); `interface: eth0` = an explicit container ifname (e.g. the mgmt/egress interface). Use one, not both. |
| `filter` | optional BPF filter expression |
| `output` | local filename (default `<id>.pcap`) |

### Steps (the timeline)

Each step is a self-contained **test case**: a name, a list of actions
(`do:`), an optional capture window (`capture:`), an optional declarative
pass/fail condition (`assert:`), and optional logs to harvest (`log:`).

**Action types** (each `do:` entry has exactly one):

| key | runs on | meaning |
| --- | --- | --- |
| `playbook: path` | controller | run an Ansible playbook (against `hosts.yml`) |
| `shell: cmd` | `node` | Jinja-templated shell command on the node |
| `raw: cmd` | `node` | raw command (no remote interpreter needed) |
| `command: cmd` | controller | controller-side shell command |

`shell`/`raw` default to the step's primary node if `node` is omitted.

**Declarative asserts** (AND-combined; evaluated against the step's combined
stdout and the last action's exit code):

| key | meaning |
| --- | --- |
| `rc_eq: N` / `rc_neq: N` | last action's exit code == / != N |
| `contains: S` | combined output contains S |
| `not_contains: S` | combined output does not contain S |
| `regex: R` | combined output matches regex R |
| `lines_ge: N` | combined output has at least N lines |

### `verify:` (connectivity check)

```yaml
verify:
  - wait: 30                # seconds to wait for convergence
  - ping:                   # optional direct reachability check (L2 labs)
      node: socks-client
      self: 10.10.0.11/24   # (re)address this interface first
      target: 10.10.0.10
```

If `ping` entries are defined, a generated ping playbook is used (appropriate
for plain L2 labs where `netlab validate` has no routing tests). Otherwise
`netlab validate` is run (skippable with `--no-validate`).

## Why it is LLM-friendly

1. **Flat declarative YAML** — a scenario is a list of captures + a list of
   steps. No imperative nesting. An LLM transcribes a natural-language test
   plan 1:1 (the SOCKS example maps directly onto `plan_socks.md` steps 5–7).
2. **Each step = one sentence of a test plan** — name + what it does + how it
   must end (the assert).
3. **Named captures referenced by id** — add/remove capture points without
   renumbering; no positional coupling.
4. **Declarative asserts** — an LLM emits pass/fail without writing shell.
5. **`describe` is round-trippable** — the `--format yaml` output re-parses to
   an equivalent scenario, so the YAML an LLM writes is the same shape `describe`
   prints.
6. **Topology stays separate** — common network patterns (nodes/links/tc) are
   the netlab topology, kept outside `scenario:`. The scenario only references
   node names. An LLM edits two small declarative files: the topology (network)
   and the scenario (test).

## Topology schema notes

* The network part is plain netlab. Add a `scenario:` block to describe the
  test, and declare it as a custom global attribute so netlab's validation
  accepts it:

  ```yaml
  defaults:
    attributes:
      global:
        scenario: { type: dict }
  ```

  (The runner auto-materializes a normalized copy that includes this
  declaration if you forget it.)

* **TC is native.** Put `tc: { delay: 50, loss: 2 }` on a link; `netlab up`
  applies it automatically.

* **Playbooks** run from the lab directory, so netlab's `ansible.cfg` and
  `hosts.yml` are picked up automatically. Refer to nodes by their netlab names
  (`hosts: socks-proxy`).

## What gets generated

In the lab directory (where the topology file lives):

```
netlab.scenario.topology.yml   # normalized copy (scenario attr declared) — netlab up input
scenario-<name>/               # generated ansible playbooks
  scenario_capture.yml         # per-capture start/stop (path-specific kill)
  scenario_step_<i>_<name>.yml # one play per step (actions + assert)
  scenario_fetch.yml           # fetch PCAPs + step logs into results/
  scenario_verify.yml          # (only if verify.ping defined)
results/                       # harvested PCAPs + logs
hosts.yml ansible.cfg ...      # netlab's own outputs
```

## Project layout

```
test_tracer/
  cli.py        # argparse entry point (`scenario`, with `describe` subcommand)
  runner.py     # lifecycle orchestrator (netlab CLI + ansible-playbook, step timeline)
  scenario.py   # scenario model (Capture, Action, Step, Scenario) + validation
  playbooks.py  # generates the capture / per-step / fetch / verify playbooks
  describe.py   # renders a scenario to text/yaml/json (the LLM-friendly surface)
  console.py    # colored logging
socks/          # reference scenario (SOCKS5 proxy) — see socks/README.md
example/        # example scenario (MongoDB/BitTorrent over a tc-impaired link)
PLAN-refactor.md  # design document for this refactor
plan.md           # original design document
```

## Testing

```bash
uv sync
# dry-run of the full lifecycle (safe: executes nothing)
uv run scenario -t socks/topology.yml --dry-run
# LLM-readable timeline
uv run scenario describe -t socks/topology.yml
```

A full end-to-end run requires a host with Docker + containerlab and the device
images pulled (`netlab up` pulls them on first use).
