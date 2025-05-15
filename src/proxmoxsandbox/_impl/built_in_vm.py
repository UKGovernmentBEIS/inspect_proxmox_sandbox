import abc
import os
import tempfile
from ipaddress import ip_address, ip_network
from logging import getLogger
from pathlib import Path
from typing import BinaryIO, Dict, cast, get_args

import tenacity
from inspect_ai.util import trace_action

from proxmoxsandbox._impl.agent_commands import AgentCommands
from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox._impl.sdn_commands import STATIC_SDN_START, SdnCommands
from proxmoxsandbox._impl.storage_commands import StorageCommands
from proxmoxsandbox._impl.task_wrapper import TaskWrapper
from proxmoxsandbox.schema import (
    DhcpRange,
    SdnConfig,
    SubnetConfig,
    VmSourceConfig,
    VnetConfig,
)


class BuiltInVM(abc.ABC):
    logger = getLogger(__name__)

    TRACE_NAME = "proxmox_built_in_vm"

    UBUNTU_24_04_OVA_FILENAME = "ubuntu24.04.ova"

    async_proxmox: AsyncProxmoxAPI
    qemu_commands: QemuCommands
    sdn_commands: SdnCommands
    task_wrapper: TaskWrapper
    storage_commands: StorageCommands
    storage: str
    node: str

    def __init__(self, async_proxmox: AsyncProxmoxAPI, node: str):
        self.async_proxmox = async_proxmox
        self.task_wrapper = TaskWrapper(async_proxmox)
        self.qemu_commands = QemuCommands(async_proxmox, node)
        self.sdn_commands = SdnCommands(async_proxmox)
        self.storage = "local"
        self.storage_commands = StorageCommands(async_proxmox, node, self.storage)
        self.node = node

    async def create_and_upload_cloudinit_iso(
        self,
        vm_id: int,
        meta_data: str = """instance-id: proxmox\n""",  # TODO sort this
        user_data: str = """#cloud-config
package_update: true
# Installs packages equivalent to Inspect's default Docker image for tool compatibility
packages:
  - qemu-guest-agent
# from buildpack-deps Dockerfile
  - autoconf
  - automake
  - bzip2
  - default-libmysqlclient-dev
  - dpkg-dev
  - file
  - g++
  - gcc
  - imagemagick
  - libbz2-dev
  - libc6-dev
  - libcurl4-openssl-dev
  - libdb-dev
  - libevent-dev
  - libffi-dev
  - libgdbm-dev
  - libglib2.0-dev
  - libgmp-dev
  - libjpeg-dev
  - libkrb5-dev
  - liblzma-dev
  - libmagickcore-dev
  - libmagickwand-dev
  - libmaxminddb-dev
  - libncurses-dev # changed from libncurses5-dev
#    - libncursesw5-dev # not available (possibly related discussion https://github.com/cardano-foundation/developer-portal/issues/1364)
  - libpng-dev
  - libpq-dev
  - libreadline-dev
  - libsqlite3-dev
  - libssl-dev
  - libtool
  - libwebp-dev
  - libxml2-dev
  - libxslt1-dev # changed from libxslt-dev
  - libyaml-dev
  - make
  - patch
  - unzip
  - xz-utils
  - zlib1g-dev
# equivalent of python3.12-bookworm Dockerfile
  - python3
  - python3-pip
  - python3-venv
  - python-is-python3
# Uncomment the ubuntu user for debugging. Password is "Password2.0"
# users:
#   - name: ubuntu
#     passwd: $6$rounds=4096$6ZjLzzWD9RGieC1y$8R5a/3Vwp3xr9ae9GVlCH0xGGofhp8xlKdddWRugOPhj3frUMr5g57x8t28JRFdS/scPl5AUwrTjah/BVe8dY1
#     lock_passwd: false
#     sudo: ALL=(ALL) NOPASSWD:ALL
#     groups: sudo

runcmd:
  - [ systemctl, enable, qemu-guest-agent ]
  - [ systemctl, start, qemu-guest-agent ]
  - [ systemctl, mask, systemd-networkd-wait-online.service ]
# systemd-networkd-wait-online.service causes startup delays
# and makes it annoying to debug network issues
""",  # noqa: E501
        network_config: str = """network:
  version: 2
  ethernets:
    default:
      match:
        name: e*
      dhcp4: true
      dhcp6: false
""",
    ) -> None:
        """
        Creates a cloud-init ISO and uploads it to Proxmox storage.

        The ISO is created in a temporary file and then uploaded to Proxmox.
        """
        from io import BytesIO

        import pycdlib

        iso = pycdlib.PyCdlib()
        iso.new(interchange_level=3, joliet=3, rock_ridge="1.12", vol_ident="CIDATA")

        # Add cloud-init files to ISO
        for filename, content in [
            ("META_DATA", meta_data),
            ("USER_DATA", user_data),
            ("NETWORK", network_config),
        ]:
            if content:
                content_bytes = content.encode("utf-8")
                buffer = BytesIO(content_bytes)

                iso_path = f"/{filename}"
                proper_name = {
                    "META_DATA": "meta-data",
                    "USER_DATA": "user-data",
                    "NETWORK": "network-config",
                }[filename]

                iso.add_fp(
                    buffer,
                    len(content_bytes),
                    iso_path,
                    joliet_path=f"/{proper_name}",
                    rr_name=proper_name,
                )

        # Create a temporary file and write the ISO to it
        with tempfile.NamedTemporaryFile(delete=False, suffix=".iso") as temp_file:
            iso.write_fp(cast(BinaryIO, temp_file))
            temp_file_path = Path(temp_file.name)

            try:
                filename = f"vm-{vm_id}-cl00udinit.iso"

                await self.storage_commands.upload_file_to_storage(
                    file=temp_file_path,
                    content_type="iso",
                    filename=filename,
                )

            finally:
                # Clean up the temporary file
                if os.path.exists(temp_file_path):
                    os.unlink(temp_file_path)

        @tenacity.retry(
            wait=tenacity.wait_exponential(min=1, exp_base=1.3),
            stop=tenacity.stop_after_delay(30),
        )
        async def attach_to_vm() -> None:
            await self.async_proxmox.request(
                "POST",
                f"/nodes/{self.node}/qemu/{vm_id}/config",
                json={"ide2": f"{self.storage}:iso/{filename},media=cdrom"},
            )

        await attach_to_vm()

    # for test code only
    async def clear_builtins(self) -> None:
        async def inner_clear_builtins() -> None:
            existing_content = await self.read_all_content(self.storage)
            for content in existing_content:
                if content["volid"] and content["volid"].endswith(
                    self.UBUNTU_24_04_OVA_FILENAME
                ):
                    await self.async_proxmox.request(
                        "DELETE",
                        f"/nodes/{self.node}/storage/{self.storage}/content/{content['volid']}",
                    )

            existing_vms = await self.known_builtins()
            for existing_vm in existing_vms:
                await self.qemu_commands.destroy_vm(vm_id=existing_vms[existing_vm])

        await self.task_wrapper.do_action_and_wait_for_tasks(inner_clear_builtins)

    async def known_builtins(self) -> Dict[str, int]:
        existing_vms = await self.qemu_commands.list_vms()

        found_builtins = {}

        for existing_vm_name in list(
            get_args(get_args(VmSourceConfig.model_fields["built_in"].annotation)[0])
        ):
            for existing_vm in existing_vms:
                if (
                    "tags" in existing_vm
                    and "template" in existing_vm
                    and existing_vm["template"] == 1
                    and "inspect" in existing_vm["tags"].split(";")
                    and f"builtin-{existing_vm_name}" in existing_vm["tags"].split(";")
                ):
                    found_builtins[existing_vm_name] = existing_vm["vmid"]
                    break
        return found_builtins

    async def content_exists(self, storage: str, content_name_end: str) -> bool:
        existing_content = await self.read_all_content(storage)
        return any(
            content["volid"] and content["volid"].endswith(content_name_end)
            for content in existing_content
        )

    async def read_all_content(self, storage):
        existing_content = await self.async_proxmox.request(
            "GET",
            f"/nodes/{self.node}/storage/{storage}/content",
        )

        return existing_content

    async def ensure_exists(self, built_in_name: str) -> None:
        if built_in_name is None:
            raise ValueError("built_in_name must be set")

        # we could cache the known_builtins here
        if built_in_name in await self.known_builtins():
            return

        next_available_vm_id = await self.qemu_commands.find_next_available_vm_id()

        # TODO: allow storage to be configurable
        storage = "local"

        if built_in_name == "ubuntu24.04":
            await self.ensure_exists_from_ova(
                storage=storage,
                next_available_vm_id=next_available_vm_id,
                built_in=built_in_name,
                ova_name=self.UBUNTU_24_04_OVA_FILENAME,
                ova_source_url="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.ova",
                ova_vmdk_filename="ubuntu-noble-24.04-cloudimg.vmdk",
            )
        else:
            raise ValueError(f"Unknown built-in {built_in_name}")

    async def ensure_exists_from_ova(
        self,
        storage: str,
        next_available_vm_id: int,
        built_in: str,
        ova_name: str,
        ova_source_url: str,
        ova_vmdk_filename: str,
    ) -> None:
        if await self.content_exists(storage, ova_name):
            self.logger.debug(f"OVA {built_in} already uploaded")
        else:
            with trace_action(
                self.logger,
                self.TRACE_NAME,
                f"upload OVA {built_in=} ",
            ):
                await self.async_proxmox.request(
                    "POST",
                    f"/nodes/{self.node}/storage/{storage}/download-url",
                    json={
                        "content": "import",
                        "filename": ova_name,
                        "url": ova_source_url,
                    },
                )

                @tenacity.retry(
                    wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
                    stop=tenacity.stop_after_delay(300),
                )
                async def upload_complete() -> None:
                    if not await self.content_exists(storage, ova_name):
                        raise ValueError("OVA upload not yet complete")

                await upload_complete()

        existing_zones = await self.sdn_commands.list_sdn_zones()

        exists_already = any(
            zone_info["zone"] and zone_info["zone"] == f"{STATIC_SDN_START}z"
            for zone_info in existing_zones
        )

        if not exists_already:
            await self.sdn_commands.create_sdn(
                proxmox_ids_start=STATIC_SDN_START,
                sdn_config=SdnConfig(
                    vnet_configs=(
                        VnetConfig(
                            subnets=(
                                SubnetConfig(
                                    cidr=ip_network("192.168.99.0/24"),
                                    gateway=ip_address("192.168.99.1"),
                                    snat=True,
                                    dhcp_ranges=(
                                        DhcpRange(
                                            start=ip_address("192.168.99.50"),
                                            end=ip_address("192.168.99.100"),
                                        ),
                                    ),
                                ),
                            )
                        ),
                    )
                ),
            )
        vnet_id = f"{STATIC_SDN_START}v0"

        with trace_action(
            self.logger,
            self.TRACE_NAME,
            f"create VM from OVA {next_available_vm_id=}",
        ):

            async def do_create() -> None:
                await self.async_proxmox.request(
                    "POST",
                    f"/nodes/{self.node}/qemu",
                    json={
                        "vmid": next_available_vm_id,
                        "name": f"inspect-{built_in}",
                        "node": self.node,
                        "cpu": "host",
                        "memory": 8192,
                        "cores": 2,
                        "ostype": "l26",
                        "scsi0": "local-lvm:0,"
                        + f"import-from=local:import/{ova_name}/{ova_vmdk_filename},"
                        + "format=qcow2,cache=writeback",
                        "scsihw": "virtio-scsi-single",
                        "net0": f"virtio,bridge={vnet_id}",
                        "serial0": "socket",
                        "start": False,
                        "agent": "enabled=1",
                    },
                )

            await self.task_wrapper.do_action_and_wait_for_tasks(do_create)

            await self.create_and_upload_cloudinit_iso(
                vm_id=next_available_vm_id,
            )

            async def update_tags() -> None:
                await self.async_proxmox.request(
                    "POST",
                    f"/nodes/{self.node}/qemu/{next_available_vm_id}/config",
                    json={
                        "tags": f"inspect,builtin-{built_in}",
                    },
                )

            await self.task_wrapper.do_action_and_wait_for_tasks(update_tags)

            await self.qemu_commands.start_and_await(
                vm_id=next_available_vm_id, is_sandbox=True
            )

            # now wait for cloud-init to finish

            agent_commands = AgentCommands(self.async_proxmox, self.node)
            res = await agent_commands.exec_command(
                vm_id=next_available_vm_id,
                command=["cloud-init", "status", "--wait"],
            )

            @tenacity.retry(
                wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
                stop=tenacity.stop_after_delay(300),
                retry=tenacity.retry_if_result(lambda x: x is False),
            )
            async def wait_for_cloud_init() -> bool:
                exec_status = await agent_commands.get_agent_exec_status(
                    vm_id=next_available_vm_id, pid=res["pid"]
                )
                if exec_status["exited"] == 1:
                    if exec_status["out-data"].strip() == "status: done":
                        return True
                    else:
                        raise ValueError(
                            f"cloud-init failed: {exec_status['out-data']}"
                        )
                else:
                    return False

            await wait_for_cloud_init()

            await self.async_proxmox.request(
                "POST",
                f"/nodes/{self.node}/qemu/{next_available_vm_id}/status/shutdown",
            )

            await self.qemu_commands.await_vm(
                vm_id=next_available_vm_id,
                is_sandbox=True,
                status_for_wait="stopped",
            )

            await self.async_proxmox.request(
                "POST",
                f"/nodes/{self.node}/qemu/{next_available_vm_id}/template",
            )

            @tenacity.retry(
                wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
                stop=tenacity.stop_after_delay(300),
                retry=tenacity.retry_if_result(lambda x: x is False),
            )
            async def is_template() -> bool:
                current_config = await self.async_proxmox.request(
                    "GET",
                    f"/nodes/{self.node}/qemu/{next_available_vm_id}/config?current=1",
                )
                return current_config["template"] == 1

            await is_template()

            @tenacity.retry(
                wait=tenacity.wait_exponential(min=1, exp_base=1.3),
                stop=tenacity.stop_after_delay(30),
            )
            async def remove_cdrom() -> None:
                await self.async_proxmox.request(
                    "POST",
                    f"/nodes/{self.node}/qemu/{next_available_vm_id}/config",
                    json={"ide2": "none,media=cdrom"},
                )

            await remove_cdrom()

            # TODO tear down SDN zone and vnet
            # TODO delete cloudinit ISO
