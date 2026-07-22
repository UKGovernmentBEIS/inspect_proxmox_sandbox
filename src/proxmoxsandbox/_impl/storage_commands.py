"""Proxmox's built-in directory storage at /var/lib/vz, always available."""

import abc
from collections.abc import Awaitable, Callable
from logging import getLogger
from pathlib import Path, PurePosixPath
from typing import Any, List, Literal, Optional
from urllib.parse import quote

import tenacity
from inspect_ai.util import trace_action

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.task_wrapper import TaskWrapper

LOCAL_STORAGE = "local"


class LocalStorageCommands(abc.ABC):
    logger = getLogger(__name__)

    TRACE_NAME = "proxmox_storage_commands"

    async_proxmox: AsyncProxmoxAPI
    task_wrapper: TaskWrapper
    node: str

    def __init__(
        self, async_proxmox: AsyncProxmoxAPI, node: str, task_wrapper: TaskWrapper
    ):
        self.async_proxmox = async_proxmox
        self.task_wrapper = task_wrapper
        self.node = node

    async def put_file_in_storage(
        self,
        get_file: Callable[[], Awaitable[None]],
        content_type: Literal["iso", "vztmpl", "import"],
        filename: str,
        size_check: Optional[int] = None,
    ) -> None:
        if size_check is not None:
            existing_file = await self._content(
                content_type=content_type, filename=filename, size_check=size_check
            )
            if existing_file is not None:
                file_size = existing_file.get("size") if existing_file else None
                self.logger.debug(
                    f"File {filename} already exists in storage {LOCAL_STORAGE}"
                    f" on node {self.node} at {existing_file['volid']};"
                    f" {size_check=} {file_size=}"
                )
                if size_check is not None and file_size == size_check:
                    return

        await self.task_wrapper.do_action_and_wait_for_tasks(get_file)

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

        async def get_file():
            await self.async_proxmox.upload_file_with_curl(
                self.node, LOCAL_STORAGE, file, content_type, filename=filename
            )

        await self.put_file_in_storage(
            get_file=get_file,
            content_type=content_type,
            filename=filename,
            size_check=size_check,
        )

    async def download_url_to_storage(
        self,
        url: str,
        content_type: Literal["iso", "vztmpl", "import"],
        filename: str,
        size_check: int | None = None,
        timeout_seconds: int = 1200,
    ) -> None:
        """Have the Proxmox host download a file from a URL into local storage.

        Unlike upload_file_to_storage, the bytes are fetched by the Proxmox server
        directly from the URL and never pass through the machine running this code.

        The download is skipped if a file with the same name (and size, if specified)
        already exists in storage.

        Args:
            url: The URL for the Proxmox host to fetch (e.g. a presigned S3 URL).
            content_type: One of the file types supported by Proxmox.
            filename: The filename to store the downloaded file as.
            size_check: If provided, also check file size before deciding the file is
                already present.
            timeout_seconds: How long to wait for the download to appear in storage.

        Returns:
            The Proxmox storage content metadata for the stored file.
        """

        async def get_file() -> None:
            with trace_action(
                self.logger,
                self.TRACE_NAME,
                f"download-url {filename} to storage",
            ):
                await self.async_proxmox.request(
                    "POST",
                    f"/nodes/{self.node}/storage/{LOCAL_STORAGE}/download-url",
                    json={
                        "content": content_type,
                        "filename": filename,
                        "url": url,
                    },
                )

                @tenacity.retry(
                    wait=tenacity.wait_exponential(exp_base=1.3, max=10),
                    stop=tenacity.stop_after_delay(timeout_seconds),
                )
                async def download_complete():
                    downloaded_content = await self._content(content_type, filename)
                    if downloaded_content is None:
                        raise ValueError("download not yet complete")
                    file_size = downloaded_content.get("size")
                    # Don't pass size_check to self._content, which won't distinguish
                    # between "file is not present" and "file is present but size
                    # mismatch". Instead, fail if the size is wrong.
                    if size_check is not None and file_size != size_check:
                        raise ValueError(
                            f"Downloaded file {filename} size mismatch: "
                            f"expected {size_check}, got {file_size}"
                        )

                return await download_complete()

        await self.put_file_in_storage(
            get_file=get_file,
            content_type=content_type,
            filename=filename,
            size_check=size_check,
        )

    async def list_import_archive_disks(self, import_filename: str) -> List[str]:
        """List disk images in an OVA host-imported under local:import.

        Returns the disk member names in the form the
        `import-from=local:import/<archive>/<disk>` spec expects. Used when the
        caller has not told us the inner disk filename and we cannot open the tar
        locally (because the host, not us, downloaded it).

        Proxmox enumerates an import archive's contents via the storage
        `import-metadata` endpoint (NOT `file-restore`, which is for PBS backups
        only and 500s on an import volume). Its `volume` parameter is
        storage-relative — `import/<file>`, without the `<storage>:` prefix, and
        must be passed as a GET *query* parameter: this endpoint rejects a
        request body on GET ("501 Unexpected content for method 'GET'"). The
        response's `disks` map is keyed by the bus Proxmox would assign (sata0,
        sata1, ...) with values `{"volid": "<storage>:import/<archive>/<disk>"}`;
        we return each `<disk>` basename, ordered by that bus key so a multi-disk
        archive keeps a stable order.
        """
        volume = quote(f"import/{import_filename}", safe="/")
        info = await self.async_proxmox.request(
            "GET",
            f"/nodes/{self.node}/storage/{LOCAL_STORAGE}/import-metadata?volume={volume}",
        )
        disks_by_bus: dict[str, Any] = (info or {}).get("disks", {})
        return [
            PurePosixPath(spec["volid"]).name
            for _bus, spec in sorted(disks_by_bus.items())
        ]

    async def _content(
        self,
        content_type: Literal["iso", "vztmpl", "import"],
        filename: str,
        size_check: int | None = None,
    ) -> dict[str, Any] | None:
        existing_content = await self.async_proxmox.request(
            "GET",
            f"/nodes/{self.node}/storage/{LOCAL_STORAGE}/content?content={content_type}",
        )
        for existing_file in existing_content or []:
            if "volid" in existing_file and existing_file["volid"].endswith(filename):
                file_size = existing_file.get("size")
                if size_check is None or file_size == size_check:
                    return existing_file
        return None

    async def list_storage(self) -> list[dict[str, Any]]:
        return await self.async_proxmox.request(
            "GET", f"/nodes/{self.node}/storage/{LOCAL_STORAGE}/content"
        )
