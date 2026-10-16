# SOCKS5 proxy scenario (plan_socks.md)

Two Debian 13 "VMs" (containerlab `linux` containers) on one L2 segment:

| Machine | IP | Role |
|---|---|---|
| `socks-proxy` | 10.10.0.10 (eth1) / 192.168.121.101 (eth0 mgmt) | Dante SOCKS5 server on :1080 |
| `socks-client` | 10.10.0.11 (eth1) / 192.168.121.102 (eth0 mgmt) | runs `curl` through the proxy |

Internet egress works out of the box: containers leave through the mgmt
interface (`eth0`) via Docker NAT, so the proxy gets working outbound TCP
80/443 + DNS without extra configuration.

## Files

```
topology.yml              netlab topology + scenario block (IPs, captures)
playbooks/setup_dante.yml installs dante, writes /etc/danted.conf, starts it
playbooks/test_socks.yml  curl via proxy (must pass), danted stopped (must fail)
results/                  <- PCAPs + logs land here after a run
```

## Requirements

* Docker + containerlab ≥ 0.75 (netlab's clab provider)
* The user running `scenario`/`netlab` must have **passwordless `sudo ip`**
  on the host — netlab configures container interfaces with
  `sudo ip netns exec ...` ([configs.py](../netlab/netsim/providers/clab/configs.py)).
  This machine's sudoers only allows `tc`/`perf`/`nice`, so `netlab up` will
  fail at "Deploying initial device configurations" until that is fixed.
* Outbound Internet on the host (for `apt-get` in the containers and for
  `example.com`).

## Run (one command)

```bash
cd /home/unclehook/test-tracer
uv run scenario -t socks/topology.yml
```

What happens:

1. `netlab up` — creates both containers (debian:13), the L2 link
   10.10.0.0/24, applies interface configs.
2. `netlab validate` — reachability check, then a 10 s convergence wait.
3. Generated `scenario_capture_start.yml` — installs `tcpdump` in both
   containers and starts background captures:
   * `socks-client/eth1`, filter `host 10.10.0.10 and tcp port 1080`
     → the SOCKS handshake + tunneled data
   * `socks-proxy/eth0`, filter `tcp port 443` → the proxy's outbound
     connection to example.com (egress goes via mgmt, not the LAN)
4. `playbooks/setup_dante.yml` — apt-installs `dante-server`, writes
   `/etc/danted.conf` (internal 10.10.0.10:1080, external = mgmt IP,
   client/socks pass only for 10.10.0.11/32, everything else restricted),
   starts `danted` as a background process (no systemd in these containers)
   and waits for the :1080 listener.
5. `playbooks/test_socks.yml` —
   * curl `socks5h://10.10.0.10:1080` https://example.com → asserts HTTP 200
   * stops `danted`, repeats → asserts the request **fails**
   * restarts `danted` (lab left in a working state)
6. Generated `scenario_capture_stop.yml` + `scenario_fetch.yml` — kills
   tcpdump, fetches PCAPs + `socks_test.log` + `danted.log` into `results/`.
7. `netlab down --cleanup` — destroys the lab.

## Result to inspect

```
socks/results/socks5_client.pcap          # SOCKS5 handshake + TLS tunnel
socks/results/socks5_proxy_outbound.pcap  # proxy -> example.com:443
socks/results/socks_test.log              # curl -v output of both tests
socks/results/danted.log                  # Dante's own log (connections)
```

Useful Wireshark/tcpdump checks on `socks5_client.pcap`:
* `tcp.port == 1080` — SOCKS5 greeting (`05 01 00`), response (`05 00`),
  CONNECT request (`05 01 00 01` + IP + port 443), response (`05 00 00 00`)
* after the CONNECT, the tunneled TLS ClientHello appears.

## Manual run (without the scenario runner)

If you prefer running the pieces yourself (e.g. to keep the lab up while
inspecting):

```bash
cd /home/unclehook/test-tracer/socks
uv run netlab up topology.yml            # create + configure
# (optionally start captures manually)
uv run ansible-playbook -i hosts.yml playbooks/setup_dante.yml
uv run ansible-playbook -i hosts.yml playbooks/test_socks.yml
# inspect, then:
uv run netlab down --cleanup
```

## Notes / known quirks

* Dante config uses `socksmethod notrequired` + `clientmethod notrequired`
  (plan_socks.md doesn't require auth). The `client`/`socks pass` blocks
  still restrict access to `10.10.0.11/32`; the `socks restrict` block
  drops everything else.
* If the host blocks outbound DNS or 443, TEST 1 will fail — that is a
  host firewall issue, not a scenario bug.
* `results/` and all netlab artifacts are git-ignored.


uv run scenario -t socks/topology.yml
# or piece by piece:
cd socks
uv run netlab up topology.yml
uv run ansible-playbook -i hosts.yml playbooks/setup_proxy.yml
uv run ansible-playbook -i hosts.yml playbooks/test_socks.yml
uv run netlab down --cleanup