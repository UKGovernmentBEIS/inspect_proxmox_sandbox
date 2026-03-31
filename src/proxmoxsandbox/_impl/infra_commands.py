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


def _gateway_mac(proxmox_ids_start: str, vnet_index: int = 0) -> str:
    """Derive a unique, deterministic MAC from per-eval prefix and vnet index.

    Uses the QEMU/KVM OUI (52:54:00) so Proxmox recognises it as a valid
    virtual NIC.  The 3-byte suffix is an MD5 hash of the prefix + vnet_index,
    giving per-eval and per-NIC uniqueness without shared state.

    Multi-vnet callers pass a different vnet_index for each sandbox NIC so
    each gets a distinct MAC.  Single-vnet callers use the default (0).
    """
    key = f"{proxmox_ids_start}:{vnet_index}"
    digest = hashlib.md5(key.encode(), usedforsecurity=False).digest()
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


def _nftables_config(sandbox_cidrs: Tuple[str, ...]) -> str:
    """Generate nftables ruleset for the gateway VM.

    Uses a named set ``sandbox_nets`` containing all sandbox CIDRs, referenced
    via ``ip saddr @sandbox_nets`` in all chains.  Scales cleanly to N vnets
    without duplicating rules per CIDR.

    Three chains:
    - prerouting_nat: intercepts all DNS from sandbox nets and redirects it to
      the gateway's dnsmasq (via ``redirect to :53``), so sandbox VMs cannot
      bypass the allowlist by configuring an alternative DNS server.
    - postrouting_nat: masquerades forwarded sandbox traffic as the gateway's
      external IP, so return traffic is routed back correctly.
    - forward: default-drop; only allows traffic to IPs in the allowed_ips set,
      which is seeded at provision time by _pre_resolve_script.

    IPv6:
    table inet covers both IPv4 and IPv6; policy drop on the forward chain
    therefore drops IPv6 forwarded through the gateway.

    INPUT chain:
    Only allows port 53 (TCP/UDP) from sandbox CIDRs and loopback.
    Defence-in-depth: limits gateway attack surface even if dnsmasq
    has a vulnerability (CVE on a listening port).
    """
    elements = ", ".join(sandbox_cidrs)
    return f"""\
#!/usr/sbin/nft -f
flush ruleset
table inet gateway {{
    set sandbox_nets {{
        type ipv4_addr
        flags interval
        elements = {{ {elements} }}
    }}

    set allowed_ips {{
        type ipv4_addr
        flags interval, timeout
        timeout 1h  # stale IPs expire; established connections survive via ct state
    }}

    chain input {{
        type filter hook input priority filter; policy drop;
        ct state established,related accept
        iif lo accept
        ip saddr @sandbox_nets udp dport 53 accept
        ip saddr @sandbox_nets tcp dport 53 accept
        counter drop
    }}

    chain prerouting_nat {{
        type nat hook prerouting priority dstnat;
        ip saddr @sandbox_nets udp dport 53 redirect to :53
        ip saddr @sandbox_nets tcp dport 53 redirect to :53
    }}

    chain postrouting_nat {{
        type nat hook postrouting priority srcnat;
        ip saddr @sandbox_nets masquerade
    }}

    chain forward {{
        type filter hook forward priority filter; policy drop;
        ct state established,related counter accept
        ip saddr @sandbox_nets tcp dport 853 drop
        ip saddr @sandbox_nets udp dport 853 drop
        ip saddr @sandbox_nets tcp daddr @allowed_ips counter accept
        ip saddr @sandbox_nets udp daddr @allowed_ips counter accept
        counter drop
    }}
}}
"""


def _dnsmasq_allowlist_config(
    gateway_ips: Tuple[str, ...],
    allow_domains: Tuple[str, ...],
) -> str:
    """Generate the per-eval dnsmasq allowlist config for the gateway VM.

    Each allowed domain gets:
    - server=/<domain>/8.8.8.8 — forward queries upstream
    - nftset=/<domain>/4#inet#gateway#allowed_ips — on every
      DNS response, push resolved IPs directly into the nftables
      allowed_ips set.  This is the cooperative design: dnsmasq
      is both the resolver and the source of truth for the
      firewall's allow-set, so there is zero resolver-desync.

    dnsmasq's /domain/ syntax matches the apex and all subdomains,
    so "gnu.org" covers ftp.gnu.org too — both for DNS forwarding
    and for nftset IP injection.

    Without a global server= directive, any unlisted domain gets
    SERVFAIL.

    listen-address is set to each gateway sandbox-facing IP (one
    per vnet). bind-interfaces is required alongside
    listen-address: without it, dnsmasq binds to 0.0.0.0:53
    regardless of listen-address.
    """
    lines = [
        "# Per-eval allowlist — generated at provision time.",
        "bind-interfaces",
    ]
    for gw_ip in gateway_ips:
        lines.append(f"listen-address={gw_ip}")
    lines.append("")
    for domain in allow_domains:
        lines.append(f"server=/{domain}/8.8.8.8")
        lines.append(
            f"nftset=/{domain}"
            f"/4#inet#gateway#allowed_ips"
        )
    return "\n".join(lines) + "\n"


def _pre_resolve_script(allow_domains: Tuple[str, ...]) -> str:
    """Return a Python script that pre-seeds the nftables allowed_ips set.

    Belt-and-suspenders: dnsmasq's nftset= directive dynamically
    adds IPs on every DNS response, but this script seeds the set
    at provision time so the very first TCP SYN (before any DNS
    query) can reach allowed IPs.  After boot, nftset= is the
    primary mechanism and this seed is redundant.

    Queries 8.8.8.8 directly (via python3-dnspython) to match
    dnsmasq's upstream.  Falls back to the system resolver if
    python3-dnspython is not installed (old gateway template).

    Filters out RFC1918/loopback/link-local IPs to prevent DNS
    rebinding attacks from seeding internal addresses.
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

    async def _prepare_sdn_for_gateway(
        self, sdn_config: SdnConfig
    ) -> SdnConfig:
        """Transform a SdnConfig to route sandbox egress through a gateway VM.

        For each user-supplied vnet:
        - gateway IP changed to network .2 (the gateway VM's sandbox-facing IP)
        - snat set to False (gateway VM does NAT, not Proxmox)
        - alias set to "sandbox-{i}" if unset

        Adds an external vnet (snat=True) as the last element for the gateway
        VM's internet-facing interface.

        Convention: sandbox vnets = vnet_configs[:-1],
                    external vnet = vnet_configs[-1].
        """
        if not sdn_config.use_pve_ipam_dnsnmasq:
            raise ValueError(
                "allow_domains requires use_pve_ipam_dnsnmasq=True. "
                "Proxmox IPAM is still used to reserve sandbox VM IPs "
                "and assign the gateway VM its address via DHCP. The "
                "gateway VM runs its own separate dnsmasq instance for "
                "DNS filtering only — it does not replace Proxmox's "
                "dnsmasq for DHCP."
            )
        if len(sdn_config.vnet_configs) < 1:
            raise ValueError(
                "allow_domains requires at least one vnet_config, "
                f"got {len(sdn_config.vnet_configs)}"
            )

        modified_sandbox_vnets: list[VnetConfig] = []
        sandbox_networks = []
        for i, sandbox_vnet in enumerate(sdn_config.vnet_configs):
            if len(sandbox_vnet.subnets) != 1:
                raise ValueError(
                    "allow_domains requires exactly one subnet "
                    f"per vnet, got {len(sandbox_vnet.subnets)} "
                    f"in vnet {i}"
                )

            sandbox_subnet = sandbox_vnet.subnets[0]
            sandbox_network = ip_network(str(sandbox_subnet.cidr))
            sandbox_networks.append(sandbox_network)
            gateway_ip = ip_address(
                sandbox_network.network_address + 2
            )

            modified_subnet = SubnetConfig(
                cidr=sandbox_subnet.cidr,
                gateway=gateway_ip,
                snat=False,
                dhcp_ranges=sandbox_subnet.dhcp_ranges,
            )
            if len(sdn_config.vnet_configs) == 1:
                default_alias = "sandbox"
            else:
                default_alias = f"sandbox-{i}"
            alias = (
                sandbox_vnet.alias
                if sandbox_vnet.alias is not None
                else default_alias
            )
            modified_sandbox_vnets.append(
                VnetConfig(
                    alias=alias,
                    subnets=(modified_subnet,),
                )
            )

        external_vnet = (
            await self.sdn_commands.find_non_overlapping_vnet_config(
                exclude_networks=sandbox_networks,
            )
        )

        # allow_domains is intentionally cleared: this returned
        # config is the internal N+1-vnet form with the gateway
        # already embedded.  Carrying it would re-trigger the
        # schema validator.
        return SdnConfig(
            vnet_configs=(*modified_sandbox_vnets, external_vnet),
            use_pve_ipam_dnsnmasq=sdn_config.use_pve_ipam_dnsnmasq,
            allow_domains=(),
        )

    async def _provision_gateway(
        self,
        proxmox_ids_start: str,
        vnet_aliases: VnetAliases,
        sdn_zone_id: str,
        sandbox_cidrs: Tuple[str, ...],
        gateway_ips: Tuple[str, ...],
        gateway_macs: Tuple[str, ...],
        allow_domains: Tuple[str, ...],
    ) -> int:
        """Clone the gateway template and configure it for this eval.

        Supports N sandbox vnets.  The gateway VM gets N+1 NICs:
        net0..net{N-1} for sandbox vnets (each with a deterministic
        MAC), net{N} for the external internet-facing vnet.
        """
        num_sandbox = len(sandbox_cidrs)
        external_vnet_id = vnet_aliases[num_sandbox][0]

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
            # Wire N sandbox NICs + 1 external NIC.
            # net0..net{N-1} = sandbox vnets (each with its MAC),
            # net{N} = external internet-facing vnet.
            # Tags: "inspect" only — "gateway" is reserved for the
            # template.
            nic_json: dict[str, str] = {}
            for i in range(num_sandbox):
                sb_vnet_id = vnet_aliases[i][0]
                nic_json[f"net{i}"] = (
                    f"virtio,bridge={sb_vnet_id}"
                    f",macaddr={gateway_macs[i].upper()}"
                )
            nic_json[f"net{num_sandbox}"] = (
                f"virtio,bridge={external_vnet_id}"
            )
            nic_json["tags"] = "inspect"
            await self.async_proxmox.request(
                "POST",
                f"/nodes/{self.node}/qemu/{new_vm_id}/config",
                json=nic_json,
            )

        await self.task_wrapper.do_action_and_wait_for_tasks(configure_gw)

        await self.qemu_commands.start_and_await(
            vm_id=new_vm_id, is_sandbox=True
        )

        agent_commands = AgentCommands(self.async_proxmox, self.node)

        # Configure a static IP on each sandbox-facing NIC via
        # systemd-networkd.  One .network file per NIC, matched by
        # MAC (00- prefix beats cloud-init's 10-cloud-init-*.network).
        for i in range(num_sandbox):
            prefix_len = ip_network(sandbox_cidrs[i]).prefixlen
            network_file = f"""\
[Match]
MACAddress={gateway_macs[i]}

[Network]
DHCP=no
Address={gateway_ips[i]}/{prefix_len}
"""
            await agent_commands.write_file(
                vm_id=new_vm_id,
                content=network_file.encode("utf-8"),
                filepath=(
                    f"/etc/systemd/network/00-sandbox-{i}.network"
                ),
            )

        # Build a grep check that waits for ALL gateway IPs to appear.
        ip_checks = " && ".join(
            f"ip -4 addr show | grep -qF '{gip}'"
            for gip in gateway_ips
        )
        set_ip_res = await agent_commands.exec_command(
            vm_id=new_vm_id,
            command=[
                "bash",
                "-c",
                f"networkctl reload && "
                f"for i in $(seq 1 15); do "
                f"  {ip_checks}"
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
                        "Failed to configure static IPs: "
                        f"stdout={status.get('out-data', '')!r}"
                        f", stderr="
                        f"{status.get('err-data', '')!r}"
                    )
                return True
            return False

        await wait_for_set_ip()

        # Inject and apply nftables config
        nft_config = _nftables_config(sandbox_cidrs)
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
                        "nft -f failed: "
                        f"stdout={status.get('out-data', '')!r}"
                        f", stderr="
                        f"{status.get('err-data', '')!r}"
                    )
                return True
            return False

        await wait_for_nft()

        # Inject dnsmasq allowlist and restart dnsmasq
        dnsmasq_config = _dnsmasq_allowlist_config(
            gateway_ips, allow_domains
        )
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
                        "dnsmasq restart failed: "
                        f"stdout={status.get('out-data', '')!r}"
                        f", stderr="
                        f"{status.get('err-data', '')!r}"
                    )
                return True
            return False

        await wait_for_dnsmasq_restart()

        # Seed the nftables allowed_ips set by resolving the allowed
        # domains now, before the first DNS query.
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
                        "pre-resolve failed: "
                        f"stdout={status.get('out-data', '')!r}"
                        f", stderr="
                        f"{status.get('err-data', '')!r}"
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
            sandbox_vnets = sdn_config.vnet_configs[:-1]
            sandbox_cidrs = tuple(
                str(v.subnets[0].cidr) for v in sandbox_vnets
            )
            gateway_ips_t = tuple(
                _gateway_ip_for_subnet(c) for c in sandbox_cidrs
            )
            gateway_macs_t = tuple(
                _gateway_mac(proxmox_ids_start, i)
                for i in range(len(sandbox_vnets))
            )
            with trace_action(
                self.logger,
                self.TRACE_NAME,
                "provision gateway VM",
            ):
                gateway_vm_id = await self._provision_gateway(
                    proxmox_ids_start=proxmox_ids_start,
                    vnet_aliases=vnet_aliases,
                    sdn_zone_id=sdn_zone_id,
                    sandbox_cidrs=sandbox_cidrs,
                    gateway_ips=gateway_ips_t,
                    gateway_macs=gateway_macs_t,
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
            sandbox_vnets = sdn_config.vnet_configs[:-1]
            arp_triples = [
                (
                    str(v.subnets[0].cidr),
                    _gateway_ip_for_subnet(str(v.subnets[0].cidr)),
                    _gateway_mac(proxmox_ids_start, i),
                )
                for i, v in enumerate(sandbox_vnets)
            ]
            agent_commands = AgentCommands(
                self.async_proxmox, self.node
            )
            for vm_id, vm_config in vm_configs_with_ids:
                if not vm_config.is_sandbox:
                    continue
                for cidr, gw_ip, gw_mac in arp_triples:
                    # The VM may only be connected to one of
                    # the sandbox vnets.  The [ -n "$iface" ]
                    # guard skips vnets this VM isn't on.
                    inject_res = await agent_commands.exec_command(
                        vm_id=vm_id,
                        command=[
                            "bash",
                            "-c",
                            f"iface=$(ip -4 route"
                            f" | awk "
                            f"'$1==\"{cidr}\""
                            f" {{print $3}}');"
                            f" [ -n \"$iface\" ] && "
                            f"ip neigh replace {gw_ip}"
                            f" lladdr {gw_mac}"
                            f" dev $iface nud permanent"
                            f" || true",
                        ],
                    )

                    # Closure-safe: bind vm_id and pid via
                    # default args to avoid late-binding bugs.
                    @tenacity.retry(
                        wait=tenacity.wait_exponential(
                            min=0.1, exp_base=1.3
                        ),
                        stop=tenacity.stop_after_delay(30),
                        retry=tenacity.retry_if_result(
                            lambda x: x is False
                        ),
                    )
                    async def wait_for_arp_inject(
                        _vm_id: int = vm_id,
                        _pid: int = inject_res["pid"],
                        _gw_ip: str = gw_ip,
                        _gw_mac: str = gw_mac,
                    ) -> bool:
                        status = (
                            await agent_commands
                            .get_agent_exec_status(
                                vm_id=_vm_id, pid=_pid
                            )
                        )
                        if status["exited"] == 1:
                            if status["exitcode"] != 0:
                                raise ValueError(
                                    "ARP injection on "
                                    f"sandbox VM {_vm_id}"
                                    " failed: stdout="
                                    f"{status.get('out-data', '')!r}"
                                    ", stderr="
                                    f"{status.get('err-data', '')!r}"
                                )
                            self.logger.info(
                                "Injected permanent ARP"
                                f" {_gw_ip} -> {_gw_mac}"
                                f" on sandbox VM {_vm_id}"
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
