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
topology.yml              netlab topology + scenario block (IPs, captures, verify ping)
playbooks/setup_proxy.yml bootstrap packages, download binary, start proxy, restrict port
playbooks/test_socks.yml  curl via proxy (must pass), proxy stopped (must fail), restart
results/                  <- PCAPs + logs land here after a run
```

## Requirements

* Docker + containerlab ≥ 0.75 (netlab's clab provider)
* The user running `scenario`/`netlab` ideally has **passwordless
  `sudo ip`** on the host — netlab configures container interfaces with
  `sudo ip netns exec ...`. Without it, `netlab up` fails at "Deploying
  initial device configurations"; the playbooks compensate by addressing
  `eth1` themselves (idempotent `ip addr replace`), so you can run with
  `--up-arg --no-config --no-validate` and let the scenario do the work.
* Outbound Internet on the host (apt in the containers, example.com, GitHub
  release download).

## Run (one command)

```bash
cd /home/unclehook/test-tracer
uv run scenario -t socks/topology.yml
# without passwordless 'sudo ip':
uv run scenario -t socks/topology.yml --up-arg --no-config --no-validate
```

What happens:

1. **CREATE** — `netlab up`: creates both containers (debian:13) and the
   L2 link 10.10.0.0/24. (With `--no-config` the interface addressing is
   done later by the playbooks.)
2. **VERIFY** — 10 s settle wait, then a direct reachability check:
   `scenario_verify.yml` (generated) installs iproute2+iputils-ping in the
   client, ensures `10.10.0.11/24` on eth1, and pings `10.10.0.10`.
   (`netlab validate` is only used when no `verify.ping` entries are
   defined — a plain L2 lab has no routing module, so it defines no tests
   and exits 1 with "No validation tests defined".)
3. **CAPTURE + SCRIPTS** —
   * `scenario_capture_start.yml` (generated): bootstraps python3 + tcpdump
     (raw, no remote interpreter needed) and starts background captures:
     - `socks-client/eth1`, filter `host 10.10.0.10 and tcp port 1080`
       → SOCKS handshake + tunneled data
     - `socks-proxy/eth0`, filter `tcp port 443` → the proxy's outbound
       connections (egress goes via mgmt, not the LAN)
   * `playbooks/setup_proxy.yml`: apt bootstrap (always runs — containers
     are ephemeral), download the go-socks5-proxy binary, ensure eth1
     addressing, start the proxy on `10.10.0.10:1080`, add the iptables
     source restriction.
   * `playbooks/test_socks.yml`:
     - **TEST 1** — `curl --proxy socks5h://10.10.0.10:1080
       https://example.com/` → asserts HTTP 200
     - **TEST 2** — stop the proxy (pkill, with a /proc-scan fallback since
       procps may be missing), repeat → asserts the request **fails**
       (connection refused)
     - restart the proxy, fetch `socks_test.log` + `go_socks5.log` into
       `results/`
   * `scenario_capture_stop.yml` (generated): kills all tcpdump processes
     via /proc scan (catches stale capture processes from earlier runs).
4. **HARVEST** — `scenario_fetch.yml` (generated) fetches the PCAPs into
   `results/` (absolute path, since Ansible's `fetch` resolves relative
   dests against the playbook directory).
5. **TEARDOWN** — `netlab down --cleanup` (use `--keep-lab` to skip and
   inspect the lab manually; the runner never tears down a lab it did not
   create itself).

## Result to inspect (verified run)

```
socks/results/socks5_client.pcap          # 44 KB — SOCKS5 handshake + TLS tunnel
socks/results/socks5_proxy_outbound.pcap  # 2.6 MB — proxy -> Internet (443)
socks/results/socks_test.log              # curl -v output of both tests
socks/results/go_socks5.log               # proxy log
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

```bash
cd /home/unclehook/test-tracer/socks
uv run netlab up --no-config topology.yml
uv run ansible-playbook -i hosts.yml playbooks/setup_proxy.yml
uv run ansible-playbook -i hosts.yml playbooks/test_socks.yml
# inspect, then:
uv run netlab down --cleanup
```

## Notes / known quirks

* **Ephemeral containers**: every `netlab up` recreates the containers, so
  packages/binaries installed during a previous run are gone. The
  playbooks therefore (re)install everything on every run; apt is idempotent
  so repeat runs are cheap.
* **No systemd** in `kind: linux` containers — the proxy runs as a detached
  background process (`nohup ... &`), not a service.
* **Process management without procps**: `pkill`/`ps` are not in the base
  image; the stop/capture-kill tasks include `/proc/[0-9]*` comm-scan
  fallbacks.
* **Ansible on these targets**: modules need a remote interpreter, so
  python3 is bootstrapped with `ansible.builtin.raw` (works without one)
  before any non-raw task runs.
* If the host blocks outbound DNS or 443, TEST 1 will fail — that is a host
  firewall issue, not a scenario bug.
* `results/` and all netlab artifacts are git-ignored.
