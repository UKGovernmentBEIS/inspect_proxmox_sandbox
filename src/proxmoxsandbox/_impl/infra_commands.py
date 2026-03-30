import abc
import hashlib
import importlib.resources
import os
import re
import sys
from ipaddress import ip_address, ip_network
from logging import getLogger
from random import randint, shuffle
from typing import Collection, List, Sequence, Set, Tuple

import tenacity
from inspect_ai.util import trace_action
from rich import box, print
from rich.prompt import Confirm
from rich.table import Table

from proxmoxsandbox._impl.agent_commands import AgentCommands
from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.built_in_vm import BuiltInVM
from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox._impl.sdn_commands import (
    ZONE_REGEX,
    IpamMapping,
    SdnCommands,
    VnetAliases,
)
from proxmoxsandbox._impl.task_wrapper import TaskWrapper
from proxmoxsandbox.schema import (
    SdnConfig,
    SdnConfigType,
    SubnetConfig,
    VmConfig,
    VnetConfig,
)


def _gateway_mac(proxmox_ids_start: str) -> str:
    """Derive a unique, deterministic MAC from the per-eval proxmox_ids_start prefix.

    Uses the QEMU/KVM OUI (52:54:00) so Proxmox recognises it as a valid virtual NIC.
    The 3-byte suffix is an MD5 hash of the prefix, giving per-eval uniqueness
    without shared state or synchronisation.
    """
    digest = hashlib.md5(proxmox_ids_start.encode(), usedforsecurity=False).digest()
    return f"52:54:00:{digest[0]:02x}:{digest[1]:02x}:{digest[2]:02x}"


def _gateway_ip_for_subnet(subnet_cidr: str) -> str:
    """Return the .2 host in the subnet — the gateway VM's sandbox-facing IP.

    .1 is permanently reserved by Proxmox SDN as the bridge device's IP for
    that subnet; it cannot be reassigned.  We use .2 to avoid a conflict with it.

    _prepare_sdn_for_gateway sets the SDN subnet's gateway field to .2 so that
    Proxmox's dnsmasq advertises .2 as the DHCP router to sandbox VMs.  A side
    effect is that the Proxmox SDN bridge device also claims .2, creating an ARP
    conflict with the gateway VM's sandbox-facing NIC.  That conflict is resolved
    in create_sdn_and_vms by injecting a permanent ARP entry on each sandbox VM.
    """
    network = ip_network(subnet_cidr)
    return str(network.network_address + 2)


def _nftables_config(sandbox_cidr: str) -> str:
    """Generate nftables ruleset for the gateway VM.

    Three chains:
    - prerouting_nat: intercepts all DNS from the sandbox and redirects it to
      the gateway's dnsmasq (via `redirect to :53`, which DNAT's to 127.0.0.1),
      so sandbox VMs cannot bypass the allowlist by configuring an alternative
      DNS server.  conntrack reverses this for replies.
    - postrouting_nat: masquerades forwarded sandbox traffic as the gateway's
      external IP, so return traffic is routed back correctly.
    - forward: default-drop; only allows traffic to IPs in the allowed_ips set,
      which is seeded at provision time by _pre_resolve_script (resolving the
      allowlist domains and adding their IPs via 'nft add element').  dnsmasq's
      nftset= directive is intentionally not used.  Although Debian 13's
      dnsmasq (≥2.87) does support nftset, it only populates the nftables set on DNS
      query — not at boot.  Pre-resolving at provision time ensures IPs are seeded
      before the first connection attempt, which is simpler and more predictable.

    IPv6:
    table inet covers both IPv4 and IPv6; policy drop on the forward chain
    therefore drops IPv6 forwarded through the gateway.  The primary IPv6 risk
    is direct routing: a sandbox VM that receives a Router Advertisement can
    get a global IPv6 address and route traffic to the internet without touching
    the gateway.  This is blocked at the sandbox VM level via accept-ra: false
    in the cloud-init network config (dhcp6: false alone only disables DHCPv6,
    not SLAAC).

    Intentional omission — INPUT chain:
    There are no INPUT chain rules blocking connections from the sandbox to the
    gateway VM's own ports.  Sandbox VMs can reach port 53 (dnsmasq, by design)
    and any other listening service.  The gateway cloud-init intentionally does
    NOT install openssh-server to keep this surface minimal.  Do not add SSH or
    other management services to the gateway VM image.
    """
    return f"""\
#!/usr/sbin/nft -f
flush ruleset
table inet gateway {{
    set allowed_ips {{
        type ipv4_addr
        flags interval
    }}

    chain prerouting_nat {{
        type nat hook prerouting priority dstnat;
        ip saddr {sandbox_cidr} udp dport 53 redirect to :53
        ip saddr {sandbox_cidr} tcp dport 53 redirect to :53
    }}

    chain postrouting_nat {{
        type nat hook postrouting priority srcnat;
        ip saddr {sandbox_cidr} masquerade
    }}

    chain forward {{
        type filter hook forward priority filter; policy drop;
        ct state established,related counter accept
        ip saddr {sandbox_cidr} tcp dport 853 drop
        ip saddr {sandbox_cidr} udp dport 853 drop
        ip saddr {sandbox_cidr} ip daddr @allowed_ips counter accept
        counter drop
    }}
}}
"""


def _dnsmasq_allowlist_config(gateway_ip: str, allow_domains: Tuple[str, ...]) -> str:
    """Generate the per-eval dnsmasq allowlist config for the gateway VM.

    Each allowed domain gets server=/<domain>/8.8.8.8 so dnsmasq forwards
    those queries upstream.  dnsmasq's /domain/ syntax matches the apex and
    all subdomains, so "gnu.org" covers ftp.gnu.org too.

    Without a global server= directive, any unlisted domain gets SERVFAIL.

    listen-address is set to the gateway's sandbox-facing IP only.
    bind-interfaces is required alongside listen-address: without it, dnsmasq
    binds to 0.0.0.0:53 regardless of listen-address (listen-address only
    affects filtering, not binding).  bind-interfaces makes dnsmasq bind to
    exactly the specified IP, so the restart doesn't race with the boot-time
    dnsmasq instance releasing 0.0.0.0:53.

    nftset= is intentionally omitted.  Although Debian 13's dnsmasq supports it
    (--enable-nftset has been compiled in since 2.87), nftset only populates
    allowed_ips on DNS query rather than at boot.  Pre-resolving at provision
    time (_pre_resolve_script) seeds IPs before the first connection attempt.
    """
    lines = [
        "# Per-eval allowlist — generated at provision time.",
        "bind-interfaces",
        f"listen-address={gateway_ip}",
        "",
    ]
    for domain in allow_domains:
        lines.append(f"server=/{domain}/8.8.8.8")
    return "\n".join(lines) + "\n"


def _pre_resolve_script(allow_domains: Tuple[str, ...]) -> str:
    """Return a Python script that resolves allowed domains and seeds the nftables set.

    Run once at provision time so traffic to those IPs is forwarded even before
    the first DNS query.

    Queries 8.8.8.8 directly (via python3-dnspython) to match dnsmasq's upstream,
    avoiding IP mismatch when the gateway's system resolver uses a different DNS
    (e.g., a corporate nameserver with different geoDNS targets).  Falls back to
    the system resolver if python3-dnspython is not installed (old gateway template).

    Limitation: only apex domains are pre-resolved.  Subdomains of allowed domains
    (e.g. ftp.gnu.org when "gnu.org" is allowed) are resolved at query time by
    dnsmasq but their IPs are NOT added to allowed_ips here — they will be dropped
    by the FORWARD chain.  Explicit subdomain listing is required.
    """
    domains_repr = repr(list(allow_domains))
    template = (
        importlib.resources.files("proxmoxsandbox._impl._resources")
        .joinpath("pre_resolve.py.template")
        .read_text(encoding="utf-8")
    )
    return template.replace("{domains_repr}", domains_repr)


class InfraCommands(abc.ABC):
    logger = getLogger(__name__)

    TRACE_NAME = "proxmox_infra_command"

    async_proxmox: AsyncProxmoxAPI
    task_wrapper: TaskWrapper
    sdn_commands: SdnCommands
    qemu_commands: QemuCommands
    built_in_vm: BuiltInVM
    node: str

    def __init__(self, async_proxmox: AsyncProxmoxAPI, node: str):
        self.async_proxmox = async_proxmox
        self.task_wrapper = TaskWrapper(async_proxmox)
        self.sdn_commands = SdnCommands(async_proxmox)
        self.qemu_commands = QemuCommands(async_proxmox, node)
        self.built_in_vm = BuiltInVM(async_proxmox, node)
        self.node = node

    async def _prepare_sdn_for_gateway(self, sdn_config: SdnConfig) -> SdnConfig:
        """Transform a SdnConfig to route sandbox egress through a gateway VM.

        Modifies the first (and only) vnet:
        - gateway IP changed to network .2 (the gateway VM's sandbox-facing IP)
        - snat set to False (gateway VM does NAT, not Proxmox)
        - alias set to "sandbox" if unset (needed so gateway's NIC can reference it)

        Adds an external vnet (snat=True) for the gateway VM's internet-facing
        interface, with a non-overlapping CIDR picked automatically.

        Raises:
            ValueError: if sdn_config has more than one vnet, if use_pve_ipam_dnsnmasq
                is False, or if no suitable external CIDR can be found.
        """
        if not sdn_config.use_pve_ipam_dnsnmasq:
            raise ValueError(
                "allow_domains requires use_pve_ipam_dnsnmasq=True. "
                "Proxmox IPAM is still used to reserve sandbox VM IPs and "
                "assign the gateway VM its address via DHCP. The gateway VM "
                "runs its own separate dnsmasq instance for DNS filtering only — "
                "it does not replace Proxmox's dnsmasq for DHCP."
            )
        if len(sdn_config.vnet_configs) != 1:
            raise ValueError(
                f"allow_domains requires exactly one vnet_config, "
                f"got {len(sdn_config.vnet_configs)}"
            )
        sandbox_vnet = sdn_config.vnet_configs[0]
        if len(sandbox_vnet.subnets) != 1:
            raise ValueError(
                f"allow_domains requires exactly one subnet per vnet, "
                f"got {len(sandbox_vnet.subnets)}"
            )

        sandbox_subnet = sandbox_vnet.subnets[0]
        sandbox_network = ip_network(str(sandbox_subnet.cidr))
        gateway_ip = ip_address(sandbox_network.network_address + 2)

        modified_subnet = SubnetConfig(
            cidr=sandbox_subnet.cidr,
            # Proxmox's gateway field controls BOTH the SDN bridge device's IP AND
            # the DHCP option-3 (router) advertised to VMs — they cannot be set
            # independently.  Setting it to .2 makes sandbox VMs route to the gateway
            # VM, but also causes the Proxmox bridge to claim .2, creating an ARP
            # conflict with the gateway VM's sandbox-facing NIC.  That conflict is
            # resolved in create_sdn_and_vms by injecting a permanent ARP entry on
            # each sandbox VM pointing .2 → the gateway VM's MAC.
            gateway=gateway_ip,
            snat=False,
            dhcp_ranges=sandbox_subnet.dhcp_ranges,
        )
        modified_sandbox_vnet = VnetConfig(
            alias=sandbox_vnet.alias if sandbox_vnet.alias is not None else "sandbox",
            subnets=(modified_subnet,),
        )

        # Pick an external VNet CIDR that doesn't overlap with the sandbox CIDR
        # or any existing Proxmox CIDRs.
        # Only exclude the sandbox_network — not modified_sandbox_vnet — because the
        # sandbox CIDR may appear in a stale leftover Proxmox zone from a failed run
        # (which create_sdn will overwrite), and including it would falsely poison
        # every iteration inside find_non_overlapping_vnet_config.
        external_vnet = await self.sdn_commands.find_non_overlapping_vnet_config(
            exclude_networks=[sandbox_network],
        )

        # allow_domains is intentionally cleared: this returned config is the
        # internal 2-vnet form (sandbox + external) with the gateway already
        # embedded. The caller captures allow_domains before calling this method
        # and uses it directly; carrying it here would re-trigger the schema
        # validator that enforces "exactly one vnet when allow_domains is set".
        return SdnConfig(
            vnet_configs=(modified_sandbox_vnet, external_vnet),
            use_pve_ipam_dnsnmasq=sdn_config.use_pve_ipam_dnsnmasq,
            allow_domains=(),
        )

    async def _provision_gateway(
        self,
        proxmox_ids_start: str,
        vnet_aliases: VnetAliases,
        sdn_zone_id: str,
        sandbox_cidr: str,
        gateway_ip: str,
        gateway_mac: str,
        allow_domains: Tuple[str, ...],
    ) -> int:
        """Clone the gateway template and configure it for this eval instance.

        Steps:
        1. Clone the gateway template (linked clone — fast, small).
        2. Wire NICs: net0 → sandbox vnet (with the static MAC), net1 → external vnet.
        3. Start VM and wait for QEMU guest agent.
        4. Write a systemd-networkd .network file to assign a static IP (.2) to the
           sandbox NIC.  A DHCP reservation is impossible here because Proxmox claims
           the subnet's gateway IP as a MAC-less topology entry in IPAM.  Using
           networkd config (rather than 'ip addr add') ensures the address is
           persistent — networkd would otherwise flush a manually-added address when
           it re-applies its own DHCP config.
        5. Inject nftables ruleset and apply it.
        6. Inject dnsmasq allowlist config and restart dnsmasq.
        7. Pre-resolve allowed domains into the nftables allowed_ips set.
        """
        sandbox_vnet_id = vnet_aliases[0][0]
        external_vnet_id = vnet_aliases[1][0]

        gateway_template_id = await self.built_in_vm.known_gateway()
        if gateway_template_id is None:
            raise ValueError(
                "Gateway VM template not found — call ensure_gateway_exists() first"
            )

        new_vm_id = await self.qemu_commands.find_next_available_vm_id()

        async def create_clone() -> None:
            await self.async_proxmox.request(
                "POST",
                f"/nodes/{self.node}/qemu/{gateway_template_id}/clone",
                json={
                    "newid": new_vm_id,
                    "full": 0,
                    "name": f"inspect-gw-{proxmox_ids_start}",
                },
            )
            await self.qemu_commands.register_created_vm(new_vm_id)

        with trace_action(
            self.logger, self.TRACE_NAME, f"clone gateway VM {new_vm_id=}"
        ):
            await self.task_wrapper.do_action_and_wait_for_tasks(create_clone)

        async def configure_gw() -> None:
            # Replace net0 (was static boot VNet from template) with sandbox VNet,
            # and add net1 for the external (internet-facing) VNet.
            # Tags: "inspect" only — "gateway" is reserved for the template.
            await self.async_proxmox.request(
                "POST",
                f"/nodes/{self.node}/qemu/{new_vm_id}/config",
                json={
                    "net0": f"virtio,bridge={sandbox_vnet_id}"
                    f",macaddr={gateway_mac.upper()}",
                    "net1": f"virtio,bridge={external_vnet_id}",
                    "tags": "inspect",
                },
            )

        await self.task_wrapper.do_action_and_wait_for_tasks(configure_gw)

        await self.qemu_commands.start_and_await(vm_id=new_vm_id, is_sandbox=True)

        agent_commands = AgentCommands(self.async_proxmox, self.node)

        # Configure a static IP on the sandbox-facing NIC via systemd-networkd.
        # Writing a .network file (matched by MAC, 00- prefix beats cloud-init's
        # 10-cloud-init-*.network) makes networkd the source of truth so the
        # address survives DHCP renewals and link state changes.
        #
        # Why not 'ip addr flush; ip addr add'? networkd manages the interface
        # with DHCP; a manually-added address is transient — networkd flushes
        # it when it re-applies its DHCP config, sometimes within seconds. By
        # the time sandbox VMs boot and ARP for .2, the Proxmox SDN bridge
        # (which also holds .2 as its own IP, because we set gateway=.2 in the
        # subnet config) wins the ARP race and all sandbox traffic hits the
        # bridge instead of the gateway VM's FORWARD chain.
        prefix_len = ip_network(sandbox_cidr).prefixlen
        network_file = f"""\
[Match]
MACAddress={gateway_mac}

[Network]
DHCP=no
Address={gateway_ip}/{prefix_len}
"""
        await agent_commands.write_file(
            vm_id=new_vm_id,
            content=network_file.encode("utf-8"),
            filepath="/etc/systemd/network/00-sandbox-static.network",
        )
        set_ip_res = await agent_commands.exec_command(
            vm_id=new_vm_id,
            command=[
                "bash",
                "-c",
                f"networkctl reload && "
                f"for i in $(seq 1 15); do "
                f"  ip -4 addr show | grep -qF '{gateway_ip}'"
                f"    && {{ echo IP_OK; exit 0; }}; "
                f"  sleep 1; "
                f"done; "
                f"echo IP_MISSING; exit 1",
            ],
        )

        @tenacity.retry(
            wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
            stop=tenacity.stop_after_delay(30),
            retry=tenacity.retry_if_result(lambda x: x is False),
        )
        async def wait_for_set_ip() -> bool:
            status = await agent_commands.get_agent_exec_status(
                vm_id=new_vm_id, pid=set_ip_res["pid"]
            )
            if status["exited"] == 1:
                if status["exitcode"] != 0:
                    raise ValueError(
                        f"Failed to configure static IP {gateway_ip}: "
                        f"stdout={status.get('out-data', '')!r}, "
                        f"stderr={status.get('err-data', '')!r}"
                    )
                return True
            return False

        await wait_for_set_ip()

        # Inject and apply nftables config
        nft_config = _nftables_config(sandbox_cidr)
        await agent_commands.write_file(
            vm_id=new_vm_id,
            content=nft_config.encode("utf-8"),
            filepath="/etc/nftables.conf",
        )
        nft_res = await agent_commands.exec_command(
            vm_id=new_vm_id,
            command=["nft", "-f", "/etc/nftables.conf"],
        )

        @tenacity.retry(
            wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
            stop=tenacity.stop_after_delay(30),
            retry=tenacity.retry_if_result(lambda x: x is False),
        )
        async def wait_for_nft() -> bool:
            status = await agent_commands.get_agent_exec_status(
                vm_id=new_vm_id, pid=nft_res["pid"]
            )
            if status["exited"] == 1:
                if status["exitcode"] != 0:
                    raise ValueError(
                        f"nft -f failed: stdout={status.get('out-data', '')!r}, "
                        f"stderr={status.get('err-data', '')!r}"
                    )
                return True
            return False

        await wait_for_nft()

        # Inject dnsmasq allowlist and restart dnsmasq
        dnsmasq_config = _dnsmasq_allowlist_config(gateway_ip, allow_domains)
        await agent_commands.write_file(
            vm_id=new_vm_id,
            content=dnsmasq_config.encode("utf-8"),
            filepath="/etc/dnsmasq.d/allowlist.conf",
        )
        dnsmasq_res = await agent_commands.exec_command(
            vm_id=new_vm_id,
            command=["systemctl", "restart", "dnsmasq"],
        )

        @tenacity.retry(
            wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
            stop=tenacity.stop_after_delay(30),
            retry=tenacity.retry_if_result(lambda x: x is False),
        )
        async def wait_for_dnsmasq_restart() -> bool:
            status = await agent_commands.get_agent_exec_status(
                vm_id=new_vm_id, pid=dnsmasq_res["pid"]
            )
            if status["exited"] == 1:
                if status["exitcode"] != 0:
                    raise ValueError(
                        f"dnsmasq restart failed: "
                        f"stdout={status.get('out-data', '')!r}, "
                        f"stderr={status.get('err-data', '')!r}"
                    )
                return True
            return False

        await wait_for_dnsmasq_restart()

        # Seed the nftables allowed_ips set by resolving the allowed domains now,
        # before the first DNS query.  nftset= would do this dynamically, but
        # pre-resolving at provision time is simpler and doesn't require dnsmasq
        # to stay running before traffic is attempted.
        pre_resolve = _pre_resolve_script(allow_domains)
        await agent_commands.write_file(
            vm_id=new_vm_id,
            content=pre_resolve.encode("utf-8"),
            filepath="/tmp/pre_resolve.py",
        )
        resolve_res = await agent_commands.exec_command(
            vm_id=new_vm_id,
            command=["python3", "/tmp/pre_resolve.py"],
        )

        @tenacity.retry(
            wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
            stop=tenacity.stop_after_delay(30),
            retry=tenacity.retry_if_result(lambda x: x is False),
        )
        async def wait_for_pre_resolve() -> bool:
            status = await agent_commands.get_agent_exec_status(
                vm_id=new_vm_id, pid=resolve_res["pid"]
            )
            if status["exited"] == 1:
                if status["exitcode"] != 0:
                    raise ValueError(
                        f"pre-resolve failed: "
                        f"stdout={status.get('out-data', '')!r}, "
                        f"stderr={status.get('err-data', '')!r}"
                    )
                return True
            return False

        await wait_for_pre_resolve()

        return new_vm_id

    async def create_sdn_and_vms(
        self,
        proxmox_ids_start: str,
        sdn_config: SdnConfigType,
        vms_config: Tuple[VmConfig, ...],
    ) -> Tuple[
        Tuple[Tuple[int, VmConfig], ...],
        str | None,
        Tuple[IpamMapping, ...],
        int | None,
    ]:
        allow_domains: Tuple[str, ...] = ()
        if isinstance(sdn_config, SdnConfig):
            allow_domains = sdn_config.allow_domains

        if allow_domains:
            # allow_domains requires an explicit SdnConfig (not "auto").  Supporting
            # allow_domains with auto-generated CIDRs would require a separate
            # allow_domains field on ProxmoxSandboxEnvironmentConfig; left for future work.
            assert isinstance(sdn_config, SdnConfig)
            sdn_config = await self._prepare_sdn_for_gateway(sdn_config)

        vm_configs_with_ids = []
        sdn_zone_id, vnet_aliases = await self.sdn_commands.create_sdn(
            proxmox_ids_start, sdn_config
        )

        gateway_vm_id: int | None = None
        if allow_domains and sdn_zone_id is not None:
            # sdn_config was reassigned to a SdnConfig by _prepare_sdn_for_gateway
            # above; assert helps mypy narrow the type.
            assert isinstance(sdn_config, SdnConfig)
            sandbox_cidr = str(sdn_config.vnet_configs[0].subnets[0].cidr)
            gateway_ip = _gateway_ip_for_subnet(sandbox_cidr)
            gateway_mac = _gateway_mac(proxmox_ids_start)
            with trace_action(self.logger, self.TRACE_NAME, "provision gateway VM"):
                gateway_vm_id = await self._provision_gateway(
                    proxmox_ids_start=proxmox_ids_start,
                    vnet_aliases=vnet_aliases,
                    sdn_zone_id=sdn_zone_id,
                    sandbox_cidr=sandbox_cidr,
                    gateway_ip=gateway_ip,
                    gateway_mac=gateway_mac,
                    allow_domains=allow_domains,
                )

        known_builtins = await self.built_in_vm.known_builtins()

        ipam_mappings: List[IpamMapping] = []

        for vm_config in vms_config:
            # We have to create the IPAM entries before booting the VMs
            # otherwise they will not get the defined static IPs.
            per_vm_ipam_mappings = await self.create_ipam_mappings(
                vnet_aliases, vm_config, sdn_zone_id
            )
            ipam_mappings.extend(per_vm_ipam_mappings)

            with trace_action(self.logger, self.TRACE_NAME, f"create VM {vm_config=}"):
                vm_id = await self.qemu_commands.create_and_start_vm(
                    sdn_vnet_aliases=vnet_aliases,
                    vm_config=vm_config,
                    built_in_vm_ids=known_builtins,
                )
                vm_configs_with_ids.append((vm_id, vm_config))

        # TODO check for failed starts in the log somehow

        for vm_configs_with_id in vm_configs_with_ids:
            await self.qemu_commands.await_vm(
                vm_configs_with_id[0], vm_configs_with_id[1].is_sandbox
            )

        # Inject a permanent ARP entry for the gateway VM on every sandbox VM.
        #
        # Root cause of the ARP conflict: setting gateway=.2 in the SDN subnet
        # config causes Proxmox to assign .2 to the SDN bridge on the Proxmox
        # host (needed so dnsmasq advertises .2 as the DHCP router option).
        # The bridge therefore ALSO responds to ARP for .2, racing with the
        # gateway VM's ens18.  If the bridge wins, sandbox traffic hits the
        # bridge instead of the gateway VM's FORWARD chain — all packets are
        # silently dropped (snat=False, no route to the internet from the bridge
        # without NAT).
        #
        # Fix: inject a `nud permanent` ARP entry pointing .2 → gateway VM MAC
        # on each sandbox VM after it boots.  Permanent entries take unconditional
        # precedence over dynamic ARP replies, eliminating the race entirely.
        if gateway_vm_id is not None:
            assert isinstance(sdn_config, SdnConfig)
            gw_sandbox_cidr = str(sdn_config.vnet_configs[0].subnets[0].cidr)
            gw_ip = _gateway_ip_for_subnet(gw_sandbox_cidr)
            gw_mac = _gateway_mac(proxmox_ids_start)
            agent_commands = AgentCommands(self.async_proxmox, self.node)
            for vm_id, vm_config in vm_configs_with_ids:
                if not vm_config.is_sandbox:
                    continue
                inject_res = await agent_commands.exec_command(
                    vm_id=vm_id,
                    command=[
                        "bash",
                        "-c",
                        f"iface=$(ip -4 route"
                        f" | awk '$1==\"{gw_sandbox_cidr}\" {{print $3}}');"
                        f" ip neigh replace {gw_ip} lladdr {gw_mac}"
                        f" dev $iface nud permanent",
                    ],
                )

                @tenacity.retry(
                    wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
                    stop=tenacity.stop_after_delay(30),
                    retry=tenacity.retry_if_result(lambda x: x is False),
                )
                async def wait_for_arp_inject() -> bool:
                    status = await agent_commands.get_agent_exec_status(
                        vm_id=vm_id, pid=inject_res["pid"]
                    )
                    if status["exited"] == 1:
                        if status["exitcode"] != 0:
                            raise ValueError(
                                f"ARP injection on sandbox VM {vm_id} failed: "
                                f"stdout={status.get('out-data', '')!r}, "
                                f"stderr={status.get('err-data', '')!r}"
                            )
                        self.logger.info(
                            f"Injected permanent ARP {gw_ip} → {gw_mac} "
                            f"on sandbox VM {vm_id}"
                        )
                        return True
                    return False

                await wait_for_arp_inject()

        return (
            tuple(vm_configs_with_ids),
            sdn_zone_id,
            tuple(ipam_mappings),
            gateway_vm_id,
        )

    async def delete_sdn_and_vms(
        self,
        sdn_zone_id: str | None,
        ipam_mappings: Sequence[IpamMapping],
        vm_ids: Tuple[int, ...],
    ):
        for vm_id in vm_ids:
            await self.qemu_commands.destroy_vm(vm_id=vm_id)
        if sdn_zone_id is not None:
            await self.sdn_commands.tear_down_sdn_zone_and_vnet(
                sdn_zone_id=sdn_zone_id, ipam_mappings=ipam_mappings
            )

    async def create_ipam_mappings(
        self,
        sdn_vnet_aliases: VnetAliases,
        vm_config: VmConfig,
        sdn_zone_id: str | None,
    ) -> List[IpamMapping]:
        # `sdn_zone_id` _might_ be None, see my comment in `sdn_commands` about this.
        # As such, the static-ip IPAM allocation is incompatible with the predefined
        # VNET functionality, unless we add logic to grab the zone id the alias belongs
        # to here.
        if not sdn_zone_id:
            if vm_config.nics and any(nic.ipv4 for nic in vm_config.nics):
                raise ValueError(
                    "Static IP configuration requires SDN configuration to be present."
                )

        if not (vm_config.nics and sdn_zone_id):
            return []

        alias_mapping = self.qemu_commands._convert_sdn_vnet_aliases(sdn_vnet_aliases)

        ipam_mappings: List[IpamMapping] = []

        for nic in vm_config.nics:
            if not (nic.mac and nic.ipv4):
                continue

            if nic.vnet_alias in alias_mapping:
                vnet_id = alias_mapping[nic.vnet_alias]
            else:
                raise ValueError(
                    f"VNET alias '{nic.vnet_alias}' not found. "
                    f"Available: {list(alias_mapping.keys())}"
                )

            # Note we don't need a `do_update_all_sdn` call after these.
            ipam_mapping = IpamMapping(
                vnet_id=vnet_id, zone_id=sdn_zone_id, mac=nic.mac, ipv4=nic.ipv4
            )
            await self.sdn_commands.create_ipam_mapping(ipam_mapping)
            ipam_mappings.append(ipam_mapping)
        return ipam_mappings

    async def find_proxmox_ids_start(self, task_name_start: str) -> str:
        existing_zone_ids = set(
            [zone["zone"] for zone in await self.sdn_commands.list_sdn_zones()]
        )
        zone_free = False
        while not zone_free:
            # IDs are 8 characters max unfortunately; we save two at the end to
            # distinguish vnet/SDN objects
            proxmox_ids_start = f"{task_name_start}{randint(0, 999):03d}"
            zone_free = f"{proxmox_ids_start}z" not in existing_zone_ids
        return proxmox_ids_start

    async def find_all_zones(self, vnet_ids: Collection[str]) -> Set[str]:
        return set(
            [
                vnet["zone"]
                for vnet in await self.sdn_commands.read_all_vnets()
                if vnet["vnet"] in vnet_ids
            ]
        )

    async def task_cleanup(self) -> None:
        self.logger.debug("infra_commands cleanup activated")
        await self.qemu_commands.task_cleanup()
        await self.sdn_commands.task_cleanup()

    async def cleanup_no_id(self) -> None:
        noticed_vnets = set()
        noticed_vms = list()

        for vm in await self.qemu_commands.list_vms():
            if (
                "tags" in vm
                and "inspect" in vm["tags"].split(";")
                and (
                    ("template" in vm and vm["template"] == 0) or ("template" not in vm)
                )
            ):
                existing_vm = await self.qemu_commands.read_vm(vm["vmid"])
                for key in existing_vm.keys():
                    if key.startswith("net"):
                        # 'virtio=BC:24:11:3E:C3:BA,bridge=tcc919v0'
                        bridge = existing_vm[key].split(",")[1].split("=")[1]
                        noticed_vnets.add(bridge)
                noticed_vms.append(vm)

        zones_to_delete = await self.find_all_zones(noticed_vnets)

        # We probably already have all the SDN zones already.
        # But in case there were no VMs in a particular SDN zone
        # (which can happen if the task setup failed)
        # we need to check for orphans.
        for zone in await self.sdn_commands.list_sdn_zones():
            if re.match(ZONE_REGEX, zone["zone"]):
                zones_to_delete.add(zone["zone"])

        noticed_ipam_mappings = [
            mapping.to_ipam_mapping()
            for mapping in await self.sdn_commands.read_all_ipam_mappings()
            if mapping.zone in zones_to_delete
            and mapping.gateway is None
            and mapping.mac is not None
        ]

        if not noticed_vms and not zones_to_delete:
            self.logger.info(
                f"No resources to delete on {self.async_proxmox.base_url}."
            )
            return

        self.logger.info(
            "The following VMs and SDNs will be destroyed on "
            + f"{self.async_proxmox.base_url}:"
        )
        vms_table = Table(
            box=box.SQUARE,
            show_lines=False,
            title_style="bold",
            title_justify="left",
        )
        vms_table.add_column("VM ID")
        vms_table.add_column("VM Name")
        for vm in noticed_vms:
            vms_table.add_row(str(vm["vmid"]), vm["name"])
        if not noticed_vms:
            vms_table.add_row("(none)", "(none)")
        print(vms_table)

        zones_table = Table(
            box=box.SQUARE,
            show_lines=False,
            title_style="bold",
            title_justify="left",
        )
        zones_table.add_column("Zone ID")
        for zone in zones_to_delete:
            zones_table.add_row(zone)
        if not zones_to_delete:
            zones_table.add_row("(none)")
        print(zones_table)

        # check if a user is actually there
        is_interactive_shell = sys.stdin.isatty()
        is_ci = "CI" in os.environ
        is_pytest = "PYTEST_CURRENT_TEST" in os.environ

        self.logger.debug(f"{is_interactive_shell=}, {is_ci=}, {is_pytest=}")

        if is_interactive_shell and not is_ci and not is_pytest:
            if not Confirm.ask(
                "Are you sure you want to delete ALL the above resources?",
            ):
                print("Cancelled.")
                return

        for vm in noticed_vms:
            await self.qemu_commands.destroy_vm(vm["vmid"])
        await self.sdn_commands.tear_down_sdn_zones_and_vnets(
            zones_to_delete, noticed_ipam_mappings
        )
