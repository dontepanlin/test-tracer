"""
test-tracer -- netlab-based network scenario runner.

Uses *netlab* as the topology provider and Ansible as the test engine to
implement the scenario lifecycle:

    Create -> TC -> Verify -> Capture/Scripts -> Harvest -> Teardown
"""

__version__ = "0.1.0"
