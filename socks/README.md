# SOCKS5 proxy scenario (plan_socks.md)

Two Debian 13 "VMs" (containerlab `linux` containers) on one L2 segment:

| Machine | IP | Role |
|---|---|---|
| `socks-proxy` | 10.10.0.10 (eth1) / 192.168.121.101 (eth0 mgmt) | go-socks5-proxy on 10.10.0.10:1080 |
| `socks-client` | 10.10.0.11 (eth1) / 192.168.121.102 (eth0 mgmt) | runs `curl` through the proxy |

Internet egress works out of the box: containers leave through the mgmt
interface (`eth0`) via Docker NAT, so the proxy gets working outbound TCP
80/443 + DNS without extra configuration.

## Proxy: go-socks5-proxy

[ariadata/go-socks5-proxy](https://github.com/ariadata/go-socks5-proxy) v1.2.0
(static Go binary, no dependencies, no systemd). The setup playbook downloads
the release binary into the proxy container, binds it to `10.10.0.10:1080`
(LAN-only, per plan_socks.md), and adds an iptables DROP rule for `:1080`
from any source other than `10.10.0.11` (best effort, `ignore_errors`).
Anonymous mode; the proxy's default internal-destination blocking is kept
(the test target is public).

## Files

```
topology.yml              netlab topology + scenario block (timeline: captures, steps, asserts)
playbooks/setup_proxy.yml bootstrap packages, download binary, start proxy, restrict port
results/                  <- PCAPs + logs land here after a run
```

The test logic (curl happy-path, negative, restart) now lives **inline in the
`scenario:` timeline** as `shell`/`raw` actions with declarative `assert:`s —
no separate `test_socks.yml` playbook.

## Requirements

* Docker + containerlab ≥ 0.75 (netlab's clab provider); the user must be in
  the `docker` group (the runner drives containers with `docker exec`).
* **No sudo needed.** On a host without passwordless `sudo ip`, run with
  `--no-validate --up-arg=--no-config` (below). `--no-config` makes netlab
  skip its privileged `sudo ip netns exec ...` initial-config step; the
  scenario then addresses every node's data link itself (the `address-links`
  step), and Ansible reaches the nodes over the Docker connection (no SSH).
  On a host *with* passwordless sudo you may drop both flags and let netlab
  do the initial config.
* Outbound Internet on the host (apt in the containers, example.com, GitHub
  release download).

## Run (one command)

```bash
cd /home/user_pc3/test-tracer
# host without passwordless sudo (the common case here):
uv run scenario -t socks/topology.yml --no-validate --up-arg=--no-config
# host with passwordless sudo ip:
uv run scenario -t socks/topology.yml

# Inspect the scenario before running (LLM-readable timeline):
uv run scenario describe -t socks/topology.yml
```

Note the `=` form: a bare `--up-arg --no-config` is swallowed by argparse as a
flag. `--no-validate` is a `scenario` flag (skips `netlab validate`);
`--no-config` is forwarded to `netlab up` via `--up-arg=`.

What happens:

1. **CREATE** — `netlab up` (with `--no-config`): creates both containers
   (debian:13) and the L2 link 10.10.0.0/24. The data interfaces are *not*
   addressed by netlab; the scenario does that next.
2. **VERIFY** — a 10 s settle wait (no `netlab validate`, since a plain L2
   lab defines no routing tests).
3. **STEPS** — the runner walks the `steps:` timeline. For each step it
   starts the captures that step declares, runs the step's actions in order,
   applies the declarative assert, and stops captures the next step no longer
   needs.
   * Captures are **controller-managed daemons**: the runner launches
     `tcpdump` in the target container with `docker exec -d` (detached) and
     stops it by pidfile. A backgrounded process started *inside* an Ansible
     task is killed when the task's docker-exec session ends, so captures —
     and the proxy itself — must be started detached, outside Ansible.
   * **step 1 `address-links`** — installs iproute2 (absent in the base
     image) and brings each node's `eth1` up with its netlab-assigned IP.
   * **step 2 `ping`** — installs iputils-ping and pings the proxy
     (`assert rc_eq 0`) to prove the L2 segment works before traffic.
   * **step 3 `bring-up`** — runs `playbooks/setup_proxy.yml` (apt bootstrap,
     download the go-socks5-proxy binary, iptables source restriction — it
     does **not** start the daemon), then starts the proxy as a **detached
     daemon** (`daemon:` action → `docker exec -d`), then asserts
     `PROXY_UP` (`ss -lnt` sees `:1080` listening).
   * **step 4 `happy-path`** — starts captures `socks_handshake`
     (socks-client/eth1, `host 10.10.0.10 and tcp port 1080`) and
     `proxy_outbound` (socks-proxy/eth0, `tcp port 443`); installs curl and
     runs the proxied request; asserts the output matches
     `HTTP/[12](.N)? 200` (the server answers over HTTP/2). Stops both
     captures. The PCAPs therefore record only the successful exchange — not
     the negative test.
   * **step 5 `negative`** — kills the proxy (`daemon_kill:` → pkill by
     name), re-runs the same curl, asserts the output does **not** contain an
     HTTP 200 (connection refused). No capture is active, so the failed
     attempt is not recorded.
   * **step 6 `restore`** — restarts the proxy daemon, asserts `PROXY_UP`
     again, and harvests `go_socks5.log`.
4. **HARVEST** — `scenario_fetch.yml` (generated) fetches the PCAPs and the
   proxy log into `results/` (absolute dest, since Ansible's `fetch`
   resolves relative dests against the playbook directory).
5. **TEARDOWN** — `netlab down --cleanup` (use `--keep-lab` to skip and
   inspect the lab manually; the runner never tears down a lab it did not
   create itself).

## Result to inspect (verified run)

```
socks/results/socks5_client.pcap          # ~10 KB — SOCKS5 handshake + tunneled TLS (happy-path step only)
socks/results/socks5_proxy_outbound.pcap  # ~12 KB — proxy -> Internet (443)
socks/results/go_socks5.log               # proxy log (harvested by the restore step)
```

What the captures show (tshark):

```
# socks5_client.pcap
  4  10.10.0.11 -> 10.10.0.10  Socks  Version: 5 Connect to server request
  6  10.10.0.10 -> 10.10.0.11  Socks  Version: 5 Connect to server response
  8  10.10.0.11 -> 10.10.0.10  Socks  Version: 5 Command Request - Connect
  10 10.10.0.10 -> 10.10.0.11  Socks  Version: 5 Command Response - Connect
  13 10.10.0.11 -> 10.10.0.10  TLSv1.2 Client Hello (SNI=example.com)   <- tunneled
...

# socks5_proxy_outbound.pcap
  ... 192.168.121.101 -> 8.47.69.5   TLSv1.2 Client Hello (SNI=example.com)
```

The example.com ClientHello is visible both inside the SOCKS tunnel (client
side) and on the proxy's egress — proving the proxy actually relayed it.

The outbound capture also picks up unrelated container background traffic
(apt/GitHub/TLS updates) on port 443; filter on the example.com SNI if you
want only the test session:

```bash
tshark -r socks5_proxy_outbound.pcap -Y 'tls.handshake.extensions_server_name == "example.com"'
```

## Manual run (without the scenario runner)

The generated per-step playbooks are named `scenario_step_<i>_<name>_action_<j>.yml`
(one per action) plus `scenario_step_<i>_<name>_assert.yml`. Captures and the
proxy daemon are started by the **runner** (via `docker exec -d`), not by the
playbooks, so a fully manual run is best done by invoking the `scenario`
command itself. To drive pieces by hand after `netlab up --no-config`:

```bash
cd /home/user_pc3/test-tracer/socks
uv run netlab up --no-config netlab.scenario.topology.yml
# address the data links (step 1 actions):
uv run ansible-playbook -i hosts.yml scenario-socks5_proxy_test/scenario_step_0_address-links_action_0.yml
uv run ansible-playbook -i hosts.yml scenario-socks5_proxy_test/scenario_step_0_address-links_action_1.yml
# install + configure the proxy (does NOT start it):
uv run ansible-playbook -i hosts.yml playbooks/setup_proxy.yml
# start the proxy detached, then verify:
docker exec -d clab-socks-socks-proxy /usr/local/bin/go-socks5-proxy --host 10.10.0.10 --port 1080 >>/var/log/go_socks5.log 2>&1
docker exec clab-socks-socks-proxy ss -lnt 'sport = :1080'
# inspect, then:
uv run netlab down --cleanup
```

## Notes / known quirks

* **Ephemeral containers**: every `netlab up` recreates the containers, so
  packages/binaries installed during a previous run are gone. The steps and
  playbooks therefore (re)install everything on every run; apt is idempotent
  so repeat runs are cheap.
* **Detached daemons, not `nohup`**: there is no systemd (or real init) in
  `kind: linux` containers, and a process backgrounded *inside* an Ansible
  task is killed when that task's docker-exec session tears down. The proxy
  and every capture are therefore started with `docker exec -d` from the
  controller (the `daemon:`/`daemon_kill:` actions and the runner's capture
  manager), and stopped by pidfile / pkill. See `test_tracer/daemons.py`.
* **`--no-config` path**: with no passwordless sudo, netlab skips its
  privileged initial config, so the scenario addresses every data link itself
  (`address-links`) and installs `iproute2`/`iputils-ping`/`curl`/`tcpdump`
  on demand (they are absent in the debian:13 base image).
* **Ansible on these targets**: modules need a remote interpreter, so
  python3 is bootstrapped with `ansible.builtin.raw` (works without one)
  before any non-raw task runs.
* **stderr in asserts**: `shell:` actions are wrapped in `( … ) 2>&1` so
  diagnostic output (e.g. `curl --verbose`'s `HTTP/2 200` line, written to
  stderr) is visible to `contains`/`regex` asserts.
* If the host blocks outbound DNS or 443, the happy-path step will fail —
  that is a host firewall issue, not a scenario bug.
* `results/` and all netlab artifacts are git-ignored.
