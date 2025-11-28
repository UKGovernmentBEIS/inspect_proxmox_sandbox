import abc
import os
import re
import sys
from logging import getLogger
from random import randint
from typing import Collection, Set, Tuple

from inspect_ai.util import trace_action
from rich import box, print
from rich.prompt import Confirm
from rich.table import Table

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.built_in_vm import BuiltInVM
from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox._impl.sdn_commands import ZONE_REGEX, SdnCommands
from proxmoxsandbox._impl.task_wrapper import TaskWrapper
from proxmoxsandbox.schema import (
    SdnConfigType,
    VmConfig,
)


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

    async def create_sdn_and_vms(
        self,
        proxmox_ids_start: str,
        sdn_config: SdnConfigType,
        vms_config: Tuple[VmConfig, ...],
    ):
        vm_configs_with_ids = []
        sdn_zone_id, vnet_aliases = await self.sdn_commands.create_sdn(
            proxmox_ids_start, sdn_config
        )

        known_builtins = await self.built_in_vm.known_builtins()

        for vm_config in vms_config:
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

        # TODO types here
        return vm_configs_with_ids, sdn_zone_id

    async def delete_sdn_and_vms(
        self, sdn_zone_id: str | None, vm_ids: Tuple[int, ...]
    ):
        for vm_id in vm_ids:
            await self.qemu_commands.destroy_vm(vm_id=vm_id)
        if sdn_zone_id is not None:
            await self.sdn_commands.tear_down_sdn_zone_and_vnet(sdn_zone_id=sdn_zone_id)

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

    async def cleanup(self) -> None:
        await self.qemu_commands.cleanup()
        await self.sdn_commands.cleanup()

    async def cleanup_no_id(self, delete_all: bool = False) -> None:
        noticed_vnets = set()
        noticed_vms = list()

        # First pass: determine which zones to delete
        if delete_all:
            # When deleting all, get all zones first
            zones_to_delete = set()
            for zone in await self.sdn_commands.list_sdn_zones():
                zones_to_delete.add(zone["zone"])

            # Get all vnets in those zones
            all_vnets = await self.sdn_commands.read_all_vnets()
            for vnet in all_vnets:
                if vnet["zone"] in zones_to_delete:
                    noticed_vnets.add(vnet["vnet"])
        else:
            # Normal mode: only look at inspect-tagged VMs
            for vm in await self.qemu_commands.list_vms():
                should_delete = (
                    "tags" in vm
                    and "inspect" in vm["tags"].split(";")
                    and (
                        ("template" in vm and vm["template"] == 0) or ("template" not in vm)
                    )
                )

                if should_delete:
                    existing_vm = await self.qemu_commands.read_vm(vm["vmid"])
                    for key in existing_vm.keys():
                        if key.startswith("net"):
                            # 'virtio=BC:24:11:3E:C3:BA,bridge=tcc919v0'
                            bridge = existing_vm[key].split(",")[1].split("=")[1]
                            noticed_vnets.add(bridge)
                    noticed_vms.append(vm)

            zones_to_delete = await self.find_all_zones(noticed_vnets)

            # Check for orphaned zones matching the pattern
            for zone in await self.sdn_commands.list_sdn_zones():
                if re.match(ZONE_REGEX, zone["zone"]):
                    zones_to_delete.add(zone["zone"])

        # Second pass: find all VMs using the networks we're about to delete
        for vm in await self.qemu_commands.list_vms():
            # Skip if already marked for deletion
            if any(existing_vm["vmid"] == vm["vmid"] for existing_vm in noticed_vms):
                continue

            if delete_all:
                # Delete all VMs (including templates)
                existing_vm = await self.qemu_commands.read_vm(vm["vmid"])
                vm_uses_target_vnet = False
                for key in existing_vm.keys():
                    if key.startswith("net"):
                        bridge = existing_vm[key].split(",")[1].split("=")[1]
                        if bridge in noticed_vnets:
                            vm_uses_target_vnet = True
                            break

                # Delete VM if it uses a network we're deleting OR if it has inspect tag
                # (This ensures we clean up everything)
                if vm_uses_target_vnet or ("tags" in vm and "inspect" in vm["tags"].split(";")):
                    noticed_vms.append(vm)
            # Normal mode VMs already collected above

        if not noticed_vms and not zones_to_delete:
            print(f"No resources to delete on {self.async_proxmox.base_url}.")
            return

        print(
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
        await self.sdn_commands.tear_down_sdn_zones_and_vnets(zones_to_delete)
