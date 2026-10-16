Plan: create two Debian VMs—one runs a SOCKS5 proxy, and the other tests website access through it.

| Machine | Role | Example private IP | Suggested resources |
|---|---|---|---|
| `socks-proxy` | Dante SOCKS5 server | `10.10.0.10` | 1 vCPU, 1 GB RAM, 10 GB disk |
| `socks-client` | Client running `curl` | `10.10.0.11` | 1 vCPU, 1 GB RAM, 10 GB disk |

1. **Create both VMs**
   - Install Debian 13 and configure SSH access.
   - Connect them to the same private network.
   - Give the proxy outbound Internet access and working DNS.

2. **Configure network access**
   - Allow TCP `1080` on `socks-proxy` only from `10.10.0.11`.
   - Allow the proxy outbound TCP `80`/`443` and access to its DNS resolver.
   - Allow SSH from your administration network.

3. **Install the software**

   On `socks-proxy`:
   ```bash
   sudo apt update
   sudo apt install -y dante-server curl ca-certificates
   ```

   On `socks-client`:
   ```bash
   sudo apt update
   sudo apt install -y curl ca-certificates
   ```

4. **Configure Dante**

   Edit `/etc/danted.conf` to:
   - Listen on `10.10.0.10:1080`.
   - Use the interface with Internet access for outbound connections.
   - Allow SOCKS connections only from `10.10.0.11/32`.
   - Permit TCP `connect` requests and enable connection logging.

   Dante uses separate client-access and SOCKS-request rules; configure both. [inet.no](https://inet.no/dante/doc/latest/config/server.html?utm_source=chatgpt.com)

5. **Start and check the proxy**
   ```bash
   sudo systemctl enable danted
   sudo systemctl restart danted
   sudo systemctl status danted --no-pager
   sudo ss -lntp 'sport = :1080'
   ```

6. **Request a website from `socks-client`**
   ```bash
   curl --verbose \
     --noproxy "" \
     --proxy socks5h://10.10.0.10:1080 \
     --connect-timeout 10 \
     --max-time 30 \
     https://example.com/
   ```

   `socks5h://` sends the destination hostname to the proxy for DNS resolution. Replace `https://example.com/` with your target website. [How To Use](https://curl.se/docs/manpage.html?utm_source=chatgpt.com)

7. **Verify the result**
   - Confirm `curl` reports a successful SOCKS5 connection and receives the website response.
   - Check proxy logs:
     ```bash
     sudo journalctl -u danted --since "5 minutes ago" --no-pager
     ```
   - Stop Dante temporarily and repeat the same request: it should fail. Restart Dante afterward.