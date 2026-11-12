import abc
import asyncio
import os
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Iterable
from io import BytesIO
from ipaddress import ip_address, ip_network
from logging import getLogger
from pathlib import Path
from typing import BinaryIO, Dict, cast, get_args
from urllib.parse import urlparse

import httpx
import platformdirs
import pycdlib
import tenacity
from inspect_ai.util import trace_action

from proxmoxsandbox._impl.agent_commands import AgentCommands
from proxmoxsandbox._impl.async_proxmox import POLL_WAIT, AsyncProxmoxAPI
from proxmoxsandbox._impl.qemu_commands import QemuCommands, proxmox_cpu
from proxmoxsandbox._impl.sdn_commands import STATIC_SDN_START, SdnCommands
from proxmoxsandbox._impl.storage_commands import LOCAL_STORAGE, LocalStorageCommands
from proxmoxsandbox.schema import (
    DhcpRange,
    SdnConfig,
    SubnetConfig,
    VmSourceConfig,
    VnetConfig,
)

VM_TIMEOUT = 1200
# /cluster/nextid reserves nothing, so concurrent builds read the same ID
_VM_ID_ATTEMPTS = 5
CLOUD_INIT_TIMEOUT = 1200
# 41 GiB used after install (39 GiB of it /usr), so ~20 GiB free for the eval
KALI_DISK_GIB = 64
# Measured 25 min on a 2-vCPU guest on an r8i.4xlarge host in eu-west-2
KALI_CLOUD_INIT_TIMEOUT = 3600

TRACE_NAME = "proxmox_built_in_vm"

UBUNTU_URL = (
    "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.ova"
)
UBUNTU_VMDK_FILENAME = "ubuntu-noble-24.04-cloudimg.vmdk"
DEBIAN_13_URL = "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"
KALI_DOWNLOAD_URL = "https://kali.download/cloud-images/kali-2025.4/kali-linux-2025.4-cloud-genericcloud-amd64.tar.xz"
KALI_DISK_RENAMED = "kali-2025.4-genericcloud-amd64.raw"

STATIC_VNET_ID = f"{STATIC_SDN_START}v0"

# built-in name -> filename of its source image in local storage
SOURCE_IMAGE_NAMES = {
    "ubuntu24.04": Path(urlparse(UBUNTU_URL).path).name,
    "debian13": Path(urlparse(DEBIAN_13_URL).path).name,
    "kali2025.4": KALI_DISK_RENAMED,
}


async def download_to_file(url: str, destination: Path) -> None:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=30, read=300, write=60, pool=60),
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            partial = destination.with_name(destination.name + ".part")
            try:
                with partial.open("wb") as file_handle:
                    async for chunk in response.aiter_bytes():
                        file_handle.write(chunk)
                partial.replace(destination)
            finally:
                partial.unlink(missing_ok=True)


class BuiltInVM(abc.ABC):
    logger = getLogger(__name__)

    async_proxmox: AsyncProxmoxAPI
    qemu_commands: QemuCommands
    sdn_commands: SdnCommands
    storage_commands: LocalStorageCommands
    node: str

    def __init__(
        self,
        async_proxmox: AsyncProxmoxAPI,
        node: str,
        image_storage: str,
        qemu_commands: QemuCommands,
        sdn_commands: SdnCommands,
        storage_commands: LocalStorageCommands,
    ):
        self.async_proxmox = async_proxmox
        self.qemu_commands = qemu_commands
        self.sdn_commands = sdn_commands
        self.storage_commands = storage_commands
        self.node = node
        self.image_storage = image_storage
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
        if not self.async_proxmox.release_at_least(required_major, required_minor):
            release_string = self.async_proxmox.get_discovered_proxmox_version().release
            raise ValueError(
                f"Proxmox version {release_string} does not meet minimum requirement "
                f"{required_major}.{required_minor}"
            )

    DEFAULT_USER_DATA = """#cloud-config
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
# "quiet": under nested virtualisation every printk byte to the serial console is a trap
  - - sh
    - -c
    - |
      mkdir -p /etc/default/grub.d
      echo 'GRUB_CMDLINE_LINUX_DEFAULT="console=tty1 console=ttyS0 quiet"' > /etc/default/grub.d/90-inspect.cfg
      update-grub
"""  # noqa: E501

    # Not in packages: the Kali image has no qemu-guest-agent, so one apt transaction
    # would keep the agent down, and the build blind, for the whole install.
    # Must stay the last runcmd entry: cloud-init only reports the last command's exit
    # status, so a failed install would otherwise still produce "status: done".
    # apt-get clean drops ~13 GiB of cached .debs from the template
    KALI_USER_DATA = (
        DEFAULT_USER_DATA.rstrip("\n")
        + "\n  - [ sh, -c, DEBIAN_FRONTEND=noninteractive apt-get install -y kali-linux-everything && apt-get clean ]\n"  # noqa: E501
    )

    async def create_and_upload_cloudinit_iso(
        self,
        vm_id: int,
        meta_data: str = """instance-id: proxmox\n""",  # TODO sort this
        user_data: str = DEFAULT_USER_DATA,
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
                json={"ide2": f"{LOCAL_STORAGE}:iso/{filename},media=cdrom"},
            )

        await attach_to_vm()

    # for test code only
    async def clear_builtin(self, built_in: str) -> None:
        """Remove one built-in's template and its uploaded source image."""
        source_image_name = SOURCE_IMAGE_NAMES[built_in]
        vm_id = (await self.known_builtins()).get(built_in)
        if vm_id is not None:
            await self.qemu_commands.destroy_vm(vm_id=vm_id, name=f"inspect-{built_in}")

        for content in await self.read_all_content():
            if content["volid"] and content["volid"].endswith(f"/{source_image_name}"):
                upid = await self.async_proxmox.request(
                    "DELETE",
                    f"/nodes/{self.node}/storage/{LOCAL_STORAGE}/content/{content['volid']}",
                )
                if upid is not None:
                    await self.async_proxmox.wait_for_task(upid=upid)

    async def known_builtins(self) -> Dict[str, int]:
        existing_vms = await self.qemu_commands.list_vms()

        found_builtins = {}

        for existing_vm_name in list(
            get_args(get_args(VmSourceConfig.model_fields["built_in"].annotation)[0])
        ):
            for existing_vm in existing_vms:
                if self.qemu_commands.vm_is_inspect(
                    existing_vm, template=True, with_tag=f"builtin-{existing_vm_name}"
                ):
                    found_builtins[existing_vm_name] = existing_vm["vmid"]
                    break
        return found_builtins

    async def content_exists(self, content_name_end: str) -> bool:
        existing_content = await self.read_all_content()
        return any(
            content["volid"] and content["volid"].endswith(f"/{content_name_end}")
            for content in existing_content
        )

    async def read_all_content(self):
        existing_content = await self.async_proxmox.request(
            "GET",
            f"/nodes/{self.node}/storage/{LOCAL_STORAGE}/content",
        )

        return existing_content

    async def ensure_exists_many(self, built_in_names: Iterable[str]) -> None:
        """Build whichever of these built-ins the host lacks, concurrently."""
        missing = sorted(set(built_in_names) - set(await self.known_builtins()))
        if not missing:
            return
        # Up front: two builds that both found it missing would both create it
        await self.ensure_static_sdn_exists()
        builds = [asyncio.create_task(self.ensure_exists(name)) for name in missing]
        try:
            await asyncio.gather(*builds)
        finally:
            # A build outliving a failed sibling would run on under the next
            # sample's pre-clean
            for build in builds:
                build.cancel()
            await asyncio.gather(*builds, return_exceptions=True)

    async def ensure_exists(self, built_in_name: str) -> None:
        if built_in_name is None:
            raise ValueError("built_in_name must be set")

        # we could cache the known_builtins here
        if built_in_name in await self.known_builtins():
            return

        if built_in_name == "ubuntu24.04":
            await self.ensure_exists_from_ova(
                built_in=built_in_name,
                source_image_source_url=UBUNTU_URL,
                ova_vmdk_filename=UBUNTU_VMDK_FILENAME,
            )
        elif built_in_name == "debian13":
            await self.ensure_version(9)
            await self.ensure_exists_from_qcow2(
                built_in=built_in_name,
                source_image_source_url=DEBIAN_13_URL,
            )
        elif built_in_name == "kali2025.4":
            await self.ensure_version(9)
            await self.ensure_exists_from_xz(
                built_in=built_in_name,
                source_image_source_url=KALI_DOWNLOAD_URL,
                disk_renamed=KALI_DISK_RENAMED,
                user_data=self.KALI_USER_DATA,
                disk_gib=KALI_DISK_GIB,
                cloud_init_timeout=KALI_CLOUD_INIT_TIMEOUT,
            )
        else:
            raise ValueError(f"Unknown built-in {built_in_name}")

    async def ensure_exists_from_ova(
        self,
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
            built_in,
            f"import/{source_image_name}/{ova_vmdk_filename}",
        )

    async def ensure_exists_from_qcow2(
        self,
        built_in: str,
        source_image_source_url: str,
    ) -> None:
        source_image_name = Path(urlparse(source_image_source_url).path).name

        await self.ensure_source_uploaded(
            built_in, source_image_name, source_image_source_url
        )

        await self.ensure_static_sdn_exists()

        await self.startup_vm(built_in, f"import/{source_image_name}")

    async def ensure_exists_from_xz(
        self,
        built_in: str,
        source_image_source_url: str,
        disk_renamed: str,
        user_data: str,
        disk_gib: int | None,
        cloud_init_timeout: int,
    ) -> None:
        # Unfortunately Kali only provide VM images in .xz and .7z formats,
        # neither of which are supported by Proxmox's "download from URL".
        # They require an intermediate step to decompress them.
        # So we must download the file locally, extract the disk image, and then
        # upload.

        download_filename = Path(urlparse(source_image_source_url).path).name

        if await self.content_exists(disk_renamed):
            self.logger.debug(f"source image {built_in} already uploaded")
        else:
            with trace_action(
                self.logger,
                TRACE_NAME,
                f"upload source image {built_in=} ",
            ):
                download_path = Path(self.cache_dir) / download_filename
                await download_to_file(source_image_source_url, download_path)

                # Not check_call: that would block other builds' polls
                tar_command = [
                    "tar",
                    "-xf",
                    str(download_path),
                    "-C",
                    str(self.cache_dir),
                ]
                tar = await asyncio.create_subprocess_exec(
                    *tar_command, stderr=asyncio.subprocess.PIPE
                )
                try:
                    _, tar_stderr = await tar.communicate()
                except asyncio.CancelledError:
                    # Left running, it would still be writing disk.raw under a retry
                    tar.kill()
                    await tar.wait()
                    raise
                if tar.returncode:
                    raise subprocess.CalledProcessError(
                        tar.returncode, tar_command, stderr=tar_stderr
                    )

                # rename the output disk.raw to something better:

                source_image_name = os.path.join(self.cache_dir, disk_renamed)
                output_path = Path(os.path.join(self.cache_dir, "disk.raw"))
                output_path.rename(source_image_name)

                await self.storage_commands.upload_file_to_storage(
                    file=Path(source_image_name),
                    content_type="import",
                    filename=disk_renamed,
                    size_check=os.path.getsize(source_image_name),
                )

                Path(download_path).unlink()
                Path(source_image_name).unlink()

        await self.ensure_static_sdn_exists()

        await self.startup_vm(
            built_in,
            f"import/{disk_renamed}",
            user_data=user_data,
            disk_gib=disk_gib,
            cloud_init_timeout=cloud_init_timeout,
        )

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
                upid = await self.async_proxmox.request(
                    "POST",
                    f"/nodes/{self.node}/storage/{LOCAL_STORAGE}/download-url",
                    json={
                        "content": "import",
                        "filename": source_image_name,
                        "url": source_image_source_url,
                    },
                )
                await self.async_proxmox.wait_for_task(upid=upid)

    async def _create_with_fresh_vm_id(
        self, create: Callable[[int], Awaitable[None]]
    ) -> int:
        """Run `create` with the next free VM ID; try the next if a build took it."""
        attempt = 0
        while True:
            attempt += 1
            vm_id = await self.qemu_commands.find_next_available_vm_id()
            try:
                await create(vm_id)
                return vm_id
            except httpx.HTTPStatusError as e:
                # `unable to create VM 105 - VM 105 already exists on node 'pve'`,
                # rejected before Proxmox writes anything
                taken = (
                    e.response.status_code == 500
                    and "already exists" in e.response.text
                )
                if attempt == _VM_ID_ATTEMPTS or not taken:
                    raise
                self.logger.info(f"VM ID {vm_id} was taken by another build; retrying")

    async def startup_vm(
        self,
        built_in: str,
        import_source: str,
        user_data: str = DEFAULT_USER_DATA,
        disk_gib: int | None = None,
        cloud_init_timeout: int = CLOUD_INIT_TIMEOUT,
    ) -> None:
        name = f"inspect-{built_in}"

        with trace_action(self.logger, TRACE_NAME, f"build template {built_in=}"):

            async def create(vm_id: int) -> None:
                with trace_action(
                    self.logger,
                    TRACE_NAME,
                    f"create VM from import {built_in=} {vm_id=}",
                ):
                    upid = await self.async_proxmox.request(
                        "POST",
                        f"/nodes/{self.node}/qemu",
                        json={
                            "vmid": vm_id,
                            "name": name,
                            "node": self.node,
                            "cpu": proxmox_cpu("host", nested_virt=False),
                            "memory": 8192,
                            "cores": 2,
                            "ostype": "l26",
                            "scsi0": f"{self.image_storage}:0,"
                            + f"import-from={LOCAL_STORAGE}:{import_source},"
                            + "format=qcow2,cache=writeback",
                            "scsihw": "virtio-scsi-single",
                            "net0": f"virtio,bridge={STATIC_VNET_ID}",
                            "serial0": "socket",
                            "vga": "none",
                            "tablet": 0,
                            "start": False,
                            "agent": "enabled=1",
                        },
                    )
                    await self.async_proxmox.wait_for_task(upid=upid)

            next_available_vm_id = await self._create_with_fresh_vm_id(create)

            if disk_gib is not None:
                # cloud-init's growpart/resizefs pick this up on first boot
                with trace_action(
                    self.logger, TRACE_NAME, f"resize disk {next_available_vm_id=}"
                ):
                    upid = await self.async_proxmox.request(
                        "PUT",
                        f"/nodes/{self.node}/qemu/{next_available_vm_id}/resize",
                        json={"disk": "scsi0", "size": f"{disk_gib}G"},
                    )
                    await self.async_proxmox.wait_for_task(upid=upid)

            with trace_action(
                self.logger, TRACE_NAME, f"cloud-init ISO {next_available_vm_id=}"
            ):
                await self.create_and_upload_cloudinit_iso(
                    vm_id=next_available_vm_id,
                    user_data=user_data,
                )

            upid = await self.async_proxmox.request(
                "POST",
                f"/nodes/{self.node}/qemu/{next_available_vm_id}/config",
                json={
                    "tags": f"inspect,builtin-{built_in}",
                },
            )
            await self.async_proxmox.wait_for_task(upid=upid)

            with trace_action(
                self.logger, TRACE_NAME, f"start VM {next_available_vm_id=}"
            ):
                await self.qemu_commands.start(vm_id=next_available_vm_id)
            # The guest installs its agent in cloud-init's package run, so
            # this is a wait for that download and gets the same budget.
            await self.qemu_commands.await_vm(
                vm_id=next_available_vm_id,
                requires_guest_agent=True,
                name=name,
                agent_timeout=cloud_init_timeout,
            )

            # now wait for cloud-init to finish

            agent_commands = AgentCommands(self.async_proxmox, self.node)
            cloud_init_pid = await agent_commands.exec_command(
                vm_id=next_available_vm_id,
                command=["cloud-init", "status", "--wait"],
            )

            @tenacity.retry(
                # Uncapped, this backoff reaches gaps of tens of minutes
                wait=tenacity.wait_exponential(multiplier=0.1, max=5, exp_base=1.3),
                stop=tenacity.stop_after_delay(cloud_init_timeout),
                retry=tenacity.retry_if_result(lambda x: x is False),
            )
            async def wait_for_cloud_init() -> bool:
                exec_status = await agent_commands.get_agent_exec_status(
                    vm_id=next_available_vm_id, pid=cloud_init_pid
                )
                if exec_status.exited == 1:
                    out_data = exec_status.out_data or ""
                    # `cloud-init status --wait` prints progress dots before the
                    # final status line, so match the trailing token, not the whole
                    # output (status: error/degraded still fall through to the raise).
                    if out_data.strip().endswith("status: done"):
                        return True
                    else:
                        raise ValueError(f"cloud-init failed: {out_data}")
                else:
                    return False

            with trace_action(
                self.logger, TRACE_NAME, f"wait for cloud-init {next_available_vm_id=}"
            ):
                await wait_for_cloud_init()

            await self.async_proxmox.request(
                "POST",
                f"/nodes/{self.node}/qemu/{next_available_vm_id}/status/shutdown",
            )

            await self.qemu_commands.await_vm(
                vm_id=next_available_vm_id,
                requires_guest_agent=True,
                name=name,
                status_for_wait="stopped",
            )

            with trace_action(
                self.logger, TRACE_NAME, f"convert to template {next_available_vm_id=}"
            ):
                upid = await self.async_proxmox.request(
                    "POST",
                    f"/nodes/{self.node}/qemu/{next_available_vm_id}/template",
                )
                # template=1 is set before the disks are renamed to base-*; a clone in
                # between fails with "Linked clone feature is not supported"
                await self.async_proxmox.wait_for_task(upid=upid)

                @tenacity.retry(
                    wait=POLL_WAIT,
                    stop=tenacity.stop_after_delay(VM_TIMEOUT),
                    retry=tenacity.retry_if_result(lambda x: x is False),
                )
                async def is_template() -> bool:
                    current_config = await self.async_proxmox.request(
                        "GET",
                        f"/nodes/{self.node}/qemu/{next_available_vm_id}/config?current=1",
                    )
                    return current_config.get("template") == 1

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
