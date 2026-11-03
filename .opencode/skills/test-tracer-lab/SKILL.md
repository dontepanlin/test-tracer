---
name: Test Tracer Lab
description: Author, validate, and run test-tracer network trace labs (netlab topology + scenario step timeline that captures PCAPs). Use when creating or editing a lab topology, a scenario timeline, capture definitions, or when the user wants to collect network traces for a software/protocol under test.
---

# Test Tracer Lab Authoring

You are helping the user create a **test-tracer lab**: a netlab topology plus a
`scenario:` step timeline that generates traffic and captures PCAPs on specific
nodes/interfaces for a specific software or protocol under test.

The two halves are **separate concerns**:

1. **The topology** (plain netlab) — the *network*: nodes, links, IPs, and
   native `tc` impairment. This is where "common patterns" live.
2. **The `scenario:` block** — the *test*: what to capture, how, and **when**
   (a step timeline with declarative asserts).

Each scenario is individual; shared network patterns belong in the topology, not
the scenario. The scenario only **references node names**.

## Workflow

1. **Understand the goal.** What software/protocol is under test? What nodes are
   involved? What traffic must flow, and on which link/interface should the
   PCAP be captured? What is the pass/fail condition for each test case?
   If anything is ambiguous, ask before writing.

2. **Read the reference** for the exact schema before writing:
   - `references/schema.md` — full `scenario:` schema (captures, steps, actions,
     asserts, verify) and the netlab topology conventions.
   - `references/examples.md` — a complete working scenario (SOCKS5 proxy) to
     copy structure from.

3. **Write the topology file** `topology.yml`:
   - netlab `name`, `provider`, `defaults`, `nodes`, `links` (with `tc:` for
     impairment).
   - Declare the custom attribute so netlab accepts the block:
     ```yaml
     defaults:
       attributes:
         global:
           scenario: { type: dict }
     ```
   - Add the `scenario:` block (captures + steps timeline).

4. **Validate** (no lab needed — fast, catches all schema errors):
   ```bash
   uv run scenario describe -t <topology.yml>          # human/LLM-readable timeline
   uv run scenario describe -t <topology.yml> --format yaml   # round-trippable
   ```
   `describe` fails with a clear `[FATAL]` message on any schema error (bad
   capture, unknown capture ref in a step, bad assert key, etc.). Fix and re-run
   until it renders cleanly.

5. **Dry-run** the full lifecycle (executes nothing):
   ```bash
   uv run scenario -t <topology.yml> --dry-run
   ```
   Confirm the per-step capture start/stop order matches the intended windows.

6. **Run** it (needs Docker + containerlab + device images on the host; the
   user must be in the `docker` group):
   ```bash
   # host with passwordless `sudo ip`:
   uv run scenario -t <topology.yml>
   # host WITHOUT passwordless sudo (common on desktops) — skip netlab's
   # privileged initial config; the scenario addresses the data links itself:
   uv run scenario -t <topology.yml> --up-arg=--no-config
   ```
   PCAPs + any step `log:` entries land in `<lab>/results/`. Note the `=` form —
   a bare `--up-arg --no-config` is swallowed by argparse. `--no-validate` is
   optional (the runner now treats a plain lab's "No validation tests defined"
   as a pass, not a failure). See `references/schema.md` → *Running*.

## Authoring rules (these make the scenario correct and LLM-friendly)

- **Captures are named and referenced by id.** Define each capture once in
  `captures:`; reference it by `id` in the `capture:` list of the steps where it
  must be active. Never embed capture config inside a step.
- **A capture is active only during the steps that list it.** Consecutive steps
  sharing a capture = one continuous capture. This is how you scope a PCAP to a
  single test case (e.g. capture the happy path but *not* the negative test).
- **Each step is one self-contained test case**: a `name`, a `do:` list of
  actions, and (usually) an `assert:`. The assert is what makes the step pass or
  fail — write it so a *correct* system passes and a *broken* one fails.
- **Use declarative asserts**, not shell logic: `rc_eq`/`rc_neq`,
  `contains`/`not_contains`, `regex`, `lines_ge`. (AND-combined.)
- **`link: N` vs `interface: X`** — use `link: 0` for the node's first data link
  (the normal case); use `interface: eth0` only when you need a specific
  container interface (e.g. the mgmt/egress interface for outbound captures).
  Never set both.
- **Actions carry their `node`** for `shell`/`raw`/`daemon`/`daemon_kill`.
  `playbook` and `command` run on the controller.
- **Never start a long-lived process from `shell`/`raw`/`playbook`.** A
  backgrounded process (e.g. `nohup … &`) started inside an Ansible task is
  killed when that task's docker-exec session tears down — these containers
  have no init to adopt it. Start servers/proxies with a **`daemon:`** action
  (detached `docker exec -d`) and stop them with **`daemon_kill:`**. Captures
  are managed the same way by the runner automatically. A typical SUT bring-up
  is: `playbook:` (install + config) → `daemon:` (start the server) → `shell:`
  (verify the port is listening) → `assert: { contains: "<UP marker>" }`.
- **Containers are ephemeral** — every `netlab up` rebuilds them. Any setup
  (installing packages, starting a daemon) must run in the scenario, not be
  assumed from a previous run. On a `--no-config` (no-sudo) host the base image
  also lacks common tools, so early steps self-install what they need
  (`iproute2`, `iputils-ping`, `curl`, `tcpdump`) and address the data links
  themselves — see the socks lab's `address-links`/`ping` steps.
- **Keep the two files small and declarative.** No imperative nesting in the
  scenario; the timeline is a flat list of steps.

## Generated artifacts (do not hand-edit)

`uv run scenario` writes into the lab directory (all git-ignored):

```
netlab.scenario.topology.yml                  # normalized copy for netlab up
scenario-<name>/
  scenario_step_<i>_<name>_action_<j>.yml     # one play PER action (shell/raw/playbook/command)
  scenario_step_<i>_<name>_assert.yml         # one play per step's declarative assert
  scenario_fetch.yml                          # fetch PCAPs + step logs into results/
  scenario_verify.yml                         # only if verify.ping defined
results/                                      # harvested PCAPs + logs
```

Each action runs as its own Ansible invocation so its stdout can be persisted
(`/tmp/stepout_<name>/<j>.out`) for the step's assert playbook (a separate
invocation) to read. `daemon`/`daemon_kill` actions are NOT generated as
playbooks — the runner executes them controller-side via `docker exec -d`.
Captures are likewise runner-managed (`docker exec -d` tcpdump + pidfile), not a
playbook. Regenerate by re-running the scenario; never edit these by hand.

## Quick reference

```yaml
scenario:
  name: <snake_case_id>
  results: results
  verify:
    - wait: 10
    - ping: { node: <node>, self: <ip/prefix>, target: <ip> }   # optional
  captures:
    - id: <capture_id>
      node: <node>
      link: 0                    # or interface: eth0
      filter: "<BPF filter>"     # optional
      output: <name>.pcap
  steps:
    - name: <step_name>
      capture: [<capture_id>, ...]   # captures active during this step
      do:
        - playbook: playbooks/<setup>.yml     # install + config (controller)
        - node: <node>
          daemon: "/path/server --port 1080 >>/var/log/server.log 2>&1"   # start SUT (detached)
        - node: <node>
          shell: "ss -lnt 'sport = :1080' | grep -q LISTEN && echo UP || echo DOWN"
        - node: <node>
          daemon_kill: "server"               # stop the SUT
        - node: <node>
          command: "sleep 1"                  # controller-side one-shot
      assert: { contains: "<expected substring>" }
      log: [{ node: <node>, src: /path/on/node, output: <local>.log }]  # optional
```

Full schema + a complete worked example: read `references/schema.md` and
`references/examples.md`.
