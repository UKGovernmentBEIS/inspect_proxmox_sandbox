"""Proxmox's built-in directory storage at /var/lib/vz, always available."""

import abc
import time
from collections.abc import Awaitable, Callable
from logging import Logger, getLogger
from pathlib import Path, PurePosixPath
from typing import Any, List, Literal, Optional
from urllib.parse import quote

import tenacity
from inspect_ai.util import trace_action

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.task_wrapper import TaskWrapper

LOCAL_STORAGE = "local"

MIN_DOWNLOAD_TIMEOUT_SECONDS = 1200
"""Floor for host download timeouts, generous enough for any small file."""

ASSUMED_DOWNLOAD_BYTES_PER_SECOND = 5 * 1024 * 1024
"""Pessimistic host download rate, used to derive a timeout from a file size.

Deliberately well below what a healthy host achieves: the timeout is a backstop
for a stuck download, and a download that has actually failed is detected from
its task status rather than by waiting this out.
"""


def download_timeout_for_size(size_bytes: Optional[int]) -> int:
    """A download timeout that a slow-but-working host can still meet.

    A fixed timeout is a trap for large files: a multi-GB OVA can spend twenty
    minutes downloading and be perfectly healthy.
    """
    if size_bytes is None:
        return MIN_DOWNLOAD_TIMEOUT_SECONDS
    return max(
        MIN_DOWNLOAD_TIMEOUT_SECONDS,
        int(size_bytes / ASSUMED_DOWNLOAD_BYTES_PER_SECOND),
    )


class DownloadIncompleteError(Exception):
    """The host's download task is still running. Retryable."""


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
        timeout_seconds: Optional[int] = None,
        progress_log_seconds: float = 60,
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
                Defaults to download_timeout_for_size(size_check).
            progress_log_seconds: How often to log the host's download progress while
                waiting.

        Raises:
            ValueError: if the host's download task failed, or the downloaded file
                has the wrong size.
            TimeoutError: if the download didn't finish in time. The message reports
                how far the host got, which is otherwise lost when the host is torn
                down after the failure.
        """
        if timeout_seconds is None:
            timeout_seconds = download_timeout_for_size(size_check)

        async def get_file() -> None:
            with trace_action(
                self.logger,
                self.TRACE_NAME,
                f"download-url {filename} to storage",
            ):
                upid = await self.async_proxmox.request(
                    "POST",
                    f"/nodes/{self.node}/storage/{LOCAL_STORAGE}/download-url",
                    json={
                        "content": content_type,
                        "filename": filename,
                        "url": url,
                    },
                )

                self.logger.info(
                    f"Host is downloading {filename}"
                    + (f" ({size_check} bytes)" if size_check is not None else "")
                    + f"; waiting up to {timeout_seconds}s"
                )

                progress = _DownloadProgress(
                    async_proxmox=self.async_proxmox,
                    node=self.node,
                    upid=upid,
                    filename=filename,
                    logger=self.logger,
                    log_every_seconds=progress_log_seconds,
                )

                @tenacity.retry(
                    wait=tenacity.wait_exponential(exp_base=1.3, max=10),
                    stop=tenacity.stop_after_delay(timeout_seconds),
                    retry=tenacity.retry_if_exception_type(DownloadIncompleteError),
                )
                async def download_complete():
                    downloaded_content = await self._content(content_type, filename)
                    if downloaded_content is None:
                        # A part-downloaded file is invisible in the storage listing
                        # (the host downloads to a temp file), so the task is our only
                        # window on it -- both for progress, and to notice a download
                        # that has already failed rather than waiting out the timeout.
                        await progress.check_and_log()
                        raise DownloadIncompleteError(
                            f"download of {filename} not yet complete"
                        )
                    file_size = downloaded_content.get("size")
                    # Don't pass size_check to self._content, which won't distinguish
                    # between "file is not present" and "file is present but size
                    # mismatch". Instead, fail if the size is wrong.
                    if size_check is not None and file_size != size_check:
                        raise ValueError(
                            f"Downloaded file {filename} size mismatch: "
                            f"expected {size_check}, got {file_size}"
                        )

                try:
                    return await download_complete()
                except tenacity.RetryError as retry_error:
                    raise TimeoutError(
                        f"Timed out after {timeout_seconds}s waiting for the host to"
                        f" download {filename}"
                        + (f" ({size_check} bytes)" if size_check is not None else "")
                        + f". {await progress.describe()}"
                    ) from retry_error

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


class _DownloadProgress:
    """Watches the Proxmox worker task behind a download-url request.

    Proxmox reports download progress only in that task's log, so we tail it:
    periodically while waiting, and again when reporting a failure or timeout. A
    timed-out download otherwise leaves no record of how far it got -- the host
    may well be torn down before anyone can look.
    """

    def __init__(
        self,
        async_proxmox: AsyncProxmoxAPI,
        node: str,
        upid: Any,
        filename: str,
        logger: Logger,
        log_every_seconds: float,
    ) -> None:
        self.async_proxmox = async_proxmox
        self.node = node
        # download-url returns the worker's UPID, but progress reporting isn't worth
        # failing a download over, so tolerate not getting one.
        self.upid: Optional[str] = upid if isinstance(upid, str) else None
        self.filename = filename
        self.logger = logger
        self.log_every_seconds = log_every_seconds
        self.last_logged_at: Optional[float] = None
        self.last_log_line: Optional[str] = None
        self.next_log_line = 0

    async def check_and_log(self) -> None:
        """Log progress periodically, and raise if the download has already failed.

        Raises:
            ValueError: if the host's download task stopped unsuccessfully.
        """
        if self.upid is None:
            return

        status = await self._status()
        exit_status = status.get("exitstatus")
        if exit_status is not None and exit_status != "OK":
            raise ValueError(
                f"Host download of {self.filename} failed: {exit_status}."
                f" {await self.describe()}"
            )

        now = time.monotonic()
        if (
            self.last_logged_at is not None
            and now - self.last_logged_at < self.log_every_seconds
        ):
            return
        self.last_logged_at = now

        await self._read_new_log_lines()
        self.logger.info(f"Downloading {self.filename}: {self._last_progress()}")

    async def describe(self) -> str:
        """Best-effort summary of how far the host's download got."""
        if self.upid is None:
            return "No download task id was returned, so no progress is available."
        try:
            status = await self._status()
            await self._read_new_log_lines()
        except Exception as read_error:
            return f"Couldn't read download task {self.upid}: {read_error!r}"
        return (
            f"Download task {self.upid} status={status.get('status')!r}"
            f" exitstatus={status.get('exitstatus')!r};"
            f" last progress: {self._last_progress()}"
        )

    def _last_progress(self) -> str:
        return self.last_log_line or "(the task has logged no output yet)"

    async def _status(self) -> dict[str, Any]:
        if self.upid is None:
            return {}
        return (
            await self.async_proxmox.request(
                "GET", f"/nodes/{self.node}/tasks/{quote(self.upid, safe='')}/status"
            )
            or {}
        )

    async def _read_new_log_lines(self) -> None:
        if self.upid is None:
            return
        entries = await self.async_proxmox.request(
            "GET",
            f"/nodes/{self.node}/tasks/{quote(self.upid, safe='')}/log"
            f"?start={self.next_log_line}&limit=1000",
        )
        for entry in entries or []:
            text = (entry.get("t") or "").strip()
            if text:
                self.last_log_line = text
            # `n` is the line's 1-based index, so it's also the 0-based offset of
            # the next line: use it as the start of the next read.
            self.next_log_line = max(self.next_log_line, entry.get("n", 0))
