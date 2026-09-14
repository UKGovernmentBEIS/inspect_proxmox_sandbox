import abc
import asyncio
import os
import sys
from logging import getLogger
from random import randint
from typing import (
    Callable,
    ClassVar,
    Collection,
    Dict,
    List,
    NamedTuple,
    Sequence,
    Set,
    Tuple,
)

from inspect_ai.util import trace_action
from rich import box, print
from rich.prompt import Confirm
from rich.table import Table

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.built_in_vm import BuiltInVM
from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox._impl.readiness import ReadinessRunner, VmReadinessState
from proxmoxsandbox._impl.readiness_display import ReadinessDisplay
from proxmoxsandbox._impl.sdn_commands import (
    IpamMapping,
    SdnCommands,
    VnetAliases,
    is_ephemeral_zone,
)
from proxmoxsandbox._impl.storage_commands import LocalStorageCommands
from proxmoxsandbox._impl.task_wrapper import TaskWrapper
from proxmoxsandbox.schema import (
    ReadinessCommand,
    SdnConfigType,
    VmConfig,
    VmConfigs,
)


class ProxmoxTarget(NamedTuple):
    """Identifies a specific Proxmox host+node combination."""

    host: str
    port: int
    node: str


class InfraCommands(abc.ABC):
    """Orchestrates Proxmox infrastructure commands.

    Collaborators (``QemuCommands``, ``SdnCommands``) track their own created
    resources so that ``task_cleanup`` can destroy anything left behind after
    an interrupted eval.
    """

    logger = getLogger(__name__)

    TRACE_NAME = "proxmox_infra_command"

    _instances: ClassVar[Dict[ProxmoxTarget, "InfraCommands"]] = {}

    async_proxmox: AsyncProxmoxAPI
    task_wrapper: TaskWrapper
    sdn_commands: SdnCommands
    qemu_commands: QemuCommands
    built_in_vm: BuiltInVM
    node: str

    def __init__(
        self,
        async_proxmox: AsyncProxmoxAPI,
        node: str,
        task_wrapper: TaskWrapper,
        sdn_commands: SdnCommands,
        qemu_commands: QemuCommands,
        built_in_vm: BuiltInVM,
    ):
        """Prefer InfraCommands.build() unless injecting collaborators for testing."""
        self.async_proxmox = async_proxmox
        self.task_wrapper = task_wrapper
        self.sdn_commands = sdn_commands
        self.qemu_commands = qemu_commands
        self.built_in_vm = built_in_vm
        self.node = node

    @classmethod
    def get_instance(cls, target: ProxmoxTarget) -> "InfraCommands":
        """Retrieve the InfraCommands instance for a Proxmox target.

        Raises:
            LookupError: If no instance has been stored for *target*
                (i.e. ``task_init`` was not called).
        """
        if target not in cls._instances:
            raise LookupError(
                f"No InfraCommands instance for {target}. Was task_init called?"
            )
        return cls._instances[target]

    @classmethod
    def set_instance(cls, target: ProxmoxTarget, instance: "InfraCommands") -> None:
        """Store an InfraCommands instance for a Proxmox target."""
        cls._instances[target] = instance

    def deregister_resources(
        self,
        vm_ids: Tuple[int, ...],
        sdn_zone_id: str | None,
        ipam_mappings: Sequence[IpamMapping],
    ) -> None:
        """Remove successfully cleaned-up resources from tracking."""
        self.qemu_commands.deregister_vms(vm_ids)
        self.sdn_commands.deregister_sdn_resources(sdn_zone_id, ipam_mappings)

    @classmethod
    def build(
        cls, async_proxmox: AsyncProxmoxAPI, node: str, image_storage: str
    ) -> "InfraCommands":
        """Build the full object graph bottom-up."""
        task_wrapper = TaskWrapper(async_proxmox)
        storage_commands = LocalStorageCommands(async_proxmox, node, task_wrapper)
        sdn_commands = SdnCommands(async_proxmox, task_wrapper)
        qemu_commands = QemuCommands(
            async_proxmox, node, image_storage, task_wrapper, storage_commands
        )
        built_in_vm = BuiltInVM(
            async_proxmox,
            node,
            image_storage,
            task_wrapper,
            qemu_commands,
            sdn_commands,
            storage_commands,
        )
        return cls(
            async_proxmox,
            node,
            task_wrapper,
            sdn_commands,
            qemu_commands,
            built_in_vm,
        )

    async def create_sdn_and_vms(
        self,
        proxmox_ids_start: str,
        sdn_config: SdnConfigType,
        vms_config: VmConfigs,
    ) -> Tuple[Tuple[Tuple[int, VmConfig], ...], str | None, Tuple[IpamMapping, ...]]:
        sdn_zone_id, vnet_aliases = await self.sdn_commands.create_sdn(
            proxmox_ids_start, sdn_config
        )
        if sdn_zone_id:
            self.sdn_commands.register_sdn_zone(sdn_zone_id)

        known_builtins = await self.built_in_vm.known_builtins()

        ipam_mappings = []
        ordered_vm_configs = (
            tuple(vms_config.values()) if isinstance(vms_config, dict) else vms_config
        )

        # Create ALL IPAM mappings FIRST, before creating/starting any VMs.
        # This prevents race conditions where a booting VM's DHCP request
        # causes Proxmox to auto-allocate IPs that we wanted to reserve.
        for vm_config in ordered_vm_configs:
            per_vm_ipam_mappings = await self.create_ipam_mappings(
                vnet_aliases, vm_config, sdn_zone_id
            )
            ipam_mappings.extend(per_vm_ipam_mappings)

        named_configs = (
            vms_config
            if isinstance(vms_config, dict)
            else {f"{i + 1}:{vm.name or 'vm'}": vm for i, vm in enumerate(vms_config)}
        )
        states = {
            name: VmReadinessState(name, vm) for name, vm in named_configs.items()
        }
        for state in states.values():
            if state.config.depends_on:
                state.detail = (
                    f"waiting for dependencies: {', '.join(state.config.depends_on)}"
                )
        async with ReadinessDisplay(
            list(states.values()), proxmox_ids_start
        ) as display:
            if isinstance(vms_config, dict):
                vm_configs_with_ids = await self._create_dependency_vms(
                    vms_config, vnet_aliases, known_builtins, states, display.changed
                )
            else:
                vm_configs_with_ids = await self._create_ordered_vms(
                    vms_config,
                    vnet_aliases,
                    known_builtins,
                    list(states.values()),
                    display.changed,
                )

        return tuple(vm_configs_with_ids), sdn_zone_id, tuple(ipam_mappings)

    async def _create_ordered_vms(
        self,
        vms_config: Tuple[VmConfig, ...],
        vnet_aliases: VnetAliases,
        known_builtins: Dict[str, int],
        states: Sequence[VmReadinessState],
        changed: Callable[[], None],
    ) -> Tuple[Tuple[int, VmConfig], ...]:
        """Create tuple-configured VMs with the legacy barrier semantics."""
        vm_configs_with_ids = []
        tasks: list[asyncio.Task[None]] = []
        try:
            for i, vm_config in enumerate(vms_config):
                # Surface already-failed readiness before creating more guests.
                for task in tasks:
                    if task.done():
                        await task
                state = states[i]
                state.phase = "creating"
                state.detail = "cloning, configuring, and starting VM"
                changed()
                vm_id = await self.qemu_commands.create_and_start_vm(
                    sdn_vnet_aliases=vnet_aliases,
                    vm_config=vm_config,
                    built_in_vm_ids=known_builtins,
                    wait_until_ready=False,
                )
                self.qemu_commands.register_vm(vm_id)
                vm_configs_with_ids.append((vm_id, vm_config))
                state.vm_id = vm_id
                task = asyncio.create_task(self._await_vm_readiness(state, changed))
                tasks.append(task)
                if vm_config.await_before_next_vm:
                    for later in states[i + 1 :]:
                        later.detail = (
                            f"waiting for legacy startup barrier: {state.name}"
                        )
                    changed()
                    await task
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        return tuple(vm_configs_with_ids)

    async def _create_dependency_vms(
        self,
        vms_config: Dict[str, VmConfig],
        vnet_aliases: VnetAliases,
        known_builtins: Dict[str, int],
        states: Dict[str, VmReadinessState],
        changed: Callable[[], None],
    ) -> Tuple[Tuple[int, VmConfig], ...]:
        """Start a VM as soon as all of its dependencies are ready."""
        pending = dict(vms_config)
        ready: Set[str] = set()
        created: Dict[str, Tuple[int, VmConfig]] = {}
        readiness_tasks: Dict[asyncio.Task[None], str] = {}

        try:
            while pending or readiness_tasks:
                for readiness_task in list(readiness_tasks):
                    if readiness_task.done():
                        vm_id = readiness_tasks.pop(readiness_task)
                        await readiness_task
                        ready.add(vm_id)
                for vm_id, vm_config in pending.items():
                    missing = [dep for dep in vm_config.depends_on if dep not in ready]
                    states[vm_id].detail = (
                        f"waiting for dependencies: {', '.join(missing)}"
                        if missing
                        else "waiting for creation slot"
                    )
                changed()
                startable = [
                    vm_id
                    for vm_id, vm_config in pending.items()
                    if set(vm_config.depends_on).issubset(ready)
                ]
                # Mutations stay serialized (VM ID allocation / TaskWrapper).
                # Re-evaluate readiness between clones, rather than after a batch.
                for vm_id in startable[:1]:
                    vm_config = pending.pop(vm_id)
                    states[vm_id].phase = "creating"
                    states[vm_id].detail = "cloning, configuring, and starting VM"
                    changed()
                    self.logger.info(
                        f"Creating VM {len(created) + 1}/{len(vms_config)}: {vm_id}"
                    )
                    with trace_action(
                        self.logger,
                        self.TRACE_NAME,
                        f"create VM {vm_id=} {vm_config=}",
                    ):
                        proxmox_vm_id = await self.qemu_commands.create_and_start_vm(
                            sdn_vnet_aliases=vnet_aliases,
                            vm_config=vm_config,
                            built_in_vm_ids=known_builtins,
                            wait_until_ready=False,
                        )
                        self.qemu_commands.register_vm(proxmox_vm_id)
                        created[vm_id] = (proxmox_vm_id, vm_config)
                    states[vm_id].vm_id = proxmox_vm_id
                    readiness_task = asyncio.create_task(
                        self._await_vm_readiness(states[vm_id], changed)
                    )
                    readiness_tasks[readiness_task] = vm_id

                if startable:
                    await asyncio.sleep(0)
                    continue
                if not pending and not readiness_tasks:
                    break
                if not readiness_tasks:
                    raise RuntimeError(
                        "VM dependency scheduler made no progress; the dependency "
                        "graph should have been rejected during validation"
                    )

                await asyncio.wait(readiness_tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for readiness_task in readiness_tasks:
                readiness_task.cancel()
            if readiness_tasks:
                await asyncio.gather(*readiness_tasks, return_exceptions=True)

        return tuple(created[vm_id] for vm_id in vms_config)

    async def _await_vm_readiness(
        self, state: VmReadinessState, changed: Callable[[], None]
    ) -> None:
        # Reuse the established Linux/Windows exec wrappers (guest-side timeout,
        # persisted results, duplicate-launch protection), not raw QGA exec-status.
        # Import here to avoid the environment -> infrastructure import cycle.
        from proxmoxsandbox._impl.agent_commands import AgentCommands
        from proxmoxsandbox._proxmox_sandbox_environment import (
            ProxmoxSandboxEnvironment,
        )

        assert state.vm_id is not None
        sandbox = ProxmoxSandboxEnvironment(
            infra_commands=self,
            agent_commands=AgentCommands(self.async_proxmox, self.node),
            ipam_mappings=(),
            vm_id=state.vm_id,
            all_vm_ids=(state.vm_id,),
            sdn_zone_id=None,
            os_type=state.config.os_type,
        )
        # Readiness runs concurrently across VMs while cloning uses TaskWrapper.
        # Keep script uploads on QGA, not the ISO fast path which mutates storage
        # and VM media configuration through that same TaskWrapper.
        sandbox._iso_fast_path_disabled = True

        async def execute(command: ReadinessCommand):
            return await sandbox.exec(
                list(command.argv), timeout=command.timeout, timeout_retry=False
            )

        await ReadinessRunner(self.qemu_commands, state, execute, changed).run()

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
            self.sdn_commands.register_ipam_mapping(ipam_mapping)
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
        """Destroy any tracked resources not already cleaned up by sample_cleanup."""
        self.logger.debug("infra_commands task_cleanup activated")
        await self.qemu_commands.task_cleanup()
        await self.sdn_commands.task_cleanup()

    async def cleanup_no_id(self, skip_confirmation=False) -> None:
        noticed_vnets = set()
        noticed_vms = list()

        for vm in await self.qemu_commands.list_vms():
            if self.qemu_commands.vm_is_inspect(vm, template=False):
                existing_vm = await self.qemu_commands.read_vm(vm["vmid"])
                for key in existing_vm.keys():
                    if key.startswith("net"):
                        # 'virtio=BC:24:11:3E:C3:BA,bridge=tcc919v0'
                        bridge = existing_vm[key].split(",")[1].split("=")[1]
                        noticed_vnets.add(bridge)
                noticed_vms.append(vm)

        # Only zones matching the provider's ephemeral-zone naming convention
        # are eligible for sweep — `create_sdn` enforces this convention at
        # creation (sdn_commands.create_sdn). Pre-existing user zones
        # (referenced via sdn_config=None) and the intentionally-permanent
        # static `inspvm*` SDN do not match, and must not be deleted even
        # when an `inspect`-tagged VM is plugged into one of their VNETs.
        # Zones that VMs we created are plugged into. Filtered to provider
        # zones only, so attaching a sandbox VM to a user-pre-existing VNET
        # does not drag the user's zone into the deletion set.
        zones_to_delete = {
            z for z in await self.find_all_zones(noticed_vnets) if is_ephemeral_zone(z)
        }

        # Also catch orphan provider zones with no VMs in them
        # (e.g. when task setup failed before VMs were created).
        for zone in await self.sdn_commands.list_sdn_zones():
            if is_ephemeral_zone(zone["zone"]):
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
        any_user = is_interactive_shell and not is_ci and not is_pytest

        self.logger.debug(f"{is_interactive_shell=}, {is_ci=}, {is_pytest=}")

        should_ask_for_confirmation = any_user and not skip_confirmation

        if should_ask_for_confirmation:
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
