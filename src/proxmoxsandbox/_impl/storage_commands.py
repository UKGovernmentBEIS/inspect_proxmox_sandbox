import abc
from logging import getLogger
from pathlib import Path
from typing import Literal, Optional

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.task_wrapper import TaskWrapper


class StorageCommands(abc.ABC):
    logger = getLogger(__name__)

    TRACE_NAME = "proxmox_storage_commands"

    async_proxmox: AsyncProxmoxAPI
    task_wrapper: TaskWrapper
    node: str
    storage: str

    def __init__(self, async_proxmox: AsyncProxmoxAPI, node: str, storage: str):
        self.async_proxmox = async_proxmox
        self.task_wrapper = TaskWrapper(async_proxmox)
        self.node = node
        self.storage = storage

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
            storage: The storage name in Proxmox, e.g. 'local'
            file: local path to the file
            content_type: One of the file types supported by Proxmox
            filename: The filename to use for the remote file in Proxmox storage.
                If not provided, the filename of the file will be used.
            size_check: If provided, the file will be uploaded only if
                if it does not exist remotely already, or if it does exist and the
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
                f"/nodes/{self.node}/storage/{self.storage}/content?content={content_type}",
            )
            for existing_file in existing_content:
                if "volid" in existing_file and existing_file["volid"].endswith(
                    filename
                ):
                    size_match = existing_file["size"] == size_check
                    self.logger.debug(
                        f"File {filename} already exists in storage {self.storage}"
                        + f" on node {self.node} at {existing_file['volid']};"
                        + f" {size_match=}"
                    )
                    if size_match:
                        return

        async def do_upload():
            await self.async_proxmox.upload_file_with_curl(
                self.node, self.storage, file, content_type, filename=filename
            )

        await self.task_wrapper.do_action_and_wait_for_tasks(do_upload)
