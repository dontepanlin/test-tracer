# Refactor: timeline-based, LLM-friendly scenario model

## Goal

Collect network traces (PCAPs) on specific scenarios with specific software and
protocols. The scenario must define **what to capture, how to capture it, and
when to capture it** in **one place**. The runner must be **LLM-friendly**: an
LLM can author, review, and edit a scenario as a small flat declarative YAML.

## Problem with the current design

The old `scenario:` block is a flat, unordered mix of three unrelated things:

```yaml
scenario:
  verify:   [ ... ]     # WHEN the lab is ready
  capture:  [ ... ]     # WHAT/HOW to capture — but says nothing about WHEN
  scripts:  [ playbook, wait_after, ... ]  # WHAT traffic to generate
```

"Where to capture" and "when to capture" are in **two different places**. The
runner fixes the capture window to *everything* (start before all scripts, stop
after all scripts). `wait_after` is the only "when" control, and it's a fixed
sleep. You cannot express "capture only while TEST 1 runs".

Confirmed bugs in the generated playbooks:

1. Capture-stop kills **all** `tcpdump` on a host (`for p in /proc/[0-9]*; do
   [ comm = tcpdump ] && kill; done`) — with two captures on one node, stopping
   one kills the other.
2. Capture-start does **not** clean stale captures — a leftover tcpdump from a
   prior run keeps appending to the same `/tmp/<out>.pcap`.
3. `-s 0` is dropped in the bounded (`seconds:`) capture path.
4. `resolve_capture_interfaces` is a **no-op**: it looks for
   `host_vars/<node>/topology.json` (netlab writes `host_vars/<node>/topology`,
   no `.json`) and even when it matched, it assigned `ifname = intf["ifname"]`
   (itself).
5. No way to stop captures early on a failed step.

## Design: named captures + step timeline

### `scenario:` schema

```yaml
scenario:
  name: socks5_proxy_test
  results: results

  # HOW to capture — named, reusable definitions
  captures:
    - id: socks_handshake
      node: socks-client
      link: 0                       # logical link id, resolved at runtime
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
      capture: [socks_handshake, proxy_outbound]   # start before, stop after
      do:
        - node: socks-client
          shell: |
            curl --proxy socks5h://{{ ... }}:1080 https://example.com/ \
                 -s -o /dev/null -w '%{http_code}'
      assert: { contains: "200" }
      log: [{ node: socks-proxy, src: /var/log/go_socks5.log, output: go_socks5.log }]

    - name: negative
      do:
        - node: socks-proxy
          raw: "pkill -x go-socks5-proxy || true"
        - node: socks-client
          shell: "curl --proxy socks5h://10.10.0.10:1080 https://example.com/ -s -o /dev/null -w '%{http_code}'"
      assert: { not_contains: "200" }

    - name: restore
      do:
        - node: socks-proxy
          shell: "restart proxy..."
```

### Semantics (the "when" contract)

- A capture is **active only during the steps that list it in `capture:`**.
  The runner starts it right before the first such step and stops it right
  after the last consecutive such step. Consecutive steps sharing a capture =
  one continuous capture (no restart gap).
- `always_capture: [ids]` = active for the whole timeline (ambient).
- Each step's `do:` actions run in order on their `node`; each step ends with
  its `assert:` evaluated against the step's captured output → per-step
  pass/fail.
- A capture uses either `link:` (logical, resolved via `hostvars[<node>]
  .interfaces`) **or** `interface:` (explicit container ifname) — not both.

### Action types (`do:` vocabulary)

- `playbook` — path to an Ansible playbook (run as-is, against hosts.yml).
- `shell` — Jinja-templated command, run on `node` (defaults to step node).
- `raw` — no remote interpreter, run on `node`.
- `command` — controller-side raw command.

### Assertions (declarative, AND-combined)

Evaluated by the runner from the step's combined stdout + per-action rc:
`rc_eq`, `rc_neq`, `contains`, `not_contains`, `regex`, `lines_ge`.

## Runner: step-driven lifecycle

`create → verify → steps → harvest → teardown`, with the capture phase inside
the step loop:

```
for each step:
    start captures newly needed by this step   (kill stale tcpdump for that pcap first)
    run do[] actions (each: execute, capture stdout, evaluate assert)
    stop captures no longer needed by the next step
```

- **Capture manager** (new): one capture playbook with per-capture `start
  <id>` / `stop <id>` tasks. Stop = kill **only** the tcpdump whose
  `/proc/<pid>/cmdline` contains `-w /tmp/<out>.pcap`; `rm` the stale pcap
  before starting. Fixes the multi-capture-per-host and stale-append bugs.
- **Harvest**: fetch each capture's `output` + each step's `log:` entries.
- **`--dry-run`** prints the *planned timeline* (per step: captures start/stop,
  actions), not just ansible command lines.
- **Failure handling**: a failed step's assert records failure, stops its
  captures, continues to teardown; exit non-zero. Best-effort teardown of a lab
  this run created (existing behavior kept).

## `scenario describe` (LLM-readable)

New subcommand: `scenario describe -t <topology> [--format text|yaml|json]`.
Prints: the lab (nodes/links/tc), the timeline (each step, its active capture
windows, actions, assert), and the capture definitions. `--format yaml` is the
canonical round-trippable form an LLM writes.

## LLM-friendliness (the main goal)

1. A scenario is a small flat declarative YAML (list of steps + list of
   captures). An LLM transcribes a natural-language test plan 1:1.
2. Each step = one sentence of a test plan (name + what it does + how it must
   end). `socks` maps directly onto `plan_socks.md` steps 5–7.
3. Named captures referenced by id — add/remove capture points without
   renumbering.
4. Declarative asserts — an LLM emits pass/fail without writing shell.
5. `describe` is round-trippable — the YAML an LLM writes is what `describe`
   prints.
6. No scenario-specific topology — common network patterns are the netlab
   topology (nodes/links/tc), kept outside `scenario:`. The scenario only
   references node names. An LLM edits two small declarative files: the
   topology (network) and the scenario (test).

## Files

| File | Change |
|---|---|
| `test_tracer/scenario.py` | New dataclasses: `Capture`, `Action`, `Step`, `Scenario`. Validation. No backward-compat (new project). |
| `test_tracer/playbooks.py` | Rewrite: one capture playbook with path-specific start/stop per capture id; step playbook generator (actions + assert → `failed_when`); log-fetch tasks; real interface resolution from `hostvars` at runtime. |
| `test_tracer/runner.py` | Step-driven loop (start captures → do → assert → stop); capture manager; harvest (pcaps + logs); timeline-aware `--dry-run`; per-step result recording. |
| `test_tracer/cli.py` | Add `describe` subcommand (+ `--format`); keep existing run flags. |
| `test_tracer/describe.py` | New — render scenario (and lab summary) to text/yaml/json. |
| `socks/topology.yml` | Rewrite `scenario:` into the new timeline form (reference scenario). |
| `example/topology.yml` | Rewrite `scenario:` into the new timeline form. |
| `README.md` | New schema, `describe`, the LLM-friendly story, lifecycle. |
| `socks/README.md` | Update "what happens" to the step timeline. |

## What stays

- netlab = Topology Provider, `ansible-playbook` = Test Engine.
- `defaults.attributes.global.scenario` auto-declaration + normalized copy.
- `main.py`, console, `--keep-lab/--no-validate/--up-arg/--phase/--dry-run`.
- Gitignore (already covers generated artifacts).

## Decisions

- **No backward compatibility** — new project, old form removed.
- **Declarative asserts** evaluated by the runner.
- **`describe` subcommand** included.
- **Each scenario is individual** — common patterns are the netlab topology.
- `socks/` external playbooks (`setup_proxy.yml`) are kept and referenced by a
  `playbook:` action; the test logic (curl happy/negative) moves into inline
  `shell`/`raw` actions with declarative asserts.
