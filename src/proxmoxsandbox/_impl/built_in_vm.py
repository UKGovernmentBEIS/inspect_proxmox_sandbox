import abc
import os
import re
import subprocess
import tempfile
from io import BytesIO
from ipaddress import ip_address, ip_network
from logging import getLogger
from pathlib import Path
from typing import BinaryIO, Dict, cast, get_args
from urllib.parse import urlparse

import platformdirs
import pycdlib
import pycurl
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

VM_TIMEOUT = 1200

TRACE_NAME = "proxmox_built_in_vm"

UBUNTU_URL = (
    "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.ova"
)
UBUNTU_VMDK_FILENAME = "ubuntu-noble-24.04-cloudimg.vmdk"
DEBIAN_13_URL = "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"
KALI_DOWNLOAD_URL = "https://kali.download/cloud-images/kali-2025.3/kali-linux-2025.3-cloud-genericcloud-amd64.tar.xz"
KALI_DISK_RENAMED = "kali-2025.3-genericcloud-amd64.raw"

STATIC_VNET_ID = f"{STATIC_SDN_START}v0"


class BuiltInVM(abc.ABC):
    logger = getLogger(__name__)

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
        self.cache_dir = platformdirs.user_cache_path(
            appname="inspect_proxmox_sandbox", ensure_exists=True
        )

    async def ensure_version(
        self, required_major: int, required_minor: int = 0
    ) -> None:
        """
        Ensures that the Proxmox version is at least the specified version.

        Args:
            async_proxmox: The AsyncProxmoxAPI instance
            required_major: Required major version number
            required_minor: Required minor version number (default: 0)

        Raises:
            ValueError: If the Proxmox version is below the required version
        """
        version_info = await self.async_proxmox.request(
            "GET", f"/nodes/{self.node}/version"
        )
        version_string = version_info.get("version", "")

        # Parse version string (e.g., "8.2.2" or "9.0")
        match = re.match(r"(\d+)\.(\d+)", version_string)
        if not match:
            raise ValueError(f"Could not parse Proxmox version: {version_string}")

        major = int(match.group(1))
        minor = int(match.group(2))

        if major < required_major or (
            major == required_major and minor < required_minor
        ):
            raise ValueError(
                f"Proxmox version {version_string} does not meet minimum requirement "
                f"{required_major}.{required_minor}"
            )

    async def create_and_upload_cloudinit_iso(
        self,
        vm_id: int,
        meta_data: str = """instance-id: proxmox\n""",  # TODO sort this
        user_data: str = """#cloud-config
package_update: true
# Installs packages equivalent to Inspect's default Docker image for tool compatibility
# TODO actually install inspect-tool-support here
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
            existing_content = await self.read_all_content()

            storage_names_to_delete = [
                Path(urlparse(UBUNTU_URL).path).name,
                Path(urlparse(DEBIAN_13_URL).path).name,
                KALI_DISK_RENAMED,
            ]

            for content in existing_content:
                if content["volid"]:
                    for storage_name in storage_names_to_delete:
                        if content["volid"].endswith(storage_name):
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

    async def content_exists(self, content_name_end: str) -> bool:
        existing_content = await self.read_all_content()
        return any(
            content["volid"] and content["volid"].endswith(content_name_end)
            for content in existing_content
        )

    async def read_all_content(self):
        existing_content = await self.async_proxmox.request(
            "GET",
            f"/nodes/{self.node}/storage/{self.storage}/content",
        )

        return existing_content

    async def ensure_exists(self, built_in_name: str) -> None:
        if built_in_name is None:
            raise ValueError("built_in_name must be set")

        # we could cache the known_builtins here
        if built_in_name in await self.known_builtins():
            return

        next_available_vm_id = await self.qemu_commands.find_next_available_vm_id()

        if built_in_name == "ubuntu24.04":
            await self.ensure_exists_from_ova(
                next_available_vm_id=next_available_vm_id,
                built_in=built_in_name,
                source_image_source_url=UBUNTU_URL,
                ova_vmdk_filename=UBUNTU_VMDK_FILENAME,
            )
        elif built_in_name == "debian13":
            await self.ensure_version(9)
            await self.ensure_exists_from_qcow2(
                next_available_vm_id=next_available_vm_id,
                built_in=built_in_name,
                source_image_source_url=DEBIAN_13_URL,
            )
        elif built_in_name == "kali2025.3":
            await self.ensure_version(9)
            await self.ensure_exists_from_xz(
                next_available_vm_id=next_available_vm_id,
                built_in=built_in_name,
                source_image_source_url=KALI_DOWNLOAD_URL,
                disk_renamed=KALI_DISK_RENAMED,
            )
        else:
            raise ValueError(f"Unknown built-in {built_in_name}")

    async def ensure_exists_from_ova(
        self,
        next_available_vm_id: int,
        built_in: str,
        source_image_source_url: str,
        ova_vmdk_filename: str,
    ) -> None:
        source_image_name = Path(urlparse(source_image_source_url).path).name

        await self.ensure_source_uploaded(
            built_in, source_image_name, source_image_source_url
        )

        await self.ensure_static_sdn_exists()

        await self.startup_vm(
            next_available_vm_id,
            built_in,
            f"import/{source_image_name}/{ova_vmdk_filename}",
        )

    async def ensure_exists_from_qcow2(
        self,
        next_available_vm_id: int,
        built_in: str,
        source_image_source_url: str,
    ) -> None:
        source_image_name = Path(urlparse(source_image_source_url).path).name

        await self.ensure_source_uploaded(
            built_in, source_image_name, source_image_source_url
        )

        await self.ensure_static_sdn_exists()

        await self.startup_vm(
            next_available_vm_id, built_in, f"import/{source_image_name}"
        )

    async def ensure_exists_from_xz(
        self,
        next_available_vm_id: int,
        built_in: str,
        source_image_source_url: str,
        disk_renamed: str,
    ) -> None:
        # Unfortunately Kali only provide VM images in .xz and .7z formats,
        # neither of which are supported by Proxmox's "download from URL".
        # They require an intermediate step to decompress them.
        # So we must download the file locally, extract the disk image, and then
        # upload.

        download_filename = Path(urlparse(source_image_source_url).path).name

        if await self.content_exists(download_filename):
            self.logger.debug(f"source image {built_in} already uploaded")
        else:
            with trace_action(
                self.logger,
                TRACE_NAME,
                f"upload source image {built_in=} ",
            ):
                download_path = os.path.join(self.cache_dir, download_filename)
                with open(download_path, "wb") as f:
                    c = pycurl.Curl()
                    c.setopt(c.URL, source_image_source_url)
                    c.setopt(c.WRITEDATA, f)
                    c.setopt(c.FOLLOWLOCATION, True)
                    c.setopt(c.FAILONERROR, True)
                    try:
                        c.perform()
                        status_code = c.getinfo(c.RESPONSE_CODE)
                        if status_code >= 400:
                            raise ValueError(
                                f"Download failed with status code: {status_code}"
                            )
                    finally:
                        c.close()

                # shell out to tar -xf with subprocess:
                subprocess.check_call(
                    ["tar", "-xf", download_path, "-C", self.cache_dir]
                )

                # rename the output disk.raw to something better:

                source_image_name = os.path.join(self.cache_dir, disk_renamed)
                output_path = Path(os.path.join(self.cache_dir, "disk.raw"))
                output_path.rename(source_image_name)

                # Delete original
                # os.remove(download_path)

                await self.storage_commands.upload_file_to_storage(
                    file=Path(source_image_name),
                    content_type="import",
                    filename=disk_renamed,
                    size_check=os.path.getsize(source_image_name),
                )

        await self.ensure_static_sdn_exists()

        await self.startup_vm(next_available_vm_id, built_in, f"import/{disk_renamed}")

    async def ensure_source_uploaded(
        self,
        built_in: str,
        source_image_name: str,
        source_image_source_url: str,
    ) -> None:
        if await self.content_exists(source_image_name):
            self.logger.debug(f"source image {built_in} already uploaded")
        else:
            with trace_action(
                self.logger,
                TRACE_NAME,
                f"upload source image {built_in=} ",
            ):
                await self.async_proxmox.request(
                    "POST",
                    f"/nodes/{self.node}/storage/{self.storage}/download-url",
                    json={
                        "content": "import",
                        "filename": source_image_name,
                        "url": source_image_source_url,
                    },
                )

                @tenacity.retry(
                    wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
                    stop=tenacity.stop_after_delay(VM_TIMEOUT),
                )
                async def upload_complete() -> None:
                    if not await self.content_exists(source_image_name):
                        raise ValueError("image upload not yet complete")

                await upload_complete()

    async def startup_vm(
        self,
        next_available_vm_id: int,
        built_in: str,
        import_source: str,
    ) -> None:
        with trace_action(
            self.logger,
            TRACE_NAME,
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
                        + f"import-from={self.storage}:{import_source},"
                        + "format=qcow2,cache=writeback",
                        "scsihw": "virtio-scsi-single",
                        "net0": f"virtio,bridge={STATIC_VNET_ID}",
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
                stop=tenacity.stop_after_delay(VM_TIMEOUT),
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
                stop=tenacity.stop_after_delay(VM_TIMEOUT),
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

    async def ensure_static_sdn_exists(self):
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
