# Worked example: SOCKS5 proxy lab

A complete, real lab from `socks/topology.yml`. Copy this structure for a new
protocol. Two Debian containers on one L2 segment; one runs a SOCKS5 proxy, the
other curls a website through it. The test: the proxied request must **succeed**,
and the same request must **fail** when the proxy is stopped.

```yaml
name: socks
provider: clab
defaults:
  device: linux
  group: test-group
  multilab.id: socks
  mgmt_base: 192.0.2
  mgmt_prefix: 24
  mgmt_mode: static
  addressing:
    v4_pool: 10.10.0.0/24
  attributes:
    global:
      scenario: { type: dict }

nodes:
  socks-proxy:
    image: debian:13
  socks-client:
    image: debian:13

links:
  - interfaces:
      - node: socks-proxy
        ipv4: 10.10.0.10/24
      - node: socks-client
        ipv4: 10.10.0.11/24
    prefix: 10.10.0.0/24

scenario:
  name: socks5_proxy_test
  results: results

  # A plain L2 lab defines no `netlab validate` tests, so verify is just a
  # settle wait. Reachability is checked as a real *step* (`ping`) below,
  # after the data links are addressed — because on a no-sudo host netlab runs
  # with --no-config and does NOT address the data interfaces.
  verify:
    - wait: 10

  # HOW to capture (named). The client-side handshake is on the L2 data link
  # (link: 0 -> eth1). The proxy's outbound egress goes via its mgmt interface
  # (Docker NAT), so that capture uses an explicit interface: eth0.
  captures:
    - id: socks_handshake
      node: socks-client
      link: 0
      filter: "host 10.10.0.10 and tcp port 1080"
      output: socks5_client.pcap
    - id: proxy_outbound
      node: socks-proxy
      interface: eth0
      filter: "tcp port 443"
      output: socks5_proxy_outbound.pcap

  # WHEN + WHAT (the timeline). Each step is a test case.
  steps:
    # Address every node's data link from its own hostvars + install iproute2
    # (absent in the base image). Required on the --no-config (no-sudo) path;
    # idempotent when netlab already did the initial config.
    - name: address-links
      do:
        - node: socks-proxy
          label: address proxy eth1
          raw: |
            command -v ip >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq iproute2 >/tmp/iproute.log 2>&1; }
            ip addr replace {{ hostvars['socks-proxy'].interfaces[0].ipv4 }} dev eth1
            ip link set dev eth1 up
            echo addressed
        - node: socks-client
          label: address client eth1
          raw: |
            command -v ip >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq iproute2 >/tmp/iproute.log 2>&1; }
            ip addr replace {{ hostvars['socks-client'].interfaces[0].ipv4 }} dev eth1
            ip link set dev eth1 up
            echo addressed
      assert: { contains: "addressed" }

    # Reachability now that both links are addressed.
    - name: ping
      do:
        - node: socks-client
          label: ping proxy
          raw: |
            command -v ping >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq iputils-ping >/tmp/ping.log 2>&1; }
            ping -c 3 -W 2 10.10.0.10
      assert: { rc_eq: 0 }

    # Install + configure the proxy (does NOT start it), then start it as a
    # detached daemon, then verify it's listening.
    - name: bring-up
      do:
        # apt bootstrap, download the go-socks5-proxy binary, iptables source
        # restriction. No daemon start here — a process backgrounded inside an
        # Ansible task dies when the task's docker-exec session ends.
        - playbook: playbooks/setup_proxy.yml
        # Start the proxy detached (survives the task).
        - node: socks-proxy
          label: start proxy daemon
          daemon: /usr/local/bin/go-socks5-proxy --host 10.10.0.10 --port 1080 >>/var/log/go_socks5.log 2>&1
        # Verify it's actually listening (catches a silent failure).
        - node: socks-proxy
          label: verify proxy listening
          shell: ss -lnt 'sport = :1080' | grep -q LISTEN && echo PROXY_UP || echo PROXY_DOWN
      assert: { contains: "PROXY_UP" }

    - name: happy-path
      # Both captures active DURING this step only.
      capture: [socks_handshake, proxy_outbound]
      do:
        - node: socks-client
          label: curl via proxy
          shell: |
            command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl ca-certificates >/tmp/curl_install.log 2>&1; }
            curl --verbose --noproxy "" \
              --proxy socks5h://10.10.0.10:1080 \
              --connect-timeout 10 --max-time 30 \
              https://example.com/
      # The server answers over HTTP/2 ("HTTP/2 200") or HTTP/1.x
      # ("HTTP/1.1 200"); match both. (curl --verbose writes the status line
      # to stderr, which the runner merges into the asserted stdout.)
      assert: { regex: "HTTP/[12](\\.[0-9]+)? 200" }

    - name: negative
      # No capture here: the failed attempt is intentionally not recorded.
      do:
        - node: socks-proxy
          label: stop proxy daemon
          daemon_kill: go-socks5-proxy
        - node: socks-client
          label: curl with proxy stopped
          shell: |
            curl --noproxy "" \
              --proxy socks5h://10.10.0.10:1080 \
              --connect-timeout 10 --max-time 30 \
              https://example.com/ || true
      # Without the proxy the request must FAIL (no HTTP 200).
      assert: { not_contains: "HTTP/" }

    - name: restore
      do:
        - node: socks-proxy
          label: restart proxy daemon
          daemon: /usr/local/bin/go-socks5-proxy --host 10.10.0.10 --port 1080 >>/var/log/go_socks5.log 2>&1
        - node: socks-proxy
          label: verify proxy listening
          shell: ss -lnt 'sport = :1080' | grep -q LISTEN && echo PROXY_UP || echo PROXY_DOWN
      assert: { contains: "PROXY_UP" }
      # Also harvest the proxy log for inspection.
      log:
        - node: socks-proxy
          src: /var/log/go_socks5.log
          output: go_socks5.log
```

## Why this is shaped this way

- **Two captures, two different interfaces.** `socks_handshake` is on the L2
  data link (`link: 0` → eth1) and sees the SOCKS handshake + tunneled bytes.
  `proxy_outbound` is on the mgmt/egress interface (`interface: eth0`) and sees
  the proxy's real outbound 443 to the Internet. Both prove the relay works.
- **Capture scoped to `happy-path` only.** Because only `happy-path` lists the
  captures, the PCAPs contain the successful exchange — not the negative test
  (proxy stopped). The negative step runs with **no** capture, so the failed
  attempt never pollutes the trace.
- **The proxy is a `daemon:`, not a backgrounded `shell`.** A process started
  with `nohup … &` inside an Ansible task is killed when that task's docker-exec
  session ends (no init in the container). The `daemon:` action starts it with
  `docker exec -d` (detached) so it survives; `daemon_kill:` stops it. The
  `playbook:` step only installs + configures; the `shell:` verify + `PROXY_UP`
  assert catch a silent start failure.
- **`address-links` + `ping` make the scenario self-sufficient on the no-sudo
  host.** With `--no-config` netlab doesn't address the data links, and the
  base image lacks `ip`/`ping`/`curl`/`tcpdump`, so the first steps install
  what they need and address each node's `eth1` from its own hostvars.
- **Asserts are the pass/fail.** `happy-path` asserts a regex that matches an
  HTTP 200 (over HTTP/1 or HTTP/2); `negative` asserts `not_contains: "HTTP/"`.
  The runner evaluates these from the step's combined stdout, so no shell `if`
  logic is needed.
- **`restore` leaves the lab working** and harvests the proxy log.

## Result after a run

```
socks/results/socks5_client.pcap          # ~10 KB — SOCKS5 handshake + TLS tunnel (happy-path only)
socks/results/socks5_proxy_outbound.pcap  # ~12 KB — proxy -> Internet (443)
socks/results/go_socks5.log               # proxy log
```

Inspect with tshark (or `tcpdump -nn -r` if tshark isn't installed):

```bash
# handshake + tunneled ClientHello (SNI=example.com) inside the SOCKS tunnel
tshark -r socks/results/socks5_client.pcap -Y 'socks'
tshark -r socks/results/socks5_client.pcap -Y 'tls.handshake.extensions_server_name == "example.com"'
# the proxy's real outbound ClientHello on the egress interface
tshark -r socks/results/socks5_proxy_outbound.pcap -Y 'tls.handshake.extensions_server_name == "example.com"'
```

## Running this lab

```bash
# host with passwordless `sudo ip`:
uv run scenario -t socks/topology.yml
# host WITHOUT passwordless sudo (the common case here):
uv run scenario -t socks/topology.yml --up-arg=--no-config
# optional, to skip the validate call outright:
uv run scenario -t socks/topology.yml --no-validate --up-arg=--no-config
```

The no-sudo form is safe to omit `--no-validate` on: the runner treats a plain
lab's `netlab validate` "No validation tests defined" as a pass, not a failure.

## Adapting to a new protocol

1. Pick the node(s) and the interface(s) where the protocol's traffic flows.
2. Add a `capture:` entry per (node, interface, BPF filter) you want.
3. If the host runs without passwordless sudo, add an early `address-links`
   step (install iproute2, address each data link from hostvars) and a `ping`
   step — copy them from this example.
4. Write a `bring-up` step: `playbook:` (install + config) → `daemon:` (start
   the SUT) → `shell:` (verify listening) → `assert: { contains: "<UP>" }`.
5. Write the traffic step(s) that generate the protocol traffic (inline
   `shell`/`raw`, or a `playbook:` for complex setup) and list the capture ids
   in that step's `capture:`.
6. Add an `assert:` per test case (positive and negative), and a `daemon_kill:`
   + restart for the negative/restore cases.
7. `uv run scenario describe -t <file>` to validate, `--dry-run` to check the
   capture windows, then run.
