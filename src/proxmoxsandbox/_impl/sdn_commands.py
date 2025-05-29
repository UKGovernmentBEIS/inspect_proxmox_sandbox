import abc
import re
from contextvars import ContextVar
from ipaddress import ip_address, ip_network
from logging import getLogger
from random import shuffle
from typing import Collection, List, Optional, Set, Tuple, TypeAlias

from inspect_ai.util import trace_action

from proxmoxsandbox._impl.async_proxmox import (
    AsyncProxmoxAPI,
    ProxmoxJsonDataType,
)
from proxmoxsandbox._impl.task_wrapper import TaskWrapper
from proxmoxsandbox.schema import (
    DhcpRange,
    SdnConfig,
    SdnConfigType,
    SubnetConfig,
    VnetConfig,
)

# a List tuples of [vnet ID, vnet alias], for a particular sdn_zone_id.
# The alias may be None for a given ID.
VnetAliases: TypeAlias = List[Tuple[str, str | None]]

ZONE_REGEX = "...[0-9]{3}z"

# A static SDN used for creating built-in VMs. It is created on demand
# and not torn down afterwards.
STATIC_SDN_START = "inspvm"


class SdnCommands(abc.ABC):
    logger = getLogger(__name__)

    TRACE_NAME = "proxmox_sdn_command"

    async_proxmox: AsyncProxmoxAPI
    task_wrapper: TaskWrapper

    _created_sdns: ContextVar[Set[str]] = ContextVar(
        "proxmox_created_sdns", default=set()
    )
    _cleanup_completed: ContextVar[bool] = ContextVar(
        "proxmox_sdns_cleanup_executed", default=False
    )

    def __init__(self, async_proxmox: AsyncProxmoxAPI):
        self.async_proxmox = async_proxmox
        self.task_wrapper = TaskWrapper(async_proxmox)

    def find_existing_cidr_overlaps(
        self, list1: List[str], list2: List[str]
    ) -> List[Tuple[str, str]]:
        overlaps = []
        networks1 = [ip_network(cidr) for cidr in list1]
        networks2 = [ip_network(cidr) for cidr in list2]

        for i, net1 in enumerate(networks1):
            for j, net2 in enumerate(networks2):
                if net1.overlaps(net2):
                    overlaps.append((list1[i], list2[j]))

        return overlaps

    def find_self_cidr_overlaps(self, list1: List[str]) -> List[Tuple[str, str]]:
        overlaps = []
        networks1 = [ip_network(cidr) for cidr in list1]
        networks2 = [ip_network(cidr) for cidr in list1]

        for i, net1 in enumerate(networks1):
            for j, net2 in enumerate(networks2):
                if net1.overlaps(net2) and i != j:
                    overlaps.append((list1[i], list1[j]))

        return overlaps

    async def check_cidrs(self, vnet_configs: List[VnetConfig]) -> None:
        existing_cidrs = await self.read_all_simple_zone_cidrs()

        new_cidrs = []
        for vnet_config in vnet_configs:
            for subnet in vnet_config.subnets:
                new_cidrs.append(str(subnet.cidr))

        # See https://forum.proxmox.com/threads/sdn-simple-zones-and-overlapping-ip-ranges.162739/
        if overlaps := self.find_existing_cidr_overlaps(
            existing_cidrs, new_cidrs
        ) + self.find_self_cidr_overlaps(new_cidrs):
            raise ValueError(f"Duplicate IP ranges found: {overlaps}")

    def simple_vnet_config(
        self, third_octet: int = 16, alias: Optional[str] = None
    ) -> VnetConfig:
        return VnetConfig(
            subnets=(
                SubnetConfig(
                    cidr=ip_network(f"192.168.{third_octet}.0/24"),
                    gateway=ip_address(f"192.168.{third_octet}.1"),
                    snat=True,
                    dhcp_ranges=(
                        DhcpRange(
                            start=ip_address(f"192.168.{third_octet}.50"),
                            end=ip_address(f"192.168.{third_octet}.100"),
                        ),
                    ),
                ),
            ),
            alias=alias,
        )

    async def generate_sdn_config(
        self, aliases: Tuple[Optional[str], ...] = ()
    ) -> SdnConfig:
        if len(aliases) == 0:
            aliases = (None,)

        vnet_configs: List[VnetConfig] = []
        for alias in aliases:
            try_third_octets = list(range(2, 253))
            # Deliberately randomize the IP address range you get if you don't specify
            # one. This is to avoid brittle evals.
            shuffle(try_third_octets)
            ok_vnet_config = None
            for third_octet in try_third_octets:
                try_vnet_config = self.simple_vnet_config(
                    third_octet=third_octet, alias=alias
                )
                try:
                    await self.check_cidrs(vnet_configs=[try_vnet_config])
                    ok_vnet_config = try_vnet_config
                    vnet_configs.append(ok_vnet_config)
                    break
                except ValueError:
                    continue
            if ok_vnet_config is None:
                raise ValueError("Could not find a suitable IP range for the SDN")
            # There is obviously a race condition here. Another eval could sneak in and
            # create a clashing IP range.
            # We could use a 10.*/24 range instead, which would give us many more ranges
            # and reduce the chance of a collision.

        return SdnConfig(vnet_configs=tuple(vnet_configs))

    def validate_ipam_dhcp_dnsnmasq(self, sdn_config: SdnConfig) -> None:
        if sdn_config.use_pve_ipam_dnsnmasq:
            found_dhcp_range = False
            for vnet_config in sdn_config.vnet_configs:
                for subnet in vnet_config.subnets:
                    if len(subnet.dhcp_ranges) > 0:
                        found_dhcp_range = True
            if not found_dhcp_range:
                raise ValueError(
                    "DHCP ranges should be provided when "
                    + f"use_pve_ipam_dnsnmasq={sdn_config.use_pve_ipam_dnsnmasq}"
                )
        if not sdn_config.use_pve_ipam_dnsnmasq:
            for vnet_config in sdn_config.vnet_configs:
                for subnet in vnet_config.subnets:
                    if len(subnet.dhcp_ranges) > 0:
                        raise ValueError(
                            "DHCP ranges cannot be provided when use_pve_ipam_dnsnmasq="
                            + f"{sdn_config.use_pve_ipam_dnsnmasq}"
                        )

    async def create_sdn(
        self, proxmox_ids_start: str, sdn_config: SdnConfigType
    ) -> Tuple[Optional[str], VnetAliases]:
        if sdn_config is None:
            # When sdn_config is None, we should still fetch existing VNETs
            # so we can use them for VM network interfaces
            all_vnets = await self.read_all_vnets()
            vnet_aliases = []
            for vnet in all_vnets:
                if "vnet" in vnet and "alias" in vnet:
                    vnet_aliases.append((vnet["vnet"], vnet["alias"]))
            return None, vnet_aliases

        resolved_sdn_config: SdnConfig = (
            await self.generate_sdn_config() if sdn_config == "auto" else sdn_config
        )

        await self.check_cidrs(list(resolved_sdn_config.vnet_configs))
        if len(resolved_sdn_config.vnet_configs) > 10:
            raise ValueError(
                f"Too many vnets; max 10, got {len(resolved_sdn_config.vnet_configs)}"
            )

        if len(resolved_sdn_config.vnet_configs) == 0:
            raise ValueError("No vnets provided")

        self.validate_ipam_dhcp_dnsnmasq(resolved_sdn_config)

        sdn_zone_id = f"{proxmox_ids_start}z"

        # sanity check so that we don't get into trouble later
        # in inspect sandbox cleanup
        if not (
            re.match(ZONE_REGEX, sdn_zone_id)
            or sdn_zone_id.startswith(STATIC_SDN_START)
        ):
            raise ValueError("Invalid zone ID")

        with trace_action(self.logger, self.TRACE_NAME, f"create sdn  {sdn_zone_id=}"):
            zone_create_json: ProxmoxJsonDataType = {
                "type": "simple",
                "zone": sdn_zone_id,
            }
            if resolved_sdn_config.use_pve_ipam_dnsnmasq:
                zone_create_json["ipam"] = "pve"
                zone_create_json["dhcp"] = "dnsmasq"

            await self.async_proxmox.request(
                "POST",
                "/cluster/sdn/zones",
                json=zone_create_json,
            )

            vnet_aliases: VnetAliases = []

            for idx, vnet_config in enumerate(resolved_sdn_config.vnet_configs):
                vnet_id = f"{proxmox_ids_start}v{idx}"

                vnet_json: ProxmoxJsonDataType = {"vnet": vnet_id, "zone": sdn_zone_id}
                if vnet_config.alias is not None:
                    vnet_json["alias"] = vnet_config.alias
                vnet_aliases.append((vnet_id, vnet_config.alias))
                await self.async_proxmox.request(
                    "POST",
                    "/cluster/sdn/vnets",
                    json=vnet_json,
                )

                for subnet in vnet_config.subnets:
                    await self.async_proxmox.request(
                        "POST",
                        f"/cluster/sdn/vnets/{vnet_id}/subnets",
                        json={
                            "subnet": str(subnet.cidr),
                            "type": "subnet",
                            "vnet": vnet_id,
                            "gateway": str(subnet.gateway),
                            "snat": subnet.snat,
                            "dhcp-range": list(
                                dhcp_range._to_proxmox_format()
                                for dhcp_range in subnet.dhcp_ranges
                            ),
                        },
                    )

            # TODO firewall to block access to proxmox?

        await self.do_update_all_sdn()

        return sdn_zone_id, vnet_aliases

    async def do_update_all_sdn(self) -> None:
        async def update_all_sdn() -> None:
            await self.async_proxmox.request("PUT", "/cluster/sdn")

        with trace_action(self.logger, self.TRACE_NAME, "update all SDN"):
            await self.task_wrapper.do_action_and_wait_for_tasks(update_all_sdn)

    async def list_sdn_zones(self):
        with trace_action(self.logger, self.TRACE_NAME, "get SDN zones"):
            return await self.async_proxmox.request("GET", "/cluster/sdn/zones")

    async def read_all_simple_zone_cidrs(self) -> List[str]:
        existing_zones = await self.list_sdn_zones()
        simple_zone_names = list(
            zone["zone"] for zone in existing_zones if zone["type"] == "simple"
        )
        all_vnets = await self.async_proxmox.request("GET", "/cluster/sdn/vnets")
        relevant_vnets = list(
            vnet for vnet in all_vnets if vnet["zone"] in simple_zone_names
        )
        relevant_subnet_cidrs = []
        for relevant_vnet in relevant_vnets:
            vnet = relevant_vnet["vnet"]
            vnet_subnets = await self.async_proxmox.request(
                "GET", f"/cluster/sdn/vnets/{vnet}/subnets"
            )
            cidrs = list(subnet["cidr"] for subnet in vnet_subnets)
            relevant_subnet_cidrs += cidrs
        return relevant_subnet_cidrs

    async def tear_down_sdn_zone_and_vnet(self, sdn_zone_id: str) -> None:
        await self.tear_down_sdn_zones_and_vnets([sdn_zone_id])

    async def tear_down_sdn_zones_and_vnets(
        self, sdn_zone_ids: Collection[str]
    ) -> None:
        with trace_action(self.logger, self.TRACE_NAME, f"delete SDNs {sdn_zone_ids}"):
            for sdn_zone_id in sdn_zone_ids:
                all_vnets = await self.read_all_vnets()
                relevant_vnets = list(
                    vnet for vnet in all_vnets if vnet["zone"] == sdn_zone_id
                )
                for vnet_details in relevant_vnets:
                    vnet = vnet_details["vnet"]
                    subnets = await self.async_proxmox.request(
                        "GET", f"/cluster/sdn/vnets/{vnet}/subnets"
                    )
                    for subnet_details in subnets:
                        subnet_id = subnet_details["id"]
                        await self.async_proxmox.request(
                            "DELETE",
                            f"/cluster/sdn/vnets/{vnet}/subnets/{subnet_id}",
                        )
                    await self.async_proxmox.request(
                        "DELETE", f"/cluster/sdn/vnets/{vnet}"
                    )
                await self.async_proxmox.request(
                    "DELETE", f"/cluster/sdn/zones/{sdn_zone_id}"
                )

        await self.do_update_all_sdn()

    async def read_all_vnets(self):
        return await self.async_proxmox.request("GET", "/cluster/sdn/vnets")

    async def cleanup(self) -> None:
        if self._cleanup_completed.get():
            return

        with trace_action(self.logger, self.TRACE_NAME, "cleanup all SDNs"):
            await self.tear_down_sdn_zones_and_vnets(self._created_sdns.get())
            self._cleanup_completed.set(True)
