import abc
from logging import getLogger
from pathlib import Path
from typing import Any, Literal, Optional

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI

LOCAL_STORAGE = "local"
"""Proxmox's built-in directory storage at /var/lib/vz, always available."""


class LocalStorageCommands(abc.ABC):
    logger = getLogger(__name__)

    TRACE_NAME = "proxmox_storage_commands"

    async_proxmox: AsyncProxmoxAPI
    node: str

    def __init__(self, async_proxmox: AsyncProxmoxAPI, node: str):
        self.async_proxmox = async_proxmox
        self.node = node

    async def upload_file_to_storage(
        self,
        file: Path,
        content_type: Literal["iso", "vztmpl", "import"],
        filename: Optional[str] = None,
        size_check: Optional[int] = None,
    ) -> None:
        """
        Uploads a file to Proxmox storage.

        Args:
            file: local path to the file
            content_type: One of the file types supported by Proxmox
            filename: The filename to use for the remote file in Proxmox storage.
                If not provided, the filename of the file will be used.
            size_check: If provided, the file will be uploaded only if
                it does not exist remotely already, or if it does exist and the
                local file size is different from the remote.
                If not provided, the file will be uploaded always.
        """
        if not isinstance(file, Path):
            raise ValueError(f"{file=} must be a Path; got {type(file)}")

        if filename is None:
            filename = file.name
        if size_check is not None:
            existing_content = await self.async_proxmox.request(
                "GET",
                f"/nodes/{self.node}/storage/{LOCAL_STORAGE}/content?content={content_type}",
            )
            for existing_file in existing_content:
                # volid is storage:content/filename; anchor on the separator so
                # x-foo.iso is not taken for foo.iso
                if "volid" in existing_file and existing_file["volid"].endswith(
                    f"/{filename}"
                ):
                    size_match = existing_file["size"] == size_check
                    self.logger.debug(
                        f"File {filename} already exists in storage {LOCAL_STORAGE}"
                        + f" on node {self.node} at {existing_file['volid']};"
                        + f" {size_match=}"
                    )
                    if size_match:
                        return

        upid = await self.async_proxmox.upload_file(
            node=self.node,
            storage=LOCAL_STORAGE,
            file=file,
            content_type=content_type,
            filename=filename,
        )
        await self.async_proxmox.wait_for_task(upid=upid)

    async def list_storage(self) -> list[dict[str, Any]]:
        return await self.async_proxmox.request(
            "GET", f"/nodes/{self.node}/storage/{LOCAL_STORAGE}/content"
        )
