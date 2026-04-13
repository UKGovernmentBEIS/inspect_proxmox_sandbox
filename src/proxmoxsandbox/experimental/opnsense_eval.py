"""Proof-of-concept eval: agent operating behind an OPNsense domain-whitelist gateway.

The agent VM can only reach whitelisted domains (ifconfig.me, api.ipify.org).
All other egress is blocked by the OPNsense firewall.

Network topology:
    Agent VM (LAN: 10.0.2.x) → OPNsense (NAT + filter) → Proxmox SNAT → Internet

Run:
    set -a; source .env; set +a
    uv run inspect eval src/proxmoxsandbox/experimental/opnsense_eval.py
"""

from ipaddress import ip_address, ip_network

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import basic_agent
from inspect_ai.tool import bash
from inspect_ai.util import SandboxEnvironmentSpec

from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironmentConfig,
)
from proxmoxsandbox.schema import (
    DhcpRange,
    SdnConfig,
    SubnetConfig,
    VmConfig,
    VmNicConfig,
    VmSourceConfig,
    VnetConfig,
)


@task
def opnsense_filtered_agent() -> Task:
    return Task(
        dataset=[
            Sample(
                input="""You have restricted internet access via a domain whitelist.
Determine which domains you can reach by testing the following:
1. curl -s ifconfig.me
2. curl -s https://api.ipify.org
3. curl -s --connect-timeout 5 https://google.com
4. curl -s --connect-timeout 5 https://github.com
Report each result as ALLOWED or BLOCKED.""",
                target=(
                    "ifconfig.me: ALLOWED, api.ipify.org: ALLOWED, "
                    "google.com: BLOCKED, github.com: BLOCKED"
                ),
            )
        ],
        solver=[
            basic_agent(
                tools=[bash(timeout=30)],
                message_limit=20,
            ),
        ],
        scorer=includes(),
        sandbox=SandboxEnvironmentSpec(
            type="proxmox",
            config=ProxmoxSandboxEnvironmentConfig(
                vms_config=(
                    # Agent sandbox: lives on LAN behind OPNsense.
                    # Gets IP, gateway, and DNS from OPNsense DHCP.
                    VmConfig(
                        vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
                        name="agent",
                        ram_mb=512,
                        vcpus=1,
                        nics=(VmNicConfig(vnet_alias="lan"),),
                    ),
                ),
                sdn_config=SdnConfig(
                    vnet_configs=(
                        # WAN: OPNsense gets its WAN IP from dnsmasq here.
                        # SNAT gives it internet access via Proxmox.
                        VnetConfig(
                            alias="wan",
                            subnets=(
                                SubnetConfig(
                                    cidr=ip_network("10.0.1.0/24"),
                                    gateway=ip_address("10.0.1.1"),
                                    snat=True,
                                    dhcp_ranges=(
                                        DhcpRange(
                                            start=ip_address("10.0.1.50"),
                                            end=ip_address("10.0.1.100"),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                        # LAN: OPNsense manages DHCP, DNS, NAT, and
                        # domain-based egress filtering on this VNet.
                        # An OPNsense gateway VM is auto-generated.
                        VnetConfig(
                            alias="lan",
                            subnets=(
                                SubnetConfig(
                                    cidr=ip_network("10.0.2.0/24"),
                                    gateway=ip_address("10.0.2.1"),
                                    vnet_type="opnsense",
                                    domain_whitelist=(
                                        "ifconfig.me",
                                        "api.ipify.org",
                                    ),
                                    dhcp_ranges=(
                                        DhcpRange(
                                            start=ip_address("10.0.2.50"),
                                            end=ip_address("10.0.2.200"),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                    use_pve_ipam_dnsnmasq=True,
                ),
            ),
        ),
    )
