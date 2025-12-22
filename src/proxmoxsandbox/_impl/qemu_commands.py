import abc
import re
import tarfile
from contextvars import ContextVar
from logging import getLogger
from pathlib import Path
from typing import Dict, List, Set

import tenacity
from inspect_ai.util import trace_action
from pydantic.networks import HttpUrl

from proxmoxsandbox._impl.async_proxmox import (
    AsyncProxmoxAPI,
    ProxmoxJsonDataType,
)
from proxmoxsandbox._impl.sdn_commands import VnetAliases
from proxmoxsandbox._impl.storage_commands import StorageCommands
from proxmoxsandbox._impl.task_wrapper import TaskWrapper
from proxmoxsandbox.schema import VmConfig


class QemuCommands(abc.ABC):
    logger = getLogger(__name__)

    TRACE_NAME = "proxmox_qemu_command"

    async_proxmox: AsyncProxmoxAPI
    task_wrapper: TaskWrapper
    # TODO disambiguate that "this.storage" is for images rather than VM disks
    # which continue to live in local-lvm
    storage: str
    storage_commands: StorageCommands
    node: str

    _running_proxmox_vms: ContextVar[Set[int]] = ContextVar(
        "proxmox_running_vms", default=set()
    )
    _cleanup_completed: ContextVar[bool] = ContextVar(
        "proxmox_vms_cleanup_executed", default=False
    )

    def __init__(self, async_proxmox: AsyncProxmoxAPI, node: str):
        self.async_proxmox = async_proxmox
        self.task_wrapper = TaskWrapper(async_proxmox)
        self.storage = "local"
        self.storage_commands = StorageCommands(async_proxmox, node, self.storage)
        self.node = node

    async def await_vm(
        self,
        vm_id: int,
        is_sandbox: bool,
        status_for_wait: str = "running",
    ) -> None:
        @tenacity.retry(
            wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
            stop=tenacity.stop_after_delay(1200),
        )
        async def is_in_status() -> None:
            vm_status = await self.async_proxmox.request(
                "GET", f"/nodes/{self.node}/qemu/{vm_id}/status/current"
            )
            if vm_status["status"] != status_for_wait:
                raise ValueError(f"vm {vm_id} not {status_for_wait}")

        with trace_action(
            self.logger,
            self.TRACE_NAME,
            f"await VM {vm_id} to be in status {status_for_wait}",
        ):
            await is_in_status()

        if is_sandbox and status_for_wait == "running":

            @tenacity.retry(
                wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
                stop=tenacity.stop_after_delay(300),
            )
            async def qemu_agent_reachable() -> None:
                await self.ping_qemu_agent(vm_id)

            with trace_action(
                self.logger, self.TRACE_NAME, f"await VM {vm_id} QEMU agent"
            ):
                await qemu_agent_reachable()

    async def destroy_vm(self, vm_id: int) -> None:
        with trace_action(self.logger, self.TRACE_NAME, f"stop VM {vm_id}"):
            await self.async_proxmox.request(
                "POST", f"/nodes/{self.node}/qemu/{vm_id}/status/stop"
            )

        @tenacity.retry(
            wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
            stop=tenacity.stop_after_delay(300),
        )
        async def is_not_running() -> None:
            vm_status = await self.async_proxmox.request(
                "GET", f"/nodes/{self.node}/qemu/{vm_id}/status/current"
            )
            if vm_status["status"] != "stopped":
                raise ValueError(f"vm {vm_id} still running")

        with trace_action(self.logger, self.TRACE_NAME, f"await VM {vm_id} stopped"):
            await is_not_running()

        with trace_action(self.logger, self.TRACE_NAME, f"delete VM {vm_id}"):
            await self.async_proxmox.request(
                "DELETE", f"/nodes/{self.node}/qemu/{vm_id}"
            )

        @tenacity.retry(
            wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
            stop=tenacity.stop_after_delay(30),
        )
        async def vm_deleted() -> None:
            current = await self.async_proxmox.request(
                method="GET",
                path=f"/nodes/{self.node}/qemu/{vm_id}/status/current",
                raise_errors=False,
            )
            if "vmid" in current:
                raise ValueError(f"vm {vm_id} still exists")

        with trace_action(self.logger, self.TRACE_NAME, f"await VM {vm_id} deleted"):
            await vm_deleted()

    async def list_vms(self):
        with trace_action(self.logger, self.TRACE_NAME, "list all VMs"):
            return await self.async_proxmox.request("GET", f"/nodes/{self.node}/qemu")

    async def read_vm(self, vm_id: int):
        return await self.async_proxmox.request(
            "GET", f"/nodes/{self.node}/qemu/{vm_id}/config"
        )

    async def find_next_available_vm_id(self) -> int:
        return await self.async_proxmox.request("GET", "/cluster/nextid")

    async def start_and_await(
        self,
        vm_id: int,
        is_sandbox: bool,
    ) -> None:
        await self.async_proxmox.request(
            "POST",
            f"/nodes/{self.node}/qemu/{vm_id}/status/start",
        )

        await self.await_vm(
            vm_id=vm_id,
            is_sandbox=is_sandbox,
        )

    def _convert_sdn_vnet_aliases(
        self, sdn_vnet_aliases: VnetAliases
    ) -> Dict[str, str]:
        """Convert list of (vnet_id, vnet_alias) tuples to alias->id mapping, skipping None aliases."""  # noqa: E501
        return {
            alias: vnet_id for vnet_id, alias in sdn_vnet_aliases if alias is not None
        }

    async def create_and_start_vm(
        self,
        sdn_vnet_aliases: VnetAliases,
        vm_config: VmConfig,
        built_in_vm_ids: Dict[str, int],
    ) -> int:
        new_vm_id: int | None = None

        if (
            vm_config.disk_controller is not None
            and vm_config.vm_source_config.ova is None
        ):
            raise NotImplementedError("disk_controller is only supported for OVA")

        if (vm_config.os_type != "l26") and (vm_config.vm_source_config.ova is None):
            raise NotImplementedError("os_type is only supported for OVA")

        if vm_config.vm_source_config.built_in:
            vm_id_to_clone = built_in_vm_ids[vm_config.vm_source_config.built_in]

            if vm_id_to_clone is None:
                raise ValueError(
                    "couldn't find template VM for "
                    + f"{vm_config.vm_source_config.built_in}"
                )

            new_vm_id = await self.clone_vm_and_start(
                vm_config, vm_id_to_clone, sdn_vnet_aliases, False
            )
        elif vm_config.vm_source_config.ova is not None:
            if isinstance(vm_config.vm_source_config.ova, HttpUrl):
                raise NotImplementedError(
                    f"Not supported: {type(vm_config.vm_source_config.ova)}"
                )
            if isinstance(vm_config.vm_source_config.ova, Path):
                ova_size = vm_config.vm_source_config.ova.stat().st_size
                ova_tag = f"ova-{vm_config.vm_source_config.ova.name}-{ova_size}"
                ova_tag = re.sub(r"[^a-zA-Z0-9_\-]", "_", ova_tag)
                ova_tag = ova_tag.lower()

                existing_vms = await self.list_vms()

                found_existing_template = None
                for existing_vm in existing_vms:
                    if (
                        "tags" in existing_vm
                        and "template" in existing_vm
                        and existing_vm["template"] == 1
                        and "inspect" in existing_vm["tags"].split(";")
                        and ova_tag in existing_vm["tags"].split(";")
                    ):
                        found_existing_template = existing_vm["vmid"]
                        break

                if found_existing_template is None:
                    await self.storage_commands.upload_file_to_storage(
                        file=vm_config.vm_source_config.ova,
                        content_type="import",
                        size_check=ova_size,
                    )

                    json_for_create: ProxmoxJsonDataType = {
                        "node": self.node,
                        "cpu": "host",
                        "ostype": vm_config.os_type,
                        "scsihw": "virtio-scsi-single",
                        "start": False,
                    }

                    disk_prefix = (
                        "scsi"
                        if vm_config.disk_controller is None
                        else vm_config.disk_controller
                    )

                    self.other_config_json(vm_config, json_for_create)

                    vmdks = []
                    with tarfile.open(vm_config.vm_source_config.ova, "r") as tar:
                        file_list = tar.getnames()

                        for file_name in file_list:
                            if file_name.endswith(".vmdk"):
                                vmdks.append(file_name)

                    # this logic is reverse-engineered from the Proxmox GUI
                    # and may be brittle
                    for i, vmdk in enumerate(vmdks):
                        json_for_create[f"{disk_prefix}{i}"] = (
                            f"local-lvm:0,import-from={self.storage}:import/{vm_config.vm_source_config.ova.name}/{vmdk},format=qcow2,cache=writeback"
                        )

                    new_vm_template_id = await self.find_next_available_vm_id()
                    json_for_create["vmid"] = new_vm_template_id

                    with trace_action(
                        self.logger,
                        self.TRACE_NAME,
                        f"create VM from OVA {new_vm_template_id=}",
                    ):

                        async def create() -> None:
                            await self.async_proxmox.request(
                                "POST", f"/nodes/{self.node}/qemu", json=json_for_create
                            )

                        await self.task_wrapper.do_action_and_wait_for_tasks(create)

                    await self.configure_network_and_tags(
                        vm_config=vm_config,
                        sdn_vnet_aliases=sdn_vnet_aliases,
                        vm_id=new_vm_template_id,
                        extra_tags=[ova_tag],
                    )

                    async def convert_to_template() -> None:
                        await self.async_proxmox.request(
                            "POST",
                            f"/nodes/{self.node}/qemu/{new_vm_template_id}/template",
                        )

                    await self.task_wrapper.do_action_and_wait_for_tasks(
                        convert_to_template
                    )

                    await self.remove_existing_nics(new_vm_template_id)

                else:
                    new_vm_template_id = found_existing_template

                new_vm_id = await self.clone_vm_and_start(
                    vm_config,
                    new_vm_template_id,
                    sdn_vnet_aliases,
                    vm_config.is_sandbox,
                )
                await self.register_created_vm(new_vm_id)

            else:
                raise NotImplementedError(
                    f"Not supported: {type(vm_config.vm_source_config.ova)}"
                )
        elif vm_config.vm_source_config.existing_vm_template_tag:
            existing_vms = await self.list_vms()

            found_vm = []

            for existing_vm in existing_vms:
                if (
                    "template" in existing_vm
                    and existing_vm["template"] == 1
                    and "tags" in existing_vm
                    and "inspect" in existing_vm["tags"].split(";")
                    and vm_config.vm_source_config.existing_vm_template_tag
                    in existing_vm["tags"].split(";")
                ):
                    found_vm.append(existing_vm)
                    break

            if len(found_vm) == 0:
                raise ValueError(
                    "Couldn't find VM with tag "
                    + f"{vm_config.vm_source_config.existing_vm_template_tag}"
                )

            if len(found_vm) > 1:
                raise ValueError(
                    "Found multiple VMs with tag "
                    + f"{vm_config.vm_source_config.existing_vm_template_tag}: "
                    + f"{found_vm=}"
                )

            vm_id_to_clone = found_vm[0]["vmid"]

            new_vm_id = await self.clone_vm_and_start(
                vm_config, vm_id_to_clone, sdn_vnet_aliases, True
            )

        else:
            raise NotImplementedError(f"Not supported: {vm_config.vm_source_config=}")
        if new_vm_id is None:
            raise ValueError("No VM ID?")
        return new_vm_id

    async def remove_existing_nics(self, vm_id):
        existing_config = await self.read_vm(vm_id)
        for key in existing_config.keys():
            if key.startswith("net"):
                await self.async_proxmox.request(
                    "PUT",
                    f"/nodes/{self.node}/qemu/{vm_id}/config",
                    body_content=f"delete={key}",
                    content_type="application/x-www-form-urlencoded",
                )

    async def configure_network_and_tags(
        self,
        vm_config: VmConfig,
        sdn_vnet_aliases: VnetAliases,
        vm_id: int,
        extra_tags: List[str] = [],
    ) -> None:
        async def update_network() -> None:
            network_update_json: ProxmoxJsonDataType = {}

            nic_prefix = (
                "virtio"
                if vm_config.nic_controller is None
                else vm_config.nic_controller
            )
            # If we have nics configured, we need to look up existing VNETs
            existing_vnet_mapping = {}
            try:
                # Fetch all existing VNETs from Proxmox
                all_vnets = await self.async_proxmox.request(
                    "GET", "/cluster/sdn/vnets"
                )

                if all_vnets:
                    for vnet in all_vnets:
                        if "alias" in vnet and vnet["alias"]:
                            # Map alias to the actual VNET ID
                            existing_vnet_mapping[vnet["alias"]] = vnet["vnet"]
            except Exception as e:
                self.logger.error(f"Error fetching existing VNETs: {e}")

            self.logger.debug(f"Existing VNET mapping: {existing_vnet_mapping}")
            self.logger.debug(f"SDN VNET aliases: {sdn_vnet_aliases}")

            if vm_config.nics is None:
                if (
                    vm_config.vm_source_config.built_in
                    or vm_config.vm_source_config.ova
                ):
                    await self.remove_existing_nics(vm_id)
                    # Only add the first VNET if
                    # there are any defined in sdn_vnet_aliases
                    if sdn_vnet_aliases and len(sdn_vnet_aliases) > 0:
                        first_vnet_id = sdn_vnet_aliases[0][0]
                        network_update_json["net0"] = (
                            f"{nic_prefix},bridge={first_vnet_id}"
                        )
                    # otherwise do nothing - no networks will be added
                # for other vm_source_configs, we *do not touch* networking config
            else:
                await self.remove_existing_nics(vm_id)
                # Convert the SDN aliases to a mapping
                alias_mapping = self._convert_sdn_vnet_aliases(sdn_vnet_aliases)

                # For each NIC in the config
                for i, nic in enumerate(vm_config.nics):
                    # Check if the alias exists in our mapping first
                    # (from configured SDN)
                    if nic.vnet_alias in alias_mapping:
                        bridge_name = alias_mapping[nic.vnet_alias]
                    # Then check if it exists in existing VNETs
                    elif nic.vnet_alias in existing_vnet_mapping:
                        bridge_name = existing_vnet_mapping[nic.vnet_alias]
                    else:
                        # If we can't find it anywhere,
                        # log what we found and raise an error
                        self.logger.error(
                            f"VNET alias '{nic.vnet_alias}' not found in Proxmox."
                        )
                        self.logger.error(
                            f"Available aliases: {list(existing_vnet_mapping.keys())}"
                        )
                        raise ValueError(
                            f"VNET alias '{nic.vnet_alias}' not found in Proxmox"
                        )

                    netx = f"{nic_prefix},bridge={bridge_name}"
                    if nic.mac:
                        netx += f",macaddr={str(nic.mac).upper()}"
                    network_update_json[f"net{i}"] = netx

            if network_update_json:
                await self.async_proxmox.request(
                    "POST",
                    f"/nodes/{self.node}/qemu/{vm_id}/config",
                    json=network_update_json,
                )

        async def update_tags() -> None:
            await self.async_proxmox.request(
                "POST",
                f"/nodes/{self.node}/qemu/{vm_id}/config",
                json={"tags": ",".join(set(extra_tags + ["inspect"]))},
            )

        await self.task_wrapper.do_action_and_wait_for_tasks(update_network)
        await self.task_wrapper.do_action_and_wait_for_tasks(update_tags)

    async def clone_vm_and_start(
        self,
        vm_config: VmConfig,
        vm_id_to_clone: int,
        sdn_vnet_aliases: VnetAliases,
        preserve_tags: bool,
    ) -> int:
        new_vm_id = await self.find_next_available_vm_id()

        async def create_clone() -> None:
            await self.async_proxmox.request(
                "POST",
                f"/nodes/{self.node}/qemu/{vm_id_to_clone}/clone",
                json={"newid": new_vm_id, "full": 0, "name": vm_config.name},
            )
            await self.register_created_vm(new_vm_id)

        await self.task_wrapper.do_action_and_wait_for_tasks(create_clone)

        extra_tags = []
        if preserve_tags:
            existing_config = await self.read_vm(vm_id_to_clone)
            if "tags" in existing_config:
                extra_tags += existing_config["tags"].split(";")

        await self.configure_network_and_tags(
            vm_config, sdn_vnet_aliases, new_vm_id, extra_tags=extra_tags
        )

        async def other_updates() -> None:
            other_update_json: ProxmoxJsonDataType = {}
            self.other_config_json(vm_config, other_update_json)

            await self.async_proxmox.request(
                "POST",
                f"/nodes/{self.node}/qemu/{new_vm_id}/config",
                json=other_update_json,
            )

        await self.task_wrapper.do_action_and_wait_for_tasks(other_updates)

        await self.start_and_await(vm_id=new_vm_id, is_sandbox=vm_config.is_sandbox)
        return new_vm_id

    def other_config_json(
        self, vm_config: VmConfig, json_for_create: ProxmoxJsonDataType
    ) -> None:
        json_for_create["agent"] = f"enabled={1 if vm_config.is_sandbox else 0}"
        json_for_create["memory"] = vm_config.ram_mb
        json_for_create["cores"] = vm_config.vcpus
        if vm_config.name is not None:
            json_for_create["name"] = vm_config.name
        if vm_config.uefi_boot:
            json_for_create["efidisk0"] = "local-lvm:0,efitype=4m,pre-enrolled-keys=0"
            json_for_create["bios"] = "ovmf"

    async def ping_qemu_agent(self, vm_id: int):
        await self.async_proxmox.request(
            "POST", f"/nodes/{self.node}/qemu/{vm_id}/agent/ping"
        )

    async def connection_url(self, vm_id: int) -> str:
        return f"{self.async_proxmox.base_url}/?console=kvm&novnc=1&vmid={vm_id}&node={self.node}"  # noqa: E501

    async def register_created_vm(self, vm_id: int | None) -> None:
        if vm_id is not None:
            self._running_proxmox_vms.get().add(vm_id)

    async def task_cleanup(self) -> None:
        cleanup_completed = self._cleanup_completed.get()
        self.logger.debug(f"qemu_commands cleanup activated; {cleanup_completed=}")
        if cleanup_completed:
            return

        with trace_action(self.logger, self.TRACE_NAME, "cleanup all VMs"):
            running_proxmox_vms = self._running_proxmox_vms.get()
            self.logger.debug(f"existing vm count: {len(running_proxmox_vms)}")
            for vm_id in running_proxmox_vms:
                self.logger.debug(f"destroy_vm {vm_id=}")
                await self.destroy_vm(vm_id)
            self._cleanup_completed.set(True)
