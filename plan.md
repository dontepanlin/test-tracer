### 1. Architectural Analysis of `ipspace/netlab`

`netlab` is an infrastructure-as-code virtual networking lab tool that translates high-level YAML topologies into provider-specific configurations (Containerlab, Libvirt, Vagrant) and generates Ansible inventories [[1]].

**How CLI Subcommands Work:**
The CLI dispatcher is dynamic. When you execute `netlab <command>`, the system attempts to import a module named `netsim.cli.<command>` and calls its `run()` function [[20]]. To add a new subcommand, you do not need to modify the core dispatcher; you simply create a file `netsim/cli/scenario.py` and implement the `run(cli_args)` function.

### 2. The "Molecule vs. Netlab" Paradigm

You mentioned that **Molecule** is better suited for testing but lacks topology creation. This is accurate: Molecule is designed to test Ansible roles against single containers, whereas `netlab` builds complex multi-node network topologies.

**The Strategy:** You do not need to force Molecule to create topologies. Instead, use `netlab` as the "Infrastructure Builder" and standard Ansible as the "Test Runner."
*   **Netlab's Role:** Creates the complex network topology (Routers, Switches, MongoDB servers, BitTorrent peers) and generates a standard Ansible inventory (`hosts.yml`).
*   **Your Subcommand's Role:** Acts as the orchestrator. It reads the topology, spins up the lab, and then runs Ansible playbooks against the generated inventory to simulate traffic, apply `tc` rules, and capture PCAPs.

### 3. Implementation Plan: The `scenario` Lifecycle

Your goal is to implement a lifecycle: **Create $\rightarrow$ TC $\rightarrow$ Verify $\rightarrow$ Capture/Script $\rightarrow$ Teardown**.

#### Phase 1: Topology Creation (`create`)
Your subcommand will call the internal `netsim.cli.create.run()` or `netsim.cli.up.run()` to spin up the lab nodes. This generates the virtual interfaces and the Ansible inventory required for the next steps.

#### Phase 2: Traffic Control (`tc` emulations)
`netlab` has native support for Traffic Control (`tc`) if `defaults.tc.enable` is set to `True` in the topology [[11]].
*   **Native Approach:** Define `tc` rules (delay, loss, rate, corruption) directly on links in the YAML. `netlab` will apply these automatically via `providers.execute_tc_commands(topology)`.
*   **Advanced Approach:** If you need complex `netem` distributions, use an Ansible playbook to run `tc qdisc add ...` directly on the Linux containers via SSH.

#### Phase 3: Verification (`verify`)
Before starting traffic, ensure the network is stable. The subcommand can invoke `netlab validate` (which checks routing adjacencies and reachability) or a custom Ansible playbook to ping specific interfaces.

#### Phase 4: Capture & Scripted Work (`capture` + `scripts`)
Using the generated `hosts.yml`, your subcommand will trigger Ansible to:
1.  **Start Captures:** Run background `tcpdump` or `tshark` processes on specific interfaces (e.g., MongoDB ports 27017, BitTorrent ports).
2.  **Generate Traffic:** Run scripts to simulate MongoDB replication or BitTorrent seeding/leeching.

#### Phase 5: Harvest & Teardown
1.  **Stop Captures:** Kill the background `tcpdump` processes.
2.  **Fetch PCAPs:** Use the Ansible `fetch` module to pull the `.pcap` files from the nodes to a local `./results/` directory.
3.  **Cleanup:** Call `netsim.cli.down.run()` to destroy the lab and remove temporary files.

---

### 4. Proposed Topology Schema

You should extend the `topology.yml` to include a `scenario:` block. This keeps the network definition and the test logic in a single file.

```yaml
defaults:
  device: linux # Use linux containers for servers
  tc:
    enable: True # Enable netlab's native tc support

nodes:
  mongo1: { device: linux }
  mongo2: { device: linux }
  bt-seed: { device: linux }
  router: { device: eos }

links:
  - mongo1-router:
      tc: { delay: 50ms, loss: 2% } # Simulate bad WAN link
  - mongo2-router:
  - bt-seed-router:

# Custom extension for your subcommand
scenario:
  name: "mongodb_bittorrent_test"
  verify:
    - wait: 30 # Wait for routing to converge
  capture:
    - node: mongo1
      interface: eth1
      filter: "port 27017"
      output: mongo_sync.pcap
    - node: bt-seed
      interface: eth1
      output: bittorrent.pcap
  scripts:
    - playbook: seed_torrent.yml
    - playbook: mongo_sync.yml
```

---

### 5. Implementation Roadmap (Python Code)

Create the file `netsim/cli/scenario.py`. This skeleton demonstrates how to orchestrate the lifecycle using `netlab`'s internal APIs and `external_commands`.

```python
#!/usr/bin/env python3
import sys
import argparse
from box import Box

# Import existing netlab modules to reuse their logic
from . import up, down, create
from ..utils import log, external_commands, read

def scenario_parse_args():
    parser = argparse.ArgumentParser(description='Run a network scenario')
    parser.add_argument('-t', '--topology', dest='topology', default='topology.yml', help='Topology file')
    return parser

def run(cli_args):
    parser = scenario_parse_args()
    args = parser.parse_args(cli_args)
    
    log.section_header('Starting', 'Scenario Environment')
    
    # 1. CREATE: Spin up the lab 
    # This handles node creation, link setup, and native 'tc' application
    up.run(['-t', args.topology])
    
    # 2. VERIFY: Check routing/connectivity
    log.section_header('Verifying', 'Lab connectivity')
    external_commands.run_command('netlab validate')
    
    # 3. CAPTURE & SCRIPTS: Run Ansible playbooks against the generated inventory
    log.section_header('Executing', 'Scenario playbooks')
    # In a real implementation, parse the 'scenario:' block from the YAML here
    # and dynamically generate a playbook that starts tcpdump, runs traffic, and fetches files.
    external_commands.run_command('ansible-playbook -i hosts.yml run_scenario.yml')
    
    # 4. HARVEST: Fetch PCAPs
    log.section_header('Harvesting', 'PCAP files')
    external_commands.run_command('ansible-playbook -i hosts.yml fetch_pcaps.yml')
    
    # 5. TEARDOWN: Destroy the lab
    log.section_header('Teardown', 'Destroying lab')
    down.run(['--cleanup'])
    
    log.status_green('DONE', f'Scenario results saved to ./results/')

if __name__ == '__main__':
    run(sys.argv[1:])
```

### 6. Summary
By treating `netlab` as the **Topology Provider** and Ansible as the **Test Engine**, you achieve your main goal: creating complex scenarios (MongoDB, BitTorrent) with specific link conditions (`tc`) and collecting PCAPs, all without relying on Molecule’s limited single-container architecture.