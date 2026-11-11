import abc
import asyncio
import contextlib
import os
import subprocess
import tempfile
from collections.abc import Iterable
from io import BytesIO
from logging import getLogger
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Dict, cast, get_args
from urllib.parse import urlparse

import httpx
import platformdirs
import pycdlib
import tenacity
from inspect_ai.util import trace_action

from proxmoxsandbox._impl.agent_commands import AgentCommands
from proxmoxsandbox._impl.async_proxmox import POLL_WAIT, AsyncProxmoxAPI
from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox._impl.sdn_commands import BUILT_IN_IDS_PREFIX, SdnCommands
from proxmoxsandbox._impl.storage_commands import LOCAL_STORAGE, LocalStorageCommands
from proxmoxsandbox.schema import VmConfig, VmSourceConfig

VM_TIMEOUT = 1200
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
# "quiet" trims clone boot (measured on ubuntu24.04, 2 vCPU nested KVM:
# kernel+initrd 3.7 s -> 2.8 s, userspace to guest agent 1.4 s -> 1.0 s):
# under nested virtualisation every printk byte to the serial console is a
# trap, and the boot log is ~50 KB. Warnings and errors still reach the
# console; the journal has everything.
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
    async def clear_builtins(self) -> None:
        for built_in in SOURCE_IMAGE_NAMES:
            await self.clear_builtin(built_in)

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
        """Build whichever of these built-ins the host lacks, concurrently.

        Each build is a VM boot plus an apt run, mostly waiting on the guest,
        so the set costs about one build's time. They share one build zone,
        which is removed once they have all succeeded.
        """
        missing = sorted(set(built_in_names) - set(await self.known_builtins()))
        if not missing:
            return
        async with self.build_sdn() as vnet_id:
            builds = [
                asyncio.ensure_future(self._build(name, vnet_id)) for name in missing
            ]
            try:
                # Not gather: cancelling a gather cancels its children, and
                # the cancel below would then be a second one, landing in
                # whatever a build awaits while it unwinds.
                finished, _ = await asyncio.wait(
                    builds, return_when=asyncio.FIRST_EXCEPTION
                )
            finally:
                # A build that outlives a failed sibling would carry on after
                # sample_init has released the instance, under the next
                # sample's pre-clean.
                for build in builds:
                    build.cancel()
                await asyncio.gather(*builds, return_exceptions=True)
            for build in finished:
                build.result()

    async def ensure_exists(self, built_in_name: str) -> None:
        if built_in_name is None:
            raise ValueError("built_in_name must be set")
        await self.ensure_exists_many([built_in_name])

    async def _build(self, built_in_name: str, vnet_id: str) -> None:
        if built_in_name == "ubuntu24.04":
            await self.ensure_exists_from_ova(
                built_in=built_in_name,
                vnet_id=vnet_id,
                source_image_source_url=UBUNTU_URL,
                ova_vmdk_filename=UBUNTU_VMDK_FILENAME,
            )
        elif built_in_name == "debian13":
            await self.ensure_version(9)
            await self.ensure_exists_from_qcow2(
                built_in=built_in_name,
                vnet_id=vnet_id,
                source_image_source_url=DEBIAN_13_URL,
            )
        elif built_in_name == "kali2025.4":
            await self.ensure_version(9)
            await self.ensure_exists_from_xz(
                built_in=built_in_name,
                vnet_id=vnet_id,
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
        vnet_id: str,
        source_image_source_url: str,
        ova_vmdk_filename: str,
    ) -> None:
        source_image_name = Path(urlparse(source_image_source_url).path).name

        await self.ensure_source_uploaded(
            built_in, source_image_name, source_image_source_url
        )

        await self.startup_vm(
            built_in,
            f"import/{source_image_name}/{ova_vmdk_filename}",
            vnet_id=vnet_id,
        )

    async def ensure_exists_from_qcow2(
        self,
        built_in: str,
        vnet_id: str,
        source_image_source_url: str,
    ) -> None:
        source_image_name = Path(urlparse(source_image_source_url).path).name

        await self.ensure_source_uploaded(
            built_in, source_image_name, source_image_source_url
        )

        await self.startup_vm(
            built_in,
            f"import/{source_image_name}",
            vnet_id=vnet_id,
        )

    async def ensure_exists_from_xz(
        self,
        built_in: str,
        vnet_id: str,
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

                # tar -xf as a subprocess; awaited rather than check_call so
                # the event loop (other builds' polls) keeps running meanwhile
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
                    # Left running, it would still be writing disk.raw when a
                    # retry starts its own extract onto the same path.
                    tar.kill()
                    await tar.wait()
                    raise
                if tar.returncode != 0:
                    raise subprocess.CalledProcessError(
                        tar.returncode or -1, tar_command, stderr=tar_stderr
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

        await self.startup_vm(
            built_in,
            f"import/{disk_renamed}",
            vnet_id=vnet_id,
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

    async def startup_vm(
        self,
        built_in: str,
        import_source: str,
        vnet_id: str,
        user_data: str = DEFAULT_USER_DATA,
        disk_gib: int | None = None,
        cloud_init_timeout: int = CLOUD_INIT_TIMEOUT,
    ) -> None:
        """Import, boot, cloud-init and template a VM, with `vnet_id` as its uplink.

        The finished template has no NIC: clones are given theirs from their
        own sample's SDN, and `vnet_id` is gone once the builds are over. A
        build that fails or is cancelled leaves its VM as it is, tracked, like
        anything else a failed sample_init made.
        """
        name = f"inspect-{built_in}"

        with trace_action(self.logger, TRACE_NAME, f"build template {built_in=}"):
            upid = ""

            async def create(vm_id: int) -> None:
                nonlocal upid
                upid = await self.async_proxmox.request(
                    "POST",
                    f"/nodes/{self.node}/qemu",
                    json={
                        "vmid": vm_id,
                        "name": name,
                        "node": self.node,
                        "cpu": "host",
                        "memory": 8192,
                        "cores": 2,
                        "ostype": "l26",
                        "scsi0": f"{self.image_storage}:0,"
                        + f"import-from={LOCAL_STORAGE}:{import_source},"
                        + "format=qcow2,cache=writeback",
                        "scsihw": "virtio-scsi-single",
                        "net0": f"virtio,bridge={vnet_id}",
                        "serial0": "socket",
                        "vga": "none",
                        "tablet": 0,
                        "start": False,
                        "agent": "enabled=1",
                        # Tagged from creation: cleanup_no_id finds a
                        # failed build's VM by tag.
                        "tags": f"inspect,builtin-{built_in}",
                    },
                )

            # Only the POST is retried: PVE writes the VM config before the
            # import work starts, so the ID is ours once the call returns and
            # a failed import task is not an ID collision.
            vm_id = await self.qemu_commands._create_with_fresh_vm_id(create)
            # Tracked until it is a template: task_cleanup destroys a failed
            # build's VM, and has to before the build zone can go, because a
            # NIC's IPAM entry keeps its subnet from being deleted.
            self.qemu_commands.register_vm(
                vm_id=vm_id,
                vm_config=VmConfig(
                    name=name, vm_source_config=VmSourceConfig(built_in=built_in)
                ),
            )
            await self._provision_template(
                vm_id,
                built_in=built_in,
                create_upid=upid,
                user_data=user_data,
                disk_gib=disk_gib,
                cloud_init_timeout=cloud_init_timeout,
            )
            self.qemu_commands.deregister_vm(vm_id)

    async def _provision_template(
        self,
        next_available_vm_id: int,
        *,
        built_in: str,
        create_upid: str,
        user_data: str,
        disk_gib: int | None,
        cloud_init_timeout: int,
    ) -> None:
        name = f"inspect-{built_in}"

        with trace_action(
            self.logger,
            TRACE_NAME,
            f"create VM from import {built_in=} {next_available_vm_id=}",
        ):
            await self.async_proxmox.wait_for_task(upid=create_upid)

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

        with trace_action(self.logger, TRACE_NAME, f"start VM {next_available_vm_id=}"):
            await self.qemu_commands.start(vm_id=next_available_vm_id)
        # The guest installs its agent in cloud-init's package run, so this
        # is a wait for that download and gets the same budget.
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
            # Uncapped, the backoff reaches tens of minutes per poll inside an
            # hour; at 30 s a 25 min Kali run still sat finished for up to 30 s.
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
            # The config's template flag flips before the disks are renamed
            # to base images; a clone in that window fails with "Linked
            # clone feature is not supported", so wait for the task itself.
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

        # Also releases the NIC's IPAM entry in the build zone. A subnet with
        # one in it cannot be deleted, including by the task_cleanup that
        # removes the zone after a sibling build failed.
        await self.qemu_commands.remove_existing_nics(next_available_vm_id)

    @contextlib.asynccontextmanager
    async def build_sdn(self) -> AsyncIterator[str]:
        """The SDN zone built-in templates are built in; yields its VNET ID.

        An `auto` SDN like a sample's default: DHCP for the guests, SNAT for
        their apt runs, and a subnet that overlaps no other zone's. It is
        removed when the `async with` body finishes. If the body raises, the
        zone stays, tracked, with the failed build's VM still on it:
        task_cleanup removes both, or --no-sandbox-cleanup holds them up, and
        the next sample_init's pre-clean sweeps them by tag and zone name.
        """
        proxmox_ids_start = await self.sdn_commands.find_proxmox_ids_start(
            prefix=BUILT_IN_IDS_PREFIX
        )
        sdn_zone_id, vnet_aliases = await self.sdn_commands.create_sdn(
            proxmox_ids_start=proxmox_ids_start, sdn_config="auto"
        )
        if sdn_zone_id is None:
            raise RuntimeError("create_sdn returned no zone for an auto SDN")
        yield vnet_aliases[0][0]
        await self._tear_down_build_sdn(sdn_zone_id)

    async def _tear_down_build_sdn(self, sdn_zone_id: str) -> None:
        """Remove the build zone, logging rather than raising if it survives.

        The templates are built by now. A zone that survives stays tracked
        for task_cleanup, and the next sample's pre-clean sweeps it by name.
        """
        gone = False
        try:
            try:
                # Every template's NIC is gone by now, so normally none.
                leases = await self.sdn_commands.read_zone_ipam_mappings(
                    zone_ids={sdn_zone_id}
                )
            except Exception as e:
                self.logger.warning(
                    f"failed to read IPAM leases in build zone {sdn_zone_id}: {e}"
                )
                leases = []
            gone = await self.sdn_commands.tear_down_sdn_zone_and_vnet(
                sdn_zone_id=sdn_zone_id, ipam_mappings=leases
            )
        except Exception as e:
            self.logger.warning(f"failed to tear down build zone {sdn_zone_id}: {e}")
        if gone:
            self.sdn_commands.deregister_sdn_resources_for_zones({sdn_zone_id})
        else:
            self.logger.warning(
                f"build zone {sdn_zone_id} was not removed; leaving it for task_cleanup"
            )
